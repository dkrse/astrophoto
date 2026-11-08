"""Astrophoto – FastAPI app entry point."""
import os

from sqlalchemy import select, text

import logging
from datetime import date
import os as _os

from .core import app, DATA_DIR, ARCHIVE_DIR, hash_pw
from .models import Base, engine, SessionLocal, User, Project, CatalogItem, MIGRATIONS, CATALOG_KINDS
from . import routes_main, routes_projects, routes_files, routes_upload, routes_calib, routes_archive  # noqa: F401  (register routes)
from . import worker


log = logging.getLogger("astrophoto")


def migrate_to_archive_dir():
    """v0.5 stored data in DATA_DIR/{projects,calibration}; move it into DATA_DIR/archive-data/."""
    rel = ARCHIVE_DIR.relative_to(DATA_DIR).as_posix()
    for sub in ("projects", "calibration"):
        old, new = DATA_DIR / sub, ARCHIVE_DIR / sub
        skipped = []
        if old.is_dir():
            new.mkdir(parents=True, exist_ok=True)
            for child in old.iterdir():
                if (new / child.name).exists():
                    log.warning("archive migration: %s already exists, leaving %s in place", new / child.name, child)
                    skipped.append(child.name)
                    continue
                _os.replace(child, new / child.name)
            try:
                old.rmdir()
            except OSError:
                pass
        with engine.begin() as c:            # repoint DB rows of moved folders only (idempotent)
            for col in ("stored_path", "thumb_path"):
                cond = " AND ".join(f"{col} NOT LIKE :s{i}" for i in range(len(skipped))) or "TRUE"
                c.execute(text(f"UPDATE frame_files SET {col} = :pre || {col} WHERE {col} LIKE :old AND {cond}"),
                          {"pre": rel + "/", "old": sub + "/%",
                           **{f"s{i}": f"{sub}/{n}/%" for i, n in enumerate(skipped)}})


def migrate_project_to_sessions(db):
    """Projects used to carry date/location/equipment; these now live in sessions (one-time, marker in settings)."""
    from .models import Setting, ImagingSession, FrameFile
    from .core import moon_illumination
    if db.get(Setting, "migr_sessions_v1"):
        return
    eq = ("location", "telescope", "camera", "mount", "software")
    for p in db.scalars(select(Project)):
        legacy = {k: getattr(p, k) for k in eq}
        if not p.sessions and (p.capture_date or any(legacy.values()) or p.files or p.calib_sets):
            d = p.capture_date or (p.created_at.date() if p.created_at else date.today())   # date is NOT NULL
            s = ImagingSession(project_id=p.id, date=d, moon_illum=moon_illumination(d), **legacy)
            db.add(s)
            db.flush()
            db.refresh(p)
        for s in p.sessions:
            for k, v in legacy.items():
                if v and not getattr(s, k):
                    setattr(s, k, v)
            for c in p.calib_sets:                  # project-level library links -> every session
                if c not in s.calib_sets:
                    s.calib_sets.append(c)
        if len(p.sessions) == 1:                    # single night: all its files belong to it
            for f in p.files:
                if not f.session_id:
                    f.session_id = p.sessions[0].id
    db.add(Setting(key="migr_sessions_v1", value="1"))
    db.commit()


def migrate_software_to_sessions(db):
    from .models import Setting
    if db.get(Setting, "migr_software_v1"):
        return
    for p in db.scalars(select(Project).where(Project.software.is_not(None))):
        for s in p.sessions:
            s.software = s.software or p.software
    db.add(Setting(key="migr_software_v1", value="1"))
    db.commit()


@app.on_event("startup")
def startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        for sql in MIGRATIONS:
            c.execute(text(sql))
    migrate_to_archive_dir()
    with SessionLocal() as db:
        # seed lookup lists from values already used in projects
        for kind in CATALOG_KINDS:
            col = getattr(Project, kind)
            used = set(db.scalars(select(col).where(col.is_not(None)).distinct()))
            have = set(db.scalars(select(CatalogItem.name).where(CatalogItem.kind == kind)))
            for name in used - have:
                db.add(CatalogItem(kind=kind, name=name))
        # thumbnails from the old naming scheme (could collide) -> regenerate in background
        from .models import FrameFile
        for f in db.scalars(select(FrameFile).where(FrameFile.thumb_path.is_not(None),
                                                   FrameFile.status == "done")):
            if "__" not in f.thumb_path.rsplit("/", 1)[-1]:
                f.status = "pending"
        # original names must never carry path components (ZIP safety)
        from .core import clean_filename
        for f in db.scalars(select(FrameFile).where(FrameFile.original_name.op("~")(r"[\\/]"))):
            f.original_name = clean_filename(f.original_name)
        migrate_project_to_sessions(db)
        migrate_software_to_sessions(db)
        if not db.scalar(select(User).where(User.username == "admin")):
            db.add(User(username="admin", password_hash=hash_pw("admin")))
        db.commit()
    if os.environ.get("ASTRO_NO_WORKER") != "1":     # tests drive the worker manually
        worker.start()
