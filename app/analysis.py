"""Plate solving and optics analysis of single files – shared by the HTTP endpoints and the background queue.

Each run is split into: inputs (DB, fast) -> compute (no DB, slow) -> store (DB, fast), so the worker can end its
read transaction while ASTAP / the star measurement runs."""
import json
from pathlib import Path

from .core import DATA_DIR, catalog, to_float
from .models import FrameFile
from .preview import FITS_EXT

ANALYSIS_KINDS = ("solve", "optics")


def rowflip(src: Path) -> str:
    """'v' when a FITS file is stored bottom-up (the viewer flips it by default), else 'none'."""
    if src.suffix.lower() not in FITS_EXT:
        return "none"
    from .preview import fits_header
    try:
        return "v" if str(fits_header(src).get("ROWORDER", "")).strip().upper() == "BOTTOM-UP" else "none"
    except Exception:
        return "none"


def load_json(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


# ---------- plate solve ----------
def solve_inputs(db, f: FrameFile) -> dict:
    s = f.session
    inp = {"src": DATA_DIR / f.stored_path, "header": f.header, "pixel_um": None, "focal": None, "sensor": None,
           "telescope": s.telescope if s else None, "camera": s.camera if s else None,
           "exposure": f.exposure_s, "gain": f.gain, "calib": None, "calib_kind": None,
           "frame_type": f.frame_type}
    if s and f.frame_type == "light":             # camera offset for the sky brightness: linked bias, else dark
        for kind in ("bias", "dark"):
            cands = [x for c in s.calib_sets if c.frame_type == kind for x in c.files if x.status == "done"]
            if cands:                             # the shortest dark has the least dark current / amp glow
                cf = min(cands, key=lambda x: (x.exposure_s or 0, x.id))
                inp["calib"], inp["calib_kind"] = DATA_DIR / cf.stored_path, kind
                break
    if s:
        cat = catalog(db)
        spec = lambda kind, name: next((it.spec for it in cat[kind] if it.name == name), {}) if name else {}
        cam = spec("camera", s.camera)
        inp["pixel_um"] = to_float(cam.get("pixel_um"))
        inp["focal"] = to_float(spec("telescope", s.telescope).get("focal_mm"))
        w, h = to_float(cam.get("width_px")), to_float(cam.get("height_px"))
        inp["sensor"] = (w, h) if w and h else None
    return inp


def solve_compute(inp: dict) -> dict:
    from . import platesolve
    if not inp["src"].exists():
        return {"ok": False, "error": "File missing on disk"}
    res = platesolve.solve(inp["src"], inp["header"], inp["pixel_um"], inp["focal"], inp["sensor"])
    if inp["telescope"] or inp["camera"]:
        res["camera"], res["telescope"] = inp["camera"], inp["telescope"]
    if res.get("ok") and inp["sensor"] and inp["pixel_um"]:
        b = res.get("binning") or 1             # compare with the binned sensor size
        platesolve.resize_check(res, (inp["sensor"][0] / b, inp["sensor"][1] / b), inp["focal"])
    if res.get("ok"):
        from . import conditions
        res["ra"], res["dec"] = platesolve.fmt_ra(res["ra_deg"]), platesolve.fmt_dec(res["dec_deg"])
        ped, src = conditions.pedestal(inp["src"], inp["header"], inp.get("calib"), inp.get("calib_kind"))
        if inp.get("frame_type", "light") == "light":       # stacks: no sky / zero point (calibrated, normalised)
            from .measured import norm_gain
            res["sky"] = conditions.sky_result(res, ped, src, inp.get("exposure"), norm_gain(inp.get("gain")))
    return res


def solve_store(f: FrameFile, res: dict) -> None:
    if res.get("error") == "File missing on disk":
        return
    f.solve = json.dumps(res, ensure_ascii=False)


# ---------- optics ----------
def optics_inputs(f: FrameFile, flip: str = "auto") -> dict:
    src = DATA_DIR / f.stored_path
    solved = load_json(f.solve) or {}
    return {"src": src, "flip": flip, "scale": solved.get("scale_arcsec_px") if solved.get("ok") else None,
            "orient": solved.get("orient") if "orient" in solved else None}


def optics_compute(inp: dict, tiles: bool = False) -> dict:
    from . import optics, platesolve
    src = inp["src"]
    if not src.exists():
        return {"ok": False, "error": "File missing on disk"}
    flip = inp["flip"]
    if flip == "auto":
        flip = rowflip(src)
    flip = flip if flip in ("none", "v", "h", "vh") else "none"
    orient = inp["orient"] if inp["orient"] is not None else platesolve._orientation(src)
    return optics.analyse(src, orient, flip, inp["scale"], tiles)


def optics_store(f: FrameFile, res: dict) -> None:
    from datetime import datetime
    if res.get("ok"):
        from .measured import optics_summary
        f.optics = json.dumps(optics_summary(res))
    elif res.get("error") != "File missing on disk" and not f.optics:
        f.optics = json.dumps({"ok": False, "error": res.get("error"),       # no fwhm -> ignored by the medians
                               "analysed_at": datetime.now().isoformat(timespec="seconds")})


# ---------- observing conditions of a session ----------
MEASURED_TAG = "(measured)"


def pick_per_session(files, n: int):
    """n frames per session spread over the night (first / middle / last …)."""
    from datetime import datetime
    by = {}
    for f in sorted(files, key=lambda x: (x.date_obs or datetime.min, x.id)):
        by.setdefault(f.session_id, []).append(f)
    out = []
    for fs in by.values():
        if len(fs) <= n:
            out += fs
        else:
            out += [fs[round(i * (len(fs) - 1) / (n - 1))] for i in range(n)] if n > 1 else [fs[len(fs) // 2]]
    return out


def queue_conditions(db, session_ids, per_session: int = 3, only_missing: bool = True) -> int:
    """Queue plate solve + optics for a few light frames of each session (what the condition values need)."""
    from sqlalchemy import select
    from .models import AnalysisTask
    files = db.scalars(select(FrameFile).where(FrameFile.session_id.in_(list(session_ids)),
                                               FrameFile.frame_type == "light", FrameFile.status == "done",
                                               FrameFile.rejected.is_not(True))).all()
    waiting = {(t.file_id, t.kind) for t in db.scalars(select(AnalysisTask).where(
        AnalysisTask.status.in_(("queued", "running"))))}
    n = 0
    for f in pick_per_session(files, per_session):
        for k in ("solve", "optics"):
            if (f.id, k) in waiting or (only_missing and (f.solve if k == "solve" else f.optics)):
                continue
            db.add(AnalysisTask(file_id=f.id, kind=k))
            n += 1
    return n


def auto_measure_project(db, project_id) -> int:
    """Settings 'measure new nights automatically': sessions with lights but no solve / optics yet."""
    from sqlalchemy import select
    from .core import load_settings
    from .models import ImagingSession, AnalysisTask
    cfg = load_settings(db)
    if not (cfg["cond_auto_measure"] and cfg["bg_analysis"]):
        return 0
    todo = []
    for s in db.scalars(select(ImagingSession).where(ImagingSession.project_id == project_id)):
        lights = [f for f in s.files if f.frame_type == "light" and f.status == "done"]
        if not lights or any(f.solve or f.optics for f in s.files):
            continue
        ids = [f.id for f in s.files]
        if db.scalar(select(AnalysisTask.id).where(AnalysisTask.file_id.in_(ids)).limit(1)):
            continue                                  # already queued once (also when it failed)
        todo.append(s.id)
    return queue_conditions(db, todo) if todo else 0


def session_conditions(db, s, transparency: bool = True) -> dict | None:
    from .core import catalog
    from .measured import zp_reference
    from .routes_projects import session_values
    return session_values(catalog(db), s, list(s.files), zp_reference(db, max_age_s=0) if transparency else None)


def fill_conditions(db, s, force: bool = False) -> dict:
    """Write measured seeing / SQM into the session fields. Without force only empty fields or values written
    by this function before (marked “(measured)”) are replaced – values typed by the user are kept."""
    m = session_conditions(db, s, transparency=False) or {}      # no archive-wide reference needed here
    done = {}
    seeing_ok = m.get("seeing") and (force or not m.get("seeing_focus"))   # defocused nights are not "seeing"
    vals = {"seeing": f"{m['seeing']:.1f}″ {MEASURED_TAG}" if seeing_ok else None,
            "sqm": f"{m['sqm']:.2f} {MEASURED_TAG}" if m.get("sqm") else None}
    for k, v in vals.items():
        cur = getattr(s, k) or ""
        if v and (force or not cur.strip() or cur.strip().endswith(MEASURED_TAG)) and cur != v:
            setattr(s, k, v)
            done[k] = v
    return done
