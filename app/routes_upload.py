"""Chunked (resumable) browser uploads and server-side folder import."""
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from fastapi import Request, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .core import (clean_filename, app, page, get_db, current_user, DATA_DIR, IMPORT_DIR, none_if_empty, to_float,
                   owner_dir, unique_dest, get_project, get_calib, load_settings, flash)
from .models import User, Project, FrameFile, CalibrationSet, ImagingSession, ImportJob, FRAME_TYPES
from .preview import KNOWN_EXT, FITS_EXT, detect_frame_type, fits_header
from . import worker

UP_DIR = DATA_DIR / ".uploads"
_FINISHING: set = set()   # upload ids whose finish is in progress
_LOCKS: dict = {}  # upload id -> asyncio.Lock (serialises chunk writes of one upload)
_HASH: dict = {}   # upload id -> running sha256 (in-memory; after a restart the hash is computed at finish)
META_KEYS = ("description", "exposure_s", "gain", "temperature", "filter")


def err(msg, code=400, **extra):
    return JSONResponse({"error": msg, **extra}, status_code=code)


def resolve_owner(db, data) -> tuple:
    """Returns (project, calib_set) from request data; exactly one is set."""
    if not isinstance(data, dict):
        raise HTTPException(400, "Invalid request")
    pid, cid = data.get("project_id"), data.get("calib_set_id")
    for v in (pid, cid):
        if v not in (None, "") and not str(v).isdigit():
            raise HTTPException(400, "Invalid target id")
    if pid:
        return get_project(db, int(pid)), None
    if cid:
        return None, get_calib(db, int(cid))
    raise HTTPException(400, "Missing target")


def session_for(db, project, sid):
    if not project or not str(sid or "").isdigit():
        return None
    s = db.get(ImagingSession, int(sid))
    return s.id if s and s.project_id == project.id else None


def describe_owner(f: FrameFile) -> str:
    if f.project_id:
        return f"project “{f.project.name}”"
    if f.calib_set_id:
        return f"calibration set “{f.calib_set.name}”"
    return "library"


# ---------- duplicate pre-check (before anything is uploaded) ----------
@app.post("/api/uploads/check")
async def upload_check(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """[{name, size}] -> for each: where a file with the same name and size already exists in this target (or null).
    The definitive content (SHA-256) check still runs when an upload finishes."""
    data = await request.json()
    project, calib = resolve_owner(db, data)
    owner_files = project.files if project else calib.files
    index = {}
    for f in owner_files:
        where = (f"session {f.session.date.strftime('%d-%m-%Y')}" if f.session and f.session.date
                 else ("not assigned to a session" if project else "this set"))
        index.setdefault((clean_filename(f.original_name), f.size), where)
    out = []
    for it in (data.get("files") or [])[:20000]:
        try:
            out.append(index.get((clean_filename(str(it.get("name", ""))), int(it.get("size", -1)))))
        except (TypeError, ValueError):
            out.append(None)
    return {"existing": out}


# ---------- chunked upload ----------
def up_paths(uid: str):
    if not re.fullmatch(r"[0-9a-f]{32}", uid):
        raise HTTPException(404, "Unknown upload")
    return UP_DIR / f"{uid}.part", UP_DIR / f"{uid}.json"


@app.post("/api/uploads")
async def upload_init(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = await request.json()
    project, calib = resolve_owner(db, data)
    ftype = data.get("frame_type")
    if ftype not in FRAME_TYPES:
        return err("Invalid frame type")
    name = clean_filename(data.get("filename") or "") if (data.get("filename") or "").strip() else ""
    if not name:
        return err("Missing filename")
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return err("Empty file or unknown size")
    for k in [k for k in _HASH if not (UP_DIR / f"{k}.json").exists()]:   # prune abandoned uploads
        _HASH.pop(k, None)
        _LOCKS.pop(k, None)
    sid = session_for(db, project, data.get("session_id"))
    if project and not sid:
        return err("Choose the session (night) the files belong to")
    try:
        free = shutil.disk_usage(DATA_DIR).free
    except OSError:
        free = None
    if free is not None and size > free - 512 * 1024 ** 2:      # keep 512 MB headroom
        return err(f"Not enough free disk space ({free // 1024 ** 2} MB free)", 507)
    UP_DIR.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex
    part, meta = up_paths(uid)
    part.touch()
    _HASH[uid] = hashlib.sha256()
    meta.write_text(json.dumps({
        "project_id": project.id if project else None, "calib_set_id": calib.id if calib else None,
        "filename": name, "size": size, "frame_type": ftype,
        "session_id": sid,
        "meta": {k: (data.get("meta") or {}).get(k) for k in META_KEYS},
        "skip_dups": bool(data.get("skip_dups", True)),
    }))
    return {"id": uid, "offset": 0}


@app.get("/api/uploads/{uid}")
def upload_status(uid: str, user: User = Depends(current_user)):
    part, meta = up_paths(uid)
    if not meta.exists():
        return err("Unknown upload", 404)
    return {"offset": part.stat().st_size if part.exists() else 0}


@app.put("/api/uploads/{uid}")
async def upload_chunk(request: Request, uid: str, offset: int, user: User = Depends(current_user)):
    import asyncio
    part, meta = up_paths(uid)
    if not meta.exists():
        return err("Unknown upload", 404)
    lock = _LOCKS.setdefault(uid, asyncio.Lock())
    async with lock:
        cur = part.stat().st_size
        if offset != cur:
            return err("Offset mismatch", 409, offset=cur)
        limit = json.loads(meta.read_text())["size"]
        h = _HASH.get(uid)
        written = 0
        with open(part, "ab") as fh:
            async for chunk in request.stream():
                if cur + written + len(chunk) > limit:
                    fh.truncate(cur)
                    _HASH.pop(uid, None)          # hash state is no longer valid -> recompute at finish
                    return err("More data than the declared file size", 400, offset=cur)
                fh.write(chunk)
                written += len(chunk)
                if h is not None:
                    h.update(chunk)
        os.utime(meta)                            # active upload -> not removed by the 48 h cleanup
        return {"offset": part.stat().st_size}


@app.delete("/api/uploads/{uid}")
def upload_cancel(uid: str, user: User = Depends(current_user)):
    part, meta = up_paths(uid)
    part.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    _HASH.pop(uid, None)
    _LOCKS.pop(uid, None)
    return {"ok": True}


@app.post("/api/uploads/{uid}/finish")
async def upload_finish(uid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    part, meta_p = up_paths(uid)
    if not meta_p.exists():
        return err("Unknown upload", 404)
    if uid in _FINISHING:                     # retried / parallel finish of the same upload
        return err("Upload is already being finished", 409)
    _FINISHING.add(uid)
    try:
        return await _finish(uid, part, meta_p, db)
    finally:
        _FINISHING.discard(uid)


async def _finish(uid, part, meta_p, db):
    m = json.loads(meta_p.read_text())
    size = part.stat().st_size
    if size != m["size"]:
        return err("Upload incomplete", 409, offset=size)
    h = _HASH.pop(uid, None)
    _LOCKS.pop(uid, None)
    sha = h.hexdigest() if h is not None else await run_in_threadpool(worker.sha256_file, part)
    return await run_in_threadpool(_store_upload, uid, part, meta_p, m, size, sha, db)


def _store_upload(uid, part, meta_p, m, size, sha, db):
    """Duplicate check + insert under the shared lock (no two copies of the same content slip through)."""
    with worker.DUP_LOCK:
        return _store_locked(part, meta_p, m, size, sha, db)


def _store_locked(part, meta_p, m, size, sha, db):
    dup = worker.find_duplicate(db, sha)
    if dup and m["skip_dups"]:
        part.unlink(missing_ok=True)
        meta_p.unlink(missing_ok=True)
        return {"duplicate": True, "existing": f"{dup.original_name} in {describe_owner(dup)}"}
    # target may have been deleted meanwhile (project / set / session)
    if (m["project_id"] and not db.get(Project, m["project_id"])) or \
            (m["calib_set_id"] and not db.get(CalibrationSet, m["calib_set_id"])):
        part.unlink(missing_ok=True)
        meta_p.unlink(missing_ok=True)
        return err("The project / calibration set was deleted during the upload", 410)
    if m.get("session_id") and not db.get(ImagingSession, m["session_id"]):
        part.unlink(missing_ok=True)
        meta_p.unlink(missing_ok=True)
        return err("The session was deleted during the upload", 410)
    dest = unique_dest(owner_dir(m["project_id"], m["calib_set_id"]) / m["frame_type"], m["filename"])
    try:
        os.replace(part, dest)
    except FileNotFoundError:                   # cancelled while finishing
        dest.unlink(missing_ok=True)
        meta_p.unlink(missing_ok=True)
        return err("The upload was cancelled", 410)
    except OSError:
        dest.unlink(missing_ok=True)            # remove the reserved placeholder
        raise
    meta_p.unlink(missing_ok=True)
    mm = m.get("meta") or {}
    ff = FrameFile(project_id=m["project_id"], calib_set_id=m["calib_set_id"], session_id=m["session_id"],
                   frame_type=m["frame_type"], original_name=m["filename"],
                   stored_path=str(dest.relative_to(DATA_DIR)), size=size, sha256=sha, status="pending",
                   description=none_if_empty(mm.get("description")), exposure_s=to_float(mm.get("exposure_s")),
                   gain=none_if_empty(mm.get("gain")), temperature=none_if_empty(mm.get("temperature")),
                   filter=none_if_empty(mm.get("filter")))
    db.add(ff)
    db.commit()
    worker.wake()
    return {"ok": True, "file_id": ff.id, "duplicate_of": (f"{dup.original_name} in {describe_owner(dup)}"
                                                           if dup else None)}


# ---------- server import ----------
def safe_import_path(rel: str) -> Path:
    p = (IMPORT_DIR / (rel or "").lstrip("/")).resolve()
    root = IMPORT_DIR.resolve()
    if p != root and root not in p.parents:
        raise HTTPException(400, "Path outside import folder")
    return p


@app.get("/import")
def import_page(request: Request, project: int | None = None, calib: int | None = None, session: int | None = None,
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_project(db, project) if project else None
    c = get_calib(db, calib) if calib else None
    if not p and not c:
        raise HTTPException(400, "Missing target")
    sel = next((s for s in p.sessions if s.id == session), None) if p else None
    return page(request, "import.html", user=user, p=p, c=c, sel_session=sel, root_ok=IMPORT_DIR.exists(),
                import_root=str(IMPORT_DIR), skip_dups=load_settings(db)["skip_duplicates"])


@app.get("/api/import/browse")
def import_browse(path: str = "", user: User = Depends(current_user)):
    d = safe_import_path(path)
    if not d.is_dir():
        return err("Not a folder", 404)
    dirs, nfiles = [], 0
    try:
        for e in sorted(os.scandir(d), key=lambda e: e.name.lower()):
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                dirs.append(e.name)
            elif Path(e.name).suffix.lower() in KNOWN_EXT:
                nfiles += 1
    except PermissionError:
        return err("Permission denied", 403)
    rel = "" if d == IMPORT_DIR.resolve() else str(d.relative_to(IMPORT_DIR.resolve()))
    return {"path": rel, "dirs": dirs, "files": nfiles}


def scan_folder(root: Path, recursive: bool, limit=20000):
    out = []
    walker = os.walk(root) if recursive else [(str(root), [], [e.name for e in os.scandir(root) if e.is_file()])]
    for dirpath, dirnames, filenames in walker:
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if fn.startswith(".") or p.suffix.lower() not in KNOWN_EXT:
                continue
            try:
                if root.resolve() not in p.resolve().parents or not p.is_file():
                    continue                    # dangling symlink or link pointing outside the import folder
            except OSError:
                continue
            rel = str(p.relative_to(root))
            header = None
            if p.suffix.lower() in FITS_EXT:
                try:
                    header = fits_header(p)
                except Exception:
                    header = None
            info = {"rel": rel, "size": p.stat().st_size,
                    "type": detect_frame_type(p, rel, header)}
            if header is not None:
                info["filter"] = str(header.get("FILTER", "") or "").strip()
                try:
                    info["exp"] = float(header.get("EXPTIME", header.get("EXPOSURE", "")) or 0) or None
                except (TypeError, ValueError):
                    info["exp"] = None
            out.append(info)
            if len(out) >= limit:
                return out
    return out


@app.post("/api/import/scan")
async def import_scan(request: Request, user: User = Depends(current_user)):
    data = await request.json()
    root = safe_import_path(data.get("path", ""))
    if not root.is_dir():
        return err("Not a folder", 404)
    files = await run_in_threadpool(scan_folder, root, bool(data.get("recursive", True)))
    return {"files": files}


@app.post("/api/import/start")
async def import_start(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = await request.json()
    project, calib = resolve_owner(db, data)
    root = safe_import_path(data.get("path", ""))
    items = data.get("files") or []
    if not items:
        return err("No files selected")
    sid = session_for(db, project, data.get("session_id"))
    if project and not sid:
        return err("Choose the session (night) the files belong to")
    mm = data.get("meta") or {}
    job = ImportJob(project_id=project.id if project else None, calib_set_id=calib.id if calib else None,
                    source_dir=str(root), skip_dups=bool(data.get("skip_dups", True)), total=0)
    db.add(job)
    db.flush()
    base = owner_dir(project.id if project else None, calib.id if calib else None)
    reserved = []
    try:
        for it in items:
            if not isinstance(it, dict):
                continue
            src = (root / str(it.get("rel", ""))).resolve()
            if root.resolve() not in src.parents:
                continue
            ftype = it.get("type") if it.get("type") in FRAME_TYPES else "light"
            if not src.is_file():
                continue
            dest = unique_dest(base / ftype, src.name)     # reserve the name; worker fills it
            reserved.append(dest)
            db.add(FrameFile(project_id=job.project_id, calib_set_id=job.calib_set_id, session_id=sid,
                             import_job_id=job.id, frame_type=ftype, original_name=src.name,
                             stored_path=str(dest.relative_to(DATA_DIR)), source_path=str(src),
                             size=src.stat().st_size, status="importing",
                             description=none_if_empty(mm.get("description")),
                             filter=none_if_empty(mm.get("filter")) or none_if_empty(it.get("filter"))))
            job.total += 1
        db.commit()
    except Exception:
        db.rollback()
        for d in reserved:                    # no rows -> no empty placeholder files left behind
            d.unlink(missing_ok=True)
        raise
    worker.wake()
    flash(request, f"Import started: {job.total} files are being copied in the background")
    return {"ok": True, "job": job.id, "total": job.total,
            "redirect": f"/projects/{project.id}" if project else f"/calibration/{calib.id}"}
