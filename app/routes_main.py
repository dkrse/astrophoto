"""Auth, settings, managed lists, overview, gallery, statistics."""
import shutil
from collections import defaultdict
from urllib.parse import urlencode

from fastapi import Request, Form, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, PlainTextResponse, JSONResponse
from sqlalchemy import select, or_, func, update
from sqlalchemy.orm import Session, selectinload

from .core import (app, page, flash, get_db, current_user, check_pw, hash_pw, load_settings, coerce_setting,
                   SETTINGS, SORT_LABELS, catalog, none_if_empty, parse_dmy, DATA_DIR, exposure_summary,
                   night_of)
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
        request.session["weak_pw"] = password == "admin"
        return RedirectResponse("/", status_code=303)
    _FAILS[ip] = (cnt + 1, first)
    flash(request, "Wrong username or password", "err")
    return RedirectResponse("/login", status_code=303)


@app.get("/logout")
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
        like = f"%{q.strip()}%"
        cols = [Project.name, Project.target, Project.object_type, Project.location, Project.telescope,
                Project.camera, Project.mount, Project.filters, Project.software, Project.tags,
                Project.description, Project.notes, Project.total_exposure]
        file_match = select(FrameFile.project_id).where(FrameFile.project_id.is_not(None), or_(
            FrameFile.original_name.ilike(like), FrameFile.description.ilike(like),
            FrameFile.filter.ilike(like), FrameFile.header.ilike(like)))
        sess_match = select(ImagingSession.project_id).where(or_(
            ImagingSession.notes.ilike(like), ImagingSession.location.ilike(like)))
        stmt = stmt.where(or_(*[c.ilike(like) for c in cols], Project.id.in_(file_match),
                              Project.id.in_(sess_match)))
    d_from, d_to = parse_dmy(flt["date_from"]), parse_dmy(flt["date_to"])
    if d_from:
        stmt = stmt.where(Project.capture_date >= d_from)
    if d_to:
        stmt = stmt.where(Project.capture_date <= d_to)
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
        if flt[kind]:
            col = getattr(Project, kind)
            stmt = stmt.where(col.is_(None) if flt[kind] == "__none__" else col == flt[kind])
    active = sum(1 for v in flt.values() if v)

    if sort == "size_desc":
        sz = (select(FrameFile.project_id, func.sum(FrameFile.size).label("total"))
              .group_by(FrameFile.project_id).subquery("sz_sort"))
        stmt = stmt.outerjoin(sz, sz.c.project_id == Project.id).order_by(
            func.coalesce(sz.c.total, 0).desc(), Project.id.desc())
    else:
        stmt = stmt.order_by(*{
            "date_desc": [Project.capture_date.desc().nulls_last(), Project.id.desc()],
            "date_asc": [Project.capture_date.asc().nulls_last(), Project.id.asc()],
            "name_asc": [func.lower(Project.name).asc(), Project.id],
            "name_desc": [func.lower(Project.name).desc(), Project.id],
            "created_desc": [Project.created_at.desc(), Project.id.desc()],
        }[sort])

    per = cfg["page_size"]
    total_found = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    pager = make_pager(request, total_found, per, page_no)
    projects = db.scalars(stmt.options(selectinload(Project.files)).limit(per)
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
        summary[p.id] = {"counts": c, "cover": project_cover(p), "size": sum(f.size for f in p.files),
                         "integration": exposure_summary(p.files)["total"]}
    return page(request, "index.html", user=user, projects=projects, q=q, stats=stats, summary=summary,
                flt=flt, active=active, cat=catalog(db), pager=pager, sort=sort)


# ---------- gallery ----------
@app.get("/gallery")
def gallery(request: Request, q: str = "", page_no: int = Query(1, alias="page"),
            user: User = Depends(current_user), db: Session = Depends(get_db)):
    cfg = load_settings(db)
    stmt = (select(FrameFile).join(Project, FrameFile.project_id == Project.id)
            .where(FrameFile.frame_type == "stack", FrameFile.thumb_path.is_not(None)))
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Project.name.ilike(like), Project.target.ilike(like), Project.tags.ilike(like),
                              FrameFile.original_name.ilike(like), FrameFile.description.ilike(like)))
    stmt = stmt.order_by(Project.capture_date.desc().nulls_last(), Project.id.desc(), FrameFile.id)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    per = max(cfg["page_size"], 12)
    pager = make_pager(request, total, per, page_no, "/gallery")
    items = db.scalars(stmt.limit(per).offset((pager["page"] - 1) * per)).all()
    # ids for slideshow (all matches, not only this page)
    all_ids = [f.id for f in db.scalars(stmt.limit(2000))]
    return page(request, "gallery.html", user=user, items=items, pager=pager, gq=q, all_ids=all_ids)


@app.get("/api/slides")
def api_slides(ids: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    out = []
    for i in [int(x) for x in ids.split(",") if x.isdigit()][:2000]:
        f = db.get(FrameFile, i)
        if f and f.thumb_path:
            p = f.project
            out.append({"id": f.id, "title": p.name if p else f.original_name,
                        "sub": " · ".join(x for x in [p.target if p else None,
                                                       p.capture_date.strftime("%d-%m-%Y") if p and p.capture_date else None,
                                                       f.original_name] if x),
                        "url": f"/projects/{p.id}" if p else f"/files/{f.id}/view"})
    return out


# ---------- statistics ----------
@app.get("/stats")
def stats_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(FrameFile.exposure_s, FrameFile.date_obs, FrameFile.filter, FrameFile.project_id,
               ImagingSession.date, Project.capture_date, Project.target, Project.name, Project.telescope,
               Project.camera, Project.object_type)
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
        d = sdate or night_of(dobs) or pdate
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
    return page(request, "stats.html", user=user, total=total, frames=len(rows), nights=len(nights),
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
        db.execute(Setting.__table__.delete())
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
def check_kind(kind):
    if kind not in CATALOG_KINDS:
        raise HTTPException(404, "Unknown list")


def get_item(db, iid) -> CatalogItem:
    it = db.get(CatalogItem, iid)
    if not it:
        raise HTTPException(404, "Item not found")
    return it


def rename_everywhere(db, kind, old, new):
    """Lookup values are stored by name in projects (and sessions/calibration sets for some kinds)."""
    from .models import CalibrationSet
    db.execute(update(Project).where(getattr(Project, kind) == old).values({kind: new}))
    if kind in ("location", "telescope", "camera", "mount"):
        db.execute(update(ImagingSession).where(getattr(ImagingSession, kind) == old).values({kind: new}))
    if kind in ("telescope", "camera"):
        db.execute(update(CalibrationSet).where(getattr(CalibrationSet, kind) == old).values({kind: new}))


@app.get("/manage")
def manage(request: Request, kind: str = "telescope", user: User = Depends(current_user),
           db: Session = Depends(get_db)):
    check_kind(kind)
    col = getattr(Project, kind)
    usage = dict(db.execute(select(col, func.count()).where(col.is_not(None)).group_by(col)).all())
    counts = dict(db.execute(select(CatalogItem.kind, func.count()).group_by(CatalogItem.kind)).all())
    return page(request, "manage.html", user=user, kind=kind, items=catalog(db)[kind], usage=usage, counts=counts)


@app.post("/manage/{kind}/add")
def manage_add(request: Request, kind: str, name: str = Form(...), notes: str = Form(""), quiet: str = Form(""),
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    check_kind(kind)
    name = name.strip()
    if name and not db.scalar(select(CatalogItem).where(CatalogItem.kind == kind, CatalogItem.name == name)):
        db.add(CatalogItem(kind=kind, name=name, notes=none_if_empty(notes)))
        db.commit()
        if quiet:
            return PlainTextResponse("ok")
        flash(request, f"Added: {name}")
    elif quiet:
        return PlainTextResponse("exists")
    else:
        flash(request, "Empty name or already exists", "err")
    return RedirectResponse(f"/manage?kind={kind}", status_code=303)


@app.post("/manage/items/{iid}/edit")
def manage_edit(request: Request, iid: int, name: str = Form(...), notes: str = Form(""),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    it = get_item(db, iid)
    name = name.strip()
    if not name:
        flash(request, "Name cannot be empty", "err")
    elif name != it.name and db.scalar(select(CatalogItem).where(CatalogItem.kind == it.kind,
                                                               CatalogItem.name == name)):
        flash(request, f"“{name}” already exists", "err")
    else:
        if name != it.name:
            rename_everywhere(db, it.kind, it.name, name)
        it.name, it.notes = name, none_if_empty(notes)
        db.commit()
        flash(request, "Saved")
    return RedirectResponse(f"/manage?kind={it.kind}", status_code=303)


@app.post("/manage/items/{iid}/delete")
def manage_delete(request: Request, iid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    it = get_item(db, iid)
    kind = it.kind
    rename_everywhere(db, kind, it.name, None)
    db.delete(it)
    db.commit()
    flash(request, f"Deleted: {it.name}")
    return RedirectResponse(f"/manage?kind={kind}", status_code=303)


def catalog_json(db, kind):
    col = getattr(Project, kind)
    usage = dict(db.execute(select(col, func.count()).where(col.is_not(None)).group_by(col)).all())
    return {"kind": kind, "label": CATALOG_KINDS[kind], "singular": CATALOG_SINGULAR[kind],
            "items": [{"id": it.id, "name": it.name, "notes": it.notes or "", "used": usage.get(it.name, 0)}
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
    if not name:
        return api_err("Name cannot be empty")
    if db.scalar(select(CatalogItem).where(CatalogItem.kind == kind, CatalogItem.name == name)):
        return api_err(f"“{name}” already exists")
    db.add(CatalogItem(kind=kind, name=name, notes=none_if_empty(data.get("notes"))))
    db.commit()
    return catalog_json(db, kind)


@app.put("/api/catalog/items/{iid}")
async def api_catalog_edit(request: Request, iid: int, user: User = Depends(current_user),
                           db: Session = Depends(get_db)):
    it = get_item(db, iid)
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        return api_err("Name cannot be empty")
    if name != it.name and db.scalar(select(CatalogItem).where(CatalogItem.kind == it.kind,
                                                               CatalogItem.name == name)):
        return api_err(f"“{name}” already exists")
    old = it.name
    if name != old:
        rename_everywhere(db, it.kind, old, name)
    it.name, it.notes = name, none_if_empty(data.get("notes"))
    db.commit()
    res = catalog_json(db, it.kind)
    res["renamed"] = {"from": old, "to": name} if name != old else None
    return res


@app.delete("/api/catalog/items/{iid}")
def api_catalog_delete(iid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    it = get_item(db, iid)
    kind, name = it.kind, it.name
    rename_everywhere(db, kind, name, None)
    db.delete(it)
    db.commit()
    res = catalog_json(db, kind)
    res["deleted"] = name
    return res
