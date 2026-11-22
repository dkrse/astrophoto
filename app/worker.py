"""Background processing: server imports (copy + hash), previews, metadata, frame statistics.

State lives in the database (frame_files.status), so work survives restarts:
  importing -> pending -> done / error
"""
import hashlib
import os
import json
import threading
import time
import traceback
from pathlib import Path

from sqlalchemy import select, func

from .core import DATA_DIR, IMPORT_DIR, night_of, thumb_rel, load_settings
from .sessets import place_light
from .framestats import frame_stats, flag_suspects
from .models import SessionLocal, FrameFile, ImportJob, ImagingSession, AnalysisTask, Setting
from .preview import (read_exif, PREVIEW_EXT, FITS_EXT, RAW_EXT, IMAGE_EXT, make_preview, header_text, header_meta,
                      exif_meta, clean_opts)

CHUNK = 8 * 1024 * 1024
_wake = threading.Event()
_started = False
current = {"file": None, "since": None}


def wake():
    _wake.set()


def get_opts(ff: FrameFile) -> dict:
    try:
        o = json.loads(ff.view_opts or "{}")
        return clean_opts(o if isinstance(o, dict) else None)
    except (ValueError, TypeError):
        return clean_opts(None)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


# "check for duplicate + store hash" must be atomic across uploads (request threads) and imports (worker)
DUP_LOCK = threading.Lock()


def find_duplicate(db, sha: str, exclude_id=None):
    q = select(FrameFile).where(FrameFile.sha256 == sha, FrameFile.status != "importing")
    if exclude_id:
        q = q.where(FrameFile.id != exclude_id)
    return db.scalars(q.limit(1)).first()


def compute_preview(stored_path: str, opts: dict, thumbs: bool = True, tz: str | None = None):
    """Slow part (no DB access): thumbnail + metadata. Returns dict or None.
    thumbs=False (setting “Generate thumbnails” off): only the metadata is read."""
    src = DATA_DIR / stored_path
    ext = src.suffix.lower()
    if ext not in PREVIEW_EXT:
        return None
    thumb = DATA_DIR / thumb_rel(stored_path)
    if thumbs:
        ok, header, error = make_preview(src, thumb, opts)
    else:
        from .preview import fits_header
        ok, error, header = False, None, None
        if ext in FITS_EXT:
            try:
                header = fits_header(src)
            except Exception as e:
                error = str(e) or e.__class__.__name__
    meta = {}
    if header is not None:
        meta = header_meta(header, tz)
    elif ext in RAW_EXT or ext in IMAGE_EXT:
        meta = exif_meta(src)
    return {"stored_path": stored_path, "thumb": str(thumb.relative_to(DATA_DIR)) if ok else None, "keep_thumb": not thumbs,
            "header": header_text(header) if header is not None else None, "meta": meta,
            "error": f"Cannot read file: {error}" if error else None}


def recompute_capture_times(db, tz: str) -> int:
    """After the site time zone changed: FITS capture times again from the stored header (DATE-LOC / DATE-OBS).
    Session assignments are not changed. Returns the number of files whose time changed."""
    from .platesolve import _hdr
    from .preview import header_date
    n = 0
    for f in db.scalars(select(FrameFile).where(FrameFile.header.is_not(None))):
        h = _hdr(f.header)
        d = header_date(h.get, tz)
        if d and d != f.date_obs:
            old_night = night_of(f.date_obs) if f.date_obs else None
            f.date_obs = d
            n += 1
            # the frame now belongs to another night: follow it when it sat in its old night's session
            # and the project has a session for the new night (manual assignments are left alone)
            new_night = night_of(d)
            if f.project_id and new_night != old_night and f.session is not None and f.session.date == old_night:
                s = db.scalars(select(ImagingSession).where(ImagingSession.project_id == f.project_id,
                                                            ImagingSession.date == new_night)
                               .order_by(ImagingSession.id)).first()
                if s:
                    f.session_id = s.id
                    place_light(db, f, s)             # one set per session: that night's session with its set
    db.commit()
    return n


def apply_preview(ff: FrameFile, res):
    """Fast part: apply results to a *fresh* row; fills only fields that are still empty."""
    if not res or res["stored_path"] != ff.stored_path:     # file moved meanwhile -> keep row as is
        return
    if not res.get("keep_thumb"):                 # thumbnails switched off: keep an existing one
        if ff.thumb_path and ff.thumb_path != res["thumb"]:
            (DATA_DIR / ff.thumb_path).unlink(missing_ok=True)
        ff.thumb_path = res["thumb"]
    ff.error = res.get("error")                   # shown in the file list / info dialog (status stays "done")
    if res["header"] is not None:
        ff.header = res["header"]
    for k, v in res["meta"].items():
        if v is not None and getattr(ff, k) in (None, ""):
            setattr(ff, k, v)


def build_preview(ff: FrameFile):
    apply_preview(ff, compute_preview(ff.stored_path, get_opts(ff), tz=load_settings()["timezone"]))


def assign_session(db, ff: FrameFile):
    if ff.session_id or not ff.project_id or not ff.date_obs:
        return
    night = night_of(ff.date_obs)
    s = db.scalars(select(ImagingSession).where(ImagingSession.project_id == ff.project_id,
                                                ImagingSession.date == night)
                   .order_by(ImagingSession.id)).first()      # several sessions that night: always the first
    if s:
        ff.session_id = s.id


def place_frame(db, ff: FrameFile):
    """After the metadata is known: night's session (unassigned frames), then the session of its set."""
    assign_session(db, ff)
    if ff.session_id and ff.frame_type == "light":
        place_light(db, ff, processed_only=False)     # still "pending" here, but its metadata is known now


def reflag_project(db, project_id):
    """Recompute 'suspect' flags per filter group once all lights of a project are processed."""
    if not load_settings(db)["bg_suspects"]:
        return
    pending = db.scalar(select(func.count()).where(FrameFile.project_id == project_id,
                                                   FrameFile.frame_type == "light",
                                                   FrameFile.status.in_(["pending", "importing"])))
    if pending:
        return
    files = db.scalars(select(FrameFile).where(FrameFile.project_id == project_id)).all()
    for f in files:
        if f.frame_type != "light":
            f.suspect = False
    groups = {}                                   # compare within one night and filter (same optics/conditions)
    for f in files:
        if f.frame_type == "light":
            groups.setdefault((f.session_id, f.filter or ""), []).append(f)
    for g in groups.values():
        flag_suspects(g)


def _drop_orphan_thumb(db, res):
    """Thumbnail written for a row that was deleted meanwhile: remove it (and the empty folders that the
    write re-created after the project / set folder was removed)."""
    rel = (res or {}).get("thumb")
    if not rel:
        return
    try:
        db.rollback()
        if db.scalar(select(func.count()).where(FrameFile.thumb_path == rel)):
            return
    except Exception:
        pass
    p = DATA_DIR / rel
    p.unlink(missing_ok=True)
    for d in (p.parent, p.parent.parent):         # .thumbs, then the owner folder – only when empty
        try:
            d.rmdir()
        except OSError:
            break


def process_file(db, ff: FrameFile, with_stats=True):
    """Heavy work runs on a snapshot; results are applied to a freshly re-read row, so user edits
    made in the meantime (metadata, moves) are not overwritten."""
    path, ftype, opts, sha = ff.stored_path, ff.frame_type, get_opts(ff), ff.sha256
    src = DATA_DIR / path
    if not src.is_file():
        raise FileNotFoundError(path)
    cfg = load_settings(db)
    db.commit()                                   # end read transaction while we work
    if not sha:
        sha = sha256_file(src)                    # always (duplicates, integrity)
    res = compute_preview(path, opts, cfg["bg_previews"], cfg["timezone"])
    st = None
    if with_stats and cfg["bg_frame_stats"] and ftype == "light" and src.suffix.lower() in (FITS_EXT | RAW_EXT | IMAGE_EXT):
        try:
            st = frame_stats(src)
        except Exception as e:
            print(f"stats failed for {src}: {e}")
    try:
        db.refresh(ff)                            # raises if the row was deleted meanwhile
    except Exception:
        _drop_orphan_thumb(db, res)               # no row -> the new thumbnail must not stay behind
        raise
    ff.sha256 = ff.sha256 or sha
    if ff.stored_path == path:
        ff.size = src.stat().st_size if src.exists() else ff.size
        apply_preview(ff, res)
        if ff.exif is None and src.suffix.lower() in (RAW_EXT | IMAGE_EXT):
            ff.exif = json.dumps(read_exif(src), ensure_ascii=False)     # exports read it from the DB
        if st and ff.frame_type == "light":
            ff.stat_bg, ff.stat_noise, ff.stat_stars, ff.stat_fwhm = st["bg"], st["noise"], st["stars"], st["fwhm"]
    else:
        ff.status = "pending"                     # moved while processing -> redo with the new path
        return False
    place_frame(db, ff)
    return True


def do_import(db, ff: FrameFile):
    """Copy one file from the import folder, hashing on the fly; skip duplicates if asked."""
    job = db.get(ImportJob, ff.import_job_id) if ff.import_job_id else None
    dest = DATA_DIR / ff.stored_path
    h = hashlib.sha256()
    try:                                          # any failure marks the row – it must never block the queue
        src = Path(ff.source_path or "")
        # re-checked here, not only when the import started: a file / folder in the import share could have
        # been replaced by a symlink pointing anywhere in the container meanwhile
        root = IMPORT_DIR.resolve()
        if root not in src.resolve().parents:
            raise ValueError("source is no longer inside the import folder")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fi, open(dest, "wb") as fo:
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
    with DUP_LOCK:
        if job and job.skip_dups and find_duplicate(db, sha, ff.id):
            dest.unlink(missing_ok=True)
            db.delete(ff)
            job.skipped += 1
            db.commit()
            return
        ff.sha256, ff.size, ff.status, ff.source_path = sha, dest.stat().st_size, "pending", None
        if job:
            job.done += 1
        fid = ff.id
        try:
            db.commit()                           # visible to concurrent uploads before the lock is released
        except Exception as e:                    # row/project deleted while copying -> drop the copy
            db.rollback()
            dest.unlink(missing_ok=True)
            row = db.get(FrameFile, fid)          # still there -> other failure: mark it, never re-copy forever
            if row is not None and row.status == "importing":
                row.stored_path = f"__failed__/{fid}/{dest.name}"
                row.status, row.error = "error", f"Import failed: {str(e)[:300]}"
                try:
                    db.commit()
                except Exception:
                    db.rollback()


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
    try:                                          # in-memory state of uploads whose files are gone
        from .routes_upload import _HASH, _LOCKS
        alive = {p.name.split(".")[0] for p in up.iterdir()}
        for d in (_HASH, _LOCKS):
            for k in [k for k in d if k not in alive]:
                d.pop(k, None)
    except Exception:
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
        if not ff and db.scalar(select(ImportJob.id).where(ImportJob.status == "running").limit(1)):
            finish_jobs(db)                       # jobs left "running" (nothing accepted, a failed commit …)
            db.commit()
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
            return restat_step(db) or run_analysis(db)
        current.update(file=ff.original_name, since=time.time())
        fid = ff.id                               # read now: a deleted row cannot be read inside except
        ff.error = PROCESSING[1] if ff.error == INTERRUPTED else PROCESSING[0]   # committed by process_file
        try:
            if process_file(db, ff) is not False:
                ff.status = "done"                # error (unreadable preview) is set by apply_preview
            if ff.error in PROCESSING:            # finished (or moved): the marker must not stay
                ff.error = None
        except Exception as e:
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
        if ff.project_id and load_settings()["sessions_auto_fix"]:
            try:                                  # Settings: session names / nights follow their files
                pid = ff.project_id
                if not db.scalar(select(func.count()).where(FrameFile.project_id == pid,
                                                            FrameFile.status.in_(["pending", "importing"]))):
                    from .sessets import fix_sessions
                    from .models import Project
                    fix_sessions(db, db.get(Project, pid))
                    db.commit()
            except Exception:
                db.rollback()
                traceback.print_exc()
            ff = db.get(FrameFile, fid)
            if ff is None:
                return True
        if ff.project_id and ff.frame_type == "light":
            reflag_project(db, ff.project_id)
            db.commit()
            try:                                  # Settings: measure new nights automatically
                if not db.scalar(select(func.count()).where(FrameFile.project_id == ff.project_id,
                                                            FrameFile.status.in_(["pending", "importing"]))):
                    from .analysis import auto_measure_project
                    if auto_measure_project(db, ff.project_id):
                        db.commit()
            except Exception:
                db.rollback()
                traceback.print_exc()
        return True


RESTAT_KEY = "restat_fwhm_v2"     # star FWHM measured with windowed moments (box moments read too large)


def restat_step(db) -> bool:
    """Recompute the frame statistics of one light frame stored with the former FWHM method (cursor in Settings:
    'last id done/highest id of the old method', 'done' at the end; frames added later are measured the new way). Mixed old / new values in one project would make the old frames look
    blurred ('suspect'). Returns True if a frame was measured."""
    cur = db.get(Setting, RESTAT_KEY)
    if cur is None or cur.value == "done" or not load_settings(db)["bg_frame_stats"]:
        return False
    last, _, upto = (cur.value or "0").partition("/")      # "last done id/highest id measured the old way"
    last, upto = int(last or 0), int(upto or 0)
    f = db.scalars(select(FrameFile).where(FrameFile.id > last, FrameFile.id <= upto, FrameFile.frame_type == "light",
                                           FrameFile.status == "done", FrameFile.stat_fwhm.is_not(None))
                   .order_by(FrameFile.id).limit(1)).first()
    if f is None:                                 # all re-measured: suspect flags of every project with lights
        for pid in db.scalars(select(FrameFile.project_id).where(FrameFile.frame_type == "light",
                                                                 FrameFile.project_id.is_not(None)).distinct()):
            reflag_project(db, pid)
        cur.value = "done"
        db.commit()
        return True
    fid, src = f.id, DATA_DIR / f.stored_path
    current.update(file=f"Re-measuring stars: {f.original_name}", since=time.time())
    db.commit()                                   # no open transaction while measuring
    st = None
    try:
        if src.is_file():
            st = frame_stats(src)
    except Exception as e:
        print(f"stats failed for {src}: {e}")
    f, cur = db.get(FrameFile, fid), db.get(Setting, RESTAT_KEY)
    if f is not None and st:
        f.stat_bg, f.stat_noise, f.stat_stars, f.stat_fwhm = st["bg"], st["noise"], st["stars"], st["fwhm"]
    if cur is not None:
        cur.value = f"{fid}/{upto}"
    db.commit()
    return True


def run_analysis(db) -> bool:
    """One queued plate solve / optics analysis (lowest priority: after imports and previews)."""
    from datetime import datetime
    from . import analysis
    if not load_settings(db)["bg_analysis"]:      # switched off in Settings: the queue waits
        return False
    t = db.scalars(select(AnalysisTask).where(AnalysisTask.status == "queued").order_by(AnalysisTask.id)
                   .limit(1)).first()
    if not t:
        return False
    f = db.get(FrameFile, t.file_id)
    if f is None or f.status != "done":
        t.status, t.error, t.finished_at = "error", "File not available", datetime.now()
        db.commit()
        return True
    current.update(file=f"{'Plate solve' if t.kind == 'solve' else 'Optics'}: {f.original_name}", since=time.time())
    t.status = "running"
    tid, fid = t.id, f.id
    try:
        inp = analysis.solve_inputs(db, f) if t.kind == "solve" else analysis.optics_inputs(f, "auto")
    except Exception as e:                        # would otherwise stay queued and block the queue forever
        traceback.print_exc()
        db.rollback()
        t = db.get(AnalysisTask, tid)
        if t is not None:
            t.status, t.error, t.finished_at = "error", str(e)[:500], datetime.now()
            db.commit()
        return True
    db.commit()                                   # no open transaction while ASTAP / measuring runs
    try:
        res = analysis.solve_compute(inp) if t.kind == "solve" else analysis.optics_compute(inp)
        err = None if res.get("ok") else res.get("error") or "failed"
    except Exception as e:
        traceback.print_exc()
        res, err = None, str(e)[:500]
    t, f = db.get(AnalysisTask, tid), db.get(FrameFile, fid)
    if t is None:                                 # cancelled meanwhile
        return True
    if res is not None and res.get("busy"):       # an interactive solve held the solver: try again later
        t.status = "queued"
        db.commit()
        return False
    if f is not None and res is not None:
        (analysis.solve_store if t.kind == "solve" else analysis.optics_store)(f, res)
    t.status, t.error, t.finished_at = ("error" if err else "done"), err, datetime.now()
    db.commit()
    if f is not None and f.session is not None and load_settings(db)["cond_auto_fill"]:
        try:                                      # Settings: fill in the session's Seeing / SQM
            if analysis.fill_conditions(db, f.session):
                db.commit()
        except Exception:
            db.rollback()
            traceback.print_exc()
    return True


def cleanup_tasks(requeue: bool = True):
    """Finished analysis tasks are only kept for the progress display (1 day).
    requeue=True (startup only): tasks left 'running' by a restart are queued again."""
    from datetime import datetime, timedelta
    with SessionLocal() as db:
        db.query(AnalysisTask).filter(AnalysisTask.status.in_(["done", "error"]),
                                      AnalysisTask.finished_at < datetime.now() - timedelta(days=1)) \
            .delete(synchronize_session=False)
        if requeue:
            # a task that was already interrupted once and is interrupted again probably crashes the server
            # (out of memory, a broken file): do not run it on every restart for ever
            db.query(AnalysisTask).filter(AnalysisTask.status == "running", AnalysisTask.error == INTERRUPTED) \
                .update({"status": "error", "finished_at": datetime.now(),
                         "error": "Interrupted twice – the analysis may crash the server; not retried"},
                        synchronize_session=False)
            db.query(AnalysisTask).filter(AnalysisTask.status == "running") \
                .update({"status": "queued", "error": INTERRUPTED}, synchronize_session=False)
            recover_interrupted_files(db)
        db.commit()


INTERRUPTED = "__interrupted__"
PROCESSING = ("__processing__", "__processing2__")     # marker while a file's preview / stats are made


def recover_interrupted_files(db):
    """Startup: files whose processing was cut off (restart or crash). First time: simply again;
    second time in a row: error – such a file may crash the server, ↻ Reprocess retries it on demand."""
    db.query(FrameFile).filter(FrameFile.status == "pending", FrameFile.error == PROCESSING[1]) \
        .update({"status": "error", "error": "Processing was interrupted twice – the file may crash the "
                                             "server. Use ↻ Reprocess to try again."}, synchronize_session=False)
    db.query(FrameFile).filter(FrameFile.status == "pending", FrameFile.error == PROCESSING[0]) \
        .update({"error": INTERRUPTED}, synchronize_session=False)


def loop():
    last_cleanup = 0
    try:
        cleanup_tasks()                           # also re-queues tasks interrupted by a restart
    except Exception:
        traceback.print_exc()
    while True:
        try:
            busy = step()
        except Exception:
            traceback.print_exc()
            busy = False
            time.sleep(5)
        current.update(file=None, since=None)
        if time.time() - last_cleanup > 3600:
            last_cleanup = time.time()
            for job in (cleanup_uploads, cleanup_cache, lambda: cleanup_tasks(requeue=False)):
                try:
                    job()
                except Exception:                 # never let housekeeping kill the worker thread
                    traceback.print_exc()
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
    aq = select(AnalysisTask.status, func.count()).group_by(AnalysisTask.status)
    if project_id or calib_set_id:
        aq = aq.join(FrameFile, FrameFile.id == AnalysisTask.file_id)
        aq = aq.where(FrameFile.project_id == project_id) if project_id else aq.where(FrameFile.calib_set_id == calib_set_id)
    ac = dict(db.execute(aq).all())
    queued = ac.get("queued", 0) + ac.get("running", 0)
    analysis = {"queued": queued, "done": ac.get("done", 0), "error": ac.get("error", 0),
                "paused": not load_settings(db)["bg_analysis"]} if queued else None
    return {"importing": counts.get("importing", 0), "pending": counts.get("pending", 0),
            "error": counts.get("error", 0), "jobs": jobs, "current": current["file"], "analysis": analysis}
