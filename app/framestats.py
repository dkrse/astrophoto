"""Frame quality statistics for light frames: background, noise, star count, FWHM."""
from pathlib import Path

import math

import numpy as np

from .preview import FITS_EXT, RAW_EXT, IMAGE_EXT, load_fits_raw, BAYER_PATTERNS, fits_cfa_pattern, clean_opts, to_image_planes

MAX_STARS = 400


def _luminance(src: Path):
    """2D float luminance of the frame + pixel scale factor (2 when CFA was binned 2x2)."""
    ext = src.suffix.lower()
    if ext in FITS_EXT:
        data, header = load_fits_raw(src)
        if data is None:
            return None, 1
        if data.ndim == 3:
            img = to_image_planes(data)
            return (img.mean(axis=-1) if img.ndim == 3 else img), 1
        if fits_cfa_pattern(header, clean_opts(None)) in BAYER_PATTERNS and min(data.shape) >= 2:
            h, w = (data.shape[0] // 2) * 2, (data.shape[1] // 2) * 2
            d = data[:h, :w]
            return (d[0::2, 0::2] + d[0::2, 1::2] + d[1::2, 0::2] + d[1::2, 1::2]) / 4, 2
        return data, 1
    if ext in RAW_EXT:
        import rawpy
        with rawpy.imread(str(src)) as raw:
            d = raw.raw_image_visible.astype(np.float32)
        h, w = (d.shape[0] // 2) * 2, (d.shape[1] // 2) * 2
        d = d[:h, :w]
        return (d[0::2, 0::2] + d[0::2, 1::2] + d[1::2, 0::2] + d[1::2, 1::2]) / 4, 2
    if ext in IMAGE_EXT:
        from PIL import Image
        with Image.open(src) as im:
            if im.mode not in ("L", "I", "I;16", "I;16B", "F", "RGB"):   # palette, alpha, CMYK … -> luminance
                im = im.convert("L")
            a = np.asarray(im, dtype=np.float32)
        return (a[..., :3].mean(axis=-1) if a.ndim == 3 else a), 1
    return None, 1


def frame_stats(src: Path) -> dict | None:
    from scipy import ndimage
    lum, scale = _luminance(src)
    if lum is None:
        return None
    lum = np.nan_to_num(lum.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)   # NaN / ±inf pixels count as empty
    sample = lum[::4, ::4]
    bg = float(np.median(sample))
    noise = float(np.median(np.abs(sample - bg)) * 1.4826)
    if noise <= 0:
        noise = float(sample.std()) or 1e-6

    # background-subtracted, lightly smoothed image for detection
    det = ndimage.uniform_filter(lum - bg, size=3)
    mask = det > 5 * noise / 1.7     # 3x3 mean reduces noise ~3x; keep threshold conservative
    labels, n = ndimage.label(mask)
    if n == 0:
        return {"bg": bg, "noise": noise, "stars": 0, "fwhm": None}
    idx = np.arange(1, n + 1)
    sizes = np.bincount(labels.ravel(), minlength=n + 1)[1:]      # no full-size float array
    peaks = ndimage.maximum(lum, labels, idx)
    sat = saturation_level(lum, 0.97)
    ok = (sizes >= 4) & (sizes <= 2500) & (peaks < sat)
    stars = int(ok.sum())

    fwhms = []
    objs = ndimage.find_objects(labels)
    cand = np.flatnonzero(ok)                     # brightest first, bounded (noisy frames: 10^5+ labels)
    for i in cand[np.argsort(-peaks[cand])][:MAX_STARS * 20]:
        sl = objs[i]
        if sl is None:
            continue
        y0, x0 = max(sl[0].start - 3, 0), max(sl[1].start - 3, 0)
        y1, x1 = min(sl[0].stop + 3, lum.shape[0]), min(sl[1].stop + 3, lum.shape[1])
        cut = lum[y0:y1, x0:x1] - bg
        m = windowed_moments(cut, (sl[1].start + sl[1].stop) / 2 - x0 - 0.5, (sl[0].start + sl[0].stop) / 2 - y0 - 0.5,
                             max(1.0, math.sqrt(sizes[i] / math.pi) / 1.5))
        if m is None:
            continue
        sigma = math.sqrt((m[2] + m[3]) / 2)
        if 0.3 < sigma < 30:
            fwhms.append(2.3548 * sigma * scale)
        if len(fwhms) >= MAX_STARS:
            break
    fwhm = float(np.median(fwhms)) if len(fwhms) >= 3 else None
    return {"bg": bg, "noise": noise, "stars": stars, "fwhm": fwhm}


def saturation_level(img: np.ndarray, frac: float) -> float:
    """Peaks at or above this are treated as saturated: frac × the image maximum, but only when the frame really
    has clipped pixels (several at the maximum = a plateau). Without clipping nothing is excluded – a frame whose
    brightest stars are merely bright must not lose them."""
    mx = float(np.nanmax(img)) if img.size else 0.0
    if not np.isfinite(mx) or mx <= 0:
        return float("inf")
    return mx * frac if int(np.count_nonzero(img >= mx * 0.999)) >= 4 else float("inf")


def windowed_moments(cut: np.ndarray, cx: float, cy: float, s0: float, iters: int = 6):
    """Second moments of a star with an adaptive Gaussian window, the window deconvolved
    (C = (Cw⁻¹ − I/σw²)⁻¹, exact for a Gaussian star). cut: background-subtracted, NOT clipped – noise
    pixels then average out instead of adding weight at large radii (plain box moments read 7–16 % too large).
    Returns (cx, cy, mxx, myy, mxy) in cut coordinates or None."""
    yy, xx = np.mgrid[0:cut.shape[0], 0:cut.shape[1]]
    cut = np.asarray(cut, dtype=np.float64)
    sw = max(float(s0), 0.7)
    C = None
    for _ in range(iters):
        dx, dy = xx - cx, yy - cy
        w = np.exp(-(dx * dx + dy * dy) / (2 * sw * sw)) * cut
        tot = w.sum()
        if not tot > 0:
            return None
        cx, cy = cx + (w * dx).sum() / tot, cy + (w * dy).sum() / tot
        if not (0 <= cx < cut.shape[1] and 0 <= cy < cut.shape[0]):
            return None
        dx, dy = xx - cx, yy - cy
        cxy = (w * dx * dy).sum() / tot
        cw = np.array([[(w * dx * dx).sum() / tot, cxy], [cxy, (w * dy * dy).sum() / tot]])
        try:
            C = np.linalg.inv(np.linalg.inv(cw) - np.eye(2) / (sw * sw))
        except np.linalg.LinAlgError:
            return None
        ev = np.linalg.eigvalsh(C)
        if not ev[0] > 0 or not math.isfinite(ev[1]):
            return None
        sw = math.sqrt(ev.mean())
        if sw > 60:                                     # diverging (no star / flat patch)
            return None
    return cx, cy, float(C[0, 0]), float(C[1, 1]), float(C[0, 1])


def flag_suspects(files) -> int:
    """Mark light frames that are clearly worse than the median of their group. Returns count."""
    for f in files:                               # also clears stale flags (stats failed, type changed)
        f.suspect = False
    lights = [f for f in files if f.frame_type == "light" and f.stat_stars is not None]
    if len(lights) < 4:
        return 0
    med = lambda xs: float(np.median(xs)) if xs else None   # noqa: E731
    m_fwhm = med([f.stat_fwhm for f in lights if f.stat_fwhm])
    m_stars = med([f.stat_stars for f in lights])
    m_bg = med([f.stat_bg for f in lights if f.stat_bg is not None])
    m_noise = med([f.stat_noise for f in lights if f.stat_noise])
    n = 0
    for f in lights:
        bad = (m_fwhm and f.stat_fwhm and f.stat_fwhm > m_fwhm * 1.3) \
            or (m_stars and f.stat_stars < m_stars * 0.5) \
            or (m_bg and m_noise and f.stat_bg is not None and f.stat_bg > m_bg + 8 * m_noise)
        f.suspect = bool(bad)
        n += f.suspect
    return n
