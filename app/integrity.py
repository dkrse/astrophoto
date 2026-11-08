"""Archive health: duplicate files (same content stored more than once) and an integrity check
(every file of the database on disk with the right size / hash, files on disk unknown to the database).

The check runs in its own thread (I/O bound, independent of the processing worker); its last report is stored
in the settings table so it survives restarts."""
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select, func

from .core import DATA_DIR, ARCHIVE_DIR
from .models import SessionLocal, FrameFile, Setting, Project, CalibrationSet

REPORT_KEY = "integrity_report"
MAX_LIST = 500
state = {"running": False, "mode": None, "done": 0, "total": 0, "started": None, "cancel": False}
_lock = threading.Lock()


# ---------- duplicates (warning only) ----------
def duplicate_summary(db) -> dict:
    """Files whose content (SHA-256) is stored more than once in the archive."""
    sub = (select(FrameFile.sha256).where(FrameFile.sha256.is_not(None), FrameFile.status == "done")
           .group_by(FrameFile.sha256).having(func.count() > 1)).subquery()
    rows = db.execute(select(FrameFile.sha256, FrameFile.size).where(FrameFile.sha256.in_(select(sub.c.sha256)),
                                                                      FrameFile.status == "done")).all()
    groups = defaultdict(list)
    for sha, size in rows:
        groups[sha].append(size or 0)
    extra = sum(len(v) - 1 for v in groups.values())
    wasted = sum(v[0] * (len(v) - 1) for v in groups.values())
    return {"groups": len(groups), "extra_files": extra, "wasted": wasted}


def project_duplicates(db, project_id: int) -> dict | None:
    """Files of one project that are stored more than once – and where the other copies are (names only)."""
    mine = db.execute(select(FrameFile.id, FrameFile.sha256).where(FrameFile.project_id == project_id,
                                                                   FrameFile.sha256.is_not(None),
                                                                   FrameFile.status == "done")).all()
    if not mine:
        return None
    shas = {s for _, s in mine}
    others = db.execute(select(FrameFile.id, FrameFile.sha256, FrameFile.project_id, FrameFile.calib_set_id)
                        .where(FrameFile.sha256.in_(shas), FrameFile.status == "done")).all()
    by_sha = defaultdict(list)
    for fid, sha, pid, cid in others:
        by_sha[sha].append((fid, pid, cid))
    dup_files, where = 0, defaultdict(int)
    for fid, sha in mine:
        copies = [c for c in by_sha[sha] if c[0] != fid]
        if not copies:
            continue
        dup_files += 1
        for _, pid, cid in copies:
            where[("p", pid) if pid else ("c", cid)] += 1
    if not dup_files:
        return None
    names = []
    for (kind, i), n in sorted(where.items(), key=lambda x: -x[1]):
        if kind == "p":
            nm = "this project" if i == project_id else (db.get(Project, i).name if db.get(Project, i) else f"#{i}")
        else:
            c = db.get(CalibrationSet, i)
            nm = f"calibration set {c.name}" if c else f"calibration set #{i}"
        names.append({"name": nm, "files": n})
    return {"files": dup_files, "where": names}


# ---------- integrity check ----------
def last_report(db) -> dict | None:
    s = db.get(Setting, REPORT_KEY)
    try:
        return json.loads(s.value) if s and s.value else None
    except ValueError:
        return None


def _save_report(rep: dict):
    with SessionLocal() as db:
        s = db.get(Setting, REPORT_KEY) or Setting(key=REPORT_KEY)
        s.value = json.dumps(rep, ensure_ascii=False)
        db.merge(s)
        db.commit()


def run_check(full: bool = False):
    """Blocking. quick: existence + size; full: also SHA-256 of every file."""
    from .worker import sha256_file
    started = time.time()
    with SessionLocal() as db:
        rows = db.execute(select(FrameFile.id, FrameFile.stored_path, FrameFile.thumb_path, FrameFile.size,
                                 FrameFile.sha256, FrameFile.status, FrameFile.original_name, FrameFile.project_id,
                                 FrameFile.calib_set_id)).all()
    state.update(total=len(rows), done=0)
    missing, size_bad, hash_bad, errors = [], [], [], []
    known, checked_bytes = set(), 0
    for fid, path, thumb, size, sha, status, name, pid, cid in rows:
        if state["cancel"]:
            break
        known.add(os.path.normpath(path))
        if thumb:
            known.add(os.path.normpath(thumb))
        state["done"] += 1
        if status == "importing":
            continue
        item = {"id": fid, "name": name, "path": path, "project_id": pid, "calib_set_id": cid}
        if status == "error":
            errors.append(item)
        p = DATA_DIR / path
        try:
            st = p.stat()
        except OSError:
            if status != "error":
                missing.append(item)
            continue
        if size and st.st_size != size:
            size_bad.append({**item, "expected": size, "actual": st.st_size})
            continue
        if full and sha:
            checked_bytes += st.st_size
            try:
                if sha256_file(p) != sha:
                    hash_bad.append(item)
            except OSError as e:
                missing.append({**item, "error": str(e)})
    orphans = []
    if not state["cancel"] and ARCHIVE_DIR.exists():
        for dirpath, dirnames, filenames in os.walk(ARCHIVE_DIR):
            for fn in filenames:
                rel = os.path.normpath(os.path.relpath(os.path.join(dirpath, fn), DATA_DIR))
                if rel not in known:
                    try:
                        sz = os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        sz = 0
                    orphans.append({"path": rel, "size": sz})
    # the check can take long: files imported / deleted / moved meanwhile are not problems – re-check the DB
    with SessionLocal() as db:
        if orphans:
            paths = [o["path"] for o in orphans]
            now = set()
            for i in range(0, len(paths), 1000):
                part = paths[i:i + 1000]
                now |= {os.path.normpath(p) for p in db.scalars(select(FrameFile.stored_path)
                                                                 .where(FrameFile.stored_path.in_(part)))}
                now |= {os.path.normpath(p) for p in db.scalars(select(FrameFile.thumb_path)
                                                                 .where(FrameFile.thumb_path.in_(part)))}
            orphans = [o for o in orphans if o["path"] not in now]
        if missing:
            still = {(i, p) for i, p in db.execute(select(FrameFile.id, FrameFile.stored_path)
                                                   .where(FrameFile.id.in_([m["id"] for m in missing])))}
            missing = [m for m in missing if (m["id"], m["path"]) in still and not (DATA_DIR / m["path"]).exists()]
    rep = {"mode": "full" if full else "quick", "finished_at": datetime.now().isoformat(timespec="seconds"),
           "seconds": round(time.time() - started, 1), "files": len(rows), "cancelled": bool(state["cancel"]),
           "hashed_bytes": checked_bytes,
           "counts": {"missing": len(missing), "size": len(size_bad), "hash": len(hash_bad), "orphans": len(orphans),
                      "errors": len(errors)},
           "missing": missing[:MAX_LIST], "size": size_bad[:MAX_LIST], "hash": hash_bad[:MAX_LIST],
           "orphans": orphans[:MAX_LIST], "orphans_size": sum(o["size"] for o in orphans),
           "errors": errors[:MAX_LIST]}
    # a cancelled check is never "ok" – it did not look at everything
    rep["ok"] = None if state["cancel"] else not any(rep["counts"][k] for k in ("missing", "size", "hash"))
    _save_report(rep)
    return rep


def start_check(full: bool) -> bool:
    from .core import load_settings
    if not load_settings()["bg_integrity"]:
        raise PermissionError("Archive integrity checks are switched off in Settings")
    with _lock:
        if state["running"]:
            return False
        state.update(running=True, mode="full" if full else "quick", done=0, total=0, started=time.time(),
                     cancel=False)

    def run():
        try:
            run_check(full)
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            state["running"] = False
    threading.Thread(target=run, name="astro-integrity", daemon=True).start()
    return True
