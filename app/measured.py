"""Measured values per session: medians of the plate solves (focal length, scale, rotation, limiting magnitude)
and of the stored optics summaries (FWHM, elongation, tilt) of its frames."""
import json
from datetime import datetime
from statistics import median


def load(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def optics_summary(res: dict) -> dict:
    """What is kept of an optics analysis (the mosaic and vectors are not stored)."""
    tilt = next((d for d in res.get("diagnosis", []) if d.get("key") == "tilt"), None)
    centre = (res.get("regions") or [{}] * 9)[4] if len(res.get("regions") or []) == 9 else {}
    return {"analysed_at": datetime.now().isoformat(timespec="seconds"), "stars": res.get("stars"),
            "fwhm_px": res.get("fwhm_px"), "fwhm_centre_px": centre.get("fwhm_px"), "elong": res.get("elong"),
            "bin": res.get("bin") or 1, "metrics": res.get("metrics") or {},
            "tilt_dir": tilt["dir"] if tilt else None,
            "findings": [d["key"] for d in res.get("diagnosis", []) if d.get("level") != "ok"]}


def _med(vals):
    vals = [v for v in vals if v is not None]
    return round(median(vals), 3) if vals else None


def sensor_scale(sol: dict):
    """arcsec per sensor pixel of a solve (corrected for resized images)."""
    return sol.get("sensor_scale_arcsec_px") or sol.get("scale_arcsec_px")


def session_measured(files, nominal_focal=None, nominal_scale=None, aperture_mm=None) -> dict | None:
    solves = [s for s in (load(f.solve) for f in files) if s and s.get("ok")]
    opts = [(f, load(f.optics)) for f in files if f.optics]
    opts = [(f, o) for f, o in opts if o and o.get("fwhm_px")]
    if not solves and not opts:
        return None
    out = {"solved": len(solves), "analysed": len(opts)}
    if solves:
        out["focal_mm"] = _med([s.get("focal_mm") for s in solves])
        out["scale"] = _med([sensor_scale(s) for s in solves])
        out["rotation"] = _med([s.get("rotation_deg") for s in solves])
        lims = [(s.get("photometry") or {}).get("limit_mag") for s in solves]
        out["limit_mag"] = _med(lims)
        out["limit_n"] = sum(1 for v in lims if v is not None)
        if out["focal_mm"] and nominal_focal:
            out["focal_diff_pct"] = round((out["focal_mm"] / nominal_focal - 1) * 100, 1)
        # conditions: sky brightness and zero point per second (transparency is relative – see zp_reference);
        # light frames only – stacks are calibrated / normalised (no offset, other exposure meaning)
        light_solves = [s for f, s in ((f, load(f.solve)) for f in files)
                        if s and s.get("ok") and getattr(f, "frame_type", "light") == "light"]
        skies = [s.get("sky") or {} for s in light_solves]
        out["sqm"] = _med([k.get("sqm") for k in skies])
        out["sqm_n"] = sum(1 for k in skies if k.get("sqm") is not None)
        notes = [k.get("sqm_note") for k in skies if k.get("sqm_note")]
        out["sqm_note"] = notes[0] if notes and out["sqm"] is None else None
        out["sqm_src"] = next((k.get("pedestal_src") for k in skies if k.get("sqm") is not None), None)
        gains = [norm_gain(k.get("gain")) for k in skies if k.get("zp_s") is not None]
        out["gain"] = max(set(gains), key=gains.count) if gains else None
        # zero point of the most common gain only (the reference is per gain)
        out["zp_s"] = _med([k.get("zp_s") for k in skies if norm_gain(k.get("gain")) == out["gain"]])
    if opts:
        scale = out.get("scale") or nominal_scale
        sol_by_id = {f.id: load(f.solve) for f, _ in opts}
        arcs = []
        for f, o in opts:
            sol = sol_by_id.get(f.id) or {}
            sc = sol.get("scale_arcsec_px") if sol.get("ok") else scale   # per image pixel (resized exports)
            if sc:
                arcs.append(o["fwhm_px"] * sc)
        out["fwhm_px"] = _med([o["fwhm_px"] for _, o in opts])
        out["fwhm_arcsec"] = _med(arcs)
        # seeing from the centre of the field (least optical aberrations)
        centre, scales = [], []
        for f, o in opts:
            if getattr(f, "frame_type", "light") != "light":
                continue
            sol = sol_by_id.get(f.id) or {}
            sc = sol.get("scale_arcsec_px") if sol.get("ok") else scale
            if sc:
                centre.append((o.get("fwhm_centre_px") or o["fwhm_px"]) * sc)
                scales.append(sc * (o.get("bin") or 1))    # sampling of the measured (2×2 binned colour) image
        from .conditions import seeing, seeing_label
        se = seeing(_med(centre), aperture_mm, _med(scales))
        if se:
            out["seeing"], out["seeing_label"] = se["seeing"], seeing_label(se["seeing"])
            out["seeing_undersampled"] = bool(se.get("undersampled"))
            out["seeing_focus"] = bool(se.get("likely_focus"))
        out["elong"] = _med([o.get("elong") for _, o in opts])
        out["tilt_pct"] = _med([(o.get("metrics") or {}).get("tilt_pct") for _, o in opts])
        dirs = [o.get("tilt_dir") for _, o in opts if o.get("tilt_dir")]
        out["tilt_frames"] = len(dirs)
        out["tilt_dir"] = max(set(dirs), key=dirs.count) if dirs else None
    return out


# ---------- transparency: zero point compared with the best night of the same setup ----------
_REF = {"at": 0.0, "data": None}


def norm_gain(g):
    """'101', '101.0', 101 -> '101'; ISO strings stay as they are."""
    if g is None or str(g).strip() == "":
        return None
    try:
        return f"{float(str(g).strip()):g}"
    except ValueError:
        return str(g).strip()


def equipment_key(telescope, camera, gain) -> str:
    return f"{telescope or ''}|{camera or ''}|{norm_gain(gain) or ''}"


def zp_reference(db, max_age_s: float = 60.0) -> dict:
    """Best (largest) session median zero point per second for each telescope | camera | gain."""
    import time
    from sqlalchemy import select
    from .models import FrameFile, ImagingSession
    if _REF["data"] is not None and time.time() - _REF["at"] < max_age_s:
        return _REF["data"]
    rows = db.execute(select(FrameFile.session_id, FrameFile.solve, ImagingSession.telescope, ImagingSession.camera)
                      .join(ImagingSession, ImagingSession.id == FrameFile.session_id)
                      .where(FrameFile.solve.is_not(None), FrameFile.frame_type == "light")).all()
    per = {}
    for sid, sol, tel, cam in rows:
        sky = (load(sol) or {}).get("sky") or {}
        if sky.get("zp_s") is not None:
            per.setdefault((sid, equipment_key(tel, cam, sky.get("gain"))), []).append(sky["zp_s"])
    best = {}
    for (sid, key), vals in per.items():
        m = median(vals)
        best[key] = max(best.get(key, m), m)
    _REF.update(at=time.time(), data=best)
    return best


def add_transparency(m: dict | None, telescope, camera, ref: dict) -> None:
    """Δ magnitude to the best night of the same setup (0 = best, 0.75 ≈ half the light lost)."""
    if not m or m.get("zp_s") is None:
        return
    best = ref.get(equipment_key(telescope, camera, m.get("gain")))
    if best is not None:
        m["transparency_mag"] = round(max(best - m["zp_s"], 0.0), 2)
        m["transparency_pct"] = round(10 ** (-0.4 * m["transparency_mag"]) * 100)
