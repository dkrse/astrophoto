"""Astrophoto – FastAPI app entry point."""
import os

from sqlalchemy import select, text

from .core import app, DATA_DIR, hash_pw
from .models import Base, engine, SessionLocal, User, Project, CatalogItem, MIGRATIONS, CATALOG_KINDS
from . import routes_main, routes_projects, routes_files, routes_upload, routes_calib  # noqa: F401  (register routes)
from . import worker


@app.on_event("startup")
def startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        for sql in MIGRATIONS:
            c.execute(text(sql))
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
        if not db.scalar(select(User).where(User.username == "admin")):
            db.add(User(username="admin", password_hash=hash_pw("admin")))
        db.commit()
    if os.environ.get("ASTRO_NO_WORKER") != "1":     # tests drive the worker manually
        worker.start()
