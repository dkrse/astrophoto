"""Background processing: server imports (copy + hash), previews, metadata, frame statistics.

State lives in the database (frame_files.status), so work survives restarts:
  importing -> pending -> done / error
"""
import hashlib
import json
import threading
import time
import traceback
from pathlib import Path

from sqlalchemy import select, func

from .core import DATA_DIR, night_of, thumb_rel
from .framestats import frame_stats, flag_suspects
from .models import SessionLocal, FrameFile, ImportJob, ImagingSession
from .preview import (PREVIEW_EXT, FITS_EXT, RAW_EXT, IMAGE_EXT, make_preview, header_text, header_meta,
                      exif_meta, clean_opts)

CHUNK = 8 * 1024 * 1024
_wake = threading.Event()
_started = False
current = {"file": None, "since": None}


def wake():
    _wake.set()


def get_opts(ff: FrameFile) -> dict:
    try:
        return clean_opts(json.loads(ff.view_opts or "{}"))
    except ValueError:
        return clean_opts(None)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def find_duplicate(db, sha: str, exclude_id=None):
    q = select(FrameFile).where(FrameFile.sha256 == sha, FrameFile.status != "importing")
    if exclude_id:
        q = q.where(FrameFile.id != exclude_id)
    return db.scalars(q.limit(1)).first()


def compute_preview(stored_path: str, opts: dict):
    """Slow part (no DB access): thumbnail + metadata. Returns dict or None."""
    src = DATA_DIR / stored_path
    ext = src.suffix.lower()
    if ext not in PREVIEW_EXT:
        return None
    thumb = DATA_DIR / thumb_rel(stored_path)
    ok, header = make_preview(src, thumb, opts)
    meta = {}
    if header is not None:
        meta = header_meta(header)
    elif ext in RAW_EXT or ext in IMAGE_EXT:
        meta = exif_meta(src)
    return {"stored_path": stored_path, "thumb": str(thumb.relative_to(DATA_DIR)) if ok else None,
            "header": header_text(header) if header is not None else None, "meta": meta}


def apply_preview(ff: FrameFile, res):
    """Fast part: apply results to a *fresh* row; fills only fields that are still empty."""
    if not res or res["stored_path"] != ff.stored_path:     # file moved meanwhile -> keep row as is
        return
    if ff.thumb_path and ff.thumb_path != res["thumb"]:
        (DATA_DIR / ff.thumb_path).unlink(missing_ok=True)
    ff.thumb_path = res["thumb"]
    if res["header"] is not None:
        ff.header = res["header"]
    for k, v in res["meta"].items():
        if v is not None and getattr(ff, k) in (None, ""):
            setattr(ff, k, v)


def build_preview(ff: FrameFile):
    apply_preview(ff, compute_preview(ff.stored_path, get_opts(ff)))


def assign_session(db, ff: FrameFile):
    if ff.session_id or not ff.project_id or not ff.date_obs:
        return
    night = night_of(ff.date_obs)
    s = db.scalars(select(ImagingSession).where(ImagingSession.project_id == ff.project_id,
                                                ImagingSession.date == night)).first()
    if s:
        ff.session_id = s.id


def reflag_project(db, project_id):
    """Recompute 'suspect' flags per filter group once all lights of a project are processed."""
    pending = db.scalar(select(func.count()).where(FrameFile.project_id == project_id,
                                                   FrameFile.frame_type == "light",
                                                   FrameFile.status.in_(["pending", "importing"])))
    if pending:
        return
    lights = db.scalars(select(FrameFile).where(FrameFile.project_id == project_id,
                                               FrameFile.frame_type == "light")).all()
    groups = {}
    for f in lights:
        groups.setdefault(f.filter or "", []).append(f)
    for g in groups.values():
        flag_suspects(g)


def process_file(db, ff: FrameFile, with_stats=True):
    """Heavy work runs on a snapshot; results are applied to a freshly re-read row, so user edits
    made in the meantime (metadata, moves) are not overwritten."""
    path, ftype, opts, sha = ff.stored_path, ff.frame_type, get_opts(ff), ff.sha256
    src = DATA_DIR / path
    if not src.is_file():
        raise FileNotFoundError(path)
    db.commit()                                   # end read transaction while we work
    if not sha:
        sha = sha256_file(src)
    res = compute_preview(path, opts)
    st = None
    if with_stats and ftype == "light" and src.suffix.lower() in (FITS_EXT | RAW_EXT | IMAGE_EXT):
        try:
            st = frame_stats(src)
        except Exception as e:
            print(f"stats failed for {src}: {e}")
    db.refresh(ff)                                # raises if the row was deleted meanwhile
    ff.sha256 = ff.sha256 or sha
    if ff.stored_path == path:
        ff.size = src.stat().st_size if src.exists() else ff.size
        apply_preview(ff, res)
        if st and ff.frame_type == "light":
            ff.stat_bg, ff.stat_noise, ff.stat_stars, ff.stat_fwhm = st["bg"], st["noise"], st["stars"], st["fwhm"]
    else:
        ff.status = "pending"                     # moved while processing -> redo with the new path
        return False
    assign_session(db, ff)
    return True


def do_import(db, ff: FrameFile):
    """Copy one file from the import folder, hashing on the fly; skip duplicates if asked."""
    job = db.get(ImportJob, ff.import_job_id) if ff.import_job_id else None
    src = Path(ff.source_path)
    dest = DATA_DIR / ff.stored_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(dest, "wb") as fo:
            while chunk := fi.read(CHUNK):
                h.update(chunk)
                fo.write(chunk)
    except Exception as e:
        dest.unlink(missing_ok=True)
        # the reserved name is released -> this row must not point to it any more
        ff.stored_path = f"__failed__/{ff.id}/{dest.name}"
        ff.status, ff.error = "error", f"Import failed: {e}"
        if job:
            job.failed += 1
        return
    sha = h.hexdigest()
    if job and job.skip_dups and find_duplicate(db, sha, ff.id):
        dest.unlink(missing_ok=True)
        db.delete(ff)
        job.skipped += 1
        return
    ff.sha256, ff.size, ff.status, ff.source_path = sha, dest.stat().st_size, "pending", None
    if job:
        job.done += 1


def finish_jobs(db):
    for job in db.scalars(select(ImportJob).where(ImportJob.status == "running")):
        left = db.scalar(select(func.count()).where(FrameFile.import_job_id == job.id,
                                                    FrameFile.status == "importing"))
        if not left:
            job.status = "done"


def cleanup_uploads(max_age_h=48):
    up = DATA_DIR / ".uploads"
    if not up.exists():
        return
    limit = time.time() - max_age_h * 3600
    for p in up.iterdir():
        try:
            if p.stat().st_mtime < limit:
                p.unlink()
        except OSError:
            pass


CACHE_MAX_BYTES = 5 * 1024 ** 3
CACHE_MAX_AGE_DAYS = 30


def cleanup_cache():
    """Render cache (.cache) is disposable: drop entries older than 30 days, then oldest above 5 GB."""
    root = DATA_DIR / ".cache"
    if not root.exists():
        return
    files = []
    limit = time.time() - CACHE_MAX_AGE_DAYS * 86400
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
            st = p.stat()
            if st.st_atime < limit and st.st_mtime < limit:
                p.unlink()
            else:
                files.append((max(st.st_atime, st.st_mtime), st.st_size, p))
        except OSError:
            pass
    total = sum(s for _, s, _ in files)
    for _, size, p in sorted(files):
        if total <= CACHE_MAX_BYTES:
            break
        try:
            p.unlink()
            total -= size
        except OSError:
            pass
    for d in root.iterdir():          # empty per-file folders
        try:
            d.rmdir()
        except OSError:
            pass


def step() -> bool:
    """Process one unit of work. Returns True if something was done."""
    with SessionLocal() as db:
        ff = db.scalars(select(FrameFile).where(FrameFile.status == "importing")
                        .order_by(FrameFile.id).limit(1)).first()
        if ff:
            current.update(file=ff.original_name, since=time.time())
            dest = DATA_DIR / ff.stored_path
            do_import(db, ff)
            try:
                db.commit()
            except Exception:                     # row/project deleted while copying -> drop the copy
                db.rollback()
                dest.unlink(missing_ok=True)
                return True
            finish_jobs(db)
            db.commit()
            return True
        ff = db.scalars(select(FrameFile).where(FrameFile.status == "pending")
                        .order_by(FrameFile.id).limit(1)).first()
        if not ff:
            return False
        current.update(file=ff.original_name, since=time.time())
        try:
            if process_file(db, ff) is not False:
                ff.status, ff.error = "done", None
        except Exception as e:
            fid = ff.id
            db.rollback()
            ff = db.get(FrameFile, fid)
            if ff is None:            # deleted by the user while being processed
                return True
            ff.status, ff.error = "error", str(e)[:500]
            traceback.print_exc()
        try:
            db.commit()
        except Exception:             # row deleted meanwhile
            db.rollback()
            return True
        if ff.project_id and ff.frame_type == "light":
            reflag_project(db, ff.project_id)
            db.commit()
        return True


def loop():
    last_cleanup = 0
    while True:
        try:
            busy = step()
        except Exception:
            traceback.print_exc()
            busy = False
            time.sleep(5)
        current.update(file=None, since=None)
        if time.time() - last_cleanup > 3600:
            cleanup_uploads()
            cleanup_cache()
            last_cleanup = time.time()
        if not busy:
            _wake.wait(5)
            _wake.clear()


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=loop, name="astro-worker", daemon=True).start()


def queue_status(db, project_id=None, calib_set_id=None) -> dict:
    q = select(FrameFile.status, func.count()).group_by(FrameFile.status)
    if project_id:
        q = q.where(FrameFile.project_id == project_id)
    if calib_set_id:
        q = q.where(FrameFile.calib_set_id == calib_set_id)
    counts = dict(db.execute(q).all())
    jobs = []
    jq = select(ImportJob).where(ImportJob.status == "running")
    if project_id:
        jq = jq.where(ImportJob.project_id == project_id)
    if calib_set_id:
        jq = jq.where(ImportJob.calib_set_id == calib_set_id)
    for j in db.scalars(jq):
        jobs.append({"id": j.id, "total": j.total, "done": j.done, "skipped": j.skipped, "failed": j.failed})
    return {"importing": counts.get("importing", 0), "pending": counts.get("pending", 0),
            "error": counts.get("error", 0), "jobs": jobs, "current": current["file"]}
