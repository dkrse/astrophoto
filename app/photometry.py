"""Limiting magnitude of a frame from the ASTAP star list (-extract2) matched to the local ASTAP star database.

ASTAP .1476 databases (D05/D20/D50/D80 – Gaia DR3, magnitude = Gaia BP): 1476 tiles in 36 declination rings
(the polar rings are half height). A file is a 110-byte text header followed by 5-byte records; a record
with RA = 0xFFFFFF starts a block and carries the high declination byte (−128) and the magnitude
((value − 16) / 10) of the stars that follow (stars are sorted by magnitude)."""
import math
from pathlib import Path

import numpy as np

RING_H = 180 / 35                 # rings 2..35; ring 1 and 36 are half rings (caps)
_TILE_CACHE: dict = {}


def _ring_of(dec: float) -> int:
    return int(min(max(math.floor((dec + 90 - RING_H / 2) / RING_H) + 2, 1), 36))


def _ring_tiles(db: Path, ring: int) -> list:
    return sorted(db.glob(f"*_{ring:02d}??.1476"))


def read_tile(path: Path):
    """(ra_deg, dec_deg, mag) arrays of one database file."""
    key = str(path)
    if key in _TILE_CACHE:
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
    out = (ra, dec, mag[g])
    if len(_TILE_CACHE) > 64:
        _TILE_CACHE.clear()
    _TILE_CACHE[key] = out
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
    """ASTAP -extract2 csv -> arrays (snr, flux, ra, dec)."""
    import csv
    snr, flux, ra, dec = [], [], [], []
    if not path.exists():
        return None
    with open(path, newline="") as fh:
        rd = csv.reader(fh)
        next(rd, None)
        for row in rd:
            try:
                snr.append(float(row[3]))
                flux.append(float(row[4]))
                ra.append(float(row[5]))
                dec.append(float(row[6]))
            except (ValueError, IndexError):
                continue
    return np.array(snr), np.array(flux), np.array(ra), np.array(dec)


def limiting_magnitude(snr, flux, ra, dec, cat_ra, cat_dec, cat_mag, match_arcsec: float = 3.0,
                       snr_limit: float = 5.0) -> dict | None:
    """Match detected stars to the catalogue, fit the zero point and the magnitude reached at `snr_limit`."""
    from scipy.spatial import cKDTree
    if len(ra) < 5 or len(cat_ra) < 5:
        return None

    def xyz(a, d):
        a, d = np.radians(a), np.radians(d)
        return np.column_stack([np.cos(d) * np.cos(a), np.cos(d) * np.sin(a), np.sin(d)])

    tree = cKDTree(xyz(cat_ra, cat_dec))
    rad = 2 * math.sin(math.radians(match_arcsec / 3600) / 2)
    near = tree.query_ball_point(xyz(ra, dec), rad)
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
    # pass 2: every detection -> the neighbour whose magnitude fits the measured flux best
    m, s = [], []
    for i in range(len(ra)):
        if not (good_det[i] and near[i]):
            continue
        pred = zp - 2.5 * math.log10(flux[i])
        j = min(near[i], key=lambda k: abs(cat_mag[k] - pred))
        if abs(cat_mag[j] - pred) < 0.75:
            m.append(cat_mag[j])
            s.append(snr[i])
    if len(m) < 8:
        return None
    m, s = np.array(m), np.array(s)
    # SNR model: mag = a + b log10(snr), robust line fit on unsaturated, matched stars
    sel = (s >= 5) & (s <= 100)                # faint, background-limited part of the SNR curve
    x, y = np.log10(s[sel]), m[sel]
    if len(x) < 8 or np.ptp(x) < 0.3:
        return None
    b, a = np.polyfit(x, y, 1)
    res = y - (a + b * x)
    keep = np.abs(res) < 3 * (np.median(np.abs(res)) * 1.4826 + 0.05)
    b, a = np.polyfit(x[keep], y[keep], 1)
    lim = float(a + b * math.log10(snr_limit))
    return {"limit_mag": round(lim, 2), "zero_point": round(zp, 2), "matched": int(len(m)),
            "faintest_detected": round(float(m.max()), 2), "band": "Gaia BP", "snr_limit": snr_limit}
