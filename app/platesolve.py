"""Plate solving with ASTAP (on demand, one file at a time) -> image scale, focal length, FOV, centre, rotation.

The frame is converted to a mono float FITS (CFA / raw frames are 2×2 binned like in framestats), solved by
astap_cli against the local star database, and the scale is converted back to the original pixels."""
import json
import math
import os
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

from .framestats import _luminance

ASTAP_BIN = os.environ.get("ASTAP_BIN", "/opt/astap/astap_cli")
ASTAP_DB = os.environ.get("ASTAP_DB", "/data/astap")
TIMEOUT_S = 180
_LOCK = threading.Lock()          # one solve at a time (CPU + RAM)


def available() -> str | None:
    """None when ready, otherwise the reason."""
    if not Path(ASTAP_BIN).exists():
        return f"ASTAP not installed ({ASTAP_BIN})"
    db = Path(ASTAP_DB)
    if not db.is_dir() or not (any(db.glob("*.1476")) or any(db.glob("*.290"))):
        return f"No ASTAP star database in {ASTAP_DB}"
    return None


def _hdr(header_text: str | None) -> dict:
    out = {}
    for line in (header_text or "").splitlines():
        if "=" not in line[:10]:
            continue
        k, _, v = line.partition("=")
        v = v.split("/")[0].strip().strip("'").strip()
        out[k.strip().upper()] = v
    return out


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ra_dec_hint(h: dict):
    """(ra_hours, dec_deg) from common FITS keywords, or (None, None)."""
    ra, dec = _float(h.get("RA")), _float(h.get("DEC"))
    if ra is not None and dec is not None:
        return ra / 15.0, dec
    ra_s, dec_s = h.get("OBJCTRA"), h.get("OBJCTDEC")     # '05 35 17' / '-05 23 28'
    try:
        r = [float(x) for x in ra_s.replace(":", " ").split()]
        d = [float(x) for x in dec_s.replace(":", " ").split()]
        sign = -1 if dec_s.strip().startswith("-") else 1
        return r[0] + r[1] / 60 + r[2] / 3600, sign * (abs(d[0]) + d[1] / 60 + d[2] / 3600)
    except Exception:
        return None, None


def _read_ini(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                out[k.strip().upper()] = v.strip()
    return out


def _run(fits_path: Path, fov_deg: float | None, ra_h, dec):
    cmd = [ASTAP_BIN, "-f", str(fits_path), "-d", ASTAP_DB, "-z", "0", "-wcs", "-extract2", "5"]
    cmd += ["-fov", f"{fov_deg:.3f}" if fov_deg else "0"]
    if ra_h is not None and dec is not None:
        cmd += ["-ra", f"{ra_h:.5f}", "-spd", f"{dec + 90:.5f}", "-r", "30"]
    else:
        cmd += ["-r", "180"]
    subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S, check=False)
    return _read_ini(fits_path.with_suffix(".ini"))


def solve(src: Path, header_text: str | None, pixel_um: float | None, focal_hint_mm: float | None,
          sensor_px: tuple | None = None) -> dict:
    """Returns a result dict (always contains 'ok'); never raises for solve failures."""
    from astropy.io import fits
    reason = available()
    if reason:
        return {"ok": False, "error": reason}
    lum, binf = _luminance(src)
    if lum is None:
        return {"ok": False, "error": "Unsupported or unreadable file"}
    lum = np.nan_to_num(np.asarray(lum, dtype=np.float32))
    h_px, w_px = lum.shape[:2]
    h = _hdr(header_text)
    xbin = _float(h.get("XBINNING")) or 1
    if not pixel_um:                       # XPIXSZ is the (binned) pixel size by convention
        pixel_um = _float(h.get("XPIXSZ"))
    elif xbin > 1:
        pixel_um = pixel_um * xbin
    ra_h, dec = _ra_dec_hint(h)
    # FOV hint (height, degrees, of the possibly 2×2-binned image) from the expected focal length
    fov_hint = None
    if pixel_um and focal_hint_mm:
        if sensor_px:      # true field from the sensor size – also right for resized / drizzled exports
            short_img, short_sensor = min(h_px, w_px) * binf, min(sensor_px)
            fov_hint = short_sensor * pixel_um / focal_hint_mm * 206.265 / 3600 * \
                (h_px * binf / short_img if short_img else 1)
        else:
            fov_hint = h_px * binf * pixel_um / focal_hint_mm * 206.265 / 3600
    started = datetime.now()
    with _LOCK, tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "frame.fits"
        fits.PrimaryHDU(lum).writeto(tmp)
        ini, attempts = {}, ([fov_hint, None] if fov_hint else [None])
        for fov in attempts:
            ini = _run(tmp, fov, ra_h, dec)
            if ini.get("PLTSOLVD") == "T":
                break
            if ra_h is not None:                     # hint position may be wrong: retry blind
                ini = _run(tmp, fov, None, None)
                if ini.get("PLTSOLVD") == "T":
                    break
        stars = None
        if ini.get("PLTSOLVD") == "T":
            from .photometry import read_extract_csv
            stars = read_extract_csv(tmp.with_suffix(".csv"))
    secs = round((datetime.now() - started).total_seconds(), 1)
    if ini.get("PLTSOLVD") != "T":
        err = ini.get("ERROR") or ini.get("WARNING") or "No solution found (too few stars, or star database " \
                                                         "does not cover this field size)"
        return {"ok": False, "error": err, "seconds": secs, "solved_at": started.isoformat(timespec="seconds")}
    cd11, cd12 = _float(ini.get("CD1_1")) or 0, _float(ini.get("CD1_2")) or 0
    cd21, cd22 = _float(ini.get("CD2_1")) or 0, _float(ini.get("CD2_2")) or 0
    det = abs(cd11 * cd22 - cd12 * cd21)
    scale_bin = math.sqrt(det) * 3600 if det else abs(_float(ini.get("CDELT2")) or 0) * 3600
    scale = scale_bin / binf                        # arcsec per original (sensor) pixel
    res = {"ok": True, "seconds": secs, "solved_at": started.isoformat(timespec="seconds"),
           "scale_arcsec_px": round(scale, 4),
           "ra_deg": round(_float(ini.get("CRVAL1")) or 0, 5), "dec_deg": round(_float(ini.get("CRVAL2")) or 0, 5),
           "rotation_deg": round(_float(ini.get("CROTA2")) or 0, 2),
           "fov_w_arcmin": round(w_px * scale_bin / 60, 2), "fov_h_arcmin": round(h_px * scale_bin / 60, 2),
           "image_px": [w_px * binf, h_px * binf], "pixel_um": pixel_um,
           # full TAN WCS of the solved (possibly 2×2-binned, un-flipped) array -> overlays / objects in field
           "wcs": {"crpix": [_float(ini.get("CRPIX1")), _float(ini.get("CRPIX2"))],
                   "crval": [_float(ini.get("CRVAL1")), _float(ini.get("CRVAL2"))],
                   "cd": [[cd11, cd12], [cd21, cd22]], "w": w_px, "h": h_px},
           "orient": _orientation(src)}
    from .preview import IMAGE_EXT, is_8bit_image
    if stars is not None and src.suffix.lower() in IMAGE_EXT and is_8bit_image(src):
        res["photometry"] = {"error": "Not measured: 8-bit image (JPEG / PNG) is stretched, fluxes are not linear"}
    elif stars is not None:
        try:
            res["photometry"] = _photometry(stars, res["ra_deg"], res["dec_deg"],
                                            math.hypot(w_px, h_px) / 2 * scale_bin / 3600)
        except Exception as e:                  # photometry is a bonus – never fail the solve
            res["photometry"] = {"error": str(e)}
    if pixel_um:
        res["focal_mm"] = round(206.265 * pixel_um / scale, 1)
    if focal_hint_mm:
        res["focal_expected_mm"] = focal_hint_mm
    return res


def _photometry(stars, ra0, dec0, radius):
    from . import photometry
    snr, flux, ra, dec = stars
    cra, cdec, cmag = photometry.catalogue_stars(Path(ASTAP_DB), ra0, dec0, radius)
    return photometry.limiting_magnitude(snr, flux, ra, dec, cra, cdec, cmag) or \
        {"error": "Too few stars matched to the star database"}


def resize_check(res: dict, sensor_px: tuple, expected_focal: float | None) -> None:
    """Image smaller / larger than the camera sensor: resized export (e.g. JPEG at 50 %, 2× drizzle) or a crop?
    A resize changes the scale per pixel -> the focal length is corrected; a crop does not change it."""
    img_long, img_short = max(res["image_px"]), min(res["image_px"])
    cam_long, cam_short = max(sensor_px), min(sensor_px)
    k = cam_long / img_long
    if abs(k - 1) < 0.03 or not res.get("focal_mm"):
        return
    raw = res["focal_mm"]
    if expected_focal:
        resized = abs(raw * k / expected_focal - 1) < abs(raw / expected_focal - 1)
    else:                                   # no telescope: same aspect ratio -> resized, otherwise cropped
        resized = abs(img_long / img_short - cam_long / cam_short) < 0.02 * cam_long / cam_short
    if resized:
        res["focal_image_mm"] = raw
        res["focal_mm"] = round(raw * k, 1)
        res["resize_factor"] = round(1 / k, 3)
        res["sensor_scale_arcsec_px"] = round(res["scale_arcsec_px"] / k, 4)
        res["note"] = (f"Image is {round(100 / k)} % of the camera sensor ({img_long} px vs. {int(cam_long)} px on the "
                       f"long side) – resized export / drizzle. Focal length corrected to the sensor: "
                       f"{res['focal_mm']:.0f} mm (per image pixel it would be {raw:.0f} mm).")
    else:
        res["note"] = (f"Image is smaller than the camera sensor ({img_long} × {img_short} vs. {int(cam_long)} × "
                       f"{int(cam_short)} px) – looks cropped, so the focal length is not changed.")


def _orientation(src: Path) -> int:
    """Clockwise rotation (0/90/180/270) the viewer applies to the sensor array of RAW / JPEG files."""
    from .preview import RAW_EXT, IMAGE_EXT
    ext = src.suffix.lower()
    try:
        if ext in RAW_EXT:
            import rawpy
            with rawpy.imread(str(src)) as raw:
                return {3: 180, 5: 270, 6: 90}.get(raw.sizes.flip, 0)
        if ext in IMAGE_EXT:
            from PIL import Image
            with Image.open(src) as im:
                return {3: 180, 6: 90, 8: 270}.get(im.getexif().get(274), 0)
    except Exception:
        pass
    return 0


# ---------- WCS, deep-sky catalogue (OpenNGC), overlays ----------
OPENNGC_DIR = Path(os.environ.get("OPENNGC_DIR", "/opt/openngc"))
TYPE_LABELS = {"G": "Galaxy", "GPair": "Galaxy pair", "GTrpl": "Galaxy triplet", "GGroup": "Galaxy group",
               "OCl": "Open cluster", "GCl": "Globular cluster", "Cl+N": "Cluster + nebula", "PN": "Planetary nebula",
               "HII": "HII region", "EmN": "Emission nebula", "RfN": "Reflection nebula", "Neb": "Nebula",
               "SNR": "Supernova remnant", "DrkN": "Dark nebula", "*Ass": "Stellar association", "Nova": "Nova",
               "**": "Double star", "*": "Star", "Other": "Other"}
_CAT = None


def _sexa(v: str, hours: bool) -> float | None:
    try:
        sign = -1 if v.strip().startswith("-") else 1
        a, b, c = (abs(float(x)) for x in v.split(":"))
        d = a + b / 60 + c / 3600
        return sign * d * (15 if hours else 1)
    except Exception:
        return None


def ident_key(v: str) -> str:
    """'NGC 0224' / 'ngc224' / 'M 31' / 'm031' -> 'ngc224' / 'm31'; other names: lowercase alphanumerics."""
    import re
    k = re.sub(r"[\W_]+", "", (v or "").casefold())
    m = re.fullmatch(r"([a-z]+)0*(\d+)([a-z]?)", k)
    return f"{m.group(1)}{int(m.group(2))}{m.group(3)}" if m else k


def catalogue() -> list:
    """OpenNGC (NGC/IC + addendum) as dicts; loaded once. Duplicates / non-existent entries are skipped."""
    global _CAT
    if _CAT is not None:
        return _CAT
    import csv
    out = []
    for fn in ("NGC.csv", "addendum.csv"):
        path = OPENNGC_DIR / fn
        if not path.exists():
            continue
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                if r["Type"] in ("Dup", "NonEx"):
                    continue
                ra, dec = _sexa(r["RA"], True), _sexa(r["Dec"], False)
                if ra is None or dec is None:
                    continue
                name = r["Name"]
                label = ident_key(name).upper().replace("NGC", "NGC ").replace("IC", "IC ") \
                    if name[:3] in ("NGC", "IC0", "IC1", "IC2", "IC3", "IC4", "IC5") else name
                mess = f"M{int(r['M'])}" if r["M"].strip().isdigit() else None
                keys = {ident_key(name)}
                if mess:
                    keys.add(ident_key(mess))
                for prefix in ("NGC", "IC"):                 # cross ids of IC / addendum entries
                    for x in filter(None, r[prefix].split(",")):
                        keys.add(ident_key(prefix + x.strip()))
                common = [c.strip() for c in r["Common names"].split(",") if c.strip()]
                keys.update(ident_key(c) for c in common)
                for x in filter(None, r["Identifiers"].split(",")):
                    keys.add(ident_key(x))
                out.append({"name": label, "messier": mess, "type": r["Type"],
                            "type_label": TYPE_LABELS.get(r["Type"], r["Type"]), "ra": ra, "dec": dec,
                            "maj": _float(r["MajAx"]), "min": _float(r["MinAx"]), "pa": _float(r["PosAng"]),
                            "mag": _float(r["V-Mag"]) or _float(r["B-Mag"]), "common": common[0] if common else None,
                            "keys": keys})
    _CAT = out
    return out


def find_object(text: str | None):
    k = ident_key(text)
    if not k:
        return None
    return next((o for o in catalogue() if k in o["keys"]), None)


def _wcs(res: dict):
    from astropy.wcs import WCS
    w = WCS(naxis=2)
    wd = res["wcs"]
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crpix, w.wcs.crval, w.wcs.cd = wd["crpix"], wd["crval"], np.array(wd["cd"])
    return w


def _uv(res: dict, x, y):
    """0-based array pixel -> fractions (u, v) of the solved array (row 0 at the top, before viewer flips)."""
    wd = res["wcs"]
    return (np.asarray(x) + 0.5) / wd["w"], (np.asarray(y) + 0.5) / wd["h"]


def _sep_deg(ra1, dec1, ra2, dec2):
    r = np.radians
    c = np.sin(r(dec1)) * np.sin(r(dec2)) + np.cos(r(dec1)) * np.cos(r(dec2)) * np.cos(r(np.asarray(ra1) - ra2))
    return np.degrees(np.arccos(np.clip(c, -1, 1)))


def _pa_deg(ra1, dec1, ra2, dec2) -> float:
    """Position angle (north through east) of point 2 seen from point 1."""
    r = np.radians
    d = r(ra2 - ra1)
    return float(np.degrees(np.arctan2(np.sin(d), np.cos(r(dec1)) * np.tan(r(dec2)) - np.sin(r(dec1)) * np.cos(d))) % 360)


def _offset(ra, dec, pa_deg, dist_deg):
    """Point at distance / position angle from (ra, dec) – small-angle approximation (fine inside one frame)."""
    pa = math.radians(pa_deg)
    return ra + dist_deg * math.sin(pa) / max(math.cos(math.radians(dec)), 1e-6), dec + dist_deg * math.cos(pa)


def annotations(res: dict) -> dict:
    """Objects in the field, a RA/Dec grid and N/E directions – positions as fractions (u, v) of the solved array."""
    w = _wcs(res)
    wd = res["wcs"]
    W, H = wd["w"], wd["h"]
    ra0, dec0 = w.all_pix2world([[W / 2 - 0.5, H / 2 - 0.5]], 0)[0]
    scale = math.sqrt(abs(np.linalg.det(np.array(wd["cd"]))))       # deg per array pixel
    radius = math.hypot(W, H) / 2 * scale
    cat = catalogue()
    objs = []
    if cat:
        ras = np.array([o["ra"] for o in cat])
        decs = np.array([o["dec"] for o in cat])
        near = np.nonzero(_sep_deg(ras, decs, ra0, dec0) < radius * 1.2)[0]
        if len(near):
            px = w.all_world2pix(np.column_stack([ras[near], decs[near]]), 0)
            for i, (x, y) in zip(near, px):
                o = cat[i]
                rad_px = (o["maj"] or 0) / 60 / 2 / scale
                if not (-rad_px <= x <= W + rad_px and -rad_px <= y <= H + rad_px):
                    continue
                u, v = _uv(res, x, y)
                d = {"name": o["name"], "messier": o["messier"], "common": o["common"], "type": o["type_label"],
                     "mag": o["mag"], "maj": o["maj"], "min": o["min"], "u": float(u), "v": float(v),
                     "inside": bool(0 <= x <= W and 0 <= y <= H)}
                if o["maj"]:                               # end of the major axis -> ellipse size and angle
                    ra2, dec2 = _offset(o["ra"], o["dec"], o["pa"] or 0, o["maj"] / 60 / 2)
                    x2, y2 = w.all_world2pix([[ra2, dec2]], 0)[0]
                    u2, v2 = _uv(res, x2, y2)
                    d["axis"] = [float(u2), float(v2)]
                objs.append(d)
    objs.sort(key=lambda o: (o["mag"] is None, o["mag"] or 0))
    # N / E direction from the centre (10 % of the image height)
    arm = H * 0.1 * scale
    comp = {}
    for k, pa in (("n", 0), ("e", 90)):
        x, y = w.all_world2pix([list(_offset(ra0, dec0, pa, arm))], 0)[0]
        comp[k] = [float(c) for c in _uv(res, x, y)]
    comp["c"] = [0.5, 0.5]
    return {"objects": objs, "compass": comp, "grid": _grid(w, W, H, ra0, dec0, radius)}


def _grid(w, W, H, ra0, dec0, radius) -> list:
    """RA / Dec lines crossing the image as polylines of (u, v) fractions."""
    steps = [1 / 60, 2 / 60, 5 / 60, 10 / 60, 15 / 60, 0.5, 1, 2, 5, 10]
    dstep = next((s for s in steps if radius * 2 / s <= 8), 10)
    cosd = max(math.cos(math.radians(dec0)), 0.05)
    rstep = next((s for s in [x * 1.0 for x in steps] if radius * 2 / cosd / s <= 8), 10)
    lines = []

    def add(pts, label):
        xy = w.all_world2pix(np.array(pts), 0)
        ok = [(float((x + 0.5) / W), float((y + 0.5) / H)) for x, y in xy
              if -0.05 * W <= x <= 1.05 * W and -0.05 * H <= y <= 1.05 * H and np.isfinite(x)]
        if len(ok) > 1:
            lines.append({"pts": ok, "label": label})

    span_d = radius * 1.3
    d_lo, d_hi = max(dec0 - span_d, -89.9), min(dec0 + span_d, 89.9)
    span_r = min(radius * 1.3 / cosd, 180)
    n = 60
    d = math.floor(d_lo / dstep) * dstep
    while d <= d_hi:
        add([[ra0 - span_r + 2 * span_r * i / n, d] for i in range(n + 1)], fmt_dec(d, short=True))
        d += dstep
    r = math.floor((ra0 - span_r) / rstep) * rstep
    while r <= ra0 + span_r:
        add([[r % 360, d_lo + (d_hi - d_lo) * i / n] for i in range(n + 1)], fmt_ra(r % 360, short=True))
        r += rstep
    return lines


def target_check(res: dict, target: str | None) -> dict | None:
    """Where is the project's object relative to the frame centre?"""
    if not target:
        return None
    o = find_object(target)
    if not o:
        return {"target": target, "found": False}
    w = _wcs(res)
    wd = res["wcs"]
    ra0, dec0 = w.all_pix2world([[wd["w"] / 2 - 0.5, wd["h"] / 2 - 0.5]], 0)[0]
    sep = float(_sep_deg(o["ra"], o["dec"], ra0, dec0))
    x, y = w.all_world2pix([[o["ra"], o["dec"]]], 0)[0]
    inside = bool(0 <= x <= wd["w"] and 0 <= y <= wd["h"])
    pa = _pa_deg(ra0, dec0, o["ra"], o["dec"])
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return {"target": target, "found": True, "name": o["name"], "messier": o["messier"], "common": o["common"],
            "sep_arcmin": round(sep * 60, 2), "inside": inside, "direction": dirs[int((pa + 22.5) // 45) % 8],
            "offset_pct": round(sep / (math.hypot(wd["w"], wd["h"]) / 2 *
                                       math.sqrt(abs(np.linalg.det(np.array(wd["cd"]))))) * 100)}


def fmt_ra(deg: float, short: bool = False) -> str:
    if short:
        m = round(deg / 15 * 60, 1)
        return f"{int(m // 60):02d}h{m % 60:04.1f}m"
    deg = deg % 360
    h = deg / 15
    hh, mm = int(h), int((h % 1) * 60)
    ss = ((h * 60) % 1) * 60
    return f"{hh:02d}h {mm:02d}m {ss:04.1f}s"


def fmt_dec(deg: float, short: bool = False) -> str:
    s = "-" if deg < 0 else "+"
    d = abs(deg)
    if short:
        m = round(d * 60)
        return f"{s}{m // 60}°{m % 60:02d}′"
    return f"{s}{int(d):02d}° {int((d % 1) * 60):02d}′ {((d * 60) % 1) * 60:04.1f}″"


def to_json(res: dict) -> str:
    return json.dumps(res, ensure_ascii=False)
