"""Frame quality statistics for light frames: background, noise, star count, FWHM."""
from pathlib import Path

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
    lum = np.nan_to_num(lum.astype(np.float32))
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
    sizes = ndimage.sum_labels(np.ones_like(lum), labels, idx)
    peaks = ndimage.maximum(lum, labels, idx)
    sat = float(lum.max()) * 0.97
    ok = (sizes >= 4) & (sizes <= 2500) & (peaks < sat)
    stars = int(ok.sum())

    fwhms = []
    objs = ndimage.find_objects(labels)
    order = np.argsort(-peaks)
    for i in order:
        if not ok[i]:
            continue
        sl = objs[i]
        if sl is None:
            continue
        y0, x0 = max(sl[0].start - 3, 0), max(sl[1].start - 3, 0)
        y1, x1 = min(sl[0].stop + 3, lum.shape[0]), min(sl[1].stop + 3, lum.shape[1])
        cut = lum[y0:y1, x0:x1] - bg
        cut = np.clip(cut, 0, None)
        tot = cut.sum()
        if tot <= 0:
            continue
        yy, xx = np.mgrid[0:cut.shape[0], 0:cut.shape[1]]
        cy, cx = (yy * cut).sum() / tot, (xx * cut).sum() / tot
        vy = ((yy - cy) ** 2 * cut).sum() / tot
        vx = ((xx - cx) ** 2 * cut).sum() / tot
        sigma = np.sqrt(max((vx + vy) / 2, 0))
        if 0.3 < sigma < 30:
            fwhms.append(2.3548 * sigma * scale)
        if len(fwhms) >= MAX_STARS:
            break
    fwhm = float(np.median(fwhms)) if len(fwhms) >= 3 else None
    return {"bg": bg, "noise": noise, "stars": stars, "fwhm": fwhm}


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
