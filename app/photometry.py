"""Limiting magnitude of a frame from the ASTAP star list (-extract2) matched to the local ASTAP star database.

ASTAP .1476 databases (D05/D20/D50/D80 – Gaia DR3, magnitude = Gaia BP): 1476 tiles in 36 declination rings
(the polar rings are half height). A file is a 110-byte text header followed by 5-byte records; a record
with RA = 0xFFFFFF starts a block and carries the high declination byte (−128) and the magnitude
((value − 16) / 10) of the stars that follow (stars are sorted by magnitude)."""
import math
from pathlib import Path

import numpy as np

RING_H = 180 / 35                 # rings 2..35; ring 1 and 36 are half rings (caps)
_TILE_CACHE: "OrderedDict" = None
TILE_CACHE_BYTES = 150 * 1024 ** 2               # decoded tiles kept in memory (least recently used dropped)


def _ring_of(dec: float) -> int:
    return int(min(max(math.floor((dec + 90 - RING_H / 2) / RING_H) + 2, 1), 36))


def _ring_tiles(db: Path, ring: int) -> list:
    return sorted(db.glob(f"*_{ring:02d}??.1476"))


import threading
from collections import OrderedDict
_TILE_CACHE = OrderedDict()
_TILE_LOCK = threading.Lock()


def read_tile(path: Path):
    """(ra_deg, dec_deg, mag) arrays of one database file (float32, LRU-cached within TILE_CACHE_BYTES)."""
    key = str(path)
    with _TILE_LOCK:
        if key in _TILE_CACHE:
            _TILE_CACHE.move_to_end(key)
            return _TILE_CACHE[key]
    b = np.fromfile(path, dtype=np.uint8)[110:]
    b = b[: len(b) // 5 * 5].reshape(-1, 5)
    hdr = (b[:, 0] == 255) & (b[:, 1] == 255) & (b[:, 2] == 255)
    block = np.cumsum(hdr) - 1
    hb = b[hdr].astype(np.int64)
    dec_hi, mag = hb[:, 3] - 128, (hb[:, 4] - 16) / 10.0
    st = ~hdr & (block >= 0)
    s, g = b[st].astype(np.int64), block[st]
    ra = (s[:, 0] | s[:, 1] << 8 | s[:, 2] << 16) / (2 ** 24 - 1) * 360.0
    dec = (s[:, 3] | s[:, 4] << 8 | (dec_hi[g] << 16)) / (128 * 65536 - 1) * 90.0
    out = (ra.astype(np.float32), dec.astype(np.float32), mag[g].astype(np.float32))   # float32: ~0.1″ precision, half the memory
    with _TILE_LOCK:
        _TILE_CACHE[key] = out
        while len(_TILE_CACHE) > 1 and sum(sum(a.nbytes for a in v) for v in _TILE_CACHE.values()) > TILE_CACHE_BYTES:
            _TILE_CACHE.popitem(last=False)
    return out


def catalogue_stars(db: Path, ra0: float, dec0: float, radius: float):
    """Database stars within `radius` degrees of (ra0, dec0)."""
    rings = range(_ring_of(dec0 - radius), _ring_of(dec0 + radius) + 1)
    ras, decs, mags = [], [], []
    for ring in rings:
        tiles = _ring_tiles(db, ring)
        n = len(tiles)
        if not n:
            continue
        cosd = max(math.cos(math.radians(min(abs(dec0) + radius, 89.9))), 1e-3)
        span = 360 if ring in (1, 36) else min(radius / cosd, 180)
        for i, t in enumerate(tiles):
            lo, hi = i * 360 / n, (i + 1) * 360 / n
            # RA overlap of [ra0 - span, ra0 + span] with the tile (with wrap-around)
            d = ((lo + hi) / 2 - ra0 + 540) % 360 - 180
            if abs(d) > span + 180 / n:
                continue
            ra, dec, mag = read_tile(t)
            ras.append(ra)
            decs.append(dec)
            mags.append(mag)
    if not ras:
        return np.empty(0), np.empty(0), np.empty(0)
    ra, dec, mag = np.concatenate(ras), np.concatenate(decs), np.concatenate(mags)
    r = np.radians
    c = np.sin(r(dec)) * np.sin(r(dec0)) + np.cos(r(dec)) * np.cos(r(dec0)) * np.cos(r(ra - ra0))
    keep = np.degrees(np.arccos(np.clip(c, -1, 1))) <= radius
    return ra[keep], dec[keep], mag[keep]


def read_extract_csv(path: Path):
    """ASTAP -extract2 csv (x, y, hfd, snr, flux, ra, dec) -> arrays (snr, flux, ra, dec, x, y)."""
    import csv
    cols = [[] for _ in range(6)]
    if not path.exists():
        return None
    with open(path, newline="") as fh:
        rd = csv.reader(fh)
        next(rd, None)
        for row in rd:
            try:
                vals = (float(row[3]), float(row[4]), float(row[5]), float(row[6]), float(row[0]), float(row[1]))
            except (ValueError, IndexError):
                continue
            for c, v in zip(cols, vals):
                c.append(v)
    return tuple(np.array(c) for c in cols)


def _xyz(a, d):
    a, d = np.radians(a), np.radians(d)
    return np.column_stack([np.cos(d) * np.cos(a), np.cos(d) * np.sin(a), np.sin(d)])


def limiting_magnitude(snr, flux, ra, dec, cat_ra, cat_dec, cat_mag, match_arcsec: float = 3.0,
                       snr_limit: float = 5.0, x=None, y=None, size=None) -> dict | None:
    """Match detected stars to the catalogue, fit the zero point and the magnitude reached at `snr_limit`.
    With pixel positions (x, y: 0-based, solved array, row 0 at the top) and its size (w, h) the result also carries a photometric quality check
    ("qc", see quality()) and the brightest matched stars ("matches": ra, dec, catalogue mag, measured mag, SNR)."""
    from scipy.spatial import cKDTree
    if len(ra) < 5 or len(cat_ra) < 5:
        return None
    tree = cKDTree(_xyz(cat_ra, cat_dec))
    rad = 2 * math.sin(math.radians(match_arcsec / 3600) / 2)
    near = tree.query_ball_point(_xyz(ra, dec), rad)
    good_det = (flux > 0) & (snr > 0)
    # pass 1: zero point from the brightest catalogue star next to each bright detection
    order = np.argsort(-flux)
    zps = []
    for i in order[: max(30, len(order) // 4)]:
        if good_det[i] and near[i]:
            j = min(near[i], key=lambda k: cat_mag[k])
            zps.append(cat_mag[j] + 2.5 * math.log10(flux[i]))
    if len(zps) < 5:
        return None
    zp = float(np.median(zps))
    # pass 2: every detection -> the neighbour whose magnitude fits the measured flux best.
    # A lone neighbour up to 3 mag "too bright" is kept for the quality check only (saturated / clipped star).
    m, s, pairs = [], [], []
    for i in range(len(ra)):
        if not (good_det[i] and near[i]):
            continue
        pred = zp - 2.5 * math.log10(flux[i])
        j = min(near[i], key=lambda k: abs(cat_mag[k] - pred))
        dm = pred - cat_mag[j]
        if abs(dm) < 0.75:
            m.append(cat_mag[j])
            s.append(snr[i])
            pairs.append((i, j))
        elif len(near[i]) == 1 and 0 < dm < 3:
            pairs.append((i, j))
    if len(m) < 8:
        return None
    m, s = np.array(m), np.array(s)
    # SNR model: mag = a + b log10(snr), robust line fit on unsaturated, matched stars
    sel = (s >= 5) & (s <= 100)                # faint, background-limited part of the SNR curve
    xs, ys = np.log10(s[sel]), m[sel]
    if len(xs) < 8 or np.ptp(xs) < 0.3:
        return None
    b, a = np.polyfit(xs, ys, 1)
    res = ys - (a + b * xs)
    keep = np.abs(res) < 3 * (np.median(np.abs(res)) * 1.4826 + 0.05)
    if keep.sum() >= 8 and np.ptp(xs[keep]) >= 0.3:     # refit only on a non-degenerate subset
        b, a = np.polyfit(xs[keep], ys[keep], 1)
    lim = float(a + b * math.log10(snr_limit))
    out = {"limit_mag": round(lim, 2), "zero_point": round(zp, 2), "matched": int(len(m)),
           "faintest_detected": round(float(m.max()), 2), "band": "Gaia BP", "snr_limit": snr_limit}
    ii = np.array([p[0] for p in pairs])
    jj = np.array([p[1] for p in pairs])
    cat = np.asarray(cat_mag, dtype=float)[jj]
    meas = zp - 2.5 * np.log10(flux[ii])
    if x is not None and y is not None and size:
        q = quality(cat, meas, snr[ii], np.asarray(x)[ii], np.asarray(y)[ii], size)
        if q:
            out["zero_point"] = round(zp + q.pop("zp_shift"), 2)      # refined on well-measured stars
            meas = meas + (out["zero_point"] - zp)
            out["qc"] = q
    top = np.argsort(cat)[:300]                                    # brightest matched stars, for the star list
    out["matches"] = [[round(float(ra[ii[k]]), 5), round(float(dec[ii[k]]), 5), round(float(cat[k]), 2),
                       round(float(meas[k]), 2), round(float(snr[ii[k]]), 1)] for k in top]
    return out


def _mad_sigma(v) -> float:
    v = np.asarray(v, dtype=float)
    return float(1.4826 * np.median(np.abs(v - np.median(v)))) if len(v) else float("nan")


QC_MIN_SNR = 20                   # stars used for scatter / linearity / uniformity
SAT_BIN_N = 8                     # stars per magnitude bin when looking for the saturation onset
SAT_MIN_RESID = 0.08              # a bin whose median residual exceeds this (and its noise) is non-linear
CAT_EDGE_MAG = 0.5                # stars this close to the faintest matched catalogue magnitude are not used


def saturation_onset(cat, resid, sigma) -> float | None:
    """Faintest magnitude of the bright, non-linear part: walking from the bright end in bins of at least
    SAT_BIN_N stars, a bin is saturated while its median residual (measured − catalogue, zero point removed)
    is clearly positive – above SAT_MIN_RESID and 2.5 × the noise of a median of that many stars."""
    order = np.argsort(cat)
    c, r = np.asarray(cat)[order], np.asarray(resid)[order]
    onset, k = None, 0
    while k + SAT_BIN_N <= len(c):
        j = k + SAT_BIN_N
        while j < len(c) and c[j] - c[k] < 0.25:      # at least 0.25 mag wide
            j += 1
        n = j - k
        thr = max(SAT_MIN_RESID, 2.5 * 1.2533 * sigma / math.sqrt(n))
        if float(np.median(r[k:j])) <= thr:
            break
        onset = round(float(c[j]) if j < len(c) else float(c[j - 1]), 2)
        k = j
    return onset


def quality(cat, meas, snr, x, y, size) -> dict | None:
    """x, y: 0-based pixel positions in the solved array (row 0 at the top), size: its (width, height).
    Photometric quality of one frame from the matched stars (measured − catalogue residuals):
    zero point uncertainty, scatter, linearity (slope of measured vs catalogue magnitude), saturation onset
    and the uniformity of the zero point over the field (linear gradient + radial term = vignetting).
    Returns None with too few well-measured stars."""
    cat, meas, snr = (np.asarray(v, dtype=float) for v in (cat, meas, snr))
    resid = meas - cat
    W, H = size
    well = snr >= QC_MIN_SNR
    if well.sum() < 10:
        return None
    # noise level of a single well-measured star: robust spread of the fainter half (no saturation there)
    sigma0 = _mad_sigma(resid[well & (cat >= np.median(cat[well]))])
    sat_mag = saturation_onset(cat[well], resid[well] - np.median(resid[well & (cat >= np.median(cat[well]))]),
                               sigma0)
    cat_edge = float(cat.max()) - CAT_EDGE_MAG     # catalogue depth limit: matches there are biased bright
    good = well & (cat <= cat_edge)
    if sat_mag is not None:
        good &= cat >= sat_mag
    if good.sum() < 10:
        return None
    r = resid[good]
    shift = float(np.median(r))
    r = r - shift
    sigma = _mad_sigma(r)
    clip = np.abs(r) < max(5 * sigma, 0.15)
    out = {"n": int(good.sum()), "zp_shift": shift, "scatter_mag": round(sigma, 3),
           "zp_err": round(sigma / math.sqrt(clip.sum()), 3), "sat_mag": sat_mag, "min_snr": QC_MIN_SNR,
           "cat_limit": round(float(cat.max()), 2)}
    c, mm = cat[good][clip], meas[good][clip] - shift
    if len(c) >= 10 and np.ptp(c) >= 1.5:
        slope, icpt = np.polyfit(c, mm, 1)
        out["slope"] = round(float(slope), 3)
        out["slope_range"] = [round(float(c.min()), 1), round(float(c.max()), 1)]
    # uniformity: residual = a + b·x + c·y + d·r² over the field (x, y in −1 … 1, r² = 2 in the corners)
    xs = (np.asarray(x, dtype=float)[good][clip] / W) * 2 - 1
    ys = (np.asarray(y, dtype=float)[good][clip] / H) * 2 - 1
    rr = r[clip]
    if len(rr) >= 30 and np.ptp(xs) > 1 and np.ptp(ys) > 1:
        A = np.column_stack([np.ones_like(xs), xs, ys, xs * xs + ys * ys])
        coef, *_ = np.linalg.lstsq(A, rr, rcond=None)
        _, gx, gy, rad = (float(v) for v in coef)
        out["gradient_mag"] = round(2 * math.hypot(gx, gy), 3)       # edge to edge along the steepest direction
        # direction in which stars measure fainter, in fractions (u right, v down) of the solved array –
        # the viewer turns it into the displayed orientation
        out["gradient_vec"] = [round(gx, 4), round(gy, 4)]
        out["vignetting_mag"] = round(2 * rad, 3)                    # corner − centre (positive: corners fainter)
    # points for the residual plot (catalogue mag, residual, used in the fit)
    k = np.argsort(-snr)[:600]
    out["plot"] = [[round(float(cat[i]), 2), round(float(resid[i] - shift), 3), int(bool(good[i]))] for i in k]
    out["verdicts"] = verdicts(out)
    return out


def verdicts(q: dict) -> list:
    """Plain-language assessment of quality() (level ok / warn / bad / info)."""
    v = []
    s = q["scatter_mag"]
    if s <= 0.05:
        v.append(("ok", "Precise photometry", f"Stars scatter by only {s:.3f} mag around the catalogue."))
    elif s <= 0.12:
        v.append(("ok", "Normal scatter", f"{s:.3f} mag around the catalogue – usual for a single frame "
                                          f"(band difference to Gaia BP, star colours, noise)."))
    else:
        v.append(("warn", "Large scatter", f"{s:.3f} mag. A colour (OSC) camera or a filter whose band differs "
                                           f"from Gaia BP adds a star-colour term of 0.1–0.2 mag; otherwise clouds / "
                                           f"haze during the exposure, a strong gradient, trailed or blended stars, "
                                           f"or a narrowband filter."))
    sl = q.get("slope")
    if sl is not None:
        if abs(sl - 1) <= 0.05:
            v.append(("ok", "Linear response", f"Measured vs catalogue magnitude slope {sl:.3f} (ideal 1.000)."))
        else:
            v.append(("warn", "Non-linear response",
                      f"Slope {sl:.3f} instead of 1.000: faint stars measure {'too bright' if sl < 1 else 'too faint'} "
                      f"relative to bright ones. Typical of a stretched / processed image, wrong bias / offset "
                      f"subtraction or a strong sky background in the aperture."))
    if q.get("sat_mag") is not None:
        v.append(("info", "Saturation", f"Stars brighter than ≈{q['sat_mag']:.1f} mag (Gaia BP) are saturated or "
                                         f"in the non-linear range – they measure too faint and are left out of "
                                         f"the checks. Shorter exposures / lower gain for bright-star photometry."))
    g = q.get("gradient_mag")
    if g is not None:
        if g > 0.15:
            v.append(("warn", "Uneven field", f"The zero point changes by {g:.2f} mag across the frame (stars "
                                              f"fainter towards the {{dir}}): dew, an obstruction, light pollution "
                                              f"gradient or haze low over the horizon."))
        vg = q.get("vignetting_mag") or 0
        if vg > 0.15:
            v.append(("warn", "Vignetting", f"Corner stars measure {vg:.2f} mag fainter than in the centre – "
                                            f"the frame is not flat-field corrected (normal for a raw light)."))
        elif vg < -0.15:
            v.append(("warn", "Corners brighter", f"Corner stars measure {-vg:.2f} mag brighter than in the centre – "
                                                  f"flats over-correcting (taken with a different setup / "
                                                  f"focus), or a stronger sky background towards the corners."))
        if g <= 0.15 and abs(vg) <= 0.15:
            v.append(("ok", "Even field", f"Zero point varies by ≤{max(g, abs(vg)):.2f} mag across the frame."))
    return [{"level": l, "title": t, "text": x} for l, t, x in v]
