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
from .sessets import (SET_FIELDS, place_light, other_lights, waiting_lights, norm_exposure, norm_gain, norm_filter,
                      parse_temp, set_label)

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
    keys = {k for k in (name_key(name), name_key(target)) if k and k != "untitled"}   # placeholder name
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


def session_values(cat, s, files, ref=None):
    """Measured values of one session (solves + optics + conditions), transparency relative to `ref`."""
    from .measured import session_measured, add_transparency
    spec_of = (lambda kind, name: next((it.spec for it in cat[kind] if it.name == name), {}) if name else {}) \
        if cat else (lambda kind, name: {})
    tel = spec_of("telescope", s.telescope)
    opt = optics(tel, spec_of("camera", s.camera))
    m = session_measured([f for f in files if f.frame_type in ("light", "stack")], tel.get("focal_mm"),
                         opt["scale"] if opt else None, tel.get("aperture_mm"))
    if ref is not None:
        add_transparency(m, s.telescope, s.camera, ref)
    return m


def session_rows(p: Project, cat=None, ref=None):
    spec_of = (lambda kind, name: next((it.spec for it in cat[kind] if it.name == name), {}) if name else {}) \
        if cat else (lambda kind, name: {})
    out = []
    by_sid = {}                                   # group once (was: every session scanned all project files)
    for f in p.files:
        by_sid.setdefault(f.session_id, []).append(f)
    for s in p.sessions:
        files = by_sid.get(s.id, [])
        out.append({"s": s, "files": files, "count": len(files),
                    "measured": session_values(cat, s, files, ref),
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


SESSION_COLS = ("night", "set", "equipment", "scale", "moon", "measured", "files")


@app.get("/projects/{pid}")
def project_detail(request: Request, pid: int, show: str = "", user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    p = get_project(db, pid)
    cfg = load_settings(db)
    groups = {t: [] for t in FRAME_TYPES}
    for f in p.files:                             # one pass instead of one per type
        groups.setdefault(f.frame_type, []).append(f)
    sizes = {t: sum(f.size for f in fs) for t, fs in groups.items()}
    lights = groups["light"]
    med = {}
    for k in ("stat_fwhm", "stat_stars", "stat_bg"):
        vals = sorted(getattr(f, k) for f in lights if getattr(f, k) is not None)
        med[k] = vals[len(vals) // 2] if vals else None
    others = db.execute(select(Project.id, Project.name).where(Project.id != p.id)
                        .order_by(func.lower(Project.name))).all()
    cat = catalog(db)
    from .measured import zp_reference
    rows = session_rows(p, cat, zp_reference(db))
    from .core import sort_param, sort_items, nat_key
    scol, sdir = sort_param(request, "so", SESSION_COLS)
    if scol:                                      # sort all sessions, then page (table + file blocks follow)
        m_of = lambda r: (r["measured"] or {})
        rows = sort_items(rows, {
            "night": lambda r: r["s"].date,
            "set": lambda r: (r["s"].exposure_s, nat_key(set_label(r["s"]))) if set_label(r["s"]) else None,
            "equipment": lambda r: nat_key(" · ".join(x for x in (r["s"].telescope, r["s"].camera, r["s"].mount) if x)) or None,
            "scale": lambda r: r["optic"]["scale"] if r["optic"] else None,
            "moon": lambda r: r["s"].moon_illum,
            "measured": lambda r: m_of(r).get("seeing") or m_of(r).get("fwhm_arcsec"),
            "files": lambda r: r["count"],
        }[scol], sdir)
    unassigned = [f for f in p.files if not f.session_id]
    sess_groups = [{"key": f"s{r['s'].id}_", "r": r, "groups": {t: [f for f in r["files"] if f.frame_type == t]
                                                               for t in FRAME_TYPES}} for r in rows]
    # pagination of the sessions (table + their file blocks); a link to a session on another page finds it
    from .core import paginate
    spg = paginate(len(sess_groups), cfg["sessions_page_size"], request.query_params.get("sp"))
    per = cfg["sessions_page_size"] or 0
    session_page = {g["r"]["s"].id: (i // per + 1 if per else 1) for i, g in enumerate(sess_groups)}
    sess_groups = sess_groups[spg["start"]:spg["end"]]
    if unassigned or not rows:
        sess_groups.append({"key": "u_", "r": None,
                            "groups": {t: [f for f in unassigned if f.frame_type == t] for t in FRAME_TYPES}})
    for g in sess_groups:
        g["sizes"] = {t: sum(f.size for f in fs) for t, fs in g["groups"].items()}
    dates = [s.date for s in p.sessions if s.date]
    last_s = p.sessions[-1] if p.sessions else None
    from .integrity import project_duplicates
    return page(request, "project_detail.html", user=user, p=p, groups=groups, sizes=sizes,
                dups=project_duplicates(db, p.id),
                sess_groups=sess_groups, unassigned=len(unassigned), last_s=last_s, spg=spg,
                page_rows=rows[spg["start"]:spg["end"]], session_page=session_page,
                date_range=(min(dates), max(dates)) if dates else None,
                cover=project_cover(p), thumbs=[f for f in p.files if f.thumb_path],
                expo=exposure_summary(p.files), sessions=rows, cat=cat,
                calib_lib=calib_library(db), all_calib=db.scalars(select(CalibrationSet)).all(), status=worker.queue_status(db, project_id=p.id),
                suspects=sum(1 for f in lights if f.suspect and not f.rejected),
                rejected=sum(1 for f in lights if f.rejected), med=med, others=others,
                settings=cfg, show=show, scol=scol, sdir=sdir)


@app.get("/api/stats/optics")
def optics_trend(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Per session (with solved / analysed frames): measured FWHM, elongation, tilt, limiting magnitude, focal."""
    from .measured import zp_reference
    cat = catalog(db)
    ref = zp_reference(db)
    from sqlalchemy.orm import selectinload
    files = db.scalars(select(FrameFile).options(selectinload(FrameFile.session).selectinload(ImagingSession.project))
                       .where(FrameFile.session_id.is_not(None),
                                               FrameFile.frame_type.in_(("light", "stack")),
                                               (FrameFile.solve.is_not(None)) | (FrameFile.optics.is_not(None)))).all()
    by_s = defaultdict(list)
    for f in files:
        by_s[f.session_id].append(f)
    out = []
    for sid, fs in by_s.items():
        s = fs[0].session
        m = session_values(cat, s, fs, ref)
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
    d = parse_dmy(form.get("date"))
    if not d:                                     # validate before a new row exists (no autoflush of a half session)
        flash(request, "Please enter a valid night date (dd-mm-yyyy)", "err")
        return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)
    if not s:
        s = ImagingSession(project_id=p.id)
        db.add(s)
    moved_out = []
    if s.id and s.date and d != s.date:
        # date changed: frames whose observation date belongs to the old night leave this session
        other = next((x for x in p.sessions if x.id != s.id and x.date == s.date), None)
        for f in s.files:
            if f.date_obs and night_of(f.date_obs) == s.date:
                f.session_id = other.id if other else None     # to another session of that night, else unassigned
                moved_out.append(f)
    s.date = d
    for f in SESSION_FIELDS:
        v = none_if_empty(form.get(f))
        setattr(s, f, None if v == "__new__" else v)
    s.moon_illum = moon_illumination(s.date)
    # the acquisition set can be chosen while the session holds no light frames (afterwards it is fixed –
    # it is what the lights in it have; a different set goes to its own session)
    set_note = ""
    new_set = {"exposure_s": norm_exposure(form.get("exposure_s")), "gain": norm_gain(form.get("gain")),
               "filter": norm_filter(form.get("filter")), "cam_temp": parse_temp(form.get("cam_temp"))}
    if new_set["cam_temp"] is not None:
        new_set["cam_temp"] = float(round(new_set["cam_temp"]))
    if "exposure_s" in form:
        if not s.id or not (other_lights(db, s) or waiting_lights(db, s)):   # same rule as the dialog
            for k, v in new_set.items():
                setattr(s, k, v)
        elif any(new_set[k] != getattr(s, k) for k in SET_FIELDS):
            set_note = " – exposure / gain / filter / camera temperature unchanged: the session already holds lights"
    db.flush()
    # frames that left for another night's session keep one set per session there too
    for f in moved_out:
        if f.session_id:
            place_light(db, f)
    # attach unassigned frames taken that night (each to the session of its set)
    if s.date:
        for f in p.files:
            if not f.session_id and f.date_obs and night_of(f.date_obs) == s.date:
                f.session_id = s.id
                place_light(db, f, s)
    db.commit()
    flash(request, "Session saved" + set_note)
    return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)


@app.post("/sessions/{sid}/delete")
def session_delete(request: Request, sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Deletes the session together with all its files (DB rows, stored files, thumbnails, cache).
    Linked calibration library sets are only unlinked – library files are never deleted here."""
    from .routes_files import delete_files
    s = db.get(ImagingSession, sid)
    if not s:                                    # already deleted (double submit)
        flash(request, "Session was already deleted")
        return RedirectResponse("/", status_code=303)
    pid = s.project_id
    files = list(s.files)
    if any(f.status == "importing" for f in files):
        flash(request, "An import into this session is still running – wait until it finishes", "err")
        return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)
    delete_files(db, files)
    s.calib_sets = []
    db.delete(s)
    db.commit()
    worker.reflag_project(db, pid)             # suspect flags were computed against the deleted frames
    db.commit()
    flash(request, f"Session deleted including {len(files)} file{'s' if len(files) != 1 else ''}")
    return RedirectResponse(f"/projects/{pid}#sessions", status_code=303)


@app.post("/projects/{pid}/sessions/auto")
def sessions_auto(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Create one session per night found in DATE-OBS / EXIF dates and assign files."""
    p = get_project(db, pid)
    existing = {}
    for s in sorted(p.sessions, key=lambda s: s.id):         # several sessions on one night: the first one
        if s.date:
            existing.setdefault(s.date, s)
    tmpl = p.sessions[-1] if p.sessions else None        # new nights inherit equipment of the latest session
    nights = defaultdict(list)
    for f in p.files:
        if f.date_obs and not f.session_id:
            nights[night_of(f.date_obs)].append(f)
    count_s = lambda: db.scalar(select(func.count(ImagingSession.id)).where(ImagingSession.project_id == p.id)) or 0
    before = count_s()
    assigned = 0
    for night, files in sorted(nights.items()):
        s = existing.get(night)
        if not s:
            s = ImagingSession(project_id=p.id, date=night, moon_illum=moon_illumination(night),
                               **{k: getattr(tmpl, k) if tmpl else None for k in EQUIP_KINDS})
            db.add(s)
            db.flush()
            existing[night] = s
        for f in files:
            f.session_id = s.id
        db.flush()
        for f in files:
            if f.frame_type == "light":           # one set per session: others go to (new) sibling sessions
                place_light(db, f, s)
            assigned += 1
    created = count_s() - before
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
def content_disposition(name: str) -> str:
    """attachment header safe for any file name (ASCII fallback + RFC 5987 UTF-8 name; Latin-1 only bytes)."""
    name = download_name(name)
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


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
    headers = {"Content-Disposition": content_disposition(name)}
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
    """EXIF tags – stored with the file after the first read (exports no longer open every RAW / JPEG)."""
    from .preview import read_exif
    if f.exif is not None:
        try:
            return json.loads(f.exif)
        except ValueError:
            pass
    src = DATA_DIR / f.stored_path
    if not src.is_file() or f.status == "importing":
        return {}
    d = read_exif(src)
    f.exif = json.dumps(d, ensure_ascii=False)     # saved with the request's commit
    return d


FILE_FIELDS = ["id", "frame_type", "original_name", "stored_path", "size", "sha256", "status", "exposure_s", "gain",
               "temperature", "filter", "date_obs", "description", "note", "uploaded_at", "updated_at", "rejected", "suspect",
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
                                                          *SET_FIELDS, "created_at", "updated_at"]},
                      "calibration_set_ids": [c.id for c in s.calib_sets]} for s in sessions],
        "calibration_sets": [{**{k: _iso(getattr(c, k)) for k in CALIB_FIELDS},
                              "files": [file_record(f) for f in c.files]} for c in sets.values()],
        "files": [file_record(f) for f in files],
    }


def preload_headers(db, project_ids=None, calib_ids=None):
    """Load the deferred FITS headers of many files in one query (instead of one query per file)."""
    from sqlalchemy.orm import undefer
    from sqlalchemy import or_
    conds = []
    if project_ids:
        conds.append(FrameFile.project_id.in_(list(project_ids)))
    if calib_ids:
        conds.append(FrameFile.calib_set_id.in_(list(calib_ids)))
    if not conds:
        return []
    # the caller must keep the returned list: the session's identity map holds rows only weakly, an unreferenced
    # result is dropped at once and every file would load its header / EXIF again one query at a time
    return db.scalars(select(FrameFile).where(or_(*conds))
                      .options(undefer(FrameFile.header), undefer(FrameFile.exif))).all()


def metadata_json_bytes(p: Project, only=None) -> bytes:
    return json.dumps(project_metadata(p, only), ensure_ascii=False, indent=2, default=str).encode()


@app.get("/projects/{pid}/export")
def project_export(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """ZIP with metadata.json (everything incl. FITS headers / EXIF) + raw header text files."""
    p = get_project(db, pid)
    _keep = preload_headers(db, [p.id], [c.id for c in linked_sets(p)])
    extra = [("metadata.json", metadata_json_bytes(p))]
    db.commit()                                  # EXIF read for the first time is kept
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


CALIB_FIELDS = ["id", "name", "frame_type", "capture_date", "camera", "telescope", "temperature", "gain", "offset",
                "exposure_s", "filter", "binning", "notes", "created_at", "updated_at"]


def _dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode()


@app.get("/export/projects")
def projects_export_all(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """ZIP of every project's full metadata: all_projects.json + per-project folders (metadata.json, raw headers).
    Streamed: every entry is generated while the ZIP is being sent, project by project with its own database
    session – memory stays at about one project, the download starts immediately."""
    from datetime import datetime as _dt
    from .models import SessionLocal
    projects = db.execute(select(Project.id, Project.name).order_by(func.lower(Project.name))).all()
    # header files: names known up front from a light query (no header text loaded here)
    labels = {}
    for pid, rows in _group(db.execute(select(ImagingSession.project_id, ImagingSession.id)
                                       .order_by(ImagingSession.project_id, ImagingSession.date.asc().nulls_last(),
                                                 ImagingSession.id)).all()):
        for nr, (_, sid) in enumerate(rows, 1):
            labels[sid] = nr
    dates = dict(db.execute(select(ImagingSession.id, ImagingSession.date)).all())
    hdr_rows = db.execute(select(FrameFile.id, FrameFile.project_id, FrameFile.session_id, FrameFile.frame_type,
                                 FrameFile.original_name)
                          .where(FrameFile.project_id.is_not(None), FrameFile.header.is_not(None))
                          .order_by(FrameFile.project_id, FrameFile.id)).all()
    by_project = dict(_group(hdr_rows, key=lambda r: r[1]))

    def one_project(pid):
        with SessionLocal() as s:
            p = s.get(Project, pid)
            if p is None:
                return None, None
            _keep = preload_headers(s, [p.id], [c.id for c in linked_sets(p)])
            md = project_metadata(p)
            s.commit()                               # EXIF read for the first time is kept
            return md, None

    def md_gen(pid):
        md, _ = one_project(pid)
        yield _dumps(md) if md is not None else b"null"

    def info_gen(pid):                               # no headers / EXIF needed for the text summary
        with SessionLocal() as s:
            p = s.get(Project, pid)
            yield project_info_text(p).encode() if p else b""

    def header_gen(fid):
        with SessionLocal() as s:
            yield (s.scalar(select(FrameFile.header).where(FrameFile.id == fid)) or "").encode()

    def all_gen():
        yield (f'{{"format": "astrophoto-metadata-all", "version": 1, '
               f'"exported_at": "{_dt.now().isoformat(timespec="seconds")}", '
               f'"project_count": {len(projects)},\n"projects": [\n').encode()
        for i, (pid, _) in enumerate(projects):
            md, _ = one_project(pid)
            yield (b",\n" if i else b"") + (_dumps(md) if md is not None else b"null")
        yield b'\n],\n"calibration_library": [\n'
        with SessionLocal() as s:
            for i, cid in enumerate(s.scalars(select(CalibrationSet.id).order_by(CalibrationSet.id)).all()):
                c = s.get(CalibrationSet, cid)            # ids first: expunge_all() below detaches loaded rows
                _keep = preload_headers(s, None, [c.id])
                rec = {**{k: _iso(getattr(c, k)) for k in CALIB_FIELDS}, "files": [file_record(f) for f in c.files]}
                yield (b",\n" if i else b"") + _dumps(rec)
                s.commit()
                s.expunge_all()
        yield b"\n]}\n"

    extra = [("all_projects.json", all_gen())]
    for pid, name in projects:
        folder = f"{pid:04d}_{safe_name(name) or 'project'}"
        extra.append((f"projects/{folder}/metadata.json", md_gen(pid)))
        extra.append((f"projects/{folder}/INFO.txt", info_gen(pid)))
        for fid, _, sid, ftype, oname in by_project.get(pid, []):
            pre = f"{labels.get(sid, 0):02d}_{dates[sid].isoformat() if dates.get(sid) else 'nodate'}" \
                if sid else "unassigned"
            extra.append((f"projects/{folder}/headers/{pre}/{ftype}/{clean_filename(oname)}.txt", header_gen(fid)))
    return zip_response(f"astrophoto_metadata_{_dt.now():%Y-%m-%d}.zip", [], None, "best", extra)


def _group(rows, key=lambda r: r[0]):
    out = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return out.items()


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
    _keep = preload_headers(db, [p.id], {c.id for s in sel for c in s.calib_sets})
    meta = metadata_json_bytes(p, only)
    db.commit()                                  # EXIF read for the first time is kept
    return zip_response(f"{p.name}_{suffix}.zip", entries, project_info_text(p, only),
                        load_settings(db)["zip_compression"], [("metadata.json", meta)])


@app.get("/calibration/{cid}/zip")
def calib_zip(cid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = get_calib(db, cid)
    entries = [(DATA_DIR / f.stored_path, f"{f.frame_type}/{f.original_name}") for f in c.files
               if f.status != "importing"]            # half-copied files would corrupt the ZIP
    if not entries:
        raise HTTPException(404, "No files to download")
    return zip_response(f"{c.name}.zip", entries, None, load_settings(db)["zip_compression"])
