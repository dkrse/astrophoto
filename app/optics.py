"""Optics inspector: star size / shape across the field of one frame -> tilt, field curvature, coma / spacing,
uniform elongation (guiding, wind, collimation), 3×3 mosaic of the corners / edges / centre.

Everything is computed in the orientation the viewer shows (flip / RAW orientation applied first), so the
returned (u, v) fractions and the mosaic match the image on screen."""
import base64
import io
import math
from pathlib import Path

import numpy as np

from .framestats import _luminance

MAX_STARS = 3000
GRID = (8, 6)          # cells for the elongation map (columns, rows)


def _orient(a: np.ndarray, orient: int, flip: str) -> np.ndarray:
    if orient == 90:
        a = np.rot90(a, -1)
    elif orient == 180:
        a = a[::-1, ::-1]
    elif orient == 270:
        a = np.rot90(a, 1)
    if "v" in flip:
        a = a[::-1]
    if "h" in flip:
        a = a[:, ::-1]
    return np.ascontiguousarray(a)


def _background(lum: np.ndarray):
    """Smooth background (handles gradients) + robust noise."""
    from scipy import ndimage
    k = max(lum.shape) // 64 or 1
    small = lum[::k, ::k]
    bgs = ndimage.median_filter(small, size=9)
    bg = ndimage.zoom(bgs, (lum.shape[0] / bgs.shape[0], lum.shape[1] / bgs.shape[1]), order=1,
                      mode="nearest", grid_mode=True)
    bg = bg[:lum.shape[0], :lum.shape[1]]
    if bg.shape != lum.shape:
        bg = np.pad(bg, ((0, lum.shape[0] - bg.shape[0]), (0, lum.shape[1] - bg.shape[1])), mode="edge")
    r = small - bgs
    noise = float(np.median(np.abs(r - np.median(r))) * 1.4826) or float(r.std()) or 1e-6
    return bg, noise


def _stars(img: np.ndarray, noise: float) -> list:
    """Isolated, unsaturated stars with second-moment shape: (x, y, fwhm, elongation, angle)."""
    from scipy import ndimage
    det = ndimage.uniform_filter(img, size=3)
    labels, n = ndimage.label(det > 5 * noise / 1.7)
    if not n:
        return []
    idx = np.arange(1, n + 1)
    sizes = ndimage.sum_labels(np.ones_like(img), labels, idx)
    peaks = ndimage.maximum(img, labels, idx)
    sat = float(img.max()) * 0.9
    objs = ndimage.find_objects(labels)
    H, W = img.shape
    out = []
    for i in np.argsort(-peaks):
        if not (5 <= sizes[i] <= 1500) or peaks[i] >= sat or objs[i] is None:
            continue
        sl = objs[i]
        cy0 = (sl[0].start + sl[0].stop) / 2
        cx0 = (sl[1].start + sl[1].stop) / 2
        r = int(max(sl[0].stop - sl[0].start, sl[1].stop - sl[1].start) * 1.2 + 3)
        y0, y1, x0, x1 = int(cy0 - r), int(cy0 + r + 1), int(cx0 - r), int(cx0 + r + 1)
        if y0 < 0 or x0 < 0 or y1 > H or x1 > W:
            continue
        lab = labels[y0:y1, x0:x1]
        if np.any((lab != 0) & (lab != i + 1)):         # neighbour inside the box -> not isolated
            continue
        cut = np.clip(img[y0:y1, x0:x1], 0, None)
        cut = np.where(cut > 2 * noise, cut, 0)
        tot = cut.sum()
        if tot <= 0:
            continue
        yy, xx = np.mgrid[0:cut.shape[0], 0:cut.shape[1]]
        cy, cx = (yy * cut).sum() / tot, (xx * cut).sum() / tot
        mxx = ((xx - cx) ** 2 * cut).sum() / tot
        myy = ((yy - cy) ** 2 * cut).sum() / tot
        mxy = ((xx - cx) * (yy - cy) * cut).sum() / tot
        t, d = (mxx + myy) / 2, math.sqrt(((mxx - myy) / 2) ** 2 + mxy ** 2)
        l1, l2 = t + d, t - d
        if l2 <= 0.05 or l1 > 400:
            continue
        out.append((x0 + cx, y0 + cy, 2.3548 * math.sqrt(t), math.sqrt(l1 / l2),
                    0.5 * math.atan2(2 * mxy, mxx - myy), float(peaks[i]) / noise))
        if len(out) >= MAX_STARS:
            break
    return out


def _cell_stats(st: list):
    """Median FWHM, median elongation and mean orientation (doubled-angle average) of a group of stars."""
    if not st:
        return None
    f = float(np.median([s[2] for s in st]))
    bright = [s for s in st if s[5] >= 25]             # shapes of faint stars are dominated by noise
    if len(bright) >= 3:
        st = bright
    e = float(np.median([s[3] for s in st]))
    w = np.array([s[3] - 1 for s in st])
    c = float((w * np.cos(2 * np.array([s[4] for s in st]))).sum())
    s_ = float((w * np.sin(2 * np.array([s[4] for s in st]))).sum())
    coh = math.hypot(c, s_) / (w.sum() or 1)            # 1 = all stars stretched the same way
    return {"fwhm": f, "elong": e, "angle": 0.5 * math.atan2(s_, c), "coherence": coh, "n": len(st)}


def _tile_png(img: np.ndarray, x: int, y: int, size: int, lo: float, hi: float, out_px: int = 220) -> str:
    from PIL import Image
    H, W = img.shape
    x0, y0 = min(max(x, 0), max(W - size, 0)), min(max(y, 0), max(H - size, 0))
    cut = img[y0:y0 + size, x0:x0 + size]
    v = np.arcsinh(np.clip(cut - lo, 0, None) / (hi - lo + 1e-9) * 10) / math.asinh(10)
    im = Image.fromarray((np.clip(v, 0, 1) * 255).astype(np.uint8))
    im = im.resize((out_px, out_px), Image.NEAREST if size < out_px else Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def analyse(src: Path, orient: int = 0, flip: str = "none", scale_arcsec: float | None = None,
            with_tiles: bool = True) -> dict:
    lum, binf = _luminance(src)
    if lum is None:
        return {"ok": False, "error": "Unsupported or unreadable file"}
    lum = _orient(np.nan_to_num(np.asarray(lum, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0), orient, flip)
    H, W = lum.shape
    bg, noise = _background(lum)
    img = lum - bg
    stars = _stars(img, noise)
    if len(stars) >= 15:                         # drop galaxy cores, blends and hot pixels
        med = float(np.median([s[2] for s in stars]))
        stars = [s for s in stars if 0.5 * med <= s[2] <= 2 * med]
    if len(stars) < 15:
        return {"ok": False, "error": f"Too few usable stars ({len(stars)}) – use a sharp light frame with more stars"}
    px = binf                                   # FWHM in sensor pixels
    to_as = (scale_arcsec or 0) * binf          # arcsec per analysed pixel (if the frame was solved)

    # 3×3 regions (corners / edges / centre)
    regions = []
    for r in range(3):
        for c in range(3):
            st = [s for s in stars if c * W / 3 <= s[0] < (c + 1) * W / 3 and r * H / 3 <= s[1] < (r + 1) * H / 3]
            cs = _cell_stats(st)
            regions.append({"row": r, "col": c, **(cs or {"n": 0})})
    # fine grid -> elongation vectors on the image + tilt plane fit
    gw, gh = GRID if W >= H else GRID[::-1]
    vectors, pts = [], []
    for r in range(gh):
        for c in range(gw):
            st = [s for s in stars if c * W / gw <= s[0] < (c + 1) * W / gw and r * H / gh <= s[1] < (r + 1) * H / gh]
            if len(st) < 3:
                continue
            cs = _cell_stats(st)
            u, v = (c + 0.5) / gw, (r + 0.5) / gh
            vectors.append({"u": u, "v": v, **{k: cs[k] for k in ("fwhm", "elong", "angle", "n", "coherence")}})
            pts.append((u * 2 - 1, v * 2 - 1, cs["fwhm"], cs["elong"], cs["angle"], cs["coherence"]))
    all_s = _cell_stats(stars)
    centre = regions[4] if regions[4]["n"] >= 3 else None
    diag, metrics = _diagnose(regions, pts, all_s, centre, W / H)

    # mosaic tiles (display orientation), same stretch for all
    sample = img[::4, ::4]
    lo, hi = float(np.median(sample)) - noise, float(np.percentile(sample, 99.95))
    size = max(int(min(W, H) / 6), 8)
    tiles = []
    for r in (range(3) if with_tiles else []):
        for c in range(3):
            x = 0 if c == 0 else (W - size if c == 2 else W // 2 - size // 2)
            y = 0 if r == 0 else (H - size if r == 2 else H // 2 - size // 2)
            tiles.append(_tile_png(img, x, y, size, lo, hi))
    f = lambda v: round(v * px, 2) if v is not None else None
    for g in regions + vectors:
        if g.get("fwhm") is not None:
            g["fwhm_px"] = f(g.pop("fwhm"))
            if to_as:
                g["fwhm_arcsec"] = round(g["fwhm_px"] / binf * to_as, 2)
            g["elong"] = round(g["elong"], 3)
            g["angle_deg"] = round(math.degrees(g.pop("angle")), 1)
            g["coherence"] = round(g.get("coherence", 0), 2)
    return {"ok": True, "stars": len(stars), "fwhm_px": f(all_s["fwhm"]), "elong": round(all_s["elong"], 3),
            "fwhm_arcsec": round(all_s["fwhm"] * to_as, 2) if to_as else None,
            "regions": regions, "vectors": vectors, "tiles": tiles, "tile_arcmin":
                round(size * to_as / 60, 1) if to_as else None, "tile_px": size * binf, "bin": binf,
            "diagnosis": diag, "metrics": metrics, "flip": flip, "orient": orient}


def _diagnose(regions, pts, all_s, centre, aspect: float = 1.0) -> list:
    """Plain-language findings: (level, title, text). level: ok / warn / bad / info."""
    out, metrics = [], {}
    corners = [regions[i] for i in (0, 2, 6, 8) if regions[i]["n"] >= 3]
    # field curvature: corners vs centre
    if centre and len(corners) >= 3:
        ratio = float(np.mean([c["fwhm"] for c in corners])) / centre["fwhm"]
        metrics["corners_pct"] = round((ratio - 1) * 100)
        if ratio > 1.25:
            out.append(("warn", "curvature", "Field curvature / corners soft",
                        f"Corner stars are {round((ratio - 1) * 100)} % larger than in the centre. Typical for a "
                        f"missing or wrongly spaced flattener / coma corrector, or focus set on the centre only."))
        else:
            out.append(("ok", "flat", "Flat field", f"Corner stars {round((ratio - 1) * 100):+d} % FWHM vs. the centre."))
    # tilt: plane fit of FWHM over the field
    if len(pts) >= 8:
        A = np.array([[1, p[0], p[1]] for p in pts])
        coef, *_ = np.linalg.lstsq(A, np.array([p[2] for p in pts]), rcond=None)
        a, b, c = coef
        grad = math.hypot(b, c)
        spread = 2 * grad / a if a > 0 else 0         # FWHM difference across the field (edge to edge) / mean
        metrics["tilt_pct"] = round(spread * 100)
        if spread > 0.2:
            ang = math.degrees(math.atan2(c, b))      # screen: x right, y down
            dirs = ["right", "bottom right", "bottom", "bottom left", "left", "top left", "top", "top right"]
            worse = dirs[int(((ang + 360) % 360 + 22.5) // 45) % 8]
            out.append(("bad" if spread > 0.4 else "warn", "tilt:" + worse, "Sensor tilt",
                        f"Stars get larger towards the {worse} of the frame (≈{round(spread * 100)} % across the "
                        f"field). The camera / focuser is tilted, or the field is not perpendicular to the optical axis "
                        f"– check the connection of camera, spacers and focuser, or use a tilt plate."))
        else:
            out.append(("ok", "notilt", "No significant tilt", f"FWHM varies by ≈{round(spread * 100)} % across the field."))
    # elongation pattern: uniform (guiding / wind) vs radial / tangential at the edges (coma, spacing)
    if centre and centre["elong"] > 1.15 and all_s["coherence"] > 0.5:
        out.append(("bad", "uniform", "Stars elongated everywhere in one direction",
                    f"Elongation {centre['elong']:.2f} also in the centre, all in the same direction. This is not a field "
                    f"aberration: tracking / guiding error, wind, flexure or (if the stars look comet-like even in "
                    f"the centre) collimation."))
    edge = [p for p in pts if math.hypot(p[0], p[1]) > 0.75 and p[3] > 1.12 and p[5] > 0.4]
    if edge:
        rad = []
        for x, y, _f, e, ang, _c in edge:
            rdir = math.atan2(y, x * aspect)           # direction from the centre in pixel space
            rad.append(math.cos(2 * (ang - rdir)))     # +1 radial, -1 tangential
        m = float(np.mean(rad))
        e_edge = float(np.median([p[3] for p in edge]))
        if m > 0.4:
            out.append(("warn", "radial", "Radial elongation at the edges",
                        f"Edge stars stretched towards / away from the centre (median elongation {e_edge:.2f}). "
                        f"Typical coma (Newtonian without / with a badly spaced coma corrector) or wrong "
                        f"flattener / reducer spacing. Change the spacing by ~1 mm and compare."))
        elif m < -0.4:
            out.append(("warn", "tangential", "Tangential elongation at the edges",
                        f"Edge stars stretched sideways, perpendicular to the centre direction (median elongation "
                        f"{e_edge:.2f}). Typical of astigmatism / wrong flattener or reducer spacing (the opposite "
                        f"direction to radial). Change the spacing by ~1 mm and compare."))
    if centre and centre["elong"] <= 1.15 and not edge:
        out.append(("ok", "round", "Round stars", "No significant elongation in the centre or at the edges."))
    if centre and centre["elong"] > 1.3 and all_s["coherence"] <= 0.5:
        out.append(("warn", "irregular", "Irregular star shapes",
                    f"Stars are not round (elongation {centre['elong']:.2f} in the centre) but in random directions. "
                    f"Usually seeing / undersampling or noise; if it repeats on good nights check collimation "
                    f"and focus."))
    res = []
    for l, k, t, x in out:
        key, _, d = k.partition(":")
        res.append({"level": l, "key": key, "dir": d or None, "title": t, "text": x})
    return res, metrics
