"""Calibration frame library: reusable sets of darks / bias / flats / dark flats."""
import shutil

from fastapi import Request, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session, selectinload

from .core import (app, page, flash, get_db, current_user, catalog, none_if_empty, parse_dmy, to_float,
                   get_calib, calib_dir, DATA_DIR, load_settings, like_pattern)
from .models import User, CalibrationSet, FrameFile, Project, CALIB_TYPES, FRAME_LABELS
from . import worker

CALIB_FIELDS = ["name", "camera", "telescope", "temperature", "gain", "offset", "filter", "binning", "notes"]


@app.get("/calibration")
def calib_list(request: Request, type: str = "", camera: str = "", q: str = "",
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(CalibrationSet).options(selectinload(CalibrationSet.files), selectinload(CalibrationSet.sessions))
    if type in CALIB_TYPES:
        stmt = stmt.where(CalibrationSet.frame_type == type)
    if camera:
        stmt = stmt.where(CalibrationSet.camera == camera)
    if q.strip():
        like = like_pattern(q)
        stmt = stmt.where(or_(CalibrationSet.name.ilike(like), CalibrationSet.notes.ilike(like),
                              CalibrationSet.filter.ilike(like)))
    sets = db.scalars(stmt.order_by(CalibrationSet.capture_date.desc().nulls_last(), CalibrationSet.id.desc())).all()
    cameras = sorted({c for c in db.scalars(select(CalibrationSet.camera).distinct()) if c})
    from .core import paginate, load_settings
    pg = paginate(len(sets), load_settings(db)["calib_page_size"], request.query_params.get("page"))
    return page(request, "calib_list.html", user=user, sets=sets[pg["start"]:pg["end"]], pg=pg, ftype=type,
                camera=camera, cq=q, cameras=cameras)


async def apply_calib_form(request: Request, c: CalibrationSet):
    form = await request.form()
    for f in CALIB_FIELDS:
        v = none_if_empty(form.get(f))
        setattr(c, f, None if v == "__new__" else v)
    ft = form.get("frame_type")
    c.frame_type = ft if ft in CALIB_TYPES else "dark"
    c.capture_date = parse_dmy(form.get("capture_date"))
    c.exposure_s = to_float(form.get("exposure_s"))
    if not c.name:
        bits = [FRAME_LABELS[c.frame_type], c.camera, f"{c.temperature}°C" if c.temperature else None,
                f"{c.exposure_s:g}s" if c.exposure_s else None,
                c.capture_date.strftime("%d-%m-%Y") if c.capture_date else None]
        c.name = " · ".join(b for b in bits if b)


@app.get("/calibration/new")
def calib_new(request: Request, type: str = "dark", user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    return page(request, "calib_form.html", user=user, c=None, ftype=type if type in CALIB_TYPES else "dark",
                cat=catalog(db))


@app.post("/calibration/new")
async def calib_create(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = CalibrationSet()
    await apply_calib_form(request, c)
    db.add(c)
    db.commit()
    flash(request, "Calibration set created – now add frames")
    return RedirectResponse(f"/calibration/{c.id}", status_code=303)


@app.get("/calibration/{cid}")
def calib_detail(request: Request, cid: int, show: str = "", user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    c = get_calib(db, cid)
    groups = {t: [f for f in c.files if f.frame_type == t] for t in CALIB_TYPES}
    others = db.execute(select(Project.id, Project.name).order_by(func.lower(Project.name))).all()
    return page(request, "calib_detail.html", user=user, c=c, groups=groups,
                sizes={t: sum(f.size for f in fs) for t, fs in groups.items()},
                status=worker.queue_status(db, calib_set_id=c.id), others=others,
                settings=load_settings(db), show=show)


@app.get("/calibration/{cid}/edit")
def calib_edit(request: Request, cid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = get_calib(db, cid)
    return page(request, "calib_form.html", user=user, c=c, ftype=c.frame_type, cat=catalog(db))


@app.post("/calibration/{cid}/edit")
async def calib_update(request: Request, cid: int, user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    c = get_calib(db, cid)
    await apply_calib_form(request, c)
    db.commit()
    flash(request, "Saved")
    return RedirectResponse(f"/calibration/{cid}", status_code=303)


@app.post("/calibration/{cid}/delete")
def calib_delete(request: Request, cid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = db.get(CalibrationSet, cid)
    if not c:                                    # already deleted (double submit / resubmitted form)
        flash(request, "Calibration set was already deleted")
        return RedirectResponse("/calibration", status_code=303)
    if any(f.status == "importing" for f in c.files):
        flash(request, "An import into this set is still running – wait until it finishes", "err")
        return RedirectResponse(f"/calibration/{cid}", status_code=303)
    fids = [f.id for f in c.files]
    db.delete(c)
    db.commit()
    shutil.rmtree(calib_dir(cid), ignore_errors=True)
    for fid in fids:
        shutil.rmtree(DATA_DIR / ".cache" / str(fid), ignore_errors=True)
    flash(request, "Calibration set deleted")
    return RedirectResponse("/calibration", status_code=303)
