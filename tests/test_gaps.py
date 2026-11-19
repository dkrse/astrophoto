"""Tests for paths the coverage report showed as untested, and for the speed / stability changes
(caches, memory guards, crash-loop protection)."""
import io
import json
import os
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select

from app import worker, integrity, analysis, platesolve, preview
from app.core import DATA_DIR, IMPORT_DIR, ARCHIVE_DIR
from app.models import SessionLocal, FrameFile, AnalysisTask, Project, CatalogItem, Setting
from conftest import drain, write_fits, upload, star_frame, default_session


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


# ---------- worker housekeeping ----------
def test_cleanup_cache_age_and_size_limits(monkeypatch):
    root = DATA_DIR / ".cache" / "999001"
    root.mkdir(parents=True, exist_ok=True)
    old, a, b = root / "old.bin", root / "a.bin", root / "b.bin"
    for p, n in ((old, 10), (a, 600), (b, 600)):
        p.write_bytes(b"x" * n)
    t_old = time.time() - (worker.CACHE_MAX_AGE_DAYS + 2) * 86400
    os.utime(old, (t_old, t_old))
    os.utime(a, (time.time() - 100, time.time() - 100))          # older of the two recent ones
    monkeypatch.setattr(worker, "CACHE_MAX_BYTES", 1000)
    worker.cleanup_cache()
    assert not old.exists()                                        # too old
    assert not a.exists() and b.exists()                           # over the size limit: oldest first
    b.unlink()
    worker.cleanup_cache()
    assert not root.exists()                                       # empty per-file folder removed


def test_cleanup_uploads_removes_stale_and_prunes_memory():
    from app import routes_upload
    up = DATA_DIR / ".uploads"
    up.mkdir(parents=True, exist_ok=True)
    stale, fresh = up / ("a" * 32 + ".part"), up / ("b" * 32 + ".part")
    stale.write_bytes(b"1")
    fresh.write_bytes(b"2")
    t = time.time() - 49 * 3600
    os.utime(stale, (t, t))
    routes_upload._HASH["c" * 32] = object()                       # upload whose files are gone
    worker.cleanup_uploads()
    assert not stale.exists() and fresh.exists() and "c" * 32 not in routes_upload._HASH
    fresh.unlink()


def test_worker_start_is_idempotent(monkeypatch):
    started = []
    monkeypatch.setattr(worker, "_started", False)
    monkeypatch.setattr(worker.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda self: started.append(kw["name"])})())
    worker.start()
    worker.start()
    assert started == ["astro-worker"]


def test_process_file_exception_marks_error(client, project, tmp_path, monkeypatch):
    upload(client, write_fits(tmp_path / "pe.fits", star_frame(seed=201)), project_id=project)

    def boom(db, ff, with_stats=True):
        raise RuntimeError("x" * 900)
    monkeypatch.setattr(worker, "process_file", boom)
    drain()
    f = files_of(project)[0]
    assert f.status == "error" and len(f.error) == 500


# ---------- crash-loop protection ----------
def test_interrupted_file_is_retried_once_then_error(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "cr.fits", star_frame(seed=202)), project_id=project)
    fid = files_of(project)[0].id
    with SessionLocal() as db:                       # as if the server died while processing it
        f = db.get(FrameFile, fid)
        f.status, f.error = "pending", worker.PROCESSING[0]
        db.commit()
    worker.cleanup_tasks()                           # = restart
    with SessionLocal() as db:
        f = db.get(FrameFile, fid)
        assert f.status == "pending" and f.error == worker.INTERRUPTED
        f.error = worker.PROCESSING[1]               # processing started again ... and died again
        db.commit()
    worker.cleanup_tasks()
    with SessionLocal() as db:
        f = db.get(FrameFile, fid)
        assert f.status == "error" and "interrupted twice" in f.error
    # ↻ Reprocess is the explicit retry
    assert client.post("/api/files/bulk", json={"ids": [fid], "action": "reprocess"}).status_code == 200
    drain()
    with SessionLocal() as db:
        f = db.get(FrameFile, fid)
        assert f.status == "done" and f.error is None


def test_processing_marker_never_shown_and_cleared(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "mk.fits", star_frame(seed=203)), project_id=project)
    fid = files_of(project)[0].id
    with SessionLocal() as db:
        db.get(FrameFile, fid).error = worker.PROCESSING[0]
        db.commit()
    assert "__processing__" not in client.get(f"/projects/{project}").text
    drain()
    assert files_of(project)[0].error is None


def test_interrupted_analysis_task_runs_once_more_then_error(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "at.fits", star_frame(seed=204)), project_id=project)
    drain()
    fid = files_of(project)[0].id
    with SessionLocal() as db:
        db.query(AnalysisTask).delete()
        db.add(AnalysisTask(file_id=fid, kind="solve", status="running"))
        db.commit()
    worker.cleanup_tasks()
    with SessionLocal() as db:
        t = db.scalars(select(AnalysisTask)).one()
        assert t.status == "queued" and t.error == worker.INTERRUPTED
        t.status = "running"
        db.commit()
    worker.cleanup_tasks()
    with SessionLocal() as db:
        t = db.scalars(select(AnalysisTask)).one()
        assert t.status == "error" and "Interrupted twice" in t.error
        db.query(AnalysisTask).delete()
        db.commit()


# ---------- plate solver lock ----------
def test_interactive_solve_does_not_wait_minutes_for_the_solver(client, project, tmp_path, monkeypatch):
    upload(client, write_fits(tmp_path / "lk.fits", star_frame(seed=205)), project_id=project)
    drain()
    f = files_of(project)[0]
    with SessionLocal() as db:
        db.get(FrameFile, f.id).solve = json.dumps({"ok": True, "scale_arcsec_px": 1.0})
        db.commit()
    monkeypatch.setattr(platesolve, "available", lambda: None)
    monkeypatch.setattr(platesolve, "LOCK_WAIT_INTERACTIVE_S", 0.2)
    platesolve._LOCK.acquire()                       # the worker is solving another frame
    try:
        t0 = time.time()
        r = client.post(f"/api/files/{f.id}/solve")
        assert r.status_code == 503 and r.json()["busy"] and time.time() - t0 < 5
    finally:
        platesolve._LOCK.release()
    assert json.loads(files_of(project)[0].solve)["ok"] is True       # stored result kept


def test_busy_solver_requeues_background_task(client, project, tmp_path, monkeypatch):
    upload(client, write_fits(tmp_path / "bq.fits", star_frame(seed=206)), project_id=project)
    drain()
    fid = files_of(project)[0].id
    before = files_of(project)[0].solve
    with SessionLocal() as db:
        db.query(AnalysisTask).delete()
        db.add(AnalysisTask(file_id=fid, kind="solve"))
        db.commit()
    monkeypatch.setattr(analysis, "solve_compute", lambda inp: {"ok": False, "busy": True, "error": "busy"})
    with SessionLocal() as db:
        assert worker.run_analysis(db) is False
    with SessionLocal() as db:
        assert db.scalars(select(AnalysisTask)).one().status == "queued"
        assert db.get(FrameFile, fid).solve == before                 # a busy result is never stored
        db.query(AnalysisTask).delete()
        db.commit()


def test_solve_time_budget_stops_retries(monkeypatch, tmp_path):
    runs = []

    def fake_run(path, fov, ra, dec):
        runs.append((fov, ra))
        return {"PLTSOLVD": "F", "ERROR": "no"}
    monkeypatch.setattr(platesolve, "_run", fake_run)
    monkeypatch.setattr(platesolve, "SOLVE_BUDGET_S", -1)          # budget already used up after the 1st run
    lum = np.zeros((20, 20), np.float32)
    res = platesolve._solve_locked(tmp_path / "x.fits", None, lum, 1, 20, 20, {}, 1, 3.76, 5.0, 10.0, 1.0, 500, 0.0)
    assert not res["ok"] and len(runs) == 1


# ---------- caches ----------
def test_optics_result_cached_per_file_version(client, project, tmp_path, monkeypatch):
    upload(client, write_fits(tmp_path / "oc.fits", star_frame(seed=207)), project_id=project)
    drain()
    fid = files_of(project)[0].id
    calls = []
    real = analysis.optics_compute

    def counting(inp, tiles=False):
        calls.append(1)
        return real(inp, tiles)
    monkeypatch.setattr(analysis, "optics_compute", counting)
    r1 = client.get(f"/api/files/{fid}/optics?tiles=0").json()
    r2 = client.get(f"/api/files/{fid}/optics?tiles=0").json()
    assert len(calls) == 1 and r1.get("fwhm_px") == r2.get("fwhm_px")
    client.get(f"/api/files/{fid}/optics?tiles=0&flip=v")             # other flip -> computed
    assert len(calls) == 2


def test_viewer_info_cached_and_header_read_once(client, project, tmp_path, monkeypatch):
    upload(client, write_fits(tmp_path / "vc.fits", star_frame(seed=208), ROWORDER="BOTTOM-UP"), project_id=project)
    drain()
    fid = files_of(project)[0].id
    from app import routes_files
    calls = []
    real = routes_files.file_info
    monkeypatch.setattr(routes_files, "file_info", lambda *a: calls.append(1) or real(*a))
    t1 = client.get(f"/files/{fid}/view").text
    client.get(f"/files/{fid}/view")
    assert len(calls) == 1 and "ROWORDER" in t1


def test_histogram_uses_render_slot(client, project, tmp_path, monkeypatch):
    upload(client, write_fits(tmp_path / "hs.fits", star_frame(seed=209)), project_id=project)
    drain()
    fid = files_of(project)[0].id
    from app import routes_files
    seen = []
    real = routes_files.slotted
    monkeypatch.setattr(routes_files, "slotted", lambda fn, *a: seen.append(fn.__name__) or real(fn, *a))
    assert client.get(f"/files/{fid}/histogram").status_code == 200
    assert "histogram" in seen


def test_small_thumbs_removed_with_file(client, project, tmp_path):
    from PIL import Image
    p = tmp_path / "st.png"
    Image.new("RGB", (80, 60), (200, 10, 10)).save(p)
    upload(client, p, "stack", project_id=project)
    drain()
    fid = files_of(project)[0].id
    assert client.get(f"/files/{fid}/thumb?size=s").status_code == 200
    cached = list((DATA_DIR / ".cache" / "thumbs").glob(f"{fid}_*"))
    assert cached
    client.post(f"/files/{fid}/delete")
    assert not any(c.exists() for c in cached)


def test_skymap_cache_is_bounded(client):
    from app import skymap
    for i in range(20):
        client.get(f"/api/skymap?mag={6 + i * 0.1:.1f}")
    assert len(skymap._CACHE) <= 9


# ---------- FITS loading ----------
def test_fits_bzero_unsigned_16bit_scaled_like_astropy(tmp_path):
    from astropy.io import fits
    p = tmp_path / "u16.fits"
    fits.PrimaryHDU(np.array([[0, 1000], [33768, 65535]], dtype=np.uint16)).writeto(p)   # stored with BZERO 32768
    with fits.open(p, do_not_scale_image_data=True) as hd:
        assert hd[0].header["BZERO"] == 32768
    data, header = preview.load_fits_raw(p)
    assert data.dtype == np.float32 and data.tolist() == [[0, 1000], [33768, 65535]] and "BZERO" not in header
    with fits.open(p) as hd:                                      # astropy's own scaling agrees
        assert np.array_equal(hd[0].data.astype(np.float32), data)


def test_fits_bscale_and_blank(tmp_path):
    from astropy.io import fits
    h = fits.Header()
    h["BLANK"] = -1
    p = tmp_path / "bl.fits"
    fits.PrimaryHDU(np.array([[5, -1], [7, 9]], dtype=np.int16), h).writeto(p)
    data, _ = preview.load_fits_raw(p)
    assert data[0, 0] == 5 and np.isnan(data[0, 1])
    p2 = tmp_path / "bs.fits"
    hd = fits.PrimaryHDU(np.array([[2, 4]], dtype=np.int16))
    hd.header["BSCALE"], hd.header["BZERO"] = 0.5, 10.0
    hd.writeto(p2)
    with fits.open(p2) as x:
        expect = x[0].data.astype(np.float32)
    data, _ = preview.load_fits_raw(p2)
    assert np.allclose(data, expect)


def test_fits_float_cube_planes(tmp_path):
    from astropy.io import fits
    p = tmp_path / "c.fits"
    fits.PrimaryHDU(np.ones((1, 3, 4, 5), np.float32)).writeto(p)
    data, _ = preview.load_fits_raw(p)
    assert data.shape == (3, 4, 5) and data.dtype == np.float32


# ---------- integrity ----------
def test_integrity_reports_missing_and_orphans_and_cancel(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "ig.fits", star_frame(seed=210)), project_id=project)
    drain()
    f = files_of(project)[0]
    (DATA_DIR / f.stored_path).unlink()
    orphan = ARCHIVE_DIR / "orphan_test.bin"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"123")
    try:
        integrity.state["cancel"] = False
        rep = integrity.run_check(False)
        assert any(m["id"] == f.id for m in rep["missing"]) and rep["ok"] is False
        assert any(o["path"].endswith("orphan_test.bin") for o in rep["orphans"])
        integrity.state["cancel"] = True
        rep = integrity.run_check(False)
        assert rep["cancelled"] and rep["ok"] is None and rep["counts"]["orphans"] == 0
    finally:
        integrity.state["cancel"] = False
        orphan.unlink()
    assert client.post("/api/integrity/cancel").json()["ok"]
    integrity.state["cancel"] = False


def test_integrity_last_report_broken_json():
    with SessionLocal() as db:
        db.merge(Setting(key=integrity.REPORT_KEY, value="{broken"))
        db.commit()
        assert integrity.last_report(db) is None
        db.query(Setting).filter(Setting.key == integrity.REPORT_KEY).delete()
        db.commit()


def test_integrity_thread_failure_resets_state(monkeypatch):
    monkeypatch.setattr(integrity, "run_check", lambda full: (_ for _ in ()).throw(RuntimeError("disk")))
    integrity.state["running"] = False
    assert integrity.start_check(False) is True
    for _ in range(100):
        if not integrity.state["running"]:
            break
        time.sleep(0.02)
    assert integrity.state["running"] is False
    assert integrity.start_check(False) is True                      # a new check can start
    for _ in range(100):
        if not integrity.state["running"]:
            break
        time.sleep(0.02)


# ---------- archive import helpers ----------
def test_path_night_invalid_dates_fall_through():
    from app.routes_archive import path_night
    assert path_night("2024-02-30/M31/light_001.fits") is None
    assert path_night("2024-02-30/2024-03-01/x.fits") == date(2024, 3, 1)
    assert path_night("x/Light_2024-03-16_01-30-00.fits") == date(2024, 3, 15)    # after midnight
    assert path_night("x/Light_2024-03-16_21-30-00.fits") == date(2024, 3, 16)


def test_guess_equipment():
    from types import SimpleNamespace as N
    from app.routes_archive import _guess_equipment
    cat = [N(name="Sky-Watcher Esprit 100"), N(name="ZWO ASI2600MC Pro")]
    assert _guess_equipment(cat, "ZWO ASI2600MC Pro") == "ZWO ASI2600MC Pro"
    assert _guess_equipment(cat, "esprit100") is None or _guess_equipment(cat, "Esprit 100") == "Sky-Watcher Esprit 100"
    assert _guess_equipment(cat, "ab") is None and _guess_equipment(cat, "Celestron") is None


def test_enough_space(monkeypatch, tmp_path):
    from app import routes_archive
    import shutil
    (tmp_path / "a.fits").write_bytes(b"x" * 1000)
    groups = [{"files": [{"rel": "a.fits"}, {"rel": "missing.fits"}]}]
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    assert routes_archive._enough_space(tmp_path, groups) is False
    monkeypatch.setattr(shutil, "disk_usage", lambda p: (_ for _ in ()).throw(OSError("nfs")))
    assert routes_archive._enough_space(tmp_path, groups) is True


# ---------- uploads / imports ----------
@pytest.mark.parametrize("body,msg", [
    ({"filename": "", "size": 10}, "filename"),
    ({"filename": "a.fits", "size": "x"}, "size"),
    ({"filename": "a.fits", "size": 0}, "size"),
    ({"filename": "a.fits", "size": 10, "frame_type": "bogus"}, "type"),
])
def test_upload_init_validation(client, project, body, msg):
    data = {"project_id": project, "session_id": default_session(project), "frame_type": "light", **body}
    r = client.post("/api/uploads", json=data)
    assert r.status_code == 400 and msg in r.text.lower()


def test_import_browse_errors(client):
    assert client.get("/api/import/browse?path=does/not/exist").status_code in (400, 404)
    assert client.get("/api/import/browse?path=../../etc").status_code in (400, 403, 404)


# ---------- export / ZIP ----------
def test_zip_duplicate_names_get_suffixes(client, tmp_path):
    pids = []
    for i in range(2):
        r = client.post("/projects/new", data={"name": f"Zip dup {i}", "confirm_dup": "1"}, follow_redirects=False)
        pids.append(int(r.headers["location"].rsplit("/", 1)[1]))
    try:
        for i, pid in enumerate(pids):
            upload(client, write_fits(tmp_path / str(i) / "same.fits", star_frame(seed=220 + i)), project_id=pid)
        drain()
        ids = [files_of(p)[0].id for p in pids]
        z = zipfile.ZipFile(io.BytesIO(client.post("/files/zip", data={"ids": ",".join(map(str, ids))}).content))
        assert sorted(z.namelist()) == ["light/same.fits", "light/same_1.fits"]
    finally:
        for p in pids:
            client.post(f"/projects/{p}/delete")


@pytest.mark.filterwarnings("ignore::UserWarning")      # the odd card is deliberate
def test_parse_header_text_fallback():
    from app.routes_projects import parse_header_text
    out = parse_header_text("NOT A FITS CARD = 12 / note\ngarbage line")      # odd text never crashes
    assert isinstance(out, list) and out
    ok = parse_header_text("EXPTIME =                 30.0 / seconds")
    assert ok[0]["key"] == "EXPTIME" and ok[0]["value"] == 30.0 and ok[0]["comment"] == "seconds"
    assert parse_header_text(None) == [] and parse_header_text("") == []


# ---------- search / manage ----------
def test_search_hits_header_once_and_counted(client, project, tmp_path):
    for i in range(3):
        upload(client, write_fits(tmp_path / f"sh{i}.fits", star_frame(seed=230 + i), OBSERVER="Zyxwv Tester"),
               project_id=project)
    drain()
    t = client.get("/?q=Zyxwv").text
    assert "FITS header" in t and "match in 3 files" in t


def test_manage_delete_unused_and_used(client, project):
    with SessionLocal() as db:
        unused = CatalogItem(kind="object_type", name="Audit unused type")
        used = CatalogItem(kind="object_type", name="Audit used type")
        db.add_all([unused, used])
        db.get(Project, project).object_type = "Audit used type"
        db.commit()
        u_id, x_id = unused.id, used.id
    r = client.post(f"/manage/items/{x_id}/delete", follow_redirects=True)
    assert "cannot be deleted" in r.text and "1 project" in r.text
    client.post(f"/manage/items/{u_id}/delete")
    with SessionLocal() as db:
        assert db.get(CatalogItem, u_id) is None and db.get(CatalogItem, x_id) is not None
        db.get(Project, project).object_type = None
        db.delete(db.get(CatalogItem, x_id))
        db.commit()


def test_bulk_move_stops_on_os_error(client, project, tmp_path, monkeypatch):
    for i in range(2):
        upload(client, write_fits(tmp_path / f"mo{i}.fits", star_frame(seed=240 + i)), project_id=project)
    drain()
    r = client.post("/projects/new", data={"name": "Move fail target", "confirm_dup": "1"}, follow_redirects=False)
    target = int(r.headers["location"].rsplit("/", 1)[1])
    from app import routes_files
    calls = []

    def flaky(f, pid, cid, ftype=None):
        calls.append(f.id)
        if len(calls) == 2:
            raise OSError("disk full")
    monkeypatch.setattr(routes_files, "move_file_storage", flaky)
    try:
        ids = [f.id for f in files_of(project)]
        r = client.post("/api/files/bulk", json={"ids": ids, "action": "move", "value": str(target)})
        assert r.status_code == 409 and "could not be moved" in r.json()["error"]
        assert len(files_of(target)) == 1                          # the first one was moved and kept
    finally:
        client.post(f"/projects/{target}/delete")


def test_overview_aggregates_without_loading_files(client, project, tmp_path):
    from PIL import Image
    upload(client, write_fits(tmp_path / "ag.fits", star_frame(seed=250), EXPTIME=120.0), project_id=project)
    p = tmp_path / "final.png"
    Image.new("RGB", (60, 40), (1, 2, 3)).save(p)
    upload(client, p, "stack", project_id=project)
    drain()
    stack_id = [f.id for f in files_of(project) if f.frame_type == "stack"][0]
    t = client.get("/?q=Test+project").text
    card = t.split(f'href="/projects/{project}"', 1)[1][:2500]
    assert "Light 1" in card and "Stack (final) 1" in card and "2m" in card
    assert f"/files/{stack_id}/thumb" in card                       # cover: the stack wins
