"""Sky map page (/skymap) and its data (/api/skymap)."""
from fastapi import Request, Depends, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from .core import app, page, get_db, current_user
from .models import User
from . import skymap


@app.get("/skymap")
def skymap_page(request: Request, user: User = Depends(current_user)):
    return page(request, "skymap.html", user=user)


@app.get("/api/skymap")
def skymap_api(user: User = Depends(current_user), db: Session = Depends(get_db),
               mag: float = Query(10.0, ge=0, le=16)):
    return skymap.skymap_data(db, mag)
