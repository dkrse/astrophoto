"""Test fixtures.

Tests run against a separate database (astrophoto_test, recreated for every run) and temporary
DATA_DIR / IMPORT_DIR folders, so production data is never touched.
"""
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import numpy as np
import pytest

# ---------- environment (must be set before the app is imported) ----------
_prod_url = os.environ.get("DATABASE_URL", "")
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or urlunparse(urlparse(_prod_url)._replace(path="/astrophoto_test"))
if urlparse(TEST_DB_URL).path.strip("/") == urlparse(_prod_url).path.strip("/"):
    sys.exit("Refusing to run tests against the production database")

_tmp = Path(tempfile.mkdtemp(prefix="astro-test-"))
os.environ.update(DATABASE_URL=TEST_DB_URL, DATA_DIR=str(_tmp / "data"), IMPORT_DIR=str(_tmp / "import"),
                  ASTRO_NO_WORKER="1", SECRET_KEY="test-secret")
(_tmp / "import").mkdir(parents=True)


def _recreate_db():
    import psycopg2
    u = urlparse(TEST_DB_URL)
    name = u.path.strip("/")
    admin = psycopg2.connect(urlunparse(u._replace(path="/postgres")))
    admin.autocommit = True
    with admin.cursor() as c:
        c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()",
                  (name,))
        c.execute(f'DROP DATABASE IF EXISTS "{name}"')
        c.execute(f'CREATE DATABASE "{name}"')
    admin.close()


_recreate_db()

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import worker  # noqa: E402
from app.core import DATA_DIR, IMPORT_DIR  # noqa: E402


def csrf_of(c):
    import re
    return re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").text).group(1)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        c.headers["X-CSRF-Token"] = csrf_of(c)           # like the browser: token from the page
        yield c


@pytest.fixture()
def anon():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def project(client):
    r = client.post("/projects/new", data={"name": "Test project", "target": "M42", "capture_date": "12-10-2025"},
                    follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    yield pid
    client.post(f"/projects/{pid}/delete")


def drain():
    """Run the background worker until there is nothing left to do."""
    for _ in range(500):
        if not worker.step():
            return
    raise AssertionError("worker did not finish")


# ---------- synthetic data ----------
def star_frame(h=300, w=400, sigma=1.6, seed=1, n=80):
    rng = np.random.default_rng(seed)
    img = np.full((h, w), 800.0)
    yy, xx = np.mgrid[0:h, 0:w]
    stars = np.random.default_rng(42)
    for _ in range(n):
        y, x, a = stars.uniform(15, h - 15), stars.uniform(15, w - 15), stars.uniform(2000, 15000)
        img += a * np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * sigma ** 2))
    return np.clip(rng.poisson(img), 0, 65535).astype(np.uint16)


def write_fits(path: Path, data=None, **header):
    from astropy.io import fits
    h = fits.Header()
    for k, v in header.items():
        h[k.replace("_", "-") if k in ("DATE_OBS", "CCD_TEMP") else k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(star_frame() if data is None else data, h).writeto(path, overwrite=True)
    return path


def default_session(pid):
    """Files always go into a session – use the project's first one (created if missing)."""
    from app.models import SessionLocal, ImagingSession
    with SessionLocal() as db:
        s = db.query(ImagingSession).filter_by(project_id=pid).order_by(ImagingSession.id).first()
        if not s:
            s = ImagingSession(project_id=pid, date=__import__("datetime").date(2025, 10, 12))
            db.add(s)
            db.commit()
        return s.id


def upload(client, path: Path, frame_type="light", **target):
    """Chunked upload through the real API (2 chunks)."""
    data = path.read_bytes()
    if target.get("project_id") and not target.get("session_id"):
        target["session_id"] = default_session(target["project_id"])
    r = client.post("/api/uploads", json={**target, "filename": path.name, "size": len(data),
                                          "frame_type": frame_type, "skip_dups": True})
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    half = len(data) // 2
    assert client.put(f"/api/uploads/{uid}?offset=0", content=data[:half]).json()["offset"] == half
    assert client.put(f"/api/uploads/{uid}?offset={half}", content=data[half:]).json()["offset"] == len(data)
    return client.post(f"/api/uploads/{uid}/finish").json()


@pytest.fixture()
def tmpdir_path(tmp_path):
    return tmp_path


__all__ = ["csrf_of", "drain", "write_fits", "star_frame", "upload", "DATA_DIR", "IMPORT_DIR"]
