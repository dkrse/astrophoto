"""Shared application objects: app, templates, settings, auth deps and helpers."""
import hashlib
import hmac
import math
import json
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
from sqlalchemy.exc import DataError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .models import (SessionLocal, User, Project, FrameFile, CatalogItem, Setting, CalibrationSet,
                     FRAME_TYPES, FRAME_LABELS, CATALOG_KINDS, CATALOG_SINGULAR, CALIB_TYPES, EQUIPMENT_SPECS)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
# everything worth backing up lives here (projects + calibration library); .cache / .uploads stay outside
ARCHIVE_DIR = DATA_DIR / "archive-data"
IMPORT_DIR = Path(os.environ.get("IMPORT_DIR", "/import"))
SECRET_KEY = os.environ.get("SECRET_KEY") or ""
if SECRET_KEY in ("", "change-me") or len(SECRET_KEY) < 16:
    print("WARNING: SECRET_KEY is missing or weak – set a long random value in .env "
          "(otherwise logins are lost on restart or session cookies can be forged)")
    SECRET_KEY = SECRET_KEY if len(SECRET_KEY) >= 16 else secrets.token_hex(32)
BASE = Path(__file__).parent

app = FastAPI(title="Astrophoto")


class CSRFMiddleware:
    """Every state-changing request of a logged-in user must carry the per-session token
    (header X-CSRF-Token from fetch(), or ?csrf=… added to form actions by base.html). A foreign site
    cannot read the token, so it cannot submit forms or call the API on the user's behalf.
    Pure ASGI (request bodies – e.g. upload chunks – are never read or buffered here)."""
    UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
    EXEMPT = {"/login"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in self.UNSAFE and scope["path"] not in self.EXEMPT:
            sess = scope.get("session") or {}
            if sess.get("uid"):                       # anonymous requests are rejected by the routes anyway
                from urllib.parse import parse_qs
                hdr = dict(scope.get("headers") or [])
                sent = hdr.get(b"x-csrf-token", b"").decode() or \
                    (parse_qs(scope.get("query_string", b"").decode()).get("csrf") or [""])[0]
                want = sess.get("csrf") or ""
                if not want or not hmac.compare_digest(sent, want):
                    from starlette.responses import JSONResponse, HTMLResponse
                    msg = "Security check failed (missing or expired form token) – reload the page and try again."
                    resp = JSONResponse({"error": msg}, status_code=403) if scope["path"].startswith("/api/") \
                        else HTMLResponse(f"<!doctype html><meta charset=utf-8><p>{msg}</p><p><a href='/'>Back</a></p>",
                                          status_code=403)
                    return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


app.add_middleware(CSRFMiddleware)          # added first = runs inside SessionMiddleware (session is decoded)
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
    v = str(v or "").strip()
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
                             FRAME_TYPES=FRAME_TYPES, FRAME_LABELS=FRAME_LABELS, CALIB_TYPES=CALIB_TYPES,
                             EQUIPMENT_SPECS=EQUIPMENT_SPECS)


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


@app.exception_handler(StarletteHTTPException)
def _http_error(request: Request, exc):
    """Browsers get a readable HTML page instead of bare JSON (e.g. a resubmitted form for a deleted item)."""
    from fastapi.responses import JSONResponse
    if request.url.path.startswith("/api/") or "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
    r = page(request, "error.html", code=exc.status_code, detail=exc.detail, user=None)
    r.status_code = exc.status_code
    return r


@app.exception_handler(DataError)
def _data_error(request: Request, exc):
    """Value longer than its column (or otherwise invalid for the DB) -> message instead of a 500."""
    from fastapi.responses import JSONResponse
    msg = "A value is too long or invalid – please shorten it and try again."
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": msg}, status_code=400)
    flash(request, msg, "err")
    return RedirectResponse(safe_referer(request), status_code=303)


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
    "sessions_page_size": (20, int, (0, 1000), "Sessions per page (project page; 0 = all)"),
    "files_page_size": (100, int, (0, 5000), "Files per page in each file group (0 = all)"),
    "calib_page_size": (25, int, (0, 1000), "Calibration sets per page (0 = all)"),
    "accent_color": ("#3b5bdb", "color", None, "Accent color"),
    "default_theme": ("auto", str, ["auto", "light", "dark"], "Default theme"),
    "default_view": ("grid", str, ["grid", "list"], "Default project view"),
    "card_size": ("medium", str, ["small", "medium", "large"], "Card size (grid)"),
    "sort": ("date_desc", str, ["date_desc", "date_asc", "name_asc", "name_desc", "created_desc", "size_desc"],
             "Default sort"),
    "show_stats": (True, bool, None, "Show statistics on overview"),
    "zip_compression": ("none", str, ["none", "fast", "best"], "ZIP compression"),
    "slideshow_seconds": (6, int, (2, 120), "Slideshow interval (s)"),
    "gallery_page_size": (24, int, (4, 500), "Images per page (gallery)"),
    "gallery_tile": ("medium", str, ["small", "medium", "large"], "Gallery tile size"),
    "gallery_sort": ("photo_desc", str, ["photo_desc", "photo_asc", "project_asc", "added_desc"], "Gallery sort"),
    "skip_duplicates": (True, bool, None, "Skip duplicate files on upload / import"),
    "files_view": ("list", str, ["list", "grid"], "File lists: default view"),
    "timezone": ("UTC", "tz", None, "Time zone of your observing site (FITS DATE-OBS is UTC – converted to this "
                                    "zone for capture times and the night a frame belongs to)"),
    # default expanded / collapsed state of panels
    "open_summary": (True, bool, None, "Overview: summary shown"),
    "open_filters": (False, bool, None, "Overview: filters expanded"),
    "open_sessions": (False, bool, None, "Project: Sessions panel expanded"),
    "open_file_groups": (True, bool, None, "File lists: type groups (Light, Dark …) expanded"),
    "open_stats_table": (False, bool, None, "Statistics: monthly table expanded"),
    # background processing (worker / background threads) – each can be switched off
    "bg_previews": (True, bool, None, "Generate thumbnails for new files"),
    "bg_frame_stats": (True, bool, None, "Measure frame quality of new light frames (FWHM, stars, background, noise)"),
    "bg_suspects": (True, bool, None, "Mark suspect light frames automatically"),
    "bg_analysis": (True, bool, None, "Run queued plate solves / optics analyses"),
    "bg_integrity": (True, bool, None, "Allow archive integrity checks"),
    "cond_auto_measure": (True, bool, None, "Measure seeing, transparency and sky brightness of new nights automatically"),
    "cond_auto_fill": (True, bool, None, "Fill in the session's Seeing and SQM from the measurements"),
    "sessions_auto_fix": (False, bool, None, "Keep session names and nights in line with their files"),
    # columns of the sessions table on the project page
    "session_columns": ("name,night,set,equipment,scale,moon,measured,files", "multi",
                        ["name", "night", "set", "equipment", "scale", "moon", "measured", "files"],
                        "Sessions table columns"),
}
SESSION_COLUMN_LABELS = {"name": "Name", "night": "Night", "set": "Set (exposure · gain · filter · camera temp.)",
                         "equipment": "Equipment", "scale": "Scale / FOV", "moon": "Moon",
                         "measured": "Measured (focal length, seeing, SQM …)", "files": "Files"}
templates.env.globals["SESSION_COLUMN_LABELS"] = SESSION_COLUMN_LABELS
SORT_LABELS = {"date_desc": "Last night (newest)", "date_asc": "First night (oldest)",
               "name_asc": "Name A–Z", "name_desc": "Name Z–A", "created_desc": "Recently added",
               "size_desc": "Size (largest)"}
templates.env.globals["SORT_LABELS"] = SORT_LABELS


def _timezones():
    from zoneinfo import available_timezones
    return ["UTC"] + sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/")))


templates.env.globals["TIMEZONES"] = _timezones()


def coerce_setting(k, v):
    default, typ, lim, _ = SETTINGS[k]
    try:
        if typ is int:
            return max(lim[0], min(lim[1], int(v)))
        if typ is bool:
            return str(v).lower() in ("1", "true", "on", "yes")
        if typ == "tz":
            from zoneinfo import ZoneInfo
            v = str(v).strip()
            ZoneInfo(v)                           # unknown zone -> exception -> default
            return v
        if typ == "multi":                        # comma list of allowed values, in their canonical order
            got = {x.strip() for x in (v if isinstance(v, (list, tuple)) else str(v).split(","))}
            return ",".join(x for x in lim if x in got)
        if typ == "color":
            v = str(v).strip()
            return v if re.fullmatch(r"#[0-9a-fA-F]{6}", v) else default
        v = str(v).strip()
        if isinstance(lim, list):
            return v if v in lim else default
        return v or default
    except (TypeError, ValueError, KeyError, OSError):
        return default


_SETTINGS_CACHE = {"at": 0.0, "value": None}
SETTINGS_CACHE_S = float(os.environ.get("SETTINGS_CACHE_S", "1.0"))   # the worker and every page read settings


def invalidate_settings():
    _SETTINGS_CACHE.update(at=0.0, value=None)


def load_settings(db=None) -> dict:
    import time as _t
    c = _SETTINGS_CACHE
    if c["value"] is not None and _t.monotonic() - c["at"] < SETTINGS_CACHE_S:
        return dict(c["value"])
    value = _load_settings(db)
    c.update(at=_t.monotonic(), value=value)
    return dict(value)


def _load_settings(db=None) -> dict:
    own = db is None
    db = db or SessionLocal()
    try:
        raw = {r.key: r.value for r in db.scalars(select(Setting).where(Setting.key.in_(list(SETTINGS))))}
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
    ctx["csrf"] = csrf_token(request)
    ctx.setdefault("q", "")
    return templates.TemplateResponse(request, name, ctx)


def csrf_token(request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(32)
    return tok


def flash(request, msg, kind="ok"):
    request.session["flash"] = {"msg": msg, "kind": kind}


# ---------- small helpers ----------
def none_if_empty(v):
    v = (v or "").strip() if isinstance(v, str) or v is None else str(v)
    return v or None


def to_int(v, default=None):
    """Lenient int parsing for user / JSON input: '12', 12, ' 12 ' -> 12; anything else -> default."""
    if isinstance(v, bool):
        return default
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def as_dict(v) -> dict:
    """Nested JSON value that must be an object (e.g. "meta") -> dict, anything else -> {}."""
    return v if isinstance(v, dict) else {}


JSON_MAX_BYTES = 8 * 1024 ** 2    # largest JSON request (20 000 file entries of an import fit easily)


async def json_body(request: Request) -> dict:
    """Request body as a JSON object; malformed JSON or a non-object (list, string…) -> 400 instead of a 500;
    larger than JSON_MAX_BYTES -> 413 (declared length, then the body read)."""
    if to_int(request.headers.get("content-length"), 0) > JSON_MAX_BYTES:
        raise HTTPException(413, "Request too large")
    body = await request.body()
    if len(body) > JSON_MAX_BYTES:                # chunked request without a length
        raise HTTPException(413, "Request too large")
    try:
        data = json.loads(body or b"null")
    except ValueError:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON object expected")
    return data


def safe_referer(request: Request, fallback: str = "/") -> str:
    """Referer for redirect-back, but only when it points into this app (no open redirect)."""
    back = request.headers.get("referer") or ""
    return back if back.startswith(str(request.base_url)) else fallback


def to_float(v):
    try:
        x = float(str(v).replace(",", ".")) if v not in (None, "") else None
    except ValueError:
        return None
    return x if x is None or math.isfinite(x) else None      # no NaN / inf in the database


def like_pattern(q: str) -> str:
    """User text -> ILIKE pattern that matches it literally (% and _ are not wildcards)."""
    q = " ".join((q or "").split())
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


NAME_MAX = 100        # catalog names are also stored in projects.object_type (VARCHAR 100)


def safe_name(name: str) -> str:
    name = os.path.basename(name or "file")
    name = re.sub(r"[^A-Za-z0-9._\-]+", "_", name).strip("._") or "file"
    if len(name) > 200:                          # shorten the stem, keep the extension (.fits, .cr2 …)
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            return stem[:200 - len(ext) - 1] + "." + ext
        return name[:200]
    return name


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


# ---------- equipment specs ----------
def parse_specs(kind, data) -> str | None:
    """Form/JSON values (keys spec_<key> or <key>) -> validated JSON string for CatalogItem.specs."""
    import json
    out = {}
    for key, _label, unit, choices in EQUIPMENT_SPECS.get(kind, []):
        v = data.get("spec_" + key, data.get(key))
        v = str(v).strip() if v is not None else ""
        if not v:
            continue
        if choices:
            if v in choices:
                out[key] = v
        else:
            n = to_float(v)
            if n is not None and 0 < n < 1e7:
                out[key] = int(n) if n.is_integer() else n
    return json.dumps(out) if out else None


def spec_summary(kind, spec: dict) -> str:
    """Short human text, e.g. '80 mm · 480 mm · f/6.0 · Refractor'."""
    parts = []
    for key, _label, unit, _ in EQUIPMENT_SPECS.get(kind, []):
        if key in spec and not (kind == "camera" and key in ("width_px", "height_px")):
            parts.append(f"{fmt_num(spec[key], 2)} {unit}" if unit else str(spec[key]))
    if kind == "telescope" and spec.get("aperture_mm") and spec.get("focal_mm"):
        parts.insert(2, f"f/{spec['focal_mm'] / spec['aperture_mm']:.1f}")
    if kind == "camera" and spec.get("width_px") and spec.get("height_px"):
        parts.insert(0, f"{spec['width_px']}×{spec['height_px']} px")
        if spec.get("pixel_um"):
            w, h = (spec[k] * spec["pixel_um"] / 1000 for k in ("width_px", "height_px"))
            parts.insert(2, f"{w:.1f}×{h:.1f} mm")
    return " · ".join(parts)


def optics(tel: dict, cam: dict) -> dict | None:
    """Image scale (″/px) and field of view (′) for a telescope + camera combination."""
    f, px = tel.get("focal_mm"), cam.get("pixel_um")
    if not f or not px:
        return None
    scale = 206.265 * px / f
    res = {"scale": scale, "fov": None}
    if cam.get("width_px") and cam.get("height_px"):
        res["fov"] = (cam["width_px"] * scale / 60, cam["height_px"] * scale / 60)
    return res


templates.env.globals.update(spec_summary=spec_summary)


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
    return ARCHIVE_DIR / "projects" / str(pid)


def calib_dir(cid: int) -> Path:
    return ARCHIVE_DIR / "calibration" / str(cid)


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
                "url": f"/projects/{f.project_id}", "col": FrameFile.project_id}
    c = f.calib_set
    if c is None:                                # orphan row (neither project nor set) -> 404, not a 500
        raise HTTPException(404, "File has no project or calibration set")
    return {"kind": "calib", "id": c.id, "name": c.name, "url": f"/calibration/{c.id}", "col": FrameFile.calib_set_id}


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
    try:
        os.replace(src, dest) if src.stat().st_dev == dest.stat().st_dev else shutil.move(src, dest)
    except BaseException:
        if src.exists():                          # original still in place: drop the placeholder / partial copy
            dest.unlink(missing_ok=True)
        raise
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
    delete_storage_paths(f.id, f.stored_path, f.thumb_path, db)


def delete_storage_paths(fid: int, stored_path: str | None, thumb_path: str | None, db=None):
    for col, rel in ((FrameFile.stored_path, stored_path), (FrameFile.thumb_path, thumb_path)):
        if not rel or rel.startswith("__"):
            continue
        if db is not None and db.scalar(select(func.count()).where(col == rel, FrameFile.id != fid)):
            continue
        (DATA_DIR / rel).unlink(missing_ok=True)
    shutil.rmtree(DATA_DIR / ".cache" / str(fid), ignore_errors=True)
    for p in (DATA_DIR / ".cache" / "thumbs").glob(f"{fid}_*"):      # cached small list thumbnails
        p.unlink(missing_ok=True)


# ---------- astronomy helpers ----------
def moon_illumination(d: date | None, tz: str | None = None) -> float | None:
    """Moon illumination (%) at 23:00 local time (site zone, Settings) of the given night.
    Meeus, Astronomical Algorithms ch. 48 (low precision): phase angle from the mean elongation and
    anomalies with the main periodic terms – within ~0.5 % instead of up to ±6 % of a plain synodic period."""
    if not d:
        return None
    from .preview import local_to_utc
    if tz is None:
        try:
            tz = load_settings()["timezone"]
        except Exception:
            tz = "UTC"
    t = local_to_utc(datetime(d.year, d.month, d.day, 23, 0), tz)
    jd = (t - datetime(2000, 1, 1, 12)).total_seconds() / 86400 + 2451545.0
    T = (jd - 2451545.0) / 36525
    r = math.radians
    D = r((297.8501921 + 445267.1114034 * T) % 360)          # mean elongation of the moon
    M = r((357.5291092 + 35999.0502909 * T) % 360)           # sun's mean anomaly
    Mp = r((134.9633964 + 477198.8675055 * T) % 360)         # moon's mean anomaly
    i = (180 - math.degrees(D) - 6.289 * math.sin(Mp) + 2.100 * math.sin(M) - 1.274 * math.sin(2 * D - Mp)
         - 0.658 * math.sin(2 * D) - 0.214 * math.sin(2 * Mp) - 0.110 * math.sin(D))
    return round((1 + math.cos(r(i))) / 2 * 100, 0)


def recompute_moon(db, tz: str | None = None) -> None:
    """Moon illumination of every session again (better formula / changed site time zone)."""
    from .models import ImagingSession
    for s in db.scalars(select(ImagingSession).where(ImagingSession.date.is_not(None))):
        s.moon_illum = moon_illumination(s.date, tz)


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


# ---------- static files with a content version (browsers never keep an outdated CSS / JS) ----------
STATIC_DIR = Path(__file__).parent / "static"
_ASSET_V: dict = {}


def asset(path: str) -> str:
    """'/static/<path>?v=<hash of the file>' – computed once per process (the image is rebuilt on every change)."""
    v = _ASSET_V.get(path)
    if v is None:
        import hashlib
        try:
            v = hashlib.sha1((STATIC_DIR / path).read_bytes()).hexdigest()[:10]
        except OSError:
            v = "0"
        _ASSET_V[path] = v
    return f"/static/{path}?v={v}"


templates.env.globals["asset"] = asset
from .sessets import set_label as _set_label  # noqa: E402
templates.env.globals["set_label"] = _set_label


def page_url(request, anchor: str = "", **changes) -> str:
    """Current URL with some query parameters changed (None removes one) – for pagers inside a page."""
    from urllib.parse import urlencode
    q = {k: v for k, v in request.query_params.items()}
    for k, v in changes.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = v
    return request.url.path + ("?" + urlencode(q) if q else "") + (f"#{anchor}" if anchor else "")


def paginate(total: int, per: int, page_no) -> dict:
    """Page window for lists rendered on the server; per = 0 shows everything."""
    try:
        page_no = int(page_no or 1)
    except (TypeError, ValueError):
        page_no = 1
    if not per or per <= 0 or total <= per:
        return {"page": 1, "pages": 1, "start": 0, "end": total, "total": total, "window": [1]}
    pages = -(-total // per)
    page_no = max(1, min(page_no, pages))
    return {"page": page_no, "pages": pages, "start": (page_no - 1) * per, "end": min(page_no * per, total),
            "total": total, "window": [n for n in range(1, pages + 1) if n in (1, pages) or abs(n - page_no) <= 2]}


templates.env.globals.update(page_url=page_url, paginate=paginate)


def _sqm_label(v):
    from .conditions import sqm_label
    return sqm_label(v)


templates.env.globals["sqm_label"] = _sqm_label


# ---------- column sorting of paginated lists (server side: sort everything, then page) ----------
_NATURAL = {"ok": None}


def natural(col):
    """ORDER BY expression for names: ICU numeric collation when available, else case-insensitive."""
    if _NATURAL["ok"] is None:
        from sqlalchemy import text
        try:
            with SessionLocal() as db:
                _NATURAL["ok"] = bool(db.scalar(text("SELECT count(*) FROM pg_collation WHERE collname = 'natsort'")))
        except Exception:
            _NATURAL["ok"] = False
    return col.collate("natsort") if _NATURAL["ok"] else func.lower(col)


def sort_param(request, param: str, allowed) -> tuple:
    """(column key, +1 / -1) from ?<param>=key&<param>_dir=asc|desc, or (None, 1) for the default order."""
    key = request.query_params.get(param) or ""
    if key not in allowed:
        return None, 1
    return key, (-1 if request.query_params.get(param + "_dir") == "desc" else 1)


def order_sql(expr, direction: int, *tiebreak):
    """SQL ORDER BY list: empty values always last, then a stable tie-breaker."""
    return [(expr.desc() if direction < 0 else expr.asc()).nulls_last(), *tiebreak]


def nat_key(v):
    """Python natural sort key: 'NGC 224' < 'NGC 3031', case-insensitive."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(v).lower())]


def sort_items(items: list, keyfn, direction: int) -> list:
    """Sort Python objects by keyfn (None = empty -> always last), stable."""
    have = [x for x in items if keyfn(x) is not None]
    none = [x for x in items if keyfn(x) is None]
    return sorted(have, key=keyfn, reverse=direction < 0) + none


templates.env.globals.update(sort_param=sort_param)


FILE_COLS = {"name": lambda f: nat_key(f.original_name), "size": lambda f: f.size,
             "added": lambda f: f.uploaded_at, "captured": lambda f: f.date_obs}


def sort_files(files: list, request, param: str) -> tuple:
    """A file group sorted by its column header (?o_<group>=name|size|added|captured), before paging."""
    col, d = sort_param(request, param, FILE_COLS)
    return (sort_items(files, FILE_COLS[col], d) if col else files), col, d


templates.env.globals.update(sort_files=sort_files)


def numeric_text(col):
    """'-10' / '120' / '2,5' stored as text -> number for ORDER BY (other text -> NULL, then sorted naturally)."""
    from sqlalchemy import case, cast, Float
    return case((col.op("~")(r"^\s*[-+]?\d+([.,]\d+)?\s*$"),
                 cast(func.replace(func.trim(col), ",", "."), Float)), else_=None)
