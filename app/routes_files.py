"""Single files: viewer, render, histogram, metadata edit, reject, bulk operations."""
import json
import os
from datetime import datetime

from fastapi import Request, Form, Depends, HTTPException, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, FileResponse, PlainTextResponse, JSONResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .core import (clean_filename, app, page, flash, get_db, current_user, none_if_empty, to_float, get_file, get_project,
                   DATA_DIR, file_owner, move_file_storage, delete_file_storage, unique_dest, project_dir,
                   load_settings)
from .models import User, Project, FrameFile, ImagingSession, CalibrationSet, AnalysisTask, FRAME_TYPES
from .preview import (PREVIEW_EXT, FITS_EXT, render, clean_opts, file_info, OPT_CHOICES, DEFAULT_OPTS,
                      RENDER_KEYS, fits_is_mono, histogram)
from . import worker


import threading
import time
_RENDER_SLOTS = threading.BoundedSemaphore(2)    # full-size FITS renders need a lot of RAM


def slotted(fn, *args):
    """Run fn in a worker thread while holding a render slot – the slot is taken *in that thread*, never on the
    event loop (a blocking acquire there would freeze every request of the server)."""
    def run():
        with _RENDER_SLOTS:
            return fn(*args)
    return run_in_threadpool(run)


def back_url(f: FrameFile) -> str:
    o = f"/projects/{f.project_id}" if f.project_id else f"/calibration/{f.calib_set_id}"
    return f"{o}#{f.frame_type}"


@app.get("/files/{fid}/download")
def file_download(fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    f = get_file(db, fid)
    src = DATA_DIR / f.stored_path
    if not src.is_file() or f.status == "importing":
        raise HTTPException(404, "File is missing on disk" if f.status != "importing" else "File is still being imported")
    return FileResponse(src, filename=f.original_name)


@app.get("/files/{fid}/view")
def file_view(request: Request, fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    f = get_file(db, fid)
    owner = file_owner(f)
    siblings = [x for x in owner["files"] if x.frame_type == f.frame_type]
    i = siblings.index(f)
    src = DATA_DIR / f.stored_path
    ext = src.suffix.lower()
    info = file_info(src, f.header) if src.exists() else {"image": [], "header": [], "exif": []}
    cfg = load_settings(db)
    return page(request, "viewer.html", user=user, f=f, owner=owner, opts=worker.get_opts(f),
                choices=OPT_CHOICES, defaults=DEFAULT_OPTS, is_fits=ext in FITS_EXT,
                fits_mono=ext in FITS_EXT and src.exists() and fits_is_mono(src),
                can_render=ext in PREVIEW_EXT and src.exists(), info=info,
                prev_f=siblings[i - 1] if i > 0 else None,
                next_f=siblings[i + 1] if i + 1 < len(siblings) else None,
                pos=i + 1, total=len(siblings), slideshow_s=cfg["slideshow_seconds"], solved=solved_view(db, f),
                rowflip=ext in FITS_EXT and src.exists() and info_rowflip(src) == "BOTTOM-UP")


def info_rowflip(src):
    from .preview import fits_header
    try:
        return str(fits_header(src).get("ROWORDER", "")).strip().upper()
    except Exception:
        return ""


def _solved(f: FrameFile):
    try:
        return json.loads(f.solve) if f.solve else None
    except ValueError:
        return None


@app.get("/files/{fid}/render")
async def file_render(request: Request, fid: int, size: str = "full",
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Render file to JPEG with view options; cached on disk."""
    f = get_file(db, fid)
    src = DATA_DIR / f.stored_path
    if not src.is_file() or f.status == "importing":
        raise HTTPException(404, "File not available")
    if size not in ("full", "preview"):          # goes into the cache file name -> strict whitelist
        size = "preview"
    opts = clean_opts(dict(request.query_params))
    max_side = None if size == "full" else 1600
    key = "_".join(opts[k] for k in RENDER_KEYS) + f"_{size}"
    cache = DATA_DIR / ".cache" / str(f.id) / f"{key}_{int(src.stat().st_mtime)}.jpg"
    if not cache.exists():
        def work():
            try:
                with _RENDER_SLOTS:
                    if cache.exists():
                        return True
                    im, _ = render(src, opts, max_side)
            except Exception as e:
                print(f"render failed for {src}: {e}")
                return False
            if im is None:
                return False
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(f"{cache.stem}.{threading.get_ident()}.{time.time_ns()}.tmp")   # unique per writer
            im.save(tmp, "JPEG", quality=90)
            tmp.replace(cache)
            return True
        if not await run_in_threadpool(work):
            raise HTTPException(404, "Preview not available")
    return FileResponse(cache, headers={"Cache-Control": "max-age=86400"})


@app.get("/files/{fid}/histogram")
async def file_histogram(request: Request, fid: int, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    f = get_file(db, fid)
    src = DATA_DIR / f.stored_path
    if not src.is_file() or f.status == "importing":
        raise HTTPException(404, "File not available")
    opts = clean_opts(dict(request.query_params))
    cache = DATA_DIR / ".cache" / str(f.id) / f"hist_{opts['bayer']}_{opts['flip']}_{int(src.stat().st_mtime)}.json"
    if cache.exists():
        try:
            return JSONResponse(json.loads(cache.read_text()))
        except ValueError:                        # damaged / concurrently written -> recompute
            pass
    try:
        h = await run_in_threadpool(histogram, src, opts)
    except Exception as e:
        raise HTTPException(500, f"Histogram failed: {e}")
    if not h:
        raise HTTPException(404, "Histogram not available")
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(f"{cache.stem}.{threading.get_ident()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(h))
    os.replace(tmp, cache)                        # atomic: readers never see a half-written file
    return h


@app.post("/files/{fid}/opts")
async def file_save_opts(request: Request, fid: int, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    """Save view options (display only) for this file or all of its type, rebuild thumbnails."""
    f = get_file(db, fid)
    form = await request.form()
    opts = clean_opts(dict(form))
    targets = [x for x in file_owner(f)["files"] if x.frame_type == f.frame_type] if form.get("all") else [f]
    for x in targets:
        x.view_opts = json.dumps(opts)
    if len(targets) == 1:
        if f.status == "done":
            await run_in_threadpool(worker.build_preview, f)
        msg = "View settings saved"
    else:   # many files: regenerate thumbnails in the background
        for x in targets:
            if x.status in ("done", "error"):
                x.status = "pending"
        worker.wake()
        msg = f"View settings saved for {len(targets)} files – thumbnails are regenerated in the background"
    db.commit()
    flash(request, msg)
    return RedirectResponse(f"/files/{fid}/view", status_code=303)


@app.get("/files/{fid}/thumb")
def file_thumb(fid: int, size: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Preview image. size=s (240 px, file lists) / m (640 px, cards, gallery) are made from the 1600 px thumbnail
    once and cached – a list of 100 files then loads ~2 MB instead of ~70 MB."""
    f = get_file(db, fid)
    src = DATA_DIR / f.thumb_path if f.thumb_path else None
    if src is None or not src.exists():
        raise HTTPException(404)
    side = THUMB_SIZES.get(size)
    if side:
        src = small_thumb(f.id, src, size, side)
    return FileResponse(src, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


THUMB_SIZES = {"s": 240, "m": 640}


def small_thumb(fid: int, src, size: str, side: int):
    """Downscaled copy of a thumbnail in the render cache, rebuilt when the thumbnail changes."""
    from PIL import Image
    dst = DATA_DIR / ".cache" / "thumbs" / f"{fid}_{size}.jpg"
    try:
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im.draft("RGB", (side * 2, side * 2))           # fast JPEG decoding at reduced size
            im = im.convert("RGB")
            im.thumbnail((side, side))
            tmp = dst.with_suffix(f".{os.getpid()}.tmp")
            im.save(tmp, "JPEG", quality=82, optimize=True)
        os.replace(tmp, dst)                                # atomic: parallel requests never see half files
        return dst
    except OSError:
        return src


@app.get("/files/{fid}/header", response_class=PlainTextResponse)
def file_header(fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return get_file(db, fid).header or "No FITS header"


@app.post("/files/{fid}/edit")
def file_edit(request: Request, fid: int, description: str = Form(""), exposure_s: str = Form(""),
              gain: str = Form(""), temperature: str = Form(""), filter: str = Form(""),
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    f = get_file(db, fid)
    f.description = none_if_empty(description)
    f.exposure_s = to_float(exposure_s)
    f.gain, f.temperature, f.filter = none_if_empty(gain), none_if_empty(temperature), none_if_empty(filter)
    f.updated_at = datetime.now()
    db.commit()
    flash(request, "File updated")
    return RedirectResponse(back_url(f), status_code=303)


@app.post("/files/{fid}/reject")
def file_reject(request: Request, fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    f = get_file(db, fid)
    f.rejected = not f.rejected
    f.updated_at = datetime.now()
    db.commit()
    if request.headers.get("accept", "").startswith("application/json"):
        return {"rejected": f.rejected}
    return RedirectResponse(request.headers.get("referer") or back_url(f), status_code=303)


@app.post("/files/{fid}/delete")
def file_delete(request: Request, fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    f = get_file(db, fid)
    url = back_url(f)
    if f.status == "importing":
        flash(request, "File is still being imported – try again in a moment", "err")
        return RedirectResponse(url, status_code=303)
    pid = f.project_id
    delete_files(db, [f])
    db.commit()
    if pid:
        worker.reflag_project(db, pid)
        db.commit()
    flash(request, "File deleted")
    return RedirectResponse(url, status_code=303)


def delete_files(db, files):
    for f in files:
        if f.project_id and f.project and f.project.cover_file_id == f.id:
            f.project.cover_file_id = None
        delete_file_storage(f, db)
        db.delete(f)


@app.post("/projects/{pid}/rejectsuspects")
def reject_suspects(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_project(db, pid)
    n = 0
    for f in p.files:
        if f.frame_type == "light" and f.suspect and not f.rejected:
            f.rejected = True
            n += 1
    db.commit()
    flash(request, f"Rejected {n} suspicious frames")
    return RedirectResponse(f"/projects/{pid}#light", status_code=303)


# ---------- bulk operations ----------
@app.post("/api/files/bulk")
async def files_bulk(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = await request.json()
    ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    files = db.scalars(select(FrameFile).where(FrameFile.id.in_(ids))).all() if ids else []
    if not files:
        return JSONResponse({"error": "No files selected"}, status_code=400)
    action, value = data.get("action"), data.get("value")
    busy = [f for f in files if f.status in ("importing", "pending")]
    if busy and action in ("delete", "type", "move"):
        return JSONResponse({"error": f"{len(busy)} selected file(s) are still being imported / processed – "
                                      "try again in a moment"}, status_code=409)
    n = len(files)
    touched = {f.project_id for f in files if f.project_id}
    if action == "delete":
        delete_files(db, files)
        msg = f"Deleted {n} files"
    elif action == "type":
        if value not in FRAME_TYPES:
            return JSONResponse({"error": "Invalid type"}, status_code=400)
        try:
            for f in files:
                if f.frame_type != value:
                    move_file_storage(f, f.project_id, f.calib_set_id, value)
                    f.frame_type = value
                    if value == "light" and f.status == "done" and f.stat_stars is None:
                        f.status = "pending"
                    f.updated_at = datetime.now()
                    db.commit()
        except OSError as e:
            db.commit()
            return JSONResponse({"error": f"Stopped after a file could not be moved ({e}). "
                                          "Files before it were changed."}, status_code=409)
        msg = f"Changed type of {n} files"
    elif action == "move":
        target = str(value or "")
        if target.startswith("calib:"):
            c = db.get(CalibrationSet, int(target[6:])) if target[6:].isdigit() else None
            if not c:
                return JSONResponse({"error": "Calibration set not found"}, status_code=404)
            try:
                for f in files:
                    if f.project_id and f.project.cover_file_id == f.id:
                        f.project.cover_file_id = None
                    move_file_storage(f, None, c.id)
                    f.project_id, f.calib_set_id, f.session_id = None, c.id, None
                    f.updated_at = datetime.now()
                    db.commit()
            except OSError as e:
                db.commit()
                return JSONResponse({"error": f"Stopped after a file could not be moved ({e}). "
                                          "Files before it were changed."}, status_code=409)
            msg = f"Moved {n} files to calibration set “{c.name}”"
        else:
            p = db.get(Project, int(target)) if target.isdigit() else None
            if not p:
                return JSONResponse({"error": "Project not found"}, status_code=404)
            try:
                for f in files:
                    if f.project_id and f.project_id != p.id and f.project.cover_file_id == f.id:
                        f.project.cover_file_id = None
                    move_file_storage(f, p.id, None)
                    if f.project_id != p.id:          # files always live in a session -> latest one of the target
                        f.session_id = p.sessions[-1].id if p.sessions else None
                    f.project_id, f.calib_set_id = p.id, None
                    f.updated_at = datetime.now()
                    db.commit()
            except OSError as e:
                db.commit()
                return JSONResponse({"error": f"Stopped after a file could not be moved ({e}). "
                                          "Files before it were changed."}, status_code=409)
            msg = f"Moved {n} files to project “{p.name}”"
    elif action == "session":
        if value in (None, ""):
            s = None                              # explicit "(none)"
        else:
            s = db.get(ImagingSession, int(value)) if str(value).isdigit() else None
            if not s:
                return JSONResponse({"error": "Session not found"}, status_code=404)
        done = 0
        for f in files:
            if not f.project_id or (s and f.project_id != s.project_id):
                continue                          # library files / other projects are not touched
            f.session_id = s.id if s else None
            done += 1
        if not done:
            return JSONResponse({"error": "None of the selected files belong to this session's project"}, status_code=400)
        msg = f"Session updated for {done} file{'s' if done != 1 else ''}"
        n = done
    elif action == "meta":
        vals = value or {}
        for f in files:
            for k in ("description", "filter", "gain", "temperature"):
                if (vals.get(k) or "").strip():
                    setattr(f, k, vals[k].strip())
            if to_float(vals.get("exposure_s")) is not None:
                f.exposure_s = to_float(vals["exposure_s"])
        msg = f"Metadata updated for {n} files"
    elif action in ("reject", "unreject"):
        for f in files:
            f.rejected = action == "reject"
        msg = f"{'Rejected' if action == 'reject' else 'Restored'} {n} files"
    elif action == "reprocess":
        for f in files:
            if f.status != "importing":
                f.status = "pending"
        worker.wake()
        msg = f"{n} files queued for processing"
    else:
        return JSONResponse({"error": "Unknown action"}, status_code=400)
    if action not in ("delete", "reprocess"):
        now = datetime.now()
        for f in files:
            f.updated_at = now
    db.commit()
    if action in ("delete", "type", "move", "session"):   # light set changed -> recompute "suspect" flags
        for pid in touched | {f.project_id for f in files if action != "delete" and f.project_id}:
            worker.reflag_project(db, pid)
        db.commit()
    flash(request, msg)
    return {"ok": True, "message": msg}



# ---------- plate solving (ASTAP, on demand) ----------
@app.post("/api/files/{fid}/solve")
async def file_solve(fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from . import analysis
    f = get_file(db, fid)
    if not (DATA_DIR / f.stored_path).exists():
        return JSONResponse({"ok": False, "error": "File missing on disk"}, status_code=404)
    res = await run_in_threadpool(analysis.solve_compute, analysis.solve_inputs(db, f))
    analysis.solve_store(f, res)
    db.commit()
    return with_target(f, res)


def file_target(f: FrameFile) -> str | None:
    p = f.project
    return (p.target or p.name) if p else None


def with_target(f: FrameFile, res: dict | None):
    """Adds the (always fresh) position of the project's object relative to the frame centre."""
    from . import platesolve
    if res and res.get("ok") and res.get("wcs"):
        try:
            res = {**res, "target_check": platesolve.target_check(res, file_target(f))}
        except Exception as e:                     # never break the viewer because of the catalogue
            res = {**res, "target_check": {"target": file_target(f), "found": False, "error": str(e)}}
    return res


@app.get("/api/files/{fid}/solved")
def file_solved(fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The stored plate solve (with a fresh pointing check)."""
    res = solved_view(db, get_file(db, fid))
    if not res:
        return JSONResponse({"ok": False, "error": "Not solved yet"}, status_code=404)
    return res


def solved_view(db, f: FrameFile):
    """Stored solve + fresh pointing check + comparison with the *current* equipment (stale warning)."""
    res = _solved(f)
    if not res:
        return None
    res = with_target(f, res)
    if f.session:
        from .core import catalog
        tel = next((it.spec for it in catalog(db)["telescope"] if it.name == f.session.telescope), {})
        if res.get("ok") and to_float(tel.get("focal_mm")):     # compare with the current equipment value
            res["focal_expected_mm"] = to_float(tel.get("focal_mm"))
        cam = next((it.spec for it in catalog(db)["camera"] if it.name == f.session.camera), {})
        px_now = to_float(cam.get("pixel_um"))
        if res.get("ok") and res.get("pixel_um") and px_now and \
                abs(px_now * (res.get("binning") or 1) - res["pixel_um"]) > 0.005:
            res["stale"] = "The camera's pixel size changed since this solve – click Solve again for a correct focal length."
        res.setdefault("telescope", f.session.telescope)
        res.setdefault("camera", f.session.camera)
    return res


@app.get("/api/files/{fid}/annotations")
def file_annotations(fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from . import platesolve
    res = _solved(get_file(db, fid))
    if not res or not res.get("ok"):
        return JSONResponse({"error": "File is not plate-solved yet"}, status_code=404)
    if not res.get("wcs"):
        return JSONResponse({"error": "Solved with an older version – click Solve again"}, status_code=409)
    out = platesolve.annotations(res)
    out["orient"] = res.get("orient", 0)
    return out


# ---------- optics inspector (on demand, not stored) ----------
@app.get("/api/files/{fid}/optics")
async def file_optics(fid: int, flip: str = "auto", tiles: int = 1, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    from . import analysis
    f = get_file(db, fid)
    if not (DATA_DIR / f.stored_path).exists():
        return JSONResponse({"ok": False, "error": "File missing on disk"}, status_code=404)
    inp = analysis.optics_inputs(f, flip)
    res = await slotted(analysis.optics_compute, inp, bool(tiles))
    analysis.optics_store(f, res)              # summary for the session / trend views
    db.commit()
    if res.get("ok") and res.get("fwhm_arcsec") and len(res.get("regions") or []) == 9:
        from .conditions import seeing, seeing_label
        from .core import catalog
        c = res["regions"][4]
        per_px = res["fwhm_arcsec"] / res["fwhm_px"]
        tel = next((it.spec for it in catalog(db)["telescope"] if f.session and it.name == f.session.telescope), {})
        se = seeing((c.get("fwhm_px") or res["fwhm_px"]) * per_px, tel.get("aperture_mm"),
                    per_px * (res.get("bin") or 1))
        if se:
            res["seeing"] = {**se, "label": seeing_label(se["seeing"])}
    return res


@app.post("/api/equipment/focal")
async def equipment_focal(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Store a measured focal length in the telescope's parameters (from a plate solve)."""
    from .models import CatalogItem
    data = await request.json()
    name, focal = data.get("telescope"), to_float(data.get("focal_mm"))
    if not name or not focal or not 10 <= focal <= 20000:
        return JSONResponse({"error": "Telescope and a focal length (10–20000 mm) are required"}, status_code=400)
    it = db.scalars(select(CatalogItem).where(CatalogItem.kind == "telescope", CatalogItem.name == name)).first()
    if not it:
        return JSONResponse({"error": "Telescope not found in the equipment list"}, status_code=404)
    spec = it.spec
    old = spec.get("focal_mm")
    spec["focal_mm"] = round(focal) if focal >= 100 else round(focal, 1)
    it.specs = json.dumps(spec)
    db.commit()
    return {"ok": True, "telescope": name, "old": old, "focal_mm": spec["focal_mm"]}


@app.get("/files/{fid}/annotated.jpg")
async def file_annotated(fid: int, flip: str = "auto", grid: int = 1, north: str = "", user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    """The image as shown in the viewer (saved view settings) with objects, RA/Dec grid and N/E drawn in."""
    from fastapi.responses import Response
    from . import annotate
    f = get_file(db, fid)
    res = _solved(f)
    if not res or not res.get("ok") or not res.get("wcs"):
        raise HTTPException(409, "Plate-solve the file first")
    src = DATA_DIR / f.stored_path
    opts = dict(worker.get_opts(f))
    if flip != "auto":
        opts["flip"] = flip if flip in ("none", "v", "h", "vh") else "none"
    eff = opts["flip"]
    if eff == "auto":
        eff = "v" if src.suffix.lower() in FITS_EXT and info_rowflip(src) == "BOTTOM-UP" else "none"
    data = await slotted(annotate.annotated_jpeg, src, opts, res, eff, bool(grid), (north or opts.get("north")) == "1")
    if data is None:
        raise HTTPException(422, "Cannot render this file")
    name = os.path.splitext(clean_filename(f.original_name))[0] + "_annotated.jpg"
    return Response(data, media_type="image/jpeg", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------- background analysis queue (plate solve / optics) ----------
from .analysis import pick_per_session  # noqa: E402  (shared with the conditions queue)


@app.post("/api/analysis/queue")
async def analysis_queue(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Queue plate solves / optics analyses for the worker.
    scope: files (ids) | project (project_id) | all;  kind: solve | optics | both;
    only_missing: skip files that already have the result;  per_session: 0 = all frames, N = N per session."""
    from .models import AnalysisTask
    data = await request.json()
    kinds = {"solve": ["solve"], "optics": ["optics"], "both": ["solve", "optics"]}.get(data.get("kind"))
    if not kinds:
        return JSONResponse({"error": "Unknown kind"}, status_code=400)
    q = select(FrameFile).where(FrameFile.status == "done", FrameFile.frame_type.in_(("light", "stack")),
                                FrameFile.rejected.is_not(True))
    scope = data.get("scope")
    if scope == "files":
        ids = [int(i) for i in data.get("ids") or [] if str(i).isdigit()]
        q = q.where(FrameFile.id.in_(ids))
    elif scope == "project":
        q = q.where(FrameFile.project_id == int(data.get("project_id") or 0))
    elif scope == "session":
        q = q.where(FrameFile.session_id == int(data.get("session_id") or 0))
    elif scope == "all":
        q = q.where(FrameFile.project_id.is_not(None))
    else:
        return JSONResponse({"error": "Unknown scope"}, status_code=400)
    files = db.scalars(q).all()
    per = int(data.get("per_session") or 0)
    if per > 0:
        files = pick_per_session([f for f in files if f.frame_type == "light"], per) + \
            [f for f in files if f.frame_type == "stack"]
    if not db.scalar(select(func.count()).select_from(AnalysisTask).where(AnalysisTask.status.in_(("queued", "running")))):
        db.query(AnalysisTask).filter(AnalysisTask.status.in_(("done", "error"))) \
            .delete(synchronize_session=False)      # new batch: progress starts at 0 (never drops queued work)
    waiting = {(t.file_id, t.kind) for t in db.scalars(select(AnalysisTask).where(AnalysisTask.status.in_(("queued", "running"))))}
    n = 0
    for f in files:
        for k in kinds:
            if k == "optics" and f.frame_type != "light":
                continue
            if data.get("only_missing") and (f.solve if k == "solve" else f.optics):
                continue
            if (f.id, k) in waiting:
                continue
            db.add(AnalysisTask(file_id=f.id, kind=k))
            n += 1
    db.commit()
    worker.wake()
    return {"ok": True, "queued": n}


@app.post("/api/analysis/cancel")
async def analysis_cancel(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from .models import AnalysisTask
    data = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    q = select(AnalysisTask.id).where(AnalysisTask.status == "queued")
    if data.get("project_id"):
        q = q.join(FrameFile, FrameFile.id == AnalysisTask.file_id).where(FrameFile.project_id == int(data["project_id"]))
    ids = list(db.scalars(q))
    if ids:
        db.query(AnalysisTask).filter(AnalysisTask.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "cancelled": len(ids)}


@app.get("/api/analysis/status")
def analysis_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return worker.queue_status(db)



# ---------- observing conditions (seeing, transparency, sky brightness) of a session ----------
@app.post("/api/sessions/{sid}/conditions/measure")
def session_measure(sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Manual: plate solve + optics of 3 frames spread over the night (background queue)."""
    from . import analysis
    s = db.get(ImagingSession, sid)
    if not s:
        raise HTTPException(404, "Session not found")
    if not db.scalar(select(func.count()).select_from(AnalysisTask).where(AnalysisTask.status.in_(("queued", "running")))):
        db.query(AnalysisTask).filter(AnalysisTask.status.in_(("done", "error"))).delete(synchronize_session=False)
    n = analysis.queue_conditions(db, [s.id], 3, only_missing=False)
    db.commit()
    worker.wake()
    return {"ok": True, "queued": n, "paused": not load_settings(db)["bg_analysis"]}


@app.post("/api/sessions/{sid}/conditions/apply")
def session_apply_conditions(sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Manual: write the measured seeing / SQM into the session (overwrites the fields)."""
    from . import analysis
    s = db.get(ImagingSession, sid)
    if not s:
        raise HTTPException(404, "Session not found")
    done = analysis.fill_conditions(db, s, force=True)
    db.commit()
    return {"ok": True, "set": done, "conditions": analysis.session_conditions(db, s)}


@app.get("/api/sessions/{sid}/conditions")
def session_get_conditions(sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from . import analysis
    s = db.get(ImagingSession, sid)
    if not s:
        raise HTTPException(404, "Session not found")
    return analysis.session_conditions(db, s) or {}
