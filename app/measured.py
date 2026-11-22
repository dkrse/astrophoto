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


SIDES = ["right", "bottom right", "bottom", "bottom left", "left", "top left", "top", "top right"]   # optics.py


def sensor_side(side: str | None, orient: int = 0, flip: str = "none") -> str | None:
    """A side seen in the analysed (rotated by `orient`, then flipped) image -> the same side of the sensor
    array as read out (row 1 at the top). Tilt is fixed to the camera, so only this frame is comparable between
    frames analysed with different view settings."""
    if side not in SIDES:
        return None
    import math
    a = math.radians(SIDES.index(side) * 45)
    dx, dy = round(math.cos(a), 6), round(math.sin(a), 6)            # screen: x right, y down
    if "h" in (flip or ""):
        dx = -dx
    if "v" in (flip or ""):
        dy = -dy
    if orient == 90:                                 # analysed image = sensor turned clockwise: undo
        dx, dy = dy, -dx
    elif orient == 180:
        dx, dy = -dx, -dy
    elif orient == 270:
        dx, dy = -dy, dx
    return SIDES[int((math.degrees(math.atan2(dy, dx)) % 360 + 22.5) // 45) % 8]


def optics_summary(res: dict) -> dict:
    """What is kept of an optics analysis (the mosaic and vectors are not stored). The tilt side is stored in
    the sensor frame (dir_frame = "sensor"), independent of the flip / rotation the analysis was viewed with."""
    tilt = next((d for d in res.get("diagnosis", []) if d.get("key") == "tilt"), None)
    centre = (res.get("regions") or [{}] * 9)[4] if len(res.get("regions") or []) == 9 else {}
    return {"analysed_at": datetime.now().isoformat(timespec="seconds"), "stars": res.get("stars"),
            "fwhm_px": res.get("fwhm_px"), "fwhm_centre_px": centre.get("fwhm_px"), "elong": res.get("elong"),
            "bin": res.get("bin") or 1, "metrics": res.get("metrics") or {},
            "tilt_dir": sensor_side(tilt["dir"], res.get("orient") or 0, res.get("flip") or "none") if tilt else None,
            "dir_frame": "sensor",
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
        out["gain"] = max(sorted(set(gains)), key=gains.count) if gains else None    # ties: deterministic
        # zero point of the most common gain only (the reference is per gain)
        out["zp_s"] = _med([k.get("zp_s") for k in skies if norm_gain(k.get("gain")) == out["gain"]])
    if opts:
        # FWHM is in image pixels: unsolved frames take the median per-IMAGE scale of the solved ones (out["scale"]
        # is per sensor pixel – 2× off for a 50 % export), else the nominal scale
        scale = _med([s.get("scale_arcsec_px") for s in solves]) or nominal_scale
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
        # sides of analyses stored before the sensor frame (view-dependent) are not comparable: ignored
        dirs = [o.get("tilt_dir") for _, o in opts if o.get("tilt_dir") and o.get("dir_frame") == "sensor"]
        out["tilt_frames"] = len(dirs)
        out["tilt_dir"] = max(sorted(set(dirs)), key=dirs.count) if dirs else None
        out["corners_pct"] = _med([(o.get("metrics") or {}).get("corners_pct") for _, o in opts])
        fnd = [set(o.get("findings") or []) for _, o in opts]       # share of frames with each optics finding
        out["findings_share"] = {k: round(sum(k in x for x in fnd) / len(fnd), 2)
                                 for k in ("curvature", "tilt", "radial", "tangential", "uniform", "irregular")}
    return out


# ---------- transparency: zero point compared with the best night of the same setup ----------
_REF = {"at": 0.0, "data": None, "gen": 0}
_REF_LOCK = __import__("threading").Lock()


def invalidate_zp_reference() -> None:
    """A plate solve stored new sky values -> the next zp_reference() recomputes (no stale transparency)."""
    with _REF_LOCK:
        _REF["data"] = None
        _REF["gen"] += 1


# The reference is recomputed after a COMMIT that changed what it is built from (a solve, a frame's session /
# type / rejection, a deleted frame, a session's telescope / camera) – not before the commit (a request could
# otherwise cache the old rows for the TTL), and not after unrelated commits (previews, stats …).
_ZP_FRAME_ATTRS = ("solve", "session_id", "frame_type", "rejected")
_ZP_SESSION_ATTRS = ("telescope", "camera")


def _zp_relevant(session) -> bool:
    from sqlalchemy import inspect
    from .models import FrameFile, ImagingSession
    for obj in session.deleted:
        if isinstance(obj, (FrameFile, ImagingSession)):
            return True
    for obj in session.new:
        if isinstance(obj, FrameFile) and obj.solve:
            return True
    for obj in session.dirty:
        attrs = _ZP_FRAME_ATTRS if isinstance(obj, FrameFile) else (
            _ZP_SESSION_ATTRS if isinstance(obj, ImagingSession) else ())
        st = inspect(obj)
        if any(st.attrs[a].history.has_changes() for a in attrs):
            return True
    return False


def _install_zp_hooks():
    from sqlalchemy import event
    from .models import SessionLocal

    @event.listens_for(SessionLocal, "before_flush")
    def _mark(session, flush_context, instances):
        if _zp_relevant(session):
            session.info["zp_dirty"] = True

    @event.listens_for(SessionLocal, "after_commit")
    def _commit(session):
        if session.info.pop("zp_dirty", False):
            invalidate_zp_reference()

    @event.listens_for(SessionLocal, "after_rollback")
    def _rollback(session):
        session.info.pop("zp_dirty", None)


_install_zp_hooks()


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
    with _REF_LOCK:
        if _REF["data"] is not None and time.time() - _REF["at"] < max_age_s:
            return _REF["data"]
        gen = _REF["gen"]
    from sqlalchemy import cast, JSON
    sky = cast(FrameFile.solve, JSON)["sky"]        # only the two values – not the whole solve JSON (WCS …)
    rows = db.execute(select(FrameFile.session_id, ImagingSession.telescope, ImagingSession.camera,
                             sky["zp_s"].as_float(), sky["gain"].as_string())
                      .join(ImagingSession, ImagingSession.id == FrameFile.session_id)
                      .where(FrameFile.solve.is_not(None), FrameFile.frame_type == "light")).all()
    per = {}
    for sid, tel, cam, zp, gain in rows:
        if zp is not None:
            per.setdefault((sid, equipment_key(tel, cam, gain)), []).append(zp)
    best = {}
    for (sid, key), vals in per.items():
        m = median(vals)
        best[key] = max(best.get(key, m), m)
    with _REF_LOCK:
        if _REF["gen"] == gen:                     # invalidated while computing -> do not cache the old state
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
