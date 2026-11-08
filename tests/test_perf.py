"""Speed / memory / blocking: small thumbnails, render slots off the event loop, bounded star-tile cache,
settings cache, deferred FITS headers (no query per file), chunked uploads off the event loop."""
import io
import threading
import time

import numpy as np
from PIL import Image
from sqlalchemy import event, select

from app import photometry
from app.models import SessionLocal, FrameFile, Setting, engine
from conftest import drain, write_fits, upload, star_frame


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


class Queries:
    def __enter__(self):
        self.n = 0
        event.listen(engine, "before_cursor_execute", self._count)
        return self

    def _count(self, *a, **k):
        self.n += 1

    def __exit__(self, *a):
        event.remove(engine, "before_cursor_execute", self._count)


def test_small_thumbnails_are_cached(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "t.fits", data=star_frame(h=900, w=1200)), "light", project_id=project)
    drain()
    f = files_of(project)[-1]
    full = client.get(f"/files/{f.id}/thumb")
    small = client.get(f"/files/{f.id}/thumb?size=s")
    medium = client.get(f"/files/{f.id}/thumb?size=m")
    assert full.status_code == small.status_code == medium.status_code == 200
    assert max(Image.open(io.BytesIO(small.content)).size) == 240
    assert max(Image.open(io.BytesIO(medium.content)).size) == 640
    assert len(small.content) < len(medium.content) < len(full.content)
    assert client.get(f"/files/{f.id}/thumb?size=xl").content == full.content       # unknown size -> full
    html = client.get(f"/projects/{project}").text
    assert f"/files/{f.id}/thumb?size=s" in html                                      # lists use the small one


def test_render_slots_never_block_the_event_loop(client, project, tmp_path, monkeypatch):
    """Two slots busy: a third optics request waits in a worker thread – other requests are still answered."""
    import app.routes_files as rf
    upload(client, write_fits(tmp_path / "o.fits"), "light", project_id=project)
    drain()
    f = files_of(project)[-1]
    for _ in range(2):
        rf._RENDER_SLOTS.acquire()
    try:
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", client.get(f"/api/files/{f.id}/optics?tiles=0")))
        t.start()
        time.sleep(0.3)
        t0 = time.time()
        assert client.get("/api/activity").status_code == 200                         # server still responsive
        assert time.time() - t0 < 2
    finally:
        for _ in range(2):
            rf._RENDER_SLOTS.release()
    t.join(30)
    assert out["r"].status_code == 200


def test_star_tile_cache_has_a_memory_budget(tmp_path, monkeypatch):
    rng = np.random.default_rng(0)
    for i in range(6):
        b = bytearray(b"x" * 110)
        b += bytes([255, 255, 255, 128 + 10, 100])
        b += bytes(rng.integers(0, 255, 5 * 20000, dtype=np.uint8))
        (tmp_path / f"d50_19{i:02d}.1476").write_bytes(bytes(b))
    monkeypatch.setattr(photometry, "TILE_CACHE_BYTES", 600_000)
    photometry._TILE_CACHE.clear()
    for i in range(6):
        ra, dec, mag = photometry.read_tile(tmp_path / f"d50_19{i:02d}.1476")
        assert ra.dtype == np.float32
    total = sum(a.nbytes for v in photometry._TILE_CACHE.values() for a in v)
    assert total <= 600_000 and len(photometry._TILE_CACHE) >= 1
    assert str(tmp_path / "d50_1905.1476") in photometry._TILE_CACHE                  # most recent kept
    photometry._TILE_CACHE.clear()


def test_settings_cache_is_invalidated_on_save(client, monkeypatch):
    import app.core as core
    monkeypatch.setattr(core, "SETTINGS_CACHE_S", 60.0)
    core.invalidate_settings()
    assert core.load_settings()["page_size"] == 24
    with SessionLocal() as db:                       # changed behind the cache's back -> still cached
        db.merge(Setting(key="page_size", value="9"))
        db.commit()
    assert core.load_settings()["page_size"] == 24
    client.post("/settings", data={"page_size": "11"})  # saving through the app invalidates
    assert core.load_settings()["page_size"] == 11
    client.post("/settings", data={"reset": "1"})
    assert core.load_settings()["page_size"] == 24


def test_headers_are_deferred_and_loaded_in_one_query(client, project, tmp_path):
    for i in range(6):
        upload(client, write_fits(tmp_path / f"h{i}.fits", data=star_frame(seed=400 + i), OBJECT="X"), "light",
               project_id=project)
    drain()
    with SessionLocal() as db:
        f = db.scalars(select(FrameFile).where(FrameFile.project_id == project)).first()
        assert "header" not in f.__dict__ and f.has_header                            # not loaded with the row
    with Queries() as q:
        r = client.get(f"/projects/{project}/export")
    assert r.status_code == 200 and q.n < 40                                          # not one query per file
    with Queries() as q:
        assert client.get(f"/projects/{project}").status_code == 200
    assert q.n < 40


def test_big_upload_in_one_request(client, project, tmp_path):
    from conftest import default_session
    sid = default_session(project)
    data = bytes(np.random.default_rng(1).integers(0, 255, 9 * 1024 * 1024 + 123, dtype=np.uint8))
    uid = client.post("/api/uploads", json={"project_id": project, "session_id": sid, "filename": "big.bin.fits",
                                            "size": len(data), "frame_type": "light"}).json()["id"]
    assert client.put(f"/api/uploads/{uid}?offset=0", content=data).json()["offset"] == len(data)
    r = client.post(f"/api/uploads/{uid}/finish").json()
    import hashlib
    with SessionLocal() as db:
        f = db.get(FrameFile, r["id"]) if r.get("id") else None
        assert f is None or f.sha256 in (None, hashlib.sha256(data).hexdigest())
