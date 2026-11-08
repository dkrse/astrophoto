"""Observing conditions from the frames themselves: seeing, transparency and sky brightness.

* Seeing ≈ star FWHM in arcsec in the centre of the field (from 🔬 Optics), with the telescope's diffraction
  removed (√(FWHM² − diffraction²)). It is an upper limit: focus, guiding and optics add to it, and when the stars
  are only ~2 px wide (undersampled) the value is coarse.
* Transparency: the photometric zero point of a plate solve (Gaia BP) per second of exposure. Compared with the
  best night of the same equipment and gain, the difference in magnitudes is the light lost in the atmosphere.
* Sky brightness (like an SQM meter, mag/arcsec²): zero point − 2.5·log10(sky level per arcsec²). The camera's
  offset (pedestal) is subtracted – from the RAW black level, a FITS keyword or a linked bias / dark frame;
  without it no value is given.
Values are estimates (Gaia BP band, not exactly the SQM / V band) – best for comparing nights of one setup."""
import math
from pathlib import Path

import numpy as np

LAMBDA_M = 550e-9
PEDESTAL_KEYS = ("PEDESTAL", "BLKLEVEL", "BLACKLEV")
_PED_CACHE: dict = {}


# ---------- seeing ----------
def diffraction_arcsec(aperture_mm) -> float | None:
    """FWHM of the Airy disk (1.03 λ/D) in arcsec."""
    try:
        d = float(aperture_mm)
    except (TypeError, ValueError):
        return None
    return 1.03 * LAMBDA_M / (d / 1000) * 206264.8 if d > 0 else None


def seeing(fwhm_arcsec, aperture_mm=None, scale_arcsec=None) -> dict | None:
    if not fwhm_arcsec or fwhm_arcsec <= 0:
        return None
    diff = diffraction_arcsec(aperture_mm) or 0.0
    if fwhm_arcsec <= diff:                   # not measurable (below the diffraction limit)
        return None
    s = math.sqrt(fwhm_arcsec ** 2 - diff ** 2)
    out = {"seeing": round(s, 2), "fwhm_arcsec": round(fwhm_arcsec, 2),
           "diffraction": round(diff, 2) if diff else None}
    if scale_arcsec and fwhm_arcsec / scale_arcsec < 2.5:
        out["undersampled"] = True            # < 2.5 px per FWHM: only a coarse upper limit
    if s > 6.0:
        out["likely_focus"] = True            # atmosphere rarely does this – focus, optics or guiding dominate
    return out


def seeing_label(s) -> str:
    if s is None:
        return ""
    for lim, txt in ((1.5, "excellent"), (2.5, "good"), (3.5, "average"), (5.0, "poor")):
        if s < lim:
            return txt
    return "bad"


# ---------- pedestal (camera offset) ----------
def _hdr_float(header_text, keys):
    for line in (header_text or "").splitlines():
        k = line[:8].strip().upper()
        if k in keys and "=" in line[:10]:
            try:
                return float(line.split("=", 1)[1].split("/")[0].strip().strip("'"))
            except ValueError:
                pass
    return None


def raw_black_level(src: Path) -> float | None:
    from .preview import RAW_EXT
    if src.suffix.lower() not in RAW_EXT:
        return None
    try:
        import rawpy
        with rawpy.imread(str(src)) as raw:
            bl = raw.black_level_per_channel
            return float(np.mean(bl)) if bl is not None and len(bl) else None
    except Exception:
        return None


def frame_median(path: Path) -> float | None:
    """Median of a bias / dark frame (same 2×2 luminance as the solved light) – cached per file."""
    from .framestats import _luminance
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return None
    if key in _PED_CACHE:
        return _PED_CACHE[key]
    try:
        lum, _ = _luminance(path)
        v = float(np.median(np.asarray(lum, dtype=np.float32)[::2, ::2])) if lum is not None else None
    except Exception:
        v = None
    if len(_PED_CACHE) > 200:
        _PED_CACHE.clear()
    _PED_CACHE[key] = v
    return v


def pedestal(src: Path, header_text, calib_path: Path | None = None, calib_kind: str | None = None):
    """(value in ADU, source) or (None, None)."""
    v = raw_black_level(src)
    if v is not None:
        return v, "RAW black level"
    v = _hdr_float(header_text, PEDESTAL_KEYS)
    if v is not None:
        return v, "FITS header"
    if calib_path is not None:
        v = frame_median(calib_path)
        if v is not None:
            return v, f"linked {calib_kind or 'calibration'} frame"
    return None, None


# ---------- sky brightness / transparency ----------
def sky_level(lum: np.ndarray) -> float:
    """Robust sky level of a frame: median after clipping bright pixels (stars, galaxies)."""
    a = np.asarray(lum, dtype=np.float32)[::4, ::4].ravel()
    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med))) * 1.4826 or 1.0
    return float(np.median(a[a < med + 3 * mad]))


def sky_brightness(zero_point, sky_adu, pedestal_adu, arcsec_per_px) -> float | None:
    """mag/arcsec² from the photometric zero point (per image of the same pixels / units)."""
    if zero_point is None or sky_adu is None or pedestal_adu is None or not arcsec_per_px:
        return None
    signal = sky_adu - pedestal_adu
    if signal <= 0:
        return None
    return round(zero_point - 2.5 * math.log10(signal / arcsec_per_px ** 2), 2)


def zp_per_second(zero_point, exposure_s) -> float | None:
    if zero_point is None or not exposure_s or exposure_s <= 0:
        return None
    return round(zero_point - 2.5 * math.log10(exposure_s), 3)


def sqm_label(v) -> str:
    if v is None:
        return ""
    for lim, txt in ((18.0, "city"), (19.5, "suburban"), (20.5, "rural / suburban"), (21.3, "dark rural")):
        if v < lim:
            return txt
    return "very dark"


def sky_result(res: dict, ped, ped_src, exposure_s=None, gain=None) -> dict:
    """Condition values of one solved frame (stored in the solve result as res['sky'])."""
    ph = res.get("photometry") or {}
    zp = ph.get("zero_point")
    lum_scale = (res.get("scale_arcsec_px") or 0) * (res.get("lum_bin") or 1)   # arcsec per measured pixel
    out = {"sky_adu": res.get("sky_adu"), "pedestal": ped, "pedestal_src": ped_src,
           "exposure_s": exposure_s, "gain": gain, "zp_s": zp_per_second(zp, exposure_s)}
    out["sqm"] = sky_brightness(zp, res.get("sky_adu"), ped, lum_scale)
    if out["sqm"] is None:
        out["sqm_note"] = ("no photometry" if zp is None else
                           "camera offset unknown – link a bias or dark set to the session" if ped is None else
                           "sky level below the offset")
    return out
