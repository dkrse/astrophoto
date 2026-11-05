"""Shared application objects: app, templates, settings, auth deps and helpers."""
import hashlib
import hmac
import math
import os
import re
import secrets
import shutil
from datetime import datetime, date
from pathlib import Path

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .models import (SessionLocal, User, Project, FrameFile, CatalogItem, Setting, CalibrationSet,
                     FRAME_TYPES, FRAME_LABELS, CATALOG_KINDS, CATALOG_SINGULAR, CALIB_TYPES)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
IMPORT_DIR = Path(os.environ.get("IMPORT_DIR", "/import"))
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
BASE = Path(__file__).parent

app = FastAPI(title="Astrophoto")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=60 * 60 * 24 * 14)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# ---------- formatting ----------
def fmt_size(n):
    n = n or 0
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_dmy(v):
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d-%m-%Y %H:%M")
    return v.strftime("%d-%m-%Y")


def fmt_dur(seconds):
    """Seconds -> '6h 12m' / '45m' / '30s'."""
    s = int(round(seconds or 0))
    if s <= 0:
        return "0s"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m" + (f" {sec}s" if sec else "")
    return f"{sec}s"


def fmt_num(v, digits=1):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def parse_dmy(v):
    """Accepts dd-mm-yyyy (also dd.mm.yyyy, dd/mm/yyyy) or yyyy-mm-dd. Returns date or None."""
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    return None


templates.env.filters.update(size=fmt_size, dmy=fmt_dmy, dur=fmt_dur, num=fmt_num)
templates.env.globals.update(CATALOG_KINDS=CATALOG_KINDS, CATALOG_SINGULAR=CATALOG_SINGULAR,
                             FRAME_TYPES=FRAME_TYPES, FRAME_LABELS=FRAME_LABELS, CALIB_TYPES=CALIB_TYPES)


# ---------- password hashing ----------
def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 260_000).hex()
    return f"pbkdf2${salt}${h}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 260_000).hex()
    return hmac.compare_digest(calc, h)


# ---------- deps ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
def _login_redirect(request: Request, exc):
    if request.url.path.startswith("/api/"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user:
        raise LoginRequired()
    return user


# ---------- app settings ----------
SETTINGS = {
    # key: (default, type, choices or (min, max), label)
    "app_title": ("Astrophoto", str, None, "Application title"),
    "page_size": (24, int, (1, 500), "Projects per page"),
    "accent_color": ("#3b5bdb", "color", None, "Accent color"),
    "default_theme": ("auto", str, ["auto", "light", "dark"], "Default theme"),
    "default_view": ("grid", str, ["grid", "list"], "Default project view"),
    "card_size": ("medium", str, ["small", "medium", "large"], "Card size (grid)"),
    "sort": ("date_desc", str, ["date_desc", "date_asc", "name_asc", "name_desc", "created_desc", "size_desc"],
             "Default sort"),
    "show_stats": (True, bool, None, "Show statistics on overview"),
    "zip_compression": ("none", str, ["none", "fast", "best"], "ZIP compression"),
    "slideshow_seconds": (6, int, (2, 120), "Slideshow interval (s)"),
    "skip_duplicates": (True, bool, None, "Skip duplicate files on upload / import"),
}
SORT_LABELS = {"date_desc": "Capture date (newest)", "date_asc": "Capture date (oldest)",
               "name_asc": "Name A–Z", "name_desc": "Name Z–A", "created_desc": "Recently added",
               "size_desc": "Size (largest)"}
templates.env.globals["SORT_LABELS"] = SORT_LABELS


def coerce_setting(k, v):
    default, typ, lim, _ = SETTINGS[k]
    try:
        if typ is int:
            return max(lim[0], min(lim[1], int(v)))
        if typ is bool:
            return str(v).lower() in ("1", "true", "on", "yes")
        if typ == "color":
            v = str(v).strip()
            return v if re.fullmatch(r"#[0-9a-fA-F]{6}", v) else default
        v = str(v).strip()
        if isinstance(lim, list):
            return v if v in lim else default
        return v or default
    except (TypeError, ValueError):
        return default


def load_settings(db=None) -> dict:
    own = db is None
    db = db or SessionLocal()
    try:
        raw = {r.key: r.value for r in db.scalars(select(Setting))}
    finally:
        if own:
            db.close()
    return {k: (coerce_setting(k, raw[k]) if raw.get(k) is not None else d[0]) for k, d in SETTINGS.items()}


def accent_vars(hex_color: str) -> dict:
    """Readable text color on accent + lighter variant for dark mode."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    lr, lg, lb = (int(c + (255 - c) * 0.35) for c in (r, g, b))
    return {"accent": hex_color, "accent_fg": "#0d1017" if lum > 150 else "#ffffff",
            "accent_dark": f"#{lr:02x}{lg:02x}{lb:02x}"}


def page(request, name, **ctx):
    ctx["flash"] = request.session.pop("flash", None)
    ctx["S"] = load_settings()
    ctx["A"] = accent_vars(ctx["S"]["accent_color"])
    ctx["weak_pw"] = bool(request.session.get("weak_pw"))
    ctx["today"] = date.today()
    ctx.setdefault("q", "")
    return templates.TemplateResponse(request, name, ctx)


def flash(request, msg, kind="ok"):
    request.session["flash"] = {"msg": msg, "kind": kind}


# ---------- small helpers ----------
def none_if_empty(v):
    v = (v or "").strip() if isinstance(v, str) or v is None else str(v)
    return v or None


def to_float(v):
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else None
    except ValueError:
        return None


def safe_name(name: str) -> str:
    name = os.path.basename(name or "file")
    name = re.sub(r"[^A-Za-z0-9._\-]+", "_", name).strip("._") or "file"
    return name[:200]


def clean_filename(name: str) -> str:
    """Original file name for display/ZIP: last path component only, no control chars (keeps unicode)."""
    name = re.split(r"[\\/]", name or "")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().lstrip(".")
    return name[:250] or "file"


def download_name(name: str) -> str:
    """Human-friendly download file name (unicode kept; only characters invalid on common filesystems replaced)."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name or "").strip(" ._")
    return name[:150] or "download"


def catalog(db) -> dict:
    out = {k: [] for k in CATALOG_KINDS}
    for it in db.scalars(select(CatalogItem).order_by(func.lower(CatalogItem.name))):
        out.setdefault(it.kind, []).append(it)
    return out


def get_project(db, pid) -> Project:
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404, "Project not found")
    return p


def get_calib(db, cid) -> CalibrationSet:
    c = db.get(CalibrationSet, cid)
    if not c:
        raise HTTPException(404, "Calibration set not found")
    return c


def get_file(db, fid) -> FrameFile:
    f = db.get(FrameFile, fid)
    if not f:
        raise HTTPException(404, "File not found")
    return f


# ---------- storage ----------
def project_dir(pid: int) -> Path:
    return DATA_DIR / "projects" / str(pid)


def calib_dir(cid: int) -> Path:
    return DATA_DIR / "calibration" / str(cid)


def owner_dir(project_id=None, calib_set_id=None) -> Path:
    return project_dir(project_id) if project_id else calib_dir(calib_set_id)


def unique_dest(folder: Path, filename: str) -> Path:
    """Atomically reserve a free file name in folder (creates an empty placeholder file)."""
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_name(filename)
    dest = folder / name
    i = 1
    while True:
        try:
            os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            return dest
        except FileExistsError:
            dest = folder / f"{Path(name).stem}_{i}{Path(name).suffix}"
            i += 1


def file_owner(f: FrameFile) -> dict:
    """Uniform info about where a file belongs (project or calibration set)."""
    if f.project_id:
        return {"kind": "project", "id": f.project_id, "name": f.project.name,
                "url": f"/projects/{f.project_id}", "files": f.project.files}
    c = f.calib_set
    return {"kind": "calib", "id": c.id, "name": c.name, "url": f"/calibration/{c.id}", "files": c.files}


def thumb_rel(stored_path: str) -> Path:
    """Thumbnail path for a stored file: <owner>/.thumbs/<type>__<name>.jpg (unique per type folder)."""
    p = Path(stored_path)
    return p.parent.parent / ".thumbs" / f"{p.parent.name}__{p.name}.jpg"


def move_file_storage(f: FrameFile, project_id=None, calib_set_id=None, frame_type=None):
    """Physically move a file (and its thumbnail) to another owner / type folder."""
    ft = frame_type or f.frame_type
    src = DATA_DIR / f.stored_path
    if (project_id, calib_set_id, ft) == (f.project_id, f.calib_set_id, f.frame_type):
        return                                   # nothing to do
    if not src.is_file():
        raise FileNotFoundError(f"{f.original_name}: file missing on disk")
    dest = unique_dest(owner_dir(project_id, calib_set_id) / ft, src.name)   # reserved empty file
    os.replace(src, dest) if src.stat().st_dev == dest.stat().st_dev else shutil.move(src, dest)
    f.stored_path = str(dest.relative_to(DATA_DIR))
    if f.thumb_path:
        t = DATA_DIR / f.thumb_path
        nt = DATA_DIR / thumb_rel(f.stored_path)
        nt.parent.mkdir(parents=True, exist_ok=True)
        if t.exists():
            os.replace(t, nt)
        f.thumb_path = str(nt.relative_to(DATA_DIR))


def delete_file_storage(f: FrameFile, db=None):
    """Remove a file's data + thumbnail – but never a path another row still points to."""
    for col, rel in ((FrameFile.stored_path, f.stored_path), (FrameFile.thumb_path, f.thumb_path)):
        if not rel or rel.startswith("__"):
            continue
        if db is not None and db.scalar(select(func.count()).where(col == rel, FrameFile.id != f.id)):
            continue
        (DATA_DIR / rel).unlink(missing_ok=True)
    shutil.rmtree(DATA_DIR / ".cache" / str(f.id), ignore_errors=True)


# ---------- astronomy helpers ----------
def moon_illumination(d: date | None) -> float | None:
    """Approximate moon illumination (%) at local midnight of the given night."""
    if not d:
        return None
    ref = datetime(2000, 1, 6, 18, 14)              # known new moon (UTC)
    t = datetime(d.year, d.month, d.day, 23, 0)
    age = ((t - ref).total_seconds() / 86400) % 29.530588853
    return round((1 - math.cos(2 * math.pi * age / 29.530588853)) / 2 * 100, 0)


def night_of(dt: datetime | None) -> date | None:
    """Astronomical night: frames taken after midnight belong to the previous evening."""
    if not dt:
        return None
    return (dt.date() if dt.hour >= 12 else date.fromordinal(dt.date().toordinal() - 1))


def exposure_summary(files) -> dict:
    """Integration time of (non-rejected) light frames, grouped by filter."""
    groups = {}
    total = 0.0
    missing = 0
    for f in files:
        if f.frame_type != "light" or f.rejected:
            continue
        key = f.filter or "—"
        g = groups.setdefault(key, {"filter": key, "count": 0, "seconds": 0.0, "exps": {}})
        g["count"] += 1
        if f.exposure_s:
            g["seconds"] += f.exposure_s
            total += f.exposure_s
            g["exps"][f.exposure_s] = g["exps"].get(f.exposure_s, 0) + 1
        else:
            missing += 1
    rows = sorted(groups.values(), key=lambda g: -g["seconds"])
    for g in rows:
        g["detail"] = " + ".join(f"{n}×{fmt_num(e)}s" for e, n in sorted(g["exps"].items(), key=lambda x: -x[1]))
    return {"rows": rows, "total": total, "count": sum(g["count"] for g in rows), "missing": missing}
