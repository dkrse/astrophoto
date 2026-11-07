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
    return {"analysed_at": datetime.now().isoformat(timespec="seconds"), "stars": res.get("stars"),
            "fwhm_px": res.get("fwhm_px"), "elong": res.get("elong"), "metrics": res.get("metrics") or {},
            "tilt_dir": tilt["dir"] if tilt else None,
            "findings": [d["key"] for d in res.get("diagnosis", []) if d.get("level") != "ok"]}


def _med(vals):
    vals = [v for v in vals if v is not None]
    return round(median(vals), 3) if vals else None


def sensor_scale(sol: dict):
    """arcsec per sensor pixel of a solve (corrected for resized images)."""
    return sol.get("sensor_scale_arcsec_px") or sol.get("scale_arcsec_px")


def session_measured(files, nominal_focal=None, nominal_scale=None) -> dict | None:
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
    if opts:
        scale = out.get("scale") or nominal_scale
        sol_by_id = {f.id: load(f.solve) for f, _ in opts}
        arcs = []
        for f, o in opts:
            sol = sol_by_id.get(f.id) or {}
            sc = sensor_scale(sol) if sol.get("ok") else scale
            if sc:
                arcs.append(o["fwhm_px"] * sc)
        out["fwhm_px"] = _med([o["fwhm_px"] for _, o in opts])
        out["fwhm_arcsec"] = _med(arcs)
        out["elong"] = _med([o.get("elong") for _, o in opts])
        out["tilt_pct"] = _med([(o.get("metrics") or {}).get("tilt_pct") for _, o in opts])
        dirs = [o.get("tilt_dir") for _, o in opts if o.get("tilt_dir")]
        out["tilt_frames"] = len(dirs)
        out["tilt_dir"] = max(set(dirs), key=dirs.count) if dirs else None
    return out
