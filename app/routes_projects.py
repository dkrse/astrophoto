"""Projects, imaging sessions (nights), calibration links, streaming ZIP downloads."""
import json
import re
import shutil
from collections import defaultdict
from urllib.parse import quote

from fastapi import Request, Form, Depends, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .core import (clean_filename, download_name, app, page, flash, get_db, current_user, catalog, none_if_empty, parse_dmy, get_project,
                   get_calib, project_dir, DATA_DIR, load_settings, safe_name, fmt_dmy, fmt_dur,
                   exposure_summary, moon_illumination, night_of, optics)
from .models import (User, Project, FrameFile, ImagingSession, CalibrationSet, FRAME_TYPES, RAW_FRAME_TYPES,
                     FRAME_LABELS)
from .routes_main import project_cover
from . import worker

PROJECT_FIELDS = ["name", "target", "object_type", "tags", "description", "notes"]
EQUIP_KINDS = ("location", "telescope", "camera", "mount", "software")
SESSION_FIELDS = ["location", "telescope", "camera", "mount", "software", "seeing", "sqm", "temperature", "humidity", "notes"]


async def apply_form(request: Request, p: Project):
    form = await request.form()
    for f in PROJECT_FIELDS:
        v = none_if_empty(form.get(f))
        setattr(p, f, None if v == "__new__" else v)
    if not p.name:
        p.name = p.target or "Untitled"


def name_key(v) -> str:
    """Comparison key for duplicate detection: case/whitespace/punctuation-insensitive ('NGC  123' == 'ngc123')."""
    return re.sub(r"[\W_]+", "", (v or "").casefold())


def similar_projects(db, name, target=None, exclude_id=None) -> list:
    keys = {k for k in (name_key(name), name_key(target)) if k}
    if not keys:
        return []
    return [(pid, n) for pid, n, t in db.execute(select(Project.id, Project.name, Project.target)).all()
            if pid != exclude_id and (name_key(n) in keys or name_key(t) in keys)]


@app.get("/api/projects/similar")
def project_similar(name: str = "", target: str = "", exclude: int = 0, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    return [{"id": i, "name": n} for i, n in similar_projects(db, name, target, exclude)]


@app.get("/projects/new")
def project_new(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return page(request, "project_form.html", user=user, p=None, cat=catalog(db))


@app.post("/projects/new")
async def project_create(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = Project()
    await apply_form(request, p)
    dups = similar_projects(db, p.name, p.target)
    if dups and (await request.form()).get("confirm_dup") != "1":
        return page(request, "project_form.html", user=user, p=None, dups=dups, cat=catalog(db),
                    draft={f: getattr(p, f) for f in PROJECT_FIELDS})
    db.add(p)
    db.commit()
    flash(request, "Project created")
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


def session_rows(p: Project, cat=None):
    spec_of = (lambda kind, name: next((it.spec for it in cat[kind] if it.name == name), {}) if name else {}) \
        if cat else (lambda kind, name: {})
    from .measured import session_measured
    out = []
    for s in p.sessions:
        files = [f for f in p.files if f.session_id == s.id]
        opt = optics(spec_of("telescope", s.telescope), spec_of("camera", s.camera))
        out.append({"s": s, "files": files, "count": len(files),
                    "measured": session_measured([f for f in files if f.frame_type in ("light", "stack")],
                                                 spec_of("telescope", s.telescope).get("focal_mm"),
                                                 opt["scale"] if opt else None),
                    "lights": sum(1 for f in files if f.frame_type == "light"),
                    "exposure": exposure_summary(files)["total"], "expo": exposure_summary(files),
                    "optic": optics(spec_of("telescope", s.telescope), spec_of("camera", s.camera))})
    return out


def session_label(s: ImagingSession) -> str:
    """Unique, sortable folder name: <nr>_<date> (nr = position in the project, so equal dates never collide)."""
    nr = next((i for i, x in enumerate(s.project.sessions, 1) if x.id == s.id), 0)
    return f"{nr:02d}_{s.date.isoformat() if s.date else 'nodate'}"


def calib_library(db) -> list:
    """All calibration sets as plain dicts (one query + one count query); sorting per session happens in the
    browser when the dialog opens, so page size and DB work do not grow with sessions × sets."""
    counts = dict(db.execute(select(FrameFile.calib_set_id, func.count()).where(FrameFile.calib_set_id.is_not(None))
                             .group_by(FrameFile.calib_set_id)).all())
    return [{"id": c.id, "name": c.name, "type": c.frame_type, "type_label": FRAME_LABELS[c.frame_type],
             "date": c.capture_date.isoformat() if c.capture_date else None, "date_dmy": fmt_dmy(c.capture_date),
             "camera": c.camera or "", "temperature": c.temperature or "", "gain": c.gain or "",
             "exposure": c.exposure_s, "files": counts.get(c.id, 0)}
            for c in db.scalars(select(CalibrationSet).order_by(CalibrationSet.capture_date.desc().nulls_last()))]


@app.get("/projects/{pid}")
def project_detail(request: Request, pid: int, show: str = "", user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    p = get_project(db, pid)
    groups = {t: [f for f in p.files if f.frame_type == t] for t in FRAME_TYPES}
    sizes = {t: sum(f.size for f in fs) for t, fs in groups.items()}
    lights = groups["light"]
    med = {}
    for k in ("stat_fwhm", "stat_stars", "stat_bg"):
        vals = sorted(getattr(f, k) for f in lights if getattr(f, k) is not None)
        med[k] = vals[len(vals) // 2] if vals else None
    others = db.execute(select(Project.id, Project.name).where(Project.id != p.id)
                        .order_by(func.lower(Project.name))).all()
    cat = catalog(db)
    rows = session_rows(p, cat)
    unassigned = [f for f in p.files if not f.session_id]
    sess_groups = [{"key": f"s{r['s'].id}_", "r": r, "groups": {t: [f for f in r["files"] if f.frame_type == t]
                                                               for t in FRAME_TYPES}} for r in rows]
    if unassigned or not rows:
        sess_groups.append({"key": "u_", "r": None,
                            "groups": {t: [f for f in unassigned if f.frame_type == t] for t in FRAME_TYPES}})
    for g in sess_groups:
        g["sizes"] = {t: sum(f.size for f in fs) for t, fs in g["groups"].items()}
    dates = [s.date for s in p.sessions if s.date]
    last_s = p.sessions[-1] if p.sessions else None
    return page(request, "project_detail.html", user=user, p=p, groups=groups, sizes=sizes,
                sess_groups=sess_groups, unassigned=len(unassigned), last_s=last_s,
                date_range=(min(dates), max(dates)) if dates else None,
                cover=project_cover(p), thumbs=[f for f in p.files if f.thumb_path],
                expo=exposure_summary(p.files), sessions=rows, cat=cat,
                calib_lib=calib_library(db), all_calib=db.scalars(select(CalibrationSet)).all(), status=worker.queue_status(db, project_id=p.id),
                suspects=sum(1 for f in lights if f.suspect and not f.rejected),
                rejected=sum(1 for f in lights if f.rejected), med=med, others=others,
                settings=load_settings(db), show=show)


@app.get("/api/stats/optics")
def optics_trend(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Per session (with solved / analysed frames): measured FWHM, elongation, tilt, limiting magnitude, focal."""
    from .measured import session_measured
    cat = catalog(db)
    spec = lambda kind, name: next((it.spec for it in cat[kind] if it.name == name), {}) if name else {}
    files = db.scalars(select(FrameFile).where(FrameFile.session_id.is_not(None),
                                               FrameFile.frame_type.in_(("light", "stack")),
                                               (FrameFile.solve.is_not(None)) | (FrameFile.optics.is_not(None)))).all()
    by_s = defaultdict(list)
    for f in files:
        by_s[f.session_id].append(f)
    out = []
    for sid, fs in by_s.items():
        s = fs[0].session
        opt = optics(spec("telescope", s.telescope), spec("camera", s.camera))
        m = session_measured(fs, spec("telescope", s.telescope).get("focal_mm"), opt["scale"] if opt else None)
        if not m:
            continue
        out.append({"session_id": sid, "project_id": s.project_id, "project": s.project.name,
                    "date": s.date.isoformat() if s.date else None, "date_dmy": fmt_dmy(s.date),
                    "equipment": " · ".join(x for x in (s.telescope, s.camera) if x) or "(equipment not set)",
                    **m})
    out.sort(key=lambda r: (r["equipment"], r["date"] or ""))
    return out


@app.get("/api/projects/{pid}/status")
def project_status(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return worker.queue_status(db, project_id=pid)


@app.get("/api/calibration/{cid}/status")
def calib_status(cid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return worker.queue_status(db, calib_set_id=cid)


@app.get("/projects/{pid}/edit")
def project_edit(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return page(request, "project_form.html", user=user, p=get_project(db, pid), cat=catalog(db))


@app.post("/projects/{pid}/edit")
async def project_update(request: Request, pid: int, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    p = get_project(db, pid)
    before = (name_key(p.name), name_key(p.target))
    await apply_form(request, p)
    changed = before != (name_key(p.name), name_key(p.target))
    dups = similar_projects(db, p.name, p.target, p.id) if changed else []
    if dups and (await request.form()).get("confirm_dup") != "1":
        draft = {f: getattr(p, f) for f in PROJECT_FIELDS}
        db.rollback()
        return page(request, "project_form.html", user=user, p=get_project(db, pid), draft=draft, dups=dups,
                    cat=catalog(db))
    db.commit()
    flash(request, "Saved")
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/delete")
def project_delete(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(Project, pid)
    if not p:                                    # already deleted (double submit / resubmitted form)
        flash(request, "Project was already deleted")
        return RedirectResponse("/", status_code=303)
    if any(f.status == "importing" for f in p.files):
        flash(request, "An import into this project is still running – wait until it finishes", "err")
        return RedirectResponse(f"/projects/{pid}", status_code=303)
    fids = [f.id for f in p.files]
    db.delete(p)
    db.commit()
    shutil.rmtree(project_dir(pid), ignore_errors=True)
    for fid in fids:
        shutil.rmtree(DATA_DIR / ".cache" / str(fid), ignore_errors=True)
    flash(request, "Project deleted")
    return RedirectResponse("/", status_code=303)


@app.post("/projects/{pid}/cover")
def project_set_cover(request: Request, pid: int, file_id: str = Form(""),
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_project(db, pid)
    fid = int(file_id) if file_id.isdigit() else None
    if fid and not any(f.id == fid for f in p.files):
        raise HTTPException(400, "File does not belong to this project")
    p.cover_file_id = fid
    db.commit()
    flash(request, "Cover set" if fid else "Cover set to automatic")
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/reprocess")
def project_reprocess(request: Request, pid: int, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    """Queue all files for preview + metadata + statistics regeneration (background)."""
    p = get_project(db, pid)
    n = 0
    for f in p.files:
        if f.status in ("done", "error"):
            f.status = "pending"
            n += 1
    db.commit()
    worker.wake()
    flash(request, f"{n} files queued for processing")
    return RedirectResponse(f"/projects/{pid}", status_code=303)


# ---------- sessions (nights) ----------
@app.post("/projects/{pid}/sessions")
async def session_save(request: Request, pid: int, user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    p = get_project(db, pid)
    form = await request.form()
    sid = form.get("sid")
    s = db.get(ImagingSession, int(sid)) if sid and sid.isdigit() else None
    if s and s.project_id != p.id:
        raise HTTPException(400)
    if not s:
        s = ImagingSession(project_id=p.id)
        db.add(s)
    d = parse_dmy(form.get("date"))
    if not d:
        flash(request, "Please enter a valid night date (dd-mm-yyyy)", "err")
        return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)
    s.date = d
    for f in SESSION_FIELDS:
        v = none_if_empty(form.get(f))
        setattr(s, f, None if v == "__new__" else v)
    s.moon_illum = moon_illumination(s.date)
    db.flush()
    # attach unassigned frames taken that night
    if s.date:
        for f in p.files:
            if not f.session_id and f.date_obs and night_of(f.date_obs) == s.date:
                f.session_id = s.id
    db.commit()
    flash(request, "Session saved")
    return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)


@app.post("/sessions/{sid}/delete")
def session_delete(request: Request, sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Deletes the session together with all its files (DB rows, stored files, thumbnails, cache).
    Linked calibration library sets are only unlinked – library files are never deleted here."""
    from .routes_files import delete_files
    s = db.get(ImagingSession, sid)
    if not s:                                    # already deleted (double submit)
        flash(request, "Session was already deleted")
        return RedirectResponse(request.headers.get("referer") or "/", status_code=303)
    pid = s.project_id
    files = list(s.files)
    if any(f.status == "importing" for f in files):
        flash(request, "An import into this session is still running – wait until it finishes", "err")
        return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)
    delete_files(db, files)
    s.calib_sets = []
    db.delete(s)
    db.commit()
    flash(request, f"Session deleted including {len(files)} file{'s' if len(files) != 1 else ''}")
    return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)


@app.post("/projects/{pid}/sessions/auto")
def sessions_auto(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Create one session per night found in DATE-OBS / EXIF dates and assign files."""
    p = get_project(db, pid)
    existing = {s.date: s for s in p.sessions if s.date}
    tmpl = p.sessions[-1] if p.sessions else None        # new nights inherit equipment of the latest session
    nights = defaultdict(list)
    for f in p.files:
        if f.date_obs and not f.session_id:
            nights[night_of(f.date_obs)].append(f)
    created = assigned = 0
    for night, files in sorted(nights.items()):
        s = existing.get(night)
        if not s:
            s = ImagingSession(project_id=p.id, date=night, moon_illum=moon_illumination(night),
                               **{k: getattr(tmpl, k) if tmpl else None for k in EQUIP_KINDS})
            db.add(s)
            db.flush()
            existing[night] = s
            created += 1
        for f in files:
            f.session_id = s.id
            assigned += 1
    db.commit()
    if not nights:
        flash(request, "No unassigned files with an observation date (DATE-OBS / EXIF) found", "err")
    else:
        flash(request, f"Sessions created: {created}, files assigned: {assigned}")
    return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)


# ---------- calibration library links (per session) ----------
@app.post("/sessions/{sid}/calibration")
async def calib_link(request: Request, sid: int, user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    s = db.get(ImagingSession, sid)
    if not s:
        raise HTTPException(404, "Session not found")
    form = await request.form()
    wanted = {int(x) for x in form.getlist("set_id") if str(x).isdigit()}
    s.calib_sets = [c for c in db.scalars(select(CalibrationSet)) if c.id in wanted]
    db.commit()
    flash(request, f"Linked calibration sets: {len(s.calib_sets)}")
    return RedirectResponse(f"/projects/{s.project_id}#files", status_code=303)


# ---------- streaming ZIP ----------
def zip_response(name: str, entries: list, info_text: str | None, comp: str, extra: list | None = None):
    """entries: [(Path, arcname)]. Streams directly – nothing is written to disk."""
    from zipstream import ZipStream, ZIP_STORED, ZIP_DEFLATED
    sized = comp == "none"
    zs = ZipStream(compress_type=ZIP_STORED if sized else ZIP_DEFLATED,
                   compress_level=None if sized else (1 if comp == "fast" else 9), sized=sized)
    used = set()
    for path, arc in entries:
        if not path.exists():
            continue
        folder, _, fname = arc.rpartition("/")
        arc = (folder + "/" if folder else "") + clean_filename(fname)
        base, n = arc, 1
        while arc in used:
            stem, dot, ext = base.rpartition(".")
            arc = f"{stem}_{n}.{ext}" if dot else f"{base}_{n}"
            n += 1
        used.add(arc)
        zs.add_path(str(path), arc)
    if info_text:
        zs.add(info_text.encode(), "INFO.txt")
    for arc, data in extra or []:
        base, n = arc, 1
        while arc in used:
            stem, dot, ext = base.rpartition(".")
            arc = f"{stem}_{n}.{ext}" if dot else f"{base}_{n}"
            n += 1
        used.add(arc)
        zs.add(data, arc)
    name = download_name(name)
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    headers = {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"}
    if sized:
        headers["Content-Length"] = str(len(zs))
    return StreamingResponse(zs, media_type="application/zip", headers=headers)


def scope(p: Project, only=None):
    """(sessions, files) of the whole project, or of one session when `only` is given."""
    if only is None:
        return list(p.sessions), list(p.files)
    return [only], [f for f in p.files if f.session_id == only.id]


def project_info_text(p: Project, only=None) -> str:
    sessions, files = scope(p, only)
    ex = exposure_summary(files)
    lines = [f"Project: {p.name}", f"Object: {p.target or ''}", f"Object type: {p.object_type or ''}"]
    if only is not None:
        lines.append(f"Contents: session {fmt_dmy(only.date)} only [{session_label(only)}/]")
    lines.append(f"Integration: {fmt_dur(ex['total'])} ({ex['count']} lights)")
    for r in ex["rows"]:
        lines.append(f"  {r['filter']}: {r['count']} frames, {fmt_dur(r['seconds'])} ({r['detail']})")
    for s in sessions:
        lines.append(f"Session {fmt_dmy(s.date)} [{session_label(s)}/]: {s.location or ''} | "
                     f"{' · '.join(x for x in (s.telescope, s.camera, s.mount) if x)} | seeing {s.seeing or '-'} "
                     f"SQM {s.sqm or '-'} moon {int(s.moon_illum or 0)}%")
        for c in s.calib_sets:
            lines.append(f"  Calibration: {FRAME_LABELS[c.frame_type]} – {c.name} ({fmt_dmy(c.capture_date)})")
    lines += ["", p.description or "", "", p.notes or ""]
    return "\n".join(lines)


# ---------- metadata export ----------
def _iso(v):
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def parse_header_text(text: str | None) -> list:
    """Stored FITS header text -> [{key, value, comment}] (keeps order and repeated HISTORY/COMMENT cards)."""
    if not text:
        return []
    try:
        from astropy.io import fits
        h = fits.Header.fromstring(text, sep="\n")
        out = []
        for c in h.cards:
            if c.keyword in ("", "END") and not str(c.value).strip():
                continue
            v = c.value
            if not isinstance(v, (str, int, float, bool)) or v is None:
                v = str(v)
            out.append({"key": c.keyword, "value": v, "comment": c.comment or ""})
        return out
    except Exception:
        out = []
        for line in text.splitlines():
            k, _, rest = line.partition("=")
            v, _, c = rest.partition("/")
            out.append({"key": k.strip(), "value": v.strip().strip("'").strip(), "comment": c.strip()})
        return out


def file_exif(f: FrameFile) -> dict:
    from .preview import IMAGE_EXT, RAW_EXT
    src = DATA_DIR / f.stored_path
    if src.suffix.lower() not in (IMAGE_EXT | RAW_EXT) or not src.is_file():
        return {}
    try:
        import exifread
        with open(src, "rb") as fh:
            tags = exifread.process_file(fh, details=False)
        return {k: str(v) for k, v in sorted(tags.items())
                if "Thumbnail" not in k and not k.startswith("MakerNote") and len(str(v)) <= 500}
    except Exception:
        return {}


FILE_FIELDS = ["id", "frame_type", "original_name", "stored_path", "size", "sha256", "status", "exposure_s", "gain",
               "temperature", "filter", "date_obs", "description", "uploaded_at", "updated_at", "rejected", "suspect",
               "session_id", "calib_set_id"]


def file_record(f: FrameFile) -> dict:
    d = {k: _iso(getattr(f, k)) for k in FILE_FIELDS}
    d["stats"] = {"fwhm_px": f.stat_fwhm, "stars": f.stat_stars, "background": f.stat_bg, "noise": f.stat_noise}
    try:
        d["view_settings"] = json.loads(f.view_opts) if f.view_opts else None
    except ValueError:
        d["view_settings"] = None
    try:
        d["plate_solve"] = json.loads(f.solve) if f.solve else None
    except ValueError:
        d["plate_solve"] = None
    try:
        d["optics"] = json.loads(f.optics) if f.optics else None
    except ValueError:
        d["optics"] = None
    d["fits_header"] = parse_header_text(f.header)
    exif = file_exif(f)
    if exif:
        d["exif"] = exif
    return d


def project_metadata(p: Project, only=None) -> dict:
    from datetime import datetime as _dt
    sessions, files = scope(p, only)
    ex = exposure_summary(files)
    sets = {c.id: c for s in sessions for c in s.calib_sets}
    proj = {k: _iso(getattr(p, k)) for k in ["id", *PROJECT_FIELDS, "cover_file_id",
                                              "created_at", "updated_at"]}
    return {
        "format": "astrophoto-metadata", "version": 1, "exported_at": _dt.now().isoformat(timespec="seconds"),
        "project": proj,
        "scope": "project" if only is None else {"session_id": only.id, "folder": session_label(only)},
        "integration": {"total_seconds": ex["total"], "light_frames": ex["count"],
                        "by_filter": [{"filter": r["filter"], "frames": r["count"], "seconds": r["seconds"],
                                       "exposures": r["detail"]} for r in ex["rows"]]},
        "sessions": [{**{k: _iso(getattr(s, k)) for k in ["id", "date", *SESSION_FIELDS, "moon_illum",
                                                          "created_at", "updated_at"]},
                      "calibration_set_ids": [c.id for c in s.calib_sets]} for s in sessions],
        "calibration_sets": [{**{k: _iso(getattr(c, k)) for k in ["id", "name", "frame_type", "capture_date", "camera",
                                                                   "telescope", "temperature", "gain", "offset",
                                                                   "exposure_s", "filter", "binning", "notes",
                                                                   "created_at", "updated_at"]},
                              "files": [file_record(f) for f in c.files]} for c in sets.values()],
        "files": [file_record(f) for f in files],
    }


def metadata_json_bytes(p: Project, only=None) -> bytes:
    return json.dumps(project_metadata(p, only), ensure_ascii=False, indent=2, default=str).encode()


@app.get("/projects/{pid}/export")
def project_export(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """ZIP with metadata.json (everything incl. FITS headers / EXIF) + raw header text files."""
    p = get_project(db, pid)
    extra = [("metadata.json", metadata_json_bytes(p))]
    for f in p.files:                        # one folder per session, like the ZIP (names repeat between nights)
        if f.header:
            pre = session_label(f.session) if f.session else "unassigned"
            extra.append((f"headers/{pre}/{f.frame_type}/{clean_filename(f.original_name)}.txt", f.header.encode()))
    for c in linked_sets(p):
        for f in c.files:
            if f.header:
                extra.append((f"headers/library-{c.id}/{f.frame_type}/{clean_filename(f.original_name)}.txt",
                              f.header.encode()))
    return zip_response(f"{p.name}_metadata.zip", [], project_info_text(p), "best", extra)


@app.get("/export/projects")
def projects_export_all(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """ZIP of every project's full metadata: all_projects.json + per-project folders (metadata.json, raw headers)."""
    from datetime import datetime as _dt
    projects = db.scalars(select(Project).order_by(func.lower(Project.name))).all()
    extra, allp = [], []
    for p in projects:
        md = project_metadata(p)
        allp.append(md)
        folder = f"{p.id:04d}_{safe_name(p.name) or 'project'}"
        extra.append((f"projects/{folder}/metadata.json",
                      json.dumps(md, ensure_ascii=False, indent=2, default=str).encode()))
        extra.append((f"projects/{folder}/INFO.txt", project_info_text(p).encode()))
        for f in p.files:
            if f.header:
                pre = session_label(f.session) if f.session else "unassigned"
                extra.append((f"projects/{folder}/headers/{pre}/{f.frame_type}/{clean_filename(f.original_name)}.txt",
                              f.header.encode()))
    lib = db.scalars(select(CalibrationSet)).all()
    summary = {"format": "astrophoto-metadata-all", "version": 1,
               "exported_at": _dt.now().isoformat(timespec="seconds"), "project_count": len(projects),
               "projects": allp,
               "calibration_library": [{**{k: _iso(getattr(c, k)) for k in [
                   "id", "name", "frame_type", "capture_date", "camera", "telescope", "temperature", "gain", "offset",
                   "exposure_s", "filter", "binning", "notes", "created_at", "updated_at"]},
                   "files": [file_record(f) for f in c.files]} for c in lib]}
    extra.insert(0, ("all_projects.json", json.dumps(summary, ensure_ascii=False, indent=2, default=str).encode()))
    return zip_response(f"astrophoto_metadata_{_dt.now():%Y-%m-%d}.zip", [], None, "best", extra)


def linked_sets(p: Project) -> list:
    seen = {}
    for s in p.sessions:
        for c in s.calib_sets:
            seen.setdefault(c.id, c)
    return list(seen.values())


@app.get("/projects/{pid}/zip")
def project_zip(pid: int, types: str = "light,dark,bias,flat,darkflat", rejected: int = 0, session: str = "",
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    """ZIP selected frame types, one folder per session (<date>/<type>/…) incl. the session's linked
    calibration library sets – the layout multi-session stacking (WBPP, Siril) expects. Rejected lights excluded."""
    p = get_project(db, pid)
    wanted = [t for t in types.split(",") if t in FRAME_TYPES]
    sel = [s for s in p.sessions if str(s.id) == session] if session else list(p.sessions)
    if session and not sel:
        raise HTTPException(404, "Session not found")
    ok = lambda f: f.frame_type in wanted and f.status != "importing"
    entries = []
    for s in sel:
        pre = session_label(s) + "/"
        entries += [(DATA_DIR / f.stored_path, f"{pre}{f.frame_type}/{f.original_name}")
                    for f in s.files if ok(f) and (rejected or not f.rejected)]
        entries += [(DATA_DIR / f.stored_path, f"{pre}{f.frame_type}/{f.original_name}")
                    for c in s.calib_sets for f in c.files if ok(f)]
    if not session:
        pre = "unassigned/" if p.sessions else ""
        entries += [(DATA_DIR / f.stored_path, f"{pre}{f.frame_type}/{f.original_name}")
                    for f in p.files if not f.session_id and ok(f) and (rejected or not f.rejected)]
    if not entries:
        raise HTTPException(404, "No files to download")
    suffix = "-".join(wanted) if set(wanted) != set(RAW_FRAME_TYPES) else "frames"
    if session:
        suffix = f"{session_label(sel[0])}_{suffix}"
    only = sel[0] if session else None
    return zip_response(f"{p.name}_{suffix}.zip", entries, project_info_text(p, only),
                        load_settings(db)["zip_compression"], [("metadata.json", metadata_json_bytes(p, only))])


@app.get("/calibration/{cid}/zip")
def calib_zip(cid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = get_calib(db, cid)
    entries = [(DATA_DIR / f.stored_path, f"{f.frame_type}/{f.original_name}") for f in c.files]
    if not entries:
        raise HTTPException(404, "No files to download")
    return zip_response(f"{c.name}.zip", entries, None, load_settings(db)["zip_compression"])
