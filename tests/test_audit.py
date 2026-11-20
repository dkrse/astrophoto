"""Regression tests for the second code audit: input validation (no 500s on bad JSON), busy-file guards,
formatting / naming edge cases and worker robustness."""
import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app import core, platesolve, worker, analysis
from app.core import DATA_DIR, safe_name, to_int, as_dict, parse_dmy
from app.models import SessionLocal, FrameFile, AnalysisTask, Project
from conftest import drain, write_fits, upload, default_session, star_frame
from test_sky import fake_solve


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


def set_status(fid, status):
    with SessionLocal() as db:
        db.get(FrameFile, fid).status = status
        db.commit()


# ---------- pure helpers ----------
@pytest.mark.parametrize("deg,full,short", [
    (83.99999, "05h 36m 00.0s", "05h36.0m"),        # was "05h 35m 60.0s"
    (359.9999, "00h 00m 00.0s", "00h00.0m"),        # was "24h00.0m" in the short form
    (0, "00h 00m 00.0s", "00h00.0m"),
    (180.5, "12h 02m 00.0s", "12h02.0m"),
    (-15, "23h 00m 00.0s", "23h00.0m"),
])
def test_fmt_ra_never_shows_60_or_24h(deg, full, short):
    assert platesolve.fmt_ra(deg) == full
    assert platesolve.fmt_ra(deg, short=True) == short


@pytest.mark.parametrize("deg,full", [
    (-5.999999, "-06° 00′ 00.0″"),                  # was "-05° 59′ 60.0″"
    (45.5, "+45° 30′ 00.0″"),
    (-0.25, "-00° 15′ 00.0″"),
    (89.99999, "+90° 00′ 00.0″"),
])
def test_fmt_dec_rounds_carry(deg, full):
    assert platesolve.fmt_dec(deg) == full


def test_safe_name_keeps_extension_of_long_names():
    n = safe_name("x" * 230 + "_Light_0001.fits")
    assert len(n) <= 200 and n.endswith(".fits")
    assert safe_name("a" * 300) == "a" * 200          # no extension: plain cut
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("...") == "file"


def test_unique_dest_long_name_keeps_suffix(tmp_path):
    d1 = core.unique_dest(tmp_path, "y" * 250 + ".fit")
    d2 = core.unique_dest(tmp_path, "y" * 250 + ".fit")
    assert d1.suffix == d2.suffix == ".fit" and d1 != d2


def test_to_int_and_as_dict():
    assert to_int("12") == 12 and to_int(7) == 7 and to_int(" 3 ") == 3
    assert to_int("abc") is None and to_int(None, 0) == 0 and to_int([1]) is None and to_int(True) is None
    assert as_dict({"a": 1}) == {"a": 1} and as_dict([1]) == {} and as_dict("x") == {} and as_dict(None) == {}


def test_parse_dmy_accepts_non_strings():
    assert parse_dmy(20251012) is None              # number -> no crash
    assert parse_dmy("12-10-2025").isoformat() == "2025-10-12"
    assert parse_dmy(None) is None


def test_to_float_rejects_nan_inf_and_accepts_comma():
    assert core.to_float("1,5") == 1.5
    assert core.to_float("nan") is None and core.to_float("inf") is None and core.to_float("x") is None
    assert core.to_float("") is None and core.to_float(0) == 0.0


def test_like_pattern_is_literal():
    assert core.like_pattern("50%_a\\b") == "%50\\%\\_a\\\\b%"
    assert core.like_pattern("  a   b ") == "%a b%"


# ---------- API input validation: malformed bodies give 400, never 500 ----------
@pytest.mark.parametrize("url", ["/api/files/bulk", "/api/analysis/queue", "/api/uploads", "/api/import/start",
                                 "/api/catalog/telescope"])
@pytest.mark.parametrize("body", ["[1, 2]", '"text"', "{broken"])
def test_json_endpoints_reject_non_object(client, url, body):
    r = client.post(url, content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400, (url, body, r.status_code, r.text)


def test_analysis_queue_bad_ids(client):
    for data in ({"kind": "solve", "scope": "project", "project_id": "abc"},
                 {"kind": "solve", "scope": "session", "session_id": "x"},
                 {"kind": "solve", "scope": "all", "per_session": "lots"}):
        r = client.post("/api/analysis/queue", json=data)
        assert r.status_code == 200 and r.json()["ok"], r.text
    client.post("/api/analysis/cancel")


def test_analysis_cancel_bad_project_id(client):
    assert client.post("/api/analysis/cancel", json={"project_id": "abc"}).status_code == 400
    assert client.post("/api/analysis/cancel", json=["x"]).status_code == 400
    assert client.post("/api/analysis/cancel").json()["ok"]


def test_upload_ignores_filter_and_description_from_the_client(client, project, tmp_path):
    """Filter only from the file (header), description / note only in the i dialog."""
    p = write_fits(tmp_path / "m.fits", OBJECT="M42")
    data = p.read_bytes()
    r = client.post("/api/uploads", json={"project_id": project, "session_id": default_session(project),
                                          "filename": p.name, "size": len(data), "frame_type": "light",
                                          "meta": {"filter": "Ha", "description": "d"}})
    uid = r.json()["id"]
    client.put(f"/api/uploads/{uid}?offset=0", content=data)
    client.post(f"/api/uploads/{uid}/finish")
    drain()
    f = files_of(project)[0]
    assert (f.filter, f.description) == (None, None)
    page = client.get(f"/projects/{project}").text
    assert "up-filter" not in page and "up-desc" not in page and 'data-bulk="meta"' not in page and 'id="meta-dlg"' not in page
    assert client.post("/api/files/bulk", json={"ids": [f.id], "action": "meta",
                                                "value": {"note": "x"}}).status_code == 400


def test_upload_meta_must_be_object(client, project):
    r = client.post("/api/uploads", json={"project_id": project, "session_id": default_session(project),
                                          "filename": "a.fits", "size": 10, "frame_type": "light", "meta": [1]})
    assert r.status_code == 200, r.text
    client.delete(f"/api/uploads/{r.json()['id']}")


def test_catalog_numeric_name(client):
    r = client.post("/api/catalog/software", json={"name": 12345})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        from app.models import CatalogItem
        it = db.scalars(select(CatalogItem).where(CatalogItem.name == "12345")).first()
        assert it is not None
        iid = it.id
    assert client.put(f"/api/catalog/items/{iid}", json={"name": 99}).status_code == 200
    client.delete(f"/api/catalog/items/{iid}")


def test_reject_does_not_redirect_offsite(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "r.fits"), project_id=project)
    drain()
    fid = files_of(project)[0].id
    r = client.post(f"/files/{fid}/reject", headers={"referer": "https://evil.example/x"}, follow_redirects=False)
    assert r.status_code == 303 and "evil.example" not in r.headers["location"]
    r = client.post(f"/files/{fid}/reject", headers={"referer": "http://testserver/gallery"}, follow_redirects=False)
    assert r.headers["location"] == "http://testserver/gallery"


# ---------- busy files are protected ----------
def test_solve_and_optics_refuse_importing_file(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "i.fits"), project_id=project)
    drain()
    fid = files_of(project)[0].id
    set_status(fid, "importing")
    try:
        assert client.post(f"/api/files/{fid}/solve").status_code == 409
        assert client.get(f"/api/files/{fid}/optics").status_code == 409
    finally:
        set_status(fid, "done")


def test_delete_blocked_while_importing(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "p.fits"), project_id=project)
    drain()
    f = files_of(project)[0]
    set_status(f.id, "importing")
    try:
        client.post(f"/sessions/{f.session_id}/delete")
        client.post(f"/projects/{project}/delete")
        with SessionLocal() as db:
            assert db.get(Project, project) is not None and db.get(FrameFile, f.id) is not None
    finally:
        set_status(f.id, "done")


def test_session_delete_reflags_remaining_lights(client, project, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "reflag_project", lambda db, pid: calls.append(pid))
    upload(client, write_fits(tmp_path / "rf.fits"), project_id=project)
    drain()
    sid = files_of(project)[0].session_id
    calls.clear()                                 # processing reflags too – only the delete counts here
    client.post(f"/sessions/{sid}/delete")
    assert calls == [project]


def test_calib_zip_skips_importing(client, tmp_path):
    import io
    import zipfile
    r = client.post("/calibration/new", data={"frame_type": "flat", "capture_date": "10-10-2025"},
                    follow_redirects=False)
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    for i, n in enumerate(("a", "b")):
        upload(client, write_fits(tmp_path / f"{n}.fits", star_frame(seed=i + 5), IMAGETYP="Flat"),
               frame_type="flat", calib_set_id=cid)
    drain()
    with SessionLocal() as db:
        fids = db.scalars(select(FrameFile.id).where(FrameFile.calib_set_id == cid).order_by(FrameFile.id)).all()
    set_status(fids[1], "importing")
    try:
        z = zipfile.ZipFile(io.BytesIO(client.get(f"/calibration/{cid}/zip").content))
        assert len(z.namelist()) == 1
    finally:
        set_status(fids[1], "done")
        client.post(f"/calibration/{cid}/delete")


def test_file_without_owner_is_404(client):
    with SessionLocal() as db:
        f = FrameFile(frame_type="light", original_name="orphan.fits", stored_path="nowhere/orphan.fits",
                      size=0, status="done")
        db.add(f)
        db.commit()
        fid = f.id
    try:
        assert client.get(f"/files/{fid}/view").status_code == 404
    finally:
        with SessionLocal() as db:
            db.delete(db.get(FrameFile, fid))
            db.commit()


# ---------- sky map / worker ----------
def test_skymap_uses_latest_successful_solve(client, project, tmp_path):
    from app.skymap import project_fields
    for i, n in enumerate(("s1", "s2")):
        upload(client, write_fits(tmp_path / f"{n}.fits", star_frame(seed=i + 20)), project_id=project)
    drain()
    a, b = files_of(project)
    with SessionLocal() as db:
        db.get(FrameFile, a.id).solve = json.dumps(fake_solve())
        db.get(FrameFile, b.id).solve = json.dumps({"ok": False, "error": "no stars"})   # newer, failed
        db.commit()
        p = next(x for x in project_fields(db) if x["id"] == project)
    assert [fl["file"] for fl in p["fields"]] == [a.id]


def test_analysis_input_error_does_not_block_queue(client, project, tmp_path, monkeypatch):
    upload(client, write_fits(tmp_path / "q.fits"), project_id=project)
    drain()
    fid = files_of(project)[0].id
    with SessionLocal() as db:
        db.query(AnalysisTask).delete()
        db.add(AnalysisTask(file_id=fid, kind="solve"))
        db.commit()

    def boom(db, f):
        raise RuntimeError("inputs broken")
    monkeypatch.setattr(analysis, "solve_inputs", boom)
    with SessionLocal() as db:
        assert worker.run_analysis(db) is True
    with SessionLocal() as db:
        t = db.scalars(select(AnalysisTask).where(AnalysisTask.file_id == fid)).one()
        assert t.status == "error" and "inputs broken" in t.error
        db.delete(t)
        db.commit()


def test_cleanup_tasks_housekeeping_does_not_requeue_running(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "c.fits"), project_id=project)
    drain()
    fid = files_of(project)[0].id
    with SessionLocal() as db:
        db.query(AnalysisTask).delete()
        db.add_all([AnalysisTask(file_id=fid, kind="solve", status="running"),
                    AnalysisTask(file_id=fid, kind="optics", status="done",
                                 finished_at=datetime.now() - timedelta(days=2))])
        db.commit()
    worker.cleanup_tasks(requeue=False)
    with SessionLocal() as db:
        assert [t.status for t in db.scalars(select(AnalysisTask))] == ["running"]
    worker.cleanup_tasks()
    with SessionLocal() as db:
        assert [t.status for t in db.scalars(select(AnalysisTask))] == ["queued"]
        db.query(AnalysisTask).delete()
        db.commit()




def test_session_files_collapsed_and_accordion(client, project):
    default_session(project)
    t = client.get(f"/projects/{project}").text
    import re
    blocks = re.findall(r'<details class="sessblock sesspick" id="s\d+_"[^>]*>', t)
    assert blocks and all(" hidden" in b and " open" not in b for b in blocks)   # nothing listed until clicked
    assert 'id="sess-hint"' in t and "function showSession" in t


# ---------- weakly covered routes ----------
@pytest.mark.parametrize("form,msg", [
    ({"old": "wrong", "new": "abcd", "new2": "abcd"}, "Current password is wrong"),
    ({"old": "admin", "new": "abcd", "new2": "abce"}, "New passwords do not match"),
    ({"old": "admin", "new": "abc", "new2": "abc"}, "at least 4 characters")])
def test_password_change_validation(client, form, msg):
    r = client.post("/password", data=form, follow_redirects=True)
    assert msg in r.text
    from app.core import check_pw
    from app.models import User
    with SessionLocal() as db:
        assert check_pw("admin", db.scalars(select(User).where(User.username == "admin")).one().password_hash)


def test_equipment_focal(client):
    from app.models import CatalogItem
    with SessionLocal() as db:
        db.add(CatalogItem(kind="telescope", name="Audit scope", specs=json.dumps({"aperture_mm": 80, "focal_mm": 480})))
        db.commit()
    try:
        for bad in ({"telescope": "Audit scope", "focal_mm": "5"}, {"telescope": "", "focal_mm": "500"},
                    {"telescope": "Audit scope", "focal_mm": "abc"}):
            assert client.post("/api/equipment/focal", json=bad).status_code == 400
        assert client.post("/api/equipment/focal", json={"telescope": "Nope", "focal_mm": 500}).status_code == 404
        j = client.post("/api/equipment/focal", json={"telescope": "Audit scope", "focal_mm": "471.6"}).json()
        assert j == {"ok": True, "telescope": "Audit scope", "old": 480, "focal_mm": 472}
        with SessionLocal() as db:
            it = db.scalars(select(CatalogItem).where(CatalogItem.name == "Audit scope")).one()
            assert it.spec == {"aperture_mm": 80, "focal_mm": 472}
    finally:
        with SessionLocal() as db:
            db.query(CatalogItem).filter(CatalogItem.name == "Audit scope").delete()
            db.commit()


def test_export_all_projects_is_zip(client, project):
    import io
    import zipfile
    r = client.get("/export/projects")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.namelist() and all(not n.startswith(("/", "..")) for n in z.namelist())


# ---------- time zone of the observing site ----------
def test_header_date_converts_utc_to_site_zone():
    from app.preview import header_date, utc_to_local
    h = {"DATE-OBS": "2025-07-12T22:30:00"}.get
    assert header_date(h, "UTC") == datetime(2025, 7, 12, 22, 30)
    assert header_date(h, "Europe/Bratislava") == datetime(2025, 7, 13, 0, 30)       # CEST = UTC+2
    assert header_date({"DATE-OBS": "2025-01-12T22:30:00"}.get, "Europe/Bratislava") == datetime(2025, 1, 12, 23, 30)
    # DATE-LOC is already local and wins
    assert header_date({"DATE-OBS": "2025-07-12T22:30:00", "DATE-LOC": "2025-07-13T00:30:00"}.get,
                       "Asia/Tokyo") == datetime(2025, 7, 13, 0, 30)
    assert utc_to_local(None, "Asia/Tokyo") is None and utc_to_local(datetime(2025, 1, 1), "Nope/Zone") == datetime(2025, 1, 1)


def test_night_correct_for_far_east_and_west():
    """Japan 20:00 local = 11:00 UTC: with the site zone it is the evening of the same day (was previous night)."""
    from app.preview import header_date
    from app.core import night_of
    jp = header_date({"DATE-OBS": "2025-10-12T11:00:00"}.get, "Asia/Tokyo")
    assert night_of(jp) == date(2025, 10, 12)
    us = header_date({"DATE-OBS": "2025-10-13T12:30:00"}.get, "America/Los_Angeles")   # 05:30 local
    assert night_of(us) == date(2025, 10, 12)


def test_timezone_setting_validation_and_recompute(client, project, tmp_path):
    from app.core import coerce_setting
    assert coerce_setting("timezone", "Europe/Bratislava") == "Europe/Bratislava"
    assert coerce_setting("timezone", "Mars/Olympus") == "UTC"
    upload(client, write_fits(tmp_path / "tz.fits", DATE_OBS="2025-07-12T22:30:00"), project_id=project)
    drain()
    assert files_of(project)[0].date_obs == datetime(2025, 7, 12, 22, 30)
    try:
        r = client.post("/settings", data={"timezone": "Mars/Olympus"}, follow_redirects=True)
        assert "Unknown time zone" in r.text
        r = client.post("/settings", data={"timezone": "Europe/Bratislava"}, follow_redirects=True)
        assert "recomputed" in r.text
        assert files_of(project)[0].date_obs == datetime(2025, 7, 13, 0, 30)
        # new uploads use the zone right away
        upload(client, write_fits(tmp_path / "tz2.fits", star_frame(seed=77), DATE_OBS="2025-07-12T20:00:00"),
               project_id=project)
        drain()
        assert files_of(project)[1].date_obs == datetime(2025, 7, 12, 22, 0)
    finally:
        client.post("/settings", data={"timezone": "UTC"})
        client.post("/settings", data={"reset": "1"})          # a partial form switches checkboxes off
    assert files_of(project)[0].date_obs == datetime(2025, 7, 12, 22, 30)
    assert 'id="tz-list"' in client.get("/settings").text


# ---------- uploads: finish / cancel vs. chunk writes ----------
def _init_upload(client, project, size=10):
    r = client.post("/api/uploads", json={"project_id": project, "session_id": default_session(project),
                                          "filename": "race.fits", "size": size, "frame_type": "light"})
    return r.json()["id"]


def test_chunk_after_cancel_is_410(client, project):
    uid = _init_upload(client, project)
    assert client.delete(f"/api/uploads/{uid}").json()["ok"]
    r = client.put(f"/api/uploads/{uid}?offset=0", content=b"0123456789")
    assert r.status_code in (404, 410)
    from app.core import DATA_DIR
    assert not (DATA_DIR / ".uploads" / f"{uid}.part").exists()


def test_chunk_while_finishing_is_rejected(client, project):
    from app import routes_upload
    uid = _init_upload(client, project)
    routes_upload._FINISHING.add(uid)
    try:
        assert client.put(f"/api/uploads/{uid}?offset=0", content=b"0123456789").status_code == 410
    finally:
        routes_upload._FINISHING.discard(uid)
    client.delete(f"/api/uploads/{uid}")


# ---------- login ----------
def test_login_requires_form_token(anon):
    from app.routes_main import _FAILS
    _FAILS.clear()
    r = anon.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    assert "form expired" in r.text and "Sign in" in r.text
    r = anon.post("/login", data={"login_csrf": "forged", "username": "admin", "password": "admin"},
                  follow_redirects=False)
    assert r.headers["location"] == "/login"
    from conftest import login_token
    r = anon.post("/login", data={"login_csrf": login_token(anon), "username": "admin", "password": "admin"},
                  follow_redirects=False)
    assert r.headers["location"] == "/"


def test_login_failures_are_pruned():
    import time
    from app.routes_main import _FAILS, LOCK_S
    _FAILS.clear()
    _FAILS["10.0.0.1"] = (3, time.time() - LOCK_S - 5)
    from fastapi.testclient import TestClient
    from app.main import app
    from conftest import login_token
    with TestClient(app) as c:
        c.post("/login", data={"login_csrf": login_token(c), "username": "admin", "password": "wrong"})
    assert "10.0.0.1" not in _FAILS
    _FAILS.clear()


# ---------- misc fixes ----------
def test_cover_file_cleared_when_file_deleted(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "cv.fits"), project_id=project)
    drain()
    fid = files_of(project)[0].id
    client.post(f"/projects/{project}/cover", data={"file_id": str(fid)})
    with SessionLocal() as db:
        assert db.get(Project, project).cover_file_id == fid
    client.post(f"/files/{fid}/delete")
    with SessionLocal() as db:
        assert db.get(Project, project).cover_file_id is None             # FK ON DELETE SET NULL


def test_session_with_bad_date_creates_nothing(client, project):
    from app.models import ImagingSession
    with SessionLocal() as db:
        before = db.query(ImagingSession).filter_by(project_id=project).count()
    client.post(f"/projects/{project}/sessions", data={"date": "31-02-2025"})
    with SessionLocal() as db:
        assert db.query(ImagingSession).filter_by(project_id=project).count() == before


def test_slides_keep_requested_order(client, project, tmp_path):
    from PIL import Image
    for i in range(3):
        Image.new("RGB", (60, 40), (10 + i * 40, 20, 120)).save(tmp_path / f"sl{i}.png")
        upload(client, tmp_path / f"sl{i}.png", "stack", project_id=project)
    drain()
    ids = [f.id for f in files_of(project)][::-1]
    r = client.get("/api/slides?ids=" + ",".join(map(str, ids + [999999])))
    got = [s["id"] for s in r.json()]
    assert got == ids


def test_resize_note_for_larger_image():
    res = {"image_px": [8000, 3000], "focal_mm": 500.0, "scale_arcsec_px": 1.0}
    platesolve.resize_check(res, (6000, 4000), None)
    assert "larger than the camera sensor" in res["note"] and res["focal_mm"] == 500.0


def test_files_view_switch(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "fv.fits"), project_id=project)
    drain()
    t = client.get(f"/projects/{project}").text
    assert "setFview('grid')" in t and "setFview('list')" in t and "dataset.fview" in t
    try:
        client.post("/settings", data={"files_view": "grid"})
        t = client.get(f"/projects/{project}").text
        assert 'var fd="grid"' in t and 'value="grid" selected' in client.get("/settings").text
    finally:
        client.post("/settings", data={"reset": "1"})



# ---------- moon / photometry cache / RA hint ----------
def test_moon_illumination_meeus_and_timezone():
    from app.core import moon_illumination
    assert moon_illumination(date(2025, 10, 6), "UTC") >= 98          # full moon 07-10-2025 03:48 UTC
    assert moon_illumination(date(2025, 10, 21), "UTC") <= 1          # new moon 21-10-2025 12:25 UTC
    assert 45 <= moon_illumination(date(2025, 10, 29), "UTC") <= 65   # first quarter 29-10-2025 16:21 UTC
    # 23:00 local differs by 17 h between Tokyo and Los Angeles – a few days before full moon that is visible
    assert moon_illumination(date(2025, 10, 2), "Asia/Tokyo") < moon_illumination(date(2025, 10, 2), "America/Los_Angeles")
    assert moon_illumination(None) is None


def test_zp_reference_invalidated_by_new_solve(client, project, tmp_path):
    from app import measured
    upload(client, write_fits(tmp_path / "zp.fits"), project_id=project)
    drain()
    fid = files_of(project)[0].id
    with SessionLocal() as db:
        measured.zp_reference(db)                       # cached (60 s)
        f = db.get(FrameFile, fid)
        analysis.solve_store(f, fake_solve(sky={"zp_s": 21.5, "gain": "100"}))
        db.commit()
        ref = measured.zp_reference(db)                 # must see the new solve right away
    assert any(abs(v - 21.5) < 1e-9 for v in ref.values()), ref


@pytest.mark.parametrize("h,out", [
    ({"RA": "150.0", "DEC": "-20.5"}, (10.0, -20.5)),                              # degrees (standard)
    ({"RA": "05:35:17", "DEC": "-05:23:28"}, (5 + 35 / 60 + 17 / 3600, -(5 + 23 / 60 + 28 / 3600))),
    ({"RA": "5.5876", "DEC": "-5.39", "OBJCTRA": "05 35 17", "OBJCTDEC": "-05 23 28"},   # RA in hours: OBJCTRA wins
     (5 + 35 / 60 + 17 / 3600, -(5 + 23 / 60 + 28 / 3600))),
    ({"RA": "150.0"}, (None, None)),
    ({"OBJCTRA": "05 35", "OBJCTDEC": "x"}, (None, None)),
])
def test_ra_dec_hint_variants(h, out):
    ra, dec = platesolve._ra_dec_hint(h)
    if out[0] is None:
        assert (ra, dec) == out
    else:
        assert ra == pytest.approx(out[0]) and dec == pytest.approx(out[1])


# ---------- viewer file-details dialog, no save buttons; projects list table ----------
def test_viewer_has_file_info_dialog_and_no_save_buttons(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "vi.fits", FILTER="Ha"), project_id=project)
    drain()
    f = files_of(project)[0]
    t = client.get(f"/files/{f.id}/view").text
    assert 'id="info-dlg"' in t and 'onclick="fileInfo(this)"' in t and "function fileInfo" in t
    assert f'"id": {f.id}' in t and '"filter": "Ha"' in t                  # same data as in the file list
    assert 'data-fi="session"' in t and 'data-fi="type"' in t
    assert "data-save" not in t and "💾" not in t and 'id="saveform"' not in t
    lst = client.get(f"/projects/{project}").text
    assert lst.count('id="info-dlg"') == 1 and 'data-bulk="info"' in lst


def test_calibration_viewer_info_dialog(client, tmp_path):
    r = client.post("/calibration/new", data={"frame_type": "dark", "capture_date": "10-10-2025"},
                    follow_redirects=False)
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    upload(client, write_fits(tmp_path / "cd.fits", star_frame(seed=90), IMAGETYP="Dark"), frame_type="dark",
           calib_set_id=cid)
    drain()
    with SessionLocal() as db:
        fid = db.scalars(select(FrameFile.id).where(FrameFile.calib_set_id == cid)).first()
    t = client.get(f"/files/{fid}/view").text
    assert 'id="info-dlg"' in t and 'data-fi="session"' not in t
    client.post(f"/calibration/{cid}/delete")


def test_projects_overview_list_is_a_table(client, project):
    t = client.get("/").text
    tbl = t.split('id="plist"', 1)[1].split("</table>", 1)[0]
    assert '<table class="kvt"' in tbl and ">Project</th>" in tbl and f'href="/projects/{project}"' in tbl
    assert 'class="tablewrap card" id="plist"' in t and "<img" not in tbl          # same as the other lists


def test_file_rows_have_no_action_buttons_bulk_bar_has_them(client, project, tmp_path):
    import io
    import re
    import zipfile
    for i in range(2):
        upload(client, write_fits(tmp_path / f"ab{i}.fits", star_frame(seed=60 + i)), project_id=project)
    drain()
    a, b = files_of(project)
    t = client.get(f"/projects/{project}").text
    rows = re.findall(r'<tr class="[^"]*" data-id="\d+".*?</tr>', t, re.S)
    assert len(rows) == 2
    for r in rows:                                  # info lives on the row, buttons only in the bulk bar
        assert "data-info=" in r and "/download" not in r and "/reject" not in r and "/delete" not in r
        assert 'class="btn' not in r
    for act in ("info", "download", "rejecttoggle", "delete"):
        assert f'data-bulk="{act}"' in t
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/files/zip?ids={a.id},{b.id},abc").content))
    assert sorted(z.namelist()) == ["light/ab0.fits", "light/ab1.fits"]
    assert client.get("/files/zip?ids=").status_code == 404


# ---------- third review: moves, deletes, nights, pages ----------
def _calib_set(client, ftype="dark"):
    r = client.post("/calibration/new", data={"frame_type": ftype, "capture_date": "10-10-2025"}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_move_lights_to_calibration_set_refused(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "ml.fits", star_frame(seed=101)), project_id=project)
    drain()
    f = files_of(project)[0]
    cid = _calib_set(client)
    try:
        r = client.post("/api/files/bulk", json={"ids": [f.id], "action": "move", "value": f"calib:{cid}"})
        assert r.status_code == 400 and "not calibration frames" in r.json()["error"]
        assert files_of(project)[0].project_id == project
        # a dark may move there
        client.post("/api/files/bulk", json={"ids": [f.id], "action": "type", "value": "dark"})
        assert client.post("/api/files/bulk", json={"ids": [f.id], "action": "move",
                                                    "value": f"calib:{cid}"}).status_code == 200
        # inside a calibration set only calibration types
        r = client.post("/api/files/bulk", json={"ids": [f.id], "action": "type", "value": "light"})
        assert r.status_code == 400
    finally:
        client.post(f"/calibration/{cid}/delete")


def test_upload_of_light_into_calibration_set_refused(client):
    cid = _calib_set(client)
    try:
        r = client.post("/api/uploads", json={"calib_set_id": cid, "filename": "l.fits", "size": 10,
                                              "frame_type": "light"})
        assert r.status_code == 400
    finally:
        client.post(f"/calibration/{cid}/delete")


def test_move_to_project_uses_the_frames_night(client, project, tmp_path):
    from app.models import ImagingSession
    r = client.post("/projects/new", data={"name": "Move target", "target": "M45"}, follow_redirects=False)
    target = int(r.headers["location"].rsplit("/", 1)[1])
    try:
        with SessionLocal() as db:
            s_old = ImagingSession(project_id=target, date=date(2025, 3, 1))
            s_new = ImagingSession(project_id=target, date=date(2025, 10, 10))
            db.add_all([s_old, s_new])
            db.commit()
            old_id, new_id = s_old.id, s_new.id
        upload(client, write_fits(tmp_path / "mv1.fits", star_frame(seed=111), DATE_OBS="2025-03-01T22:00:00"),
               project_id=project)
        upload(client, write_fits(tmp_path / "mv2.fits", star_frame(seed=112), DATE_OBS="2024-01-01T22:00:00"),
               project_id=project)
        drain()
        a, b = files_of(project)
        assert client.post("/api/files/bulk", json={"ids": [a.id, b.id], "action": "move",
                                                    "value": str(target)}).status_code == 200
        with SessionLocal() as db:
            assert db.get(FrameFile, a.id).session_id == old_id          # its own night
            assert db.get(FrameFile, b.id).session_id == new_id          # no such night -> the latest session
    finally:
        client.post(f"/projects/{target}/delete")


def test_delete_files_removes_disk_data_only_after_commit(client, project, tmp_path, monkeypatch):
    from app.core import DATA_DIR
    upload(client, write_fits(tmp_path / "dc.fits", star_frame(seed=121)), project_id=project)
    drain()
    f = files_of(project)[0]
    path = DATA_DIR / f.stored_path
    assert path.exists()
    from sqlalchemy.orm import Session as _S
    orig = _S.commit

    def failing(self):
        raise RuntimeError("db down")
    monkeypatch.setattr(_S, "commit", failing)
    with pytest.raises(RuntimeError):
        from app.routes_files import delete_files
        with SessionLocal() as db:
            delete_files(db, [db.get(FrameFile, f.id)])
    monkeypatch.setattr(_S, "commit", orig)
    assert path.exists()                                     # commit failed -> data still on disk
    with SessionLocal() as db:
        assert db.get(FrameFile, f.id) is not None


def test_overview_size_filter_overflow(client):
    for v in ("inf", "1e400", "-inf", "nan", "abc"):
        assert client.get(f"/?size_from={v}&size_to={v}").status_code == 200


def test_orphan_thumbnail_removed_when_row_deleted_while_processing(client, project, tmp_path, monkeypatch):
    from app.core import DATA_DIR
    upload(client, write_fits(tmp_path / "ot.fits", star_frame(seed=131)), project_id=project)
    with SessionLocal() as db:                               # not processed yet
        fid = db.scalars(select(FrameFile.id).where(FrameFile.project_id == project)).one()
    real = worker.compute_preview
    made = {}

    def compute_then_delete(*a, **k):
        res = real(*a, **k)
        made["thumb"] = res and res.get("thumb")
        with SessionLocal() as db2:                          # the user deletes the file meanwhile
            db2.delete(db2.get(FrameFile, fid))
            db2.commit()
        return res
    monkeypatch.setattr(worker, "compute_preview", compute_then_delete)
    drain()
    assert made.get("thumb") and not (DATA_DIR / made["thumb"]).exists()


def test_timezone_change_moves_frame_to_its_new_night(client, project, tmp_path):
    from app.models import ImagingSession
    with SessionLocal() as db:
        s1 = ImagingSession(project_id=project, date=date(2025, 7, 12))
        s2 = ImagingSession(project_id=project, date=date(2025, 7, 13))
        db.add_all([s1, s2])
        db.commit()
        s1_id, s2_id = s1.id, s2.id
    # DATE-OBS 13-07 11:30 UTC: in UTC before noon -> night of 12-07; in Tokyo 20:30 -> night of 13-07
    upload(client, write_fits(tmp_path / "tzn.fits", star_frame(seed=141), DATE_OBS="2025-07-13T11:30:00"),
           project_id=project, session_id=s1_id)
    drain()
    fid = files_of(project)[0].id
    with SessionLocal() as db:
        db.get(FrameFile, fid).session_id = s1_id
        db.commit()
    try:
        client.post("/settings", data={"timezone": "Asia/Tokyo"})   # 20:30 local on 13-07 -> night of 13-07
        with SessionLocal() as db:
            f = db.get(FrameFile, fid)
            assert f.date_obs == datetime(2025, 7, 13, 20, 30) and f.session_id == s2_id
    finally:
        client.post("/settings", data={"timezone": "UTC"})
        client.post("/settings", data={"reset": "1"})


def test_session_order_is_stable_for_duplicate_nights(client, project, tmp_path):
    from app.models import ImagingSession
    with SessionLocal() as db:
        first = ImagingSession(project_id=project, date=date(2025, 5, 5))
        db.add(first)
        db.commit()
        second = ImagingSession(project_id=project, date=date(2025, 5, 5))
        db.add(second)
        db.commit()
        first_id = first.id
    upload(client, write_fits(tmp_path / "dn.fits", star_frame(seed=151), DATE_OBS="2025-05-05T22:00:00"),
           project_id=project)
    with SessionLocal() as db:
        f = db.scalars(select(FrameFile).where(FrameFile.project_id == project)).one()
        f.session_id = None
        db.commit()
    drain()
    assert files_of(project)[0].session_id == first_id


def test_calibration_list_paginates_in_sql(client):
    ids = [_calib_set(client) for _ in range(3)]
    try:
        client.post("/settings", data={"calib_page_size": "2"})
        t1 = client.get("/calibration").text
        t2 = client.get("/calibration?page=2").text
        found = lambda t: {i for i in ids if f'href="/calibration/{i}"' in t}
        assert len(found(t1)) + len(found(t2)) >= 3 and not (found(t1) & found(t2))
    finally:
        client.post("/settings", data={"reset": "1"})
        for i in ids:
            client.post(f"/calibration/{i}/delete")


def test_files_zip_post_and_calib_move_targets(client, project, tmp_path):
    import io
    import zipfile
    for i in range(2):
        upload(client, write_fits(tmp_path / f"zp{i}.fits", star_frame(seed=160 + i)), project_id=project)
    drain()
    ids = ",".join(str(f.id) for f in files_of(project))
    z = zipfile.ZipFile(io.BytesIO(client.post("/files/zip", data={"ids": ids}).content))
    assert len(z.namelist()) == 2
    a, b = _calib_set(client), _calib_set(client, "flat")
    try:
        t = client.get(f"/calibration/{a}").text
        assert f'value="calib:{b}"' in t and f'value="calib:{a}"' not in t      # other sets as move targets
    finally:
        client.post(f"/calibration/{a}/delete")
        client.post(f"/calibration/{b}/delete")


def test_project_page_hidden_sessions_lose_selection_and_hint_text(client, project):
    default_session(project)
    t = client.get(f"/projects/{project}").text
    assert "window.bulkRefresh" in t and ".selfile:checked,.selgroup:checked" in t
    assert "a file’s <b>i</b>" not in t


def test_tables_sortable_script_on_every_page(client, project):
    for url in ("/", "/calibration", f"/projects/{project}", "/manage", "/skymap"):
        t = client.get(url).text
        assert "sorttable.js" in t, url
    js = client.get("/static/sorttable.js").text
    assert "Intl.Collator" in js and "applyViewSort" in js and "localStorage" in js


# ---------- server-side column sorting (whole list, then pages) ----------
def test_projects_sorted_by_column_before_paging(client):
    from app.models import Setting
    names = ["Sort NGC 3031", "Sort NGC 224", "Sort NGC 7000"]
    pids = []
    for n in names:
        r = client.post("/projects/new", data={"name": n, "confirm_dup": "1"}, follow_redirects=False)
        pids.append(int(r.headers["location"].rsplit("/", 1)[1]))
    try:
        with SessionLocal() as db:
            db.merge(Setting(key="page_size", value="1"))
            db.commit()
        from app.core import invalidate_settings
        invalidate_settings()
        first = lambda url: [n for n in names if n in client.get(url).text.split('id="plist"')[1]]
        assert first("/?q=Sort+NGC&ps=name") == ["Sort NGC 224"]                 # natural: 224 < 3031 < 7000
        assert first("/?q=Sort+NGC&ps=name&page=3") == ["Sort NGC 7000"]
        assert first("/?q=Sort+NGC&ps=name&ps_dir=desc") == ["Sort NGC 7000"]
        t = client.get("/?q=Sort+NGC&ps=name&ps_dir=desc").text
        assert 'data-key="name"' in t and "sort-desc" in t and 'name="ps"' not in t     # select resets column sort
        for col in ("target", "object_type", "nights", "first", "last", "lights", "files", "size", "integration"):
            assert client.get(f"/?ps={col}&ps_dir=desc").status_code == 200, col
        assert client.get("/?ps=bogus").status_code == 200
    finally:
        client.post("/settings", data={"reset": "1"})
        for p in pids:
            client.post(f"/projects/{p}/delete")


def test_calibration_library_sorted_by_column(client):
    from app.models import CalibrationSet
    ids = []
    with SessionLocal() as db:
        for name, temp in (("Sort A", "-5"), ("Sort B", "-10"), ("Sort C", "0")):
            c = CalibrationSet(name=name, frame_type="dark", temperature=temp)
            db.add(c)
            db.flush()
            ids.append(c.id)
        db.commit()
    try:
        t = client.get("/calibration?q=Sort&co=temperature").text
        order = sorted(("Sort A", "Sort B", "Sort C"), key=t.index)
        assert order == ["Sort B", "Sort A", "Sort C"]                            # -10 < -5 < 0 (numeric)
        for col in ("name", "type", "date", "camera", "telescope", "gain", "offset", "exposure", "filter",
                    "binning", "files", "size", "sessions"):
            assert client.get(f"/calibration?co={col}&co_dir=desc").status_code == 200, col
    finally:
        for i in ids:
            client.post(f"/calibration/{i}/delete")


def test_session_table_sort_columns(client, project):
    default_session(project)
    for col in ("night", "equipment", "scale", "moon", "measured", "files"):
        for d in ("asc", "desc"):
            assert client.get(f"/projects/{project}?so={col}&so_dir={d}").status_code == 200
    t = client.get(f"/projects/{project}").text
    assert 'data-sort-param="so"' in t and "Show all" not in t
    js = client.get("/static/sorttable.js").text
    assert "syncServerSort" in js and "location.replace" in js
