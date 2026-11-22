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


# ---------- mosaic plan / coverage of a project ----------
from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from .core import get_project, json_body  # noqa: E402


@app.get("/api/projects/{pid}/mosaic")
def mosaic_get(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from . import mosaic
    return mosaic.project_data(db, get_project(db, pid))


@app.post("/api/projects/{pid}/mosaic")
async def mosaic_save(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Store the plan ({"clear": true} removes it); returns the recomputed coverage."""
    import json
    from . import mosaic
    data = await json_body(request)
    p = get_project(db, pid)
    if data.get("clear"):
        p.mosaic = None
    else:
        plan = mosaic.normalize(data.get("plan"))
        if not plan:
            return JSONResponse({"error": "Invalid plan: columns / rows 1–10, overlap 0–60 %, field 1–1200′, "
                                          "RA 0–360°, Dec −90…+90°"}, status_code=400)
        p.mosaic = json.dumps(plan)
    db.commit()
    return await run_in_threadpool(mosaic.project_data, db, p)
