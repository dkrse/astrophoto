"""Single files: viewer, render, histogram, metadata edit, reject, bulk operations."""
import json
import os
from datetime import datetime

from fastapi import Request, Form, Depends, HTTPException, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, FileResponse, PlainTextResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .core import (clean_filename, app, page, flash, get_db, current_user, none_if_empty, to_float, get_file, get_project,
                   DATA_DIR, file_owner, move_file_storage, delete_file_storage, unique_dest, project_dir,
                   load_settings)
from .models import User, Project, FrameFile, ImagingSession, CalibrationSet, FRAME_TYPES
from .preview import (PREVIEW_EXT, FITS_EXT, render, clean_opts, file_info, OPT_CHOICES, DEFAULT_OPTS,
                      RENDER_KEYS, fits_is_mono, histogram)
from . import worker


import threading
import time
_RENDER_SLOTS = threading.BoundedSemaphore(2)    # full-size FITS renders need a lot of RAM


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
                pos=i + 1, total=len(siblings), slideshow_s=cfg["slideshow_seconds"])


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
def file_thumb(fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    f = get_file(db, fid)
    if not f.thumb_path or not (DATA_DIR / f.thumb_path).exists():
        raise HTTPException(404)
    return FileResponse(DATA_DIR / f.thumb_path, headers={"Cache-Control": "no-cache"})


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

