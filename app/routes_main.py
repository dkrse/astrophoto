"""Auth, settings, managed lists, overview, gallery, statistics."""
import secrets
import shutil
from collections import defaultdict
from urllib.parse import urlencode

from fastapi import Request, Form, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, PlainTextResponse, JSONResponse
from sqlalchemy import select, or_, func, update
from sqlalchemy.orm import Session, selectinload

from .core import (templates, app, page, flash, get_db, current_user, check_pw, hash_pw, load_settings, coerce_setting,
                   SETTINGS, SORT_LABELS, catalog, none_if_empty, parse_dmy, DATA_DIR, exposure_summary,
                   night_of, parse_specs, spec_summary, like_pattern, NAME_MAX)
from .models import (User, Project, FrameFile, CatalogItem, Setting, ImagingSession,
                     CATALOG_KINDS, CATALOG_SINGULAR, FRAME_TYPES)


# ---------- auth ----------
@app.get("/login")
def login_page(request: Request):
    return page(request, "login.html")


_FAILS: dict = {}          # ip -> [count, first_failure_ts]
MAX_FAILS, LOCK_S = 5, 600


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    import time
    ip = request.client.host if request.client else "?"
    cnt, first = _FAILS.get(ip, (0, time.time()))
    if time.time() - first > LOCK_S:
        cnt, first = 0, time.time()
    if cnt >= MAX_FAILS:
        wait = int(LOCK_S - (time.time() - first)) // 60 + 1
        flash(request, f"Too many failed attempts – try again in {wait} min", "err")
        return RedirectResponse("/login", status_code=303)
    user = db.scalar(select(User).where(User.username == username))
    if user and check_pw(password, user.password_hash):
        _FAILS.pop(ip, None)
        request.session.clear()
        request.session["uid"] = user.id
        request.session["csrf"] = secrets.token_urlsafe(32)
        request.session["weak_pw"] = password == "admin"
        return RedirectResponse("/", status_code=303)
    _FAILS[ip] = (cnt + 1, first)
    flash(request, "Wrong username or password", "err")
    return RedirectResponse("/login", status_code=303)


@app.get("/logout")
def logout_get(request: Request):
    """Plain link (old bookmarks): no side effect, just offer the button."""
    return RedirectResponse("/" if request.session.get("uid") else "/login", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/password")
def password_page(request: Request, user: User = Depends(current_user)):
    return page(request, "password.html", user=user)


@app.post("/password")
def password_change(request: Request, old: str = Form(...), new: str = Form(...), new2: str = Form(...),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not check_pw(old, user.password_hash):
        flash(request, "Current password is wrong", "err")
    elif new != new2:
        flash(request, "New passwords do not match", "err")
    elif len(new) < 4:
        flash(request, "Password must be at least 4 characters", "err")
    else:
        user.password_hash = hash_pw(new)
        db.commit()
        request.session["weak_pw"] = new == "admin"
        flash(request, "Password changed")
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/password", status_code=303)


# ---------- overview ----------
FILTER_KEYS = ["date_from", "date_to", "size_from", "size_to", *CATALOG_KINDS]


def to_bytes_gb(v):
    try:
        return int(float(str(v).replace(",", ".")) * 1024 ** 3) if str(v).strip() else None
    except ValueError:
        return None


def make_pager(request, total, per, page_no, path="/"):
    pages = max(1, -(-total // per))
    page_no = max(1, min(page_no, pages))
    base = {k: v for k, v in request.query_params.items() if k != "page" and v}
    return {"page": page_no, "pages": pages, "total": total, "per": per,
            "from": (page_no - 1) * per + 1 if total else 0, "to": min(page_no * per, total),
            "url": lambda n: f"{path}?" + urlencode({**base, "page": n}),
            "window": [n for n in range(1, pages + 1) if n in (1, pages) or abs(n - page_no) <= 2]}


def project_cover(p: Project):
    """Chosen cover if set and still has a thumbnail, else first stack, else first file with thumbnail."""
    if p.cover_file_id:
        f = next((f for f in p.files if f.id == p.cover_file_id and f.thumb_path), None)
        if f:
            return f
    auto = None
    for f in p.files:
        if f.thumb_path and (auto is None or f.frame_type == "stack" and auto.frame_type != "stack"):
            auto = f
    return auto


@app.get("/")
def index(request: Request, q: str = "", page_no: int = Query(1, alias="page"), sort: str = "",
          user: User = Depends(current_user), db: Session = Depends(get_db)):
    cfg = load_settings(db)
    flt = {k: (request.query_params.get(k) or "").strip() for k in FILTER_KEYS}
    sort = sort if sort in SORT_LABELS else cfg["sort"]
    stmt = select(Project)
    if q.strip():
        like = like_pattern(q)
        cols = [Project.name, Project.target, Project.object_type, Project.tags,
                Project.description, Project.notes]
        file_match = select(FrameFile.project_id).where(FrameFile.project_id.is_not(None), or_(
            FrameFile.original_name.ilike(like), FrameFile.description.ilike(like),
            FrameFile.filter.ilike(like), FrameFile.header.ilike(like)))
        sess_match = select(ImagingSession.project_id).where(or_(
            ImagingSession.notes.ilike(like), ImagingSession.location.ilike(like),
            ImagingSession.telescope.ilike(like), ImagingSession.camera.ilike(like),
            ImagingSession.mount.ilike(like), ImagingSession.software.ilike(like)))
        stmt = stmt.where(or_(*[c.ilike(like) for c in cols], Project.id.in_(file_match),
                              Project.id.in_(sess_match)))
    d_from, d_to = parse_dmy(flt["date_from"]), parse_dmy(flt["date_to"])
    # night-level filters are combined with AND on ONE session: e.g. telescope A + camera B + date range
    # match only projects that have a night where all of them were true at the same time
    sess_conds = [ImagingSession.date >= d_from] if d_from else []
    sess_conds += [ImagingSession.date <= d_to] if d_to else []
    for kind in SESSION_KINDS:
        if flt[kind]:
            col = getattr(ImagingSession, kind)
            sess_conds.append(col.is_(None) if flt[kind] == "__none__" else col == flt[kind])
    if sess_conds:
        stmt = stmt.where(Project.id.in_(select(ImagingSession.project_id).where(*sess_conds)))
    s_from, s_to = to_bytes_gb(flt["size_from"]), to_bytes_gb(flt["size_to"])
    if s_from is not None or s_to is not None:
        size_sq = (select(FrameFile.project_id, func.sum(FrameFile.size).label("total"))
                   .group_by(FrameFile.project_id).subquery())
        stmt = stmt.outerjoin(size_sq, size_sq.c.project_id == Project.id)
        total = func.coalesce(size_sq.c.total, 0)
        if s_from is not None:
            stmt = stmt.where(total >= s_from)
        if s_to is not None:
            stmt = stmt.where(total <= s_to)
    for kind in CATALOG_KINDS:
        if flt[kind] and kind not in SESSION_KINDS:        # project-level (object type)
            col = getattr(Project, kind)
            stmt = stmt.where(col.is_(None) if flt[kind] == "__none__" else col == flt[kind])
    active = sum(1 for v in flt.values() if v)

    if sort == "size_desc":
        sz = (select(FrameFile.project_id, func.sum(FrameFile.size).label("total"))
              .group_by(FrameFile.project_id).subquery("sz_sort"))
        stmt = stmt.outerjoin(sz, sz.c.project_id == Project.id).order_by(
            func.coalesce(sz.c.total, 0).desc(), Project.id.desc())
    else:
        last = (select(ImagingSession.project_id, func.max(ImagingSession.date).label("last"),
                       func.min(ImagingSession.date).label("first"))
                .group_by(ImagingSession.project_id).subquery("sess_sort"))
        if sort in ("date_desc", "date_asc"):
            stmt = stmt.outerjoin(last, last.c.project_id == Project.id)
        stmt = stmt.order_by(*{
            "date_desc": [last.c.last.desc().nulls_last(), Project.created_at.desc(), Project.id.desc()],
            "date_asc": [last.c.first.asc().nulls_last(), Project.created_at.asc(), Project.id.asc()],
            "name_asc": [func.lower(Project.name).asc(), Project.id],
            "name_desc": [func.lower(Project.name).desc(), Project.id],
            "created_desc": [Project.created_at.desc(), Project.id.desc()],
        }[sort])

    per = cfg["page_size"]
    total_found = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    pager = make_pager(request, total_found, per, page_no)
    projects = db.scalars(stmt.options(selectinload(Project.files), selectinload(Project.sessions)).limit(per)
                          .offset((pager["page"] - 1) * per)).all()

    counts = dict(db.execute(select(FrameFile.frame_type, func.count()).group_by(FrameFile.frame_type)).all())
    total_size = db.scalar(select(func.coalesce(func.sum(FrameFile.size), 0)))
    lights_s = db.scalar(select(func.coalesce(func.sum(FrameFile.exposure_s), 0)).where(
        FrameFile.frame_type == "light", FrameFile.rejected.is_not(True), FrameFile.project_id.is_not(None)))
    stats = {"projects": db.scalar(select(func.count(Project.id))), "files": sum(counts.values()),
             "size": total_size, "counts": counts, "integration": lights_s}

    summary = {}
    for p in projects:
        c = {}
        for f in p.files:
            c[f.frame_type] = c.get(f.frame_type, 0) + 1
        dates = [s.date for s in p.sessions if s.date]
        summary[p.id] = {"counts": c, "cover": project_cover(p), "size": sum(f.size for f in p.files),
                         "first": min(dates) if dates else None, "last": max(dates) if dates else None,
                         "nights": len(p.sessions),
                         "integration": exposure_summary(p.files)["total"],
                         "hits": search_hits(p, q)}
    return page(request, "index.html", user=user, projects=projects, q=q, stats=stats, summary=summary,
                flt=flt, active=active, cat=catalog(db), pager=pager, sort=sort)


# ---------- search hits ("found in" snippets) ----------
def _snippet(text: str, q: str, width: int = 60):
    """Escaped excerpt around the first match with <mark>ed occurrences, or None."""
    import re
    from markupsafe import Markup, escape
    text = " ".join(str(text).split())
    m = re.search(re.escape(q), text, re.I)
    if not m:
        return None
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    part = text[a:b]
    out = escape("…" if a else "")
    pos = 0
    for mm in re.finditer(re.escape(q), part, re.I):
        out += escape(part[pos:mm.start()]) + Markup("<mark>") + escape(mm.group(0)) + Markup("</mark>")
        pos = mm.end()
    return out + escape(part[pos:]) + escape("…" if b < len(text) else "")


def search_hits(p: Project, q: str) -> list:
    """Every place in a project where q occurs: [{'where': label, 'snip': Markup}] – project first."""
    q = " ".join(q.split())
    if not q:
        return []
    hits = []

    def add(where, text):
        if text:
            sn = _snippet(text, q)
            if sn is not None:
                hits.append({"where": where, "snip": sn})
    for label, v in (("Name", p.name), ("Object", p.target), ("Object type", p.object_type), ("Tags", p.tags),
                     ("Description", p.description), ("Notes", p.notes)):
        add(label, v)
    for se in p.sessions:
        d = se.date.strftime("%d-%m-%Y") if se.date else "session"
        add(f"Session {d} · notes", se.notes)
        for label, v in (("location", se.location), ("telescope", se.telescope), ("camera", se.camera),
                         ("mount", se.mount), ("software", se.software)):
            add(f"Session {d} · {label}", v)
    hdr = 0
    for f in p.files:
        add(f"File {f.original_name}", f.original_name if q.lower() in (f.original_name or "").lower() else None)
        add(f"File {f.original_name} · description", f.description)
        add(f"File {f.original_name} · filter", f.filter)
        if f.header and q.lower() in f.header.lower():
            hdr += 1
            if hdr <= 1:
                line = next((ln for ln in f.header.splitlines() if q.lower() in ln.lower()), "")
                add(f"FITS header · {f.original_name}", line)
    if hdr > 1:
        from markupsafe import Markup
        hits.append({"where": "FITS headers", "snip": Markup(f"match in {hdr} files")})
    return hits


# ---------- gallery ----------
GALLERY_SORTS = {"photo_desc": "Photo date (newest)", "photo_asc": "Photo date (oldest)",
                 "project_asc": "Project A–Z", "added_desc": "Recently added"}
templates.env.globals["GALLERY_SORTS"] = GALLERY_SORTS

@app.get("/gallery")
def gallery(request: Request, q: str = "", page_no: int = Query(1, alias="page"),
            user: User = Depends(current_user), db: Session = Depends(get_db)):
    cfg = load_settings(db)
    stmt = (select(FrameFile).join(Project, FrameFile.project_id == Project.id)
            .outerjoin(ImagingSession, FrameFile.session_id == ImagingSession.id)
            .where(FrameFile.frame_type == "stack", FrameFile.thumb_path.is_not(None)))
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Project.name.ilike(like), Project.target.ilike(like), Project.tags.ilike(like),
                              FrameFile.original_name.ilike(like), FrameFile.description.ilike(like)))
    sort = request.query_params.get("sort") or cfg["gallery_sort"]
    sort = sort if sort in GALLERY_SORTS else "photo_desc"
    shot = func.coalesce(func.date(FrameFile.date_obs), ImagingSession.date)     # photo date, else session date
    stmt = stmt.order_by(*{
        "photo_desc": [shot.desc().nulls_last(), FrameFile.id.desc()],
        "photo_asc": [shot.asc().nulls_last(), FrameFile.id],
        "project_asc": [func.lower(Project.name), shot.desc().nulls_last(), FrameFile.id],
        "added_desc": [FrameFile.uploaded_at.desc(), FrameFile.id.desc()],
    }[sort])
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    try:
        per = max(4, min(500, int(request.query_params.get("per") or cfg["gallery_page_size"])))
    except ValueError:
        per = cfg["gallery_page_size"]
    pager = make_pager(request, total, per, page_no, "/gallery")
    items = db.scalars(stmt.limit(per).offset((pager["page"] - 1) * per)).all()
    # ids for slideshow (all matches, not only this page)
    all_ids = [f.id for f in db.scalars(stmt.limit(2000))]
    return page(request, "gallery.html", user=user, items=items, pager=pager, gq=q, all_ids=all_ids,
                gsort=sort, per=per, GALLERY_SORTS=GALLERY_SORTS)


@app.get("/api/slides")
def api_slides(ids: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    out = []
    for i in [int(x) for x in ids.split(",") if x.isdigit()][:2000]:
        f = db.get(FrameFile, i)
        if f and f.thumb_path:
            p = f.project
            out.append({"id": f.id, "title": p.name if p else f.original_name,
                        "sub": " · ".join(x for x in [p.target if p else None,
                                                       photo_date(f),
                                                       f.original_name] if x),
                        "url": f"/projects/{p.id}" if p else f"/files/{f.id}/view"})
    return out


def last_night(p):
    dates = [s.date for s in p.sessions if s.date] if p else []
    return max(dates).strftime("%d-%m-%Y") if dates else None


def photo_date(f) -> str | None:
    """Capture date of the image (DATE-OBS / EXIF), else the date of its session."""
    if f.date_obs:
        return f.date_obs.strftime("%d-%m-%Y")
    if f.session and f.session.date:
        return f.session.date.strftime("%d-%m-%Y")
    return None


templates.env.globals.update(last_night=last_night, photo_date=photo_date)


# ---------- statistics ----------
@app.get("/stats")
def stats_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(FrameFile.exposure_s, FrameFile.date_obs, FrameFile.filter, FrameFile.project_id,
               ImagingSession.date, Project.created_at, Project.target, Project.name, ImagingSession.telescope,
               ImagingSession.camera, Project.object_type)
        .join(Project, FrameFile.project_id == Project.id)
        .outerjoin(ImagingSession, FrameFile.session_id == ImagingSession.id)
        .where(FrameFile.frame_type == "light", FrameFile.rejected.is_not(True))).all()
    by_month = defaultdict(float)
    by_year = defaultdict(float)
    by_obj = defaultdict(float)
    by_tel = defaultdict(float)
    by_cam = defaultdict(float)
    by_filter = defaultdict(float)
    by_type = defaultdict(float)
    nights = set()
    total = 0.0
    for exp, dobs, flt, pid, sdate, pdate, target, pname, tel, cam, otype in rows:
        e = exp or 0.0
        total += e
        d = sdate or night_of(dobs)              # unknown date -> not counted as a night / month
        if d:
            by_month[(d.year, d.month)] += e
            by_year[d.year] += e
            nights.add((pid, d))
        by_obj[target or pname] += e
        by_tel[tel or "(not set)"] += e
        by_cam[cam or "(not set)"] += e
        by_filter[flt or "(no filter)"] += e
        by_type[otype or "(not set)"] += e

    # last 24 months, continuous
    months = []
    if by_month:
        last = max(by_month)
        y, m = last
        for _ in range(24):
            months.append(((y, m), by_month.get((y, m), 0.0)))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        months.reverse()

    def top(d, n=10):
        return sorted(d.items(), key=lambda kv: -kv[1])[:n]

    disk_types = dict(db.execute(select(FrameFile.frame_type, func.coalesce(func.sum(FrameFile.size), 0))
                                 .group_by(FrameFile.frame_type)).all())
    calib_size = db.scalar(select(func.coalesce(func.sum(FrameFile.size), 0))
                           .where(FrameFile.calib_set_id.is_not(None)))
    files_total = db.scalar(select(func.coalesce(func.sum(FrameFile.size), 0)))
    try:
        du = shutil.disk_usage(DATA_DIR)
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        disk = None
    from . import worker, integrity
    return page(request, "stats.html", user=user, status=worker.queue_status(db), total=total,
                dups=integrity.duplicate_summary(db), integrity_state=dict(integrity.state),
                integrity_report=integrity.last_report(db), frames=len(rows), nights=len(nights),
                months=months, years=sorted(by_year.items()), objects=top(by_obj, 15),
                telescopes=top(by_tel), cameras=top(by_cam), filters=top(by_filter), types=top(by_type),
                disk_types=disk_types, calib_size=calib_size, files_total=files_total, disk=disk,
                projects=db.scalar(select(func.count(Project.id))))


# ---------- settings page ----------
@app.get("/settings")
def settings_page(request: Request, user: User = Depends(current_user)):
    return page(request, "settings.html", user=user, defs=SETTINGS)


@app.post("/settings")
async def settings_save(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    if form.get("reset"):
        db.execute(Setting.__table__.delete().where(Setting.key.in_(list(SETTINGS))))   # keep e.g. integrity_report
        db.commit()
        flash(request, "Settings reset to defaults")
        return RedirectResponse("/settings", status_code=303)
    for k, (default, typ, lim, _) in SETTINGS.items():
        v = ("1" if form.get(k) else "0") if typ is bool else form.get(k)
        if v is None:
            continue
        v = coerce_setting(k, v)
        row = db.get(Setting, k)
        if row:
            row.value = str(v)
        else:
            db.add(Setting(key=k, value=str(v)))
    db.commit()
    flash(request, "Settings saved")
    return RedirectResponse("/settings", status_code=303)


# ---------- managed lists ----------
def name_problem(db, kind, name, exclude_id=None) -> str | None:
    """Validation for catalog names: required, max length, unique ignoring case."""
    if not name:
        return "Name cannot be empty"
    if len(name) > NAME_MAX:
        return f"Name is too long (max {NAME_MAX} characters)"
    q = select(CatalogItem).where(CatalogItem.kind == kind, func.lower(CatalogItem.name) == name.lower())
    if exclude_id:
        q = q.where(CatalogItem.id != exclude_id)
    if db.scalar(q):
        return f"“{name}” already exists"
    return None


def check_kind(kind):
    if kind not in CATALOG_KINDS:
        raise HTTPException(404, "Unknown list")


def get_item(db, iid) -> CatalogItem:
    it = db.get(CatalogItem, iid)
    if not it:
        raise HTTPException(404, "Item not found")
    return it


SESSION_KINDS = ("location", "telescope", "camera", "mount", "software")


def usage_counts(db, kind) -> dict:
    """name -> number of projects using it (equipment/location via sessions)."""
    if kind in SESSION_KINDS:
        col = getattr(ImagingSession, kind)
        return dict(db.execute(select(col, func.count(func.distinct(ImagingSession.project_id)))
                               .where(col.is_not(None)).group_by(col)).all())
    col = getattr(Project, kind)
    return dict(db.execute(select(col, func.count()).where(col.is_not(None)).group_by(col)).all())


def rename_everywhere(db, kind, old, new):
    """Lookup values are stored by name in projects (and sessions/calibration sets for some kinds)."""
    from .models import CalibrationSet
    db.execute(update(Project).where(getattr(Project, kind) == old).values({kind: new}))
    if kind in SESSION_KINDS:
        db.execute(update(ImagingSession).where(getattr(ImagingSession, kind) == old).values({kind: new}))
    if kind in ("telescope", "camera"):
        db.execute(update(CalibrationSet).where(getattr(CalibrationSet, kind) == old).values({kind: new}))


@app.get("/manage")
def manage(request: Request, kind: str = "telescope", user: User = Depends(current_user),
           db: Session = Depends(get_db)):
    check_kind(kind)
    usage = usage_counts(db, kind)
    counts = dict(db.execute(select(CatalogItem.kind, func.count()).group_by(CatalogItem.kind)).all())
    items = catalog(db)[kind]
    return page(request, "manage.html", user=user, kind=kind, items=items, usage=usage, counts=counts,
                in_use={it.id: item_in_use(db, it) for it in items})


@app.post("/manage/{kind}/add")
async def manage_add(request: Request, kind: str, name: str = Form(...), notes: str = Form(""),
                     quiet: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    check_kind(kind)
    form = await request.form()
    name = name.strip()
    problem = name_problem(db, kind, name)
    if not problem:
        db.add(CatalogItem(kind=kind, name=name, notes=none_if_empty(notes), specs=parse_specs(kind, form)))
        db.commit()
        if quiet:
            return PlainTextResponse("ok")
        flash(request, f"Added: {name}")
    elif quiet:
        return PlainTextResponse("exists")
    else:
        flash(request, problem, "err")
    return RedirectResponse(f"/manage?kind={kind}", status_code=303)


@app.post("/manage/items/{iid}/edit")
async def manage_edit(request: Request, iid: int, name: str = Form(...), notes: str = Form(""),
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    it = get_item(db, iid)
    form = await request.form()
    name = name.strip()
    problem = name_problem(db, it.kind, name, it.id)
    if problem:
        flash(request, problem, "err")
    else:
        if name != it.name:
            rename_everywhere(db, it.kind, it.name, name)
        it.name, it.notes = name, none_if_empty(notes)
        it.specs = parse_specs(it.kind, form)
        db.commit()
        flash(request, "Saved")
    return RedirectResponse(f"/manage?kind={it.kind}", status_code=303)


@app.post("/manage/items/{iid}/delete")
def manage_delete(request: Request, iid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    it = get_item(db, iid)
    kind = it.kind
    used = item_in_use(db, it)
    if used:
        flash(request, f"“{it.name}” cannot be deleted – it is used in {used}. Change it there first "
                       f"(renaming is possible).", "err")
        return RedirectResponse(f"/manage?kind={kind}", status_code=303)
    rename_everywhere(db, kind, it.name, None)          # clears unused legacy project columns only
    db.delete(it)
    db.commit()
    flash(request, f"Deleted: {it.name}")
    return RedirectResponse(f"/manage?kind={kind}", status_code=303)


def item_in_use(db, it: CatalogItem) -> str | None:
    """Where a catalog value is still used – such items cannot be deleted (rename is fine)."""
    from .models import CalibrationSet
    parts = []
    if it.kind in SESSION_KINDS:
        n = db.scalar(select(func.count()).where(getattr(ImagingSession, it.kind) == it.name))
        if n:
            parts.append(f"{n} session{'s' if n != 1 else ''}")
    if it.kind == "object_type":
        n = db.scalar(select(func.count()).where(Project.object_type == it.name))
        if n:
            parts.append(f"{n} project{'s' if n != 1 else ''}")
    if it.kind in ("telescope", "camera"):
        n = db.scalar(select(func.count()).where(getattr(CalibrationSet, it.kind) == it.name))
        if n:
            parts.append(f"{n} calibration set{'s' if n != 1 else ''}")
    return " and ".join(parts) or None


def catalog_json(db, kind):
    usage = usage_counts(db, kind)
    return {"kind": kind, "label": CATALOG_KINDS[kind], "singular": CATALOG_SINGULAR[kind],
            "items": [{"id": it.id, "name": it.name, "notes": it.notes or "", "used": usage.get(it.name, 0),
                       "specs": it.spec, "summary": spec_summary(kind, it.spec), "in_use": item_in_use(db, it)}
                      for it in catalog(db)[kind]]}


def api_err(msg, code=400):
    return JSONResponse({"error": msg}, status_code=code)


@app.get("/api/catalog/{kind}")
def api_catalog(kind: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    check_kind(kind)
    return catalog_json(db, kind)


@app.post("/api/catalog/{kind}")
async def api_catalog_add(request: Request, kind: str, user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    check_kind(kind)
    data = await request.json()
    name = (data.get("name") or "").strip()
    problem = name_problem(db, kind, name)
    if problem:
        return api_err(problem)
    db.add(CatalogItem(kind=kind, name=name, notes=none_if_empty(data.get("notes")),
                       specs=parse_specs(kind, data.get("specs") or {})))
    db.commit()
    return catalog_json(db, kind)


@app.put("/api/catalog/items/{iid}")
async def api_catalog_edit(request: Request, iid: int, user: User = Depends(current_user),
                           db: Session = Depends(get_db)):
    it = get_item(db, iid)
    data = await request.json()
    name = (data.get("name") or "").strip()
    problem = name_problem(db, it.kind, name, it.id)
    if problem:
        return api_err(problem)
    old = it.name
    if name != old:
        rename_everywhere(db, it.kind, old, name)
    it.name, it.notes = name, none_if_empty(data.get("notes"))
    if isinstance(data.get("specs"), dict):          # omitted -> keep existing specs
        it.specs = parse_specs(it.kind, data["specs"])
    db.commit()
    res = catalog_json(db, it.kind)
    res["renamed"] = {"from": old, "to": name} if name != old else None
    return res


@app.delete("/api/catalog/items/{iid}")
def api_catalog_delete(iid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    it = get_item(db, iid)
    kind, name = it.kind, it.name
    used = item_in_use(db, it)
    if used:
        return api_err(f"“{name}” cannot be deleted – it is used in {used}. Change it there first "
                       f"(renaming is possible).", 409)
    rename_everywhere(db, kind, name, None)
    db.delete(it)
    db.commit()
    res = catalog_json(db, kind)
    res["deleted"] = name
    return res
