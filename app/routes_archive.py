"""Import of a whole archive folder tree into many projects and sessions at once.

Scan: every file gets a proposed project (FITS OBJECT, else the top folder under the scanned folder), a night
(DATE-OBS / EXIF date -> night_of, else a date in the path) and a frame type. Files are grouped by
(project, night); the user reviews / renames the groups, then the import creates missing projects and sessions
and copies the files in the background like the normal server import."""
import os
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from fastapi import Request, Depends
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from .core import (app, page, get_db, current_user, DATA_DIR, IMPORT_DIR, owner_dir, unique_dest, load_settings,
                   flash, catalog, night_of, moon_illumination, parse_dmy, fmt_dmy)
from .models import User, Project, FrameFile, ImagingSession, ImportJob, FRAME_TYPES
from .preview import KNOWN_EXT, FITS_EXT, RAW_EXT, IMAGE_EXT, detect_frame_type, fits_header, header_meta, exif_meta
from .routes_projects import name_key, EQUIP_KINDS
from .routes_upload import safe_import_path, err
from . import worker

SCAN_LIMIT = 50000
DATE_IN_PATH = re.compile(r"(?<!\d)(20\d\d|19\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")
SESSION_FOLDER_WORDS = re.compile(r"^(lights?|darks?|flats?|bias(es)?|dark[\s_-]*flats?|flat[\s_-]*darks?|"
                                  r"calibration|calib|raw|frames?|stacks?|final|results?|processed|\d+)$", re.I)


def clean_object(v) -> str:
    v = re.sub(r"\s+", " ", str(v or "").strip().strip("'\"")).strip()
    return "" if v.lower() in ("", "none", "unknown", "object", "light", "dark", "flat", "bias") else v


def path_project(rel: str, root_name: str) -> str:
    """Project from the folder structure: first folder below the scanned folder that is not a date or a
    'Light' / 'Darks' … folder; files directly in the scanned folder use the scanned folder's name."""
    for part in Path(rel).parts[:-1]:
        if DATE_IN_PATH.search(part) and len(re.sub(r"[\d_.\-\s]", "", part)) < 3:
            continue
        if SESSION_FOLDER_WORDS.match(part.strip()):
            continue
        return part
    return root_name


TIME_AFTER = re.compile(r"^[T_\-\s]?(\d\d)[-_:.]?(\d\d)")


def path_night(rel: str):
    """Night from a date in the folders (last one), otherwise from the file name – a time after a date in the
    file name (e.g. Light_2024-03-16_01-30-00) is treated like DATE-OBS (after midnight -> previous night)."""
    parts = Path(rel).parts
    for part in reversed(parts[:-1]):
        for m in reversed(list(DATE_IN_PATH.finditer(part))):
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
    for m in reversed(list(DATE_IN_PATH.finditer(parts[-1]))):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        t = TIME_AFTER.match(parts[-1][m.end():])
        if t and int(t.group(1)) < 24:
            return night_of(datetime(d.year, d.month, d.day, int(t.group(1))))
        return d
    return None


def scan_archive(root: Path) -> list:
    out = []
    root_name = root.name or "Import"
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            ext = p.suffix.lower()
            if fn.startswith(".") or ext not in KNOWN_EXT:
                continue
            try:
                if root.resolve() not in p.resolve().parents or not p.is_file():
                    continue                    # dangling symlink or link pointing outside the import folder
                size = p.stat().st_size
            except OSError:
                continue
            rel = str(p.relative_to(root))
            header, meta = None, {}
            if ext in FITS_EXT:
                try:
                    header = fits_header(p)
                    meta = header_meta(header)
                except Exception:
                    header = None
            elif ext in RAW_EXT or ext in IMAGE_EXT:
                meta = exif_meta(p)
            ftype = detect_frame_type(p, rel, header)
            obj = clean_object(header.get("OBJECT")) if header is not None else ""
            dobs = meta.get("date_obs")
            night = night_of(dobs) if isinstance(dobs, datetime) else path_night(rel)
            out.append({"rel": rel, "size": size, "type": ftype, "filter": str(meta.get("filter") or ""),
                        "object": obj, "project": obj or path_project(rel, root_name),
                        "night": night.isoformat() if night else None,
                        "telescope": str(header.get("TELESCOP", "")).strip() if header is not None else "",
                        "camera": str(header.get("INSTRUME", "")).strip() if header is not None else ""})
            if len(out) >= SCAN_LIMIT:
                return out
    return out


def _guess_equipment(cat_items, raw: str):
    """Catalog name matching a TELESCOP / INSTRUME header value (one contains the other, ignoring case)."""
    k = name_key(raw)
    if len(k) < 3:
        return None
    for it in cat_items:
        n = name_key(it.name)
        if n and (n in k or k in n):
            return it.name
    return None


def group_files(db, files: list) -> list:
    projects = db.execute(select(Project.id, Project.name, Project.target)).all()
    by_key = {}
    for pid, name, target in projects:
        for v in (name, target):
            if name_key(v):
                by_key.setdefault(name_key(v), (pid, name))
    cat = catalog(db)
    groups = defaultdict(list)
    for f in files:
        groups[(name_key(f["project"]) or f["project"], f["night"])].append(f)
    out = []
    for (key, night), fs in groups.items():
        names = defaultdict(int)
        for f in fs:
            names[f["project"]] += 1
        name = max(names, key=names.get)
        match = by_key.get(key)
        sess = None
        if match and night:
            s = db.scalars(select(ImagingSession).where(ImagingSession.project_id == match[0],
                                                        ImagingSession.date == date.fromisoformat(night))).first()
            sess = s.id if s else None
        counts = defaultdict(int)
        for f in fs:
            counts[f["type"]] += 1
        tel = next((t for t in (_guess_equipment(cat["telescope"], f["telescope"]) for f in fs) if t), None)
        cam = next((c for c in (_guess_equipment(cat["camera"], f["camera"]) for f in fs) if c), None)
        out.append({"project": name, "match": {"id": match[0], "name": match[1]} if match else None,
                    "night": night, "night_dmy": fmt_dmy(date.fromisoformat(night)) if night else "",
                    "session_exists": bool(sess), "files": fs, "counts": dict(counts),
                    "size": sum(f["size"] for f in fs), "telescope": tel, "camera": cam})
    out.sort(key=lambda g: (g["project"].lower(), g["night"] or ""))
    return out


@app.get("/import/archive")
def archive_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return page(request, "import_archive.html", user=user, root_ok=IMPORT_DIR.exists(), import_root=str(IMPORT_DIR),
                skip_dups=load_settings(db)["skip_duplicates"], cat=catalog(db),
                projects=db.execute(select(Project.id, Project.name).order_by(Project.name)).all())


@app.post("/api/import/archive/scan")
async def archive_scan(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = await request.json()
    root = safe_import_path(data.get("path", ""))
    if not root.is_dir():
        return err("Not a folder", 404)
    files = await run_in_threadpool(scan_archive, root)
    groups = await run_in_threadpool(group_files, db, files)
    return {"groups": groups, "files": len(files), "limit": SCAN_LIMIT,
            "truncated": len(files) >= SCAN_LIMIT}


@app.post("/api/import/archive/start")
async def archive_start(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = await request.json()
    root = safe_import_path(data.get("path", ""))
    groups = [g for g in data.get("groups") or [] if isinstance(g, dict) and g.get("files")]
    if not groups:
        return err("Nothing selected")
    for g in groups:
        if not isinstance(g["files"], list) or not all(isinstance(it, dict) for it in g["files"]):
            return err("Invalid file list")
        if not str(g.get("project") or "").strip() and not str(g.get("project_id") or "").isdigit():
            return err("Every group needs a project name")
        if not parse_dmy(g.get("night") or ""):
            return err(f"Missing night date for “{g.get('project')}” – enter it (dd-mm-yyyy)")
    if not _enough_space(root, groups):
        return err("Not enough free disk space for this import", 507)
    res = await run_in_threadpool(_archive_start, db, root, groups, bool(data.get("skip_dups", True)))
    if isinstance(res, str):
        return err(res)
    worker.wake()
    flash(request, f"Archive import started: {res['files']} files into {res['projects']} project(s) – "
                   f"{res['new_projects']} new project(s), {res['new_sessions']} new session(s). "
                   f"Files are copied in the background.")
    return {"ok": True, **res}


def _enough_space(root: Path, groups) -> bool:
    import shutil
    total = 0
    for g in groups:
        for it in g["files"]:
            try:
                total += (root / str(it.get("rel", ""))).stat().st_size
            except OSError:
                pass
    try:
        return total < shutil.disk_usage(DATA_DIR).free - 512 * 1024 ** 2
    except OSError:
        return True


def _archive_start(db, root: Path, groups: list, skip: bool):
    """Blocking part (thread pool): projects, sessions and reserved files; placeholders are removed on failure."""
    projects_by_key = {}
    for i, n, t in db.execute(select(Project.id, Project.name, Project.target)):
        for v in (n, t):                          # same matching as the scan (name or object)
            if name_key(v):
                projects_by_key.setdefault(name_key(v), i)
    jobs, created_p, created_s, total, reserved = {}, 0, 0, 0, []
    try:
        for g in groups:
            p = db.get(Project, int(g["project_id"])) if str(g.get("project_id") or "").isdigit() else None
            if p is None:                         # no / deleted match -> by name
                name = re.sub(r"\s+", " ", str(g.get("project") or "").strip())[:200]
                if not name:
                    db.rollback()
                    return "A matched project was deleted meanwhile – enter a project name and scan again"
                pid = projects_by_key.get(name_key(name))
                p = db.get(Project, pid) if pid else None
                if p is None:
                    p = Project(name=name, target=name)
                    db.add(p)
                    db.flush()
                    projects_by_key[name_key(name)] = p.id
                    created_p += 1
            night = parse_dmy(g["night"])
            s = db.scalars(select(ImagingSession).where(ImagingSession.project_id == p.id,
                                                        ImagingSession.date == night)).first()
            new = s is None
            if new:
                tmpl = db.scalars(select(ImagingSession).where(ImagingSession.project_id == p.id)
                                  .order_by(ImagingSession.date.desc().nulls_last())).first()
                s = ImagingSession(project_id=p.id, date=night, moon_illum=moon_illumination(night),
                                   **{k: getattr(tmpl, k) if tmpl else None for k in EQUIP_KINDS})
                db.add(s)
                created_s += 1
            for k in ("telescope", "camera"):     # never overwrite equipment of an existing session
                if g.get(k) and (new or not getattr(s, k)):
                    setattr(s, k, str(g[k])[:200])
            db.flush()
            job = jobs.get(p.id)
            if job is None:
                job = jobs[p.id] = ImportJob(project_id=p.id, source_dir=str(root), skip_dups=skip, total=0)
                db.add(job)
                db.flush()
            base = owner_dir(p.id, None)
            for it in g["files"]:
                src = (root / str(it.get("rel", ""))).resolve()
                if root.resolve() not in src.parents or not src.is_file():
                    continue
                ftype = it.get("type") if it.get("type") in FRAME_TYPES else "light"
                dest = unique_dest(base / ftype, src.name)
                reserved.append(dest)
                db.add(FrameFile(project_id=p.id, session_id=s.id, import_job_id=job.id, frame_type=ftype,
                                 original_name=src.name, stored_path=str(dest.relative_to(DATA_DIR)),
                                 source_path=str(src), size=src.stat().st_size, status="importing",
                                 filter=(str(it.get("filter") or "").strip() or None)))
                job.total += 1
                total += 1
        db.commit()
    except Exception:
        db.rollback()
        for d in reserved:                        # no rows -> no empty placeholder files left behind
            d.unlink(missing_ok=True)
        raise
    return {"files": total, "projects": len(jobs), "new_projects": created_p, "new_sessions": created_s}


# ---------- archive health: integrity check ----------
@app.post("/api/integrity/start")
async def integrity_start(request: Request, user: User = Depends(current_user)):
    from . import integrity
    data = await request.json()
    try:
        if not integrity.start_check(bool(data.get("full"))):
            return err("A check is already running", 409)
    except PermissionError as e:
        return err(str(e), 403)
    return {"ok": True}


@app.post("/api/integrity/cancel")
def integrity_cancel(user: User = Depends(current_user)):
    from . import integrity
    integrity.state["cancel"] = True
    return {"ok": True}


@app.get("/api/integrity/status")
def integrity_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from . import integrity
    st = {k: integrity.state[k] for k in ("running", "mode", "done", "total")}
    return {**st, "report": None if st["running"] else integrity.last_report(db)}


# ---------- global activity indicator (header) ----------
@app.get("/api/activity")
def activity(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Everything the server is doing in the background right now, as short lines for the header indicator."""
    from . import integrity
    st = worker.queue_status(db)
    items = []
    for j in st["jobs"]:
        items.append(f"Importing {j['done'] + j['skipped'] + j['failed']} / {j['total']} files")
    if st["pending"]:
        items.append(f"Processing {st['pending']} file(s) – previews, metadata, statistics")
    a = st.get("analysis")
    if a:
        items.append(f"Analysis paused in Settings – {a['queued']} waiting" if a.get("paused") else
                     f"Plate solve / optics {a['done'] + a['error']} / {a['done'] + a['error'] + a['queued']}")
    if integrity.state["running"]:
        items.append(f"{'Full' if integrity.state['mode'] == 'full' else 'Quick'} integrity check "
                     f"{integrity.state['done']} / {integrity.state['total']}")
    paused_only = bool(a and a.get("paused")) and len(items) == 1
    return {"busy": bool(items) and not paused_only, "paused": paused_only, "items": items, "current": st["current"]}
