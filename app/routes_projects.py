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
                   exposure_summary, moon_illumination, night_of)
from .models import (User, Project, FrameFile, ImagingSession, CalibrationSet, FRAME_TYPES, RAW_FRAME_TYPES,
                     FRAME_LABELS)
from .routes_main import project_cover
from . import worker

PROJECT_FIELDS = ["name", "target", "object_type", "location", "telescope", "camera", "mount",
                  "filters", "total_exposure", "software", "tags", "description", "notes"]
SESSION_FIELDS = ["location", "telescope", "camera", "mount", "seeing", "sqm", "temperature", "humidity", "notes"]


async def apply_form(request: Request, p: Project):
    form = await request.form()
    for f in PROJECT_FIELDS:
        v = none_if_empty(form.get(f))
        setattr(p, f, None if v == "__new__" else v)
    p.capture_date = parse_dmy(form.get("capture_date"))
    if not p.name:
        p.name = p.target or "Untitled"


@app.get("/projects/new")
def project_new(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return page(request, "project_form.html", user=user, p=None, cat=catalog(db))


@app.post("/projects/new")
async def project_create(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = Project()
    await apply_form(request, p)
    db.add(p)
    db.commit()
    flash(request, "Project created")
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


def session_rows(p: Project):
    out = []
    for s in p.sessions:
        files = [f for f in p.files if f.session_id == s.id]
        out.append({"s": s, "count": len(files), "lights": sum(1 for f in files if f.frame_type == "light"),
                    "exposure": exposure_summary(files)["total"]})
    return out


def calib_suggestions(db, p: Project):
    """All calibration sets, best matches first (same camera, closest date)."""
    sets = db.scalars(select(CalibrationSet)).all()
    linked = {c.id for c in p.calib_sets}
    ref = p.capture_date

    def score(c):
        s = 0
        if p.camera and c.camera == p.camera:
            s -= 1000
        if ref and c.capture_date:
            s += abs((c.capture_date - ref).days)
        else:
            s += 500
        return s
    return [{"c": c, "linked": c.id in linked, "camera_match": bool(p.camera and c.camera == p.camera),
             "days": abs((c.capture_date - ref).days) if ref and c.capture_date else None,
             "count": len(c.files)} for c in sorted(sets, key=score)]


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
    return page(request, "project_detail.html", user=user, p=p, groups=groups, sizes=sizes,
                cover=project_cover(p), thumbs=[f for f in p.files if f.thumb_path],
                expo=exposure_summary(p.files), sessions=session_rows(p), cat=catalog(db),
                calib=calib_suggestions(db, p), status=worker.queue_status(db, project_id=p.id),
                suspects=sum(1 for f in lights if f.suspect and not f.rejected),
                rejected=sum(1 for f in lights if f.rejected), med=med, others=others,
                settings=load_settings(db), show=show)


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
    await apply_form(request, p)
    db.commit()
    flash(request, "Saved")
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/delete")
def project_delete(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_project(db, pid)
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
    s.date = parse_dmy(form.get("date"))
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
    if s.date and (not p.capture_date or s.date < p.capture_date):
        p.capture_date = s.date
    db.commit()
    flash(request, "Session saved")
    return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)


@app.post("/sessions/{sid}/delete")
def session_delete(request: Request, sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = db.get(ImagingSession, sid)
    if not s:
        raise HTTPException(404)
    pid = s.project_id
    for f in s.files:
        f.session_id = None
    db.delete(s)
    db.commit()
    flash(request, "Session deleted (files kept)")
    return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)


@app.post("/projects/{pid}/sessions/auto")
def sessions_auto(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Create one session per night found in DATE-OBS / EXIF dates and assign files."""
    p = get_project(db, pid)
    existing = {s.date: s for s in p.sessions if s.date}
    nights = defaultdict(list)
    for f in p.files:
        if f.date_obs and not f.session_id:
            nights[night_of(f.date_obs)].append(f)
    created = assigned = 0
    for night, files in sorted(nights.items()):
        s = existing.get(night)
        if not s:
            s = ImagingSession(project_id=p.id, date=night, location=p.location, telescope=p.telescope,
                               camera=p.camera, mount=p.mount, moon_illum=moon_illumination(night))
            db.add(s)
            db.flush()
            existing[night] = s
            created += 1
        for f in files:
            f.session_id = s.id
            assigned += 1
    if existing and not p.capture_date:
        p.capture_date = min(existing)
    db.commit()
    if not nights:
        flash(request, "No unassigned files with an observation date (DATE-OBS / EXIF) found", "err")
    else:
        flash(request, f"Sessions created: {created}, files assigned: {assigned}")
    return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)


# ---------- calibration links ----------
@app.post("/projects/{pid}/calibration")
async def calib_link(request: Request, pid: int, user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    p = get_project(db, pid)
    form = await request.form()
    wanted = {int(x) for x in form.getlist("set_id") if str(x).isdigit()}
    p.calib_sets = [c for c in db.scalars(select(CalibrationSet)) if c.id in wanted]
    db.commit()
    flash(request, f"Linked calibration sets: {len(wanted)}")
    return RedirectResponse(f"/projects/{pid}#calibration", status_code=303)


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
        if arc in used:
            continue
        used.add(arc)
        zs.add(data, arc)
    name = download_name(name)
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    headers = {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"}
    if sized:
        headers["Content-Length"] = str(len(zs))
    return StreamingResponse(zs, media_type="application/zip", headers=headers)


def project_info_text(p: Project) -> str:
    ex = exposure_summary(p.files)
    lines = [f"Project: {p.name}", f"Object: {p.target or ''}", f"Date: {fmt_dmy(p.capture_date)}",
             f"Telescope: {p.telescope or ''}", f"Camera: {p.camera or ''}", f"Mount: {p.mount or ''}",
             f"Filters: {p.filters or ''}", f"Integration: {fmt_dur(ex['total'])} ({ex['count']} lights)"]
    for r in ex["rows"]:
        lines.append(f"  {r['filter']}: {r['count']} frames, {fmt_dur(r['seconds'])} ({r['detail']})")
    for s in p.sessions:
        lines.append(f"Session {fmt_dmy(s.date)}: {s.location or ''} seeing {s.seeing or '-'} "
                     f"SQM {s.sqm or '-'} moon {int(s.moon_illum or 0)}%")
    for c in p.calib_sets:
        lines.append(f"Calibration: {FRAME_LABELS[c.frame_type]} – {c.name} ({fmt_dmy(c.capture_date)})")
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
    d["fits_header"] = parse_header_text(f.header)
    exif = file_exif(f)
    if exif:
        d["exif"] = exif
    return d


def project_metadata(p: Project) -> dict:
    from datetime import datetime as _dt
    ex = exposure_summary(p.files)
    proj = {k: _iso(getattr(p, k)) for k in ["id", *PROJECT_FIELDS, "capture_date", "cover_file_id",
                                              "created_at", "updated_at"]}
    return {
        "format": "astrophoto-metadata", "version": 1, "exported_at": _dt.now().isoformat(timespec="seconds"),
        "project": proj,
        "integration": {"total_seconds": ex["total"], "light_frames": ex["count"],
                        "by_filter": [{"filter": r["filter"], "frames": r["count"], "seconds": r["seconds"],
                                       "exposures": r["detail"]} for r in ex["rows"]]},
        "sessions": [{k: _iso(getattr(s, k)) for k in ["id", "date", *SESSION_FIELDS, "moon_illum",
                                                       "created_at", "updated_at"]} for s in p.sessions],
        "calibration_sets": [{**{k: _iso(getattr(c, k)) for k in ["id", "name", "frame_type", "capture_date", "camera",
                                                                   "telescope", "temperature", "gain", "offset",
                                                                   "exposure_s", "filter", "binning", "notes",
                                                                   "created_at", "updated_at"]},
                              "files": [file_record(f) for f in c.files]} for c in p.calib_sets],
        "files": [file_record(f) for f in p.files],
    }


def metadata_json_bytes(p: Project) -> bytes:
    return json.dumps(project_metadata(p), ensure_ascii=False, indent=2, default=str).encode()


@app.get("/projects/{pid}/export")
def project_export(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """ZIP with metadata.json (everything incl. FITS headers / EXIF) + raw header text files."""
    p = get_project(db, pid)
    extra = [("metadata.json", metadata_json_bytes(p))]
    for f in list(p.files) + [f for c in p.calib_sets for f in c.files]:
        if f.header:
            extra.append((f"headers/{f.frame_type}/{clean_filename(f.original_name)}.txt", f.header.encode()))
    return zip_response(f"{p.name}_metadata.zip", [], project_info_text(p), "best", extra)


@app.get("/projects/{pid}/zip")
def project_zip(pid: int, types: str = "light,dark,bias,flat,darkflat", rejected: int = 0,
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    """ZIP selected frame types incl. linked calibration library sets; rejected lights excluded."""
    p = get_project(db, pid)
    wanted = [t for t in types.split(",") if t in FRAME_TYPES]
    files = [f for f in p.files if f.frame_type in wanted and (rejected or not f.rejected)
             and f.status != "importing"]
    entries = [(DATA_DIR / f.stored_path, f"{f.frame_type}/{f.original_name}") for f in files]
    for c in p.calib_sets:
        for f in c.files:
            if f.frame_type in wanted and f.status != "importing":
                entries.append((DATA_DIR / f.stored_path, f"{f.frame_type}/{f.original_name}"))
    if not entries:
        raise HTTPException(404, "No files to download")
    suffix = "-".join(wanted) if set(wanted) != set(RAW_FRAME_TYPES) else "frames"
    return zip_response(f"{p.name}_{suffix}.zip", entries, project_info_text(p),
                        load_settings(db)["zip_compression"], [("metadata.json", metadata_json_bytes(p))])


@app.get("/calibration/{cid}/zip")
def calib_zip(cid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = get_calib(db, cid)
    entries = [(DATA_DIR / f.stored_path, f"{f.frame_type}/{f.original_name}") for f in c.files]
    if not entries:
        raise HTTPException(404, "No files to download")
    return zip_response(f"{c.name}.zip", entries, None, load_settings(db)["zip_compression"])
