"""Feature coverage: viewer endpoints, odd image files, single-file actions, managed lists,
calibration set lifecycle, sessions automation, ZIP variants, background cleanup."""
import io
import json
import os
import time
import zipfile

import numpy as np
import pytest
from sqlalchemy import select

from app.models import SessionLocal, FrameFile, ImagingSession, CalibrationSet, CatalogItem
from conftest import drain, write_fits, upload, star_frame, default_session, DATA_DIR


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


def one_file(client, project, tmp_path, name="v.fits", data=None, ftype="light", **hdr):
    upload(client, write_fits(tmp_path / name, data=star_frame(seed=7) if data is None else data, **hdr), ftype,
           project_id=project)
    drain()
    return [f for f in files_of(project) if f.original_name == name][0]


# ---------- viewer endpoints ----------
def test_render_histogram_header_thumb(client, project, tmp_path):
    f = one_file(client, project, tmp_path, EXPTIME=30.0, FILTER="Ha")
    assert f.status == "done" and f.thumb_path
    for q in ("", "?stretch=linked", "?stretch=manual&bp=0.1&mp=0.3&wp=0.9", "?channels=lum&flip=vh",
              "?bp=abc&wp=NaN&stretch=bogus"):                       # junk options fall back to defaults
        r = client.get(f"/files/{f.id}/render{q}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", q
    h = client.get(f"/files/{f.id}/histogram").json()
    assert h and isinstance(h, dict)
    assert client.get(f"/files/{f.id}/thumb").headers["content-type"] == "image/jpeg"
    hdr = client.get(f"/files/{f.id}/header").text
    assert "EXPTIME" in hdr and "FILTER" in hdr
    assert client.get(f"/files/{f.id}/view").status_code == 200


def test_render_cache_key_changes_with_options(client, project, tmp_path):
    f = one_file(client, project, tmp_path)
    a = client.get(f"/files/{f.id}/render?stretch=manual&bp=0&wp=1").content
    b = client.get(f"/files/{f.id}/render?stretch=manual&bp=0.5&wp=0.6").content
    assert a != b                                                   # different levels -> different image


def test_view_settings_saved_for_file_and_type(client, project, tmp_path):
    f1 = one_file(client, project, tmp_path, "o1.fits")
    f2 = one_file(client, project, tmp_path, "o2.fits", data=star_frame(seed=8))
    orig = (DATA_DIR / f1.stored_path).read_bytes()
    client.post(f"/files/{f1.id}/opts", data={"stretch": "linked", "brightness": "20"})
    with SessionLocal() as db:
        assert json.loads(db.get(FrameFile, f1.id).view_opts)["stretch"] == "linked"
        assert db.get(FrameFile, f2.id).view_opts is None
    client.post(f"/files/{f1.id}/opts", data={"stretch": "linear", "all": "1"})
    with SessionLocal() as db:
        assert json.loads(db.get(FrameFile, f2.id).view_opts)["stretch"] == "linear"
    assert (DATA_DIR / f1.stored_path).read_bytes() == orig          # original never touched


# ---------- odd files must never crash processing or the viewer ----------
ODD = {
    "nan.fits": lambda: np.where(np.arange(100 * 120).reshape(100, 120) % 7 == 0, np.nan, 100.0).astype(np.float32),
    "zero.fits": lambda: np.zeros((80, 90), np.uint16),
    "tiny.fits": lambda: np.array([[5]], np.uint16),
    "row.fits": lambda: np.arange(50, dtype=np.float32).reshape(1, 50),
    "cube.fits": lambda: np.stack([star_frame(60, 80, seed=s) for s in (1, 2, 3)]),        # (3, h, w) RGB
    "inf.fits": lambda: np.full((40, 40), np.inf, np.float32),
}


@pytest.mark.parametrize("name", list(ODD))
def test_odd_fits_files_process_and_render(client, project, tmp_path, name):
    f = one_file(client, project, tmp_path, name, data=ODD[name]())
    assert f.status in ("done", "error")                            # never stuck / never kills the worker
    for url in (f"/files/{f.id}/render", f"/files/{f.id}/histogram", f"/files/{f.id}/view",
                f"/files/{f.id}/render?stretch=manual&bp=0.2&wp=0.8"):
        assert client.get(url).status_code in (200, 404, 415), url  # no 500


def test_images_8_and_16_bit_and_corrupt(client, project, tmp_path):
    from PIL import Image
    p8 = tmp_path / "rgb.png"
    Image.fromarray((np.random.default_rng(1).random((40, 60, 3)) * 255).astype(np.uint8)).save(p8)
    p16 = tmp_path / "g16.tif"
    Image.fromarray((np.random.default_rng(2).random((40, 60)) * 65535).astype(np.uint16)).save(p16)
    bad = tmp_path / "broken.fits"
    bad.write_bytes(b"SIMPLE  =                    T" + b"\x00" * 100)
    for p in (p8, p16, bad):
        upload(client, p, "stack" if p.suffix != ".fits" else "light", project_id=project)
    drain()
    by = {f.original_name: f for f in files_of(project)}
    assert by["rgb.png"].thumb_path and by["g16.tif"].thumb_path
    assert by["broken.fits"].error and "Cannot read file" in by["broken.fits"].error   # reported, not silent
    for f in by.values():
        assert client.get(f"/files/{f.id}/render").status_code != 500
    # worker survived the broken file: a later upload is still processed
    g = one_file(client, project, tmp_path, "after.fits", data=star_frame(seed=9))
    assert g.status == "done"


def test_bayer_and_metadata_extraction(client, project, tmp_path):
    f = one_file(client, project, tmp_path, "osc.fits", data=star_frame(120, 160, seed=3), BAYERPAT="RGGB",
                 EXPTIME=120.0, GAIN=100, CCD_TEMP=-10.0, FILTER="L", DATE_OBS="2025-11-02T23:30:00")
    assert (f.exposure_s, f.filter) == (120.0, "L") and f.gain in ("100", "100.0")
    assert f.temperature and f.temperature.startswith("-10") and f.date_obs.year == 2025
    for b in ("auto", "RGGB", "none"):
        assert client.get(f"/files/{f.id}/render?bayer={b}").status_code == 200


# ---------- single-file actions ----------
def test_single_file_edit_reject_delete(client, project, tmp_path):
    f = one_file(client, project, tmp_path)
    client.post(f"/files/{f.id}/edit", data={"exposure_s": "12,5", "gain": "200", "filter": "OIII",
                                             "temperature": "-5", "description": "nice"})
    with SessionLocal() as db:
        x = db.get(FrameFile, f.id)
        assert (x.exposure_s, x.gain, x.filter, x.description) == (12.5, "200", "OIII", "nice") and x.updated_at
    client.post(f"/files/{f.id}/reject")
    assert files_of(project)[0].rejected
    client.post(f"/files/{f.id}/reject")
    assert not files_of(project)[0].rejected
    stored = DATA_DIR / f.stored_path
    client.post(f"/files/{f.id}/delete")
    assert files_of(project) == [] and not stored.exists()
    assert client.post(f"/files/{f.id}/delete").status_code in (303, 404)


def test_bulk_type_change_and_move_to_library(client, project, tmp_path):
    f = one_file(client, project, tmp_path, "t.fits")
    old = DATA_DIR / f.stored_path
    r = client.post("/api/files/bulk", json={"ids": [f.id], "action": "type", "value": "flat"})
    assert r.status_code == 200
    [x] = files_of(project)
    assert x.frame_type == "flat" and "/flat/" in x.stored_path and (DATA_DIR / x.stored_path).is_file()
    assert not old.exists()
    assert client.post("/api/files/bulk", json={"ids": [f.id], "action": "type", "value": "bogus"}).status_code == 400
    cid = int(client.post("/calibration/new", data={"frame_type": "flat", "name": "F"}, follow_redirects=False)
              .headers["location"].rsplit("/", 1)[1])
    r = client.post("/api/files/bulk", json={"ids": [f.id], "action": "move", "value": f"calib:{cid}"})
    assert r.status_code == 200 and files_of(project) == []
    with SessionLocal() as db:
        y = db.get(FrameFile, f.id)
        assert (y.calib_set_id, y.project_id, y.session_id) == (cid, None, None)
        assert (DATA_DIR / y.stored_path).is_file() and "calibration" in y.stored_path
    assert client.post("/api/files/bulk", json={"ids": [f.id], "action": "move", "value": "calib:999999"}).status_code == 404
    client.post(f"/calibration/{cid}/delete")


def test_bulk_meta_reject_reprocess(client, project, tmp_path):
    a = one_file(client, project, tmp_path, "m1.fits")
    b = one_file(client, project, tmp_path, "m2.fits", data=star_frame(seed=11))
    ids = [a.id, b.id]
    client.post("/api/files/bulk", json={"ids": ids, "action": "meta", "value": {"filter": "Ha", "exposure_s": "300"}})
    assert {(f.filter, f.exposure_s) for f in files_of(project)} == {("Ha", 300.0)}
    client.post("/api/files/bulk", json={"ids": ids, "action": "reject"})
    assert all(f.rejected for f in files_of(project))
    client.post("/api/files/bulk", json={"ids": ids, "action": "unreject"})
    client.post("/api/files/bulk", json={"ids": ids, "action": "reprocess"})
    assert {f.status for f in files_of(project)} == {"pending"}
    drain()
    assert client.post("/api/files/bulk", json={"ids": [], "action": "reject"}).status_code == 400
    assert client.post("/api/files/bulk", json={"ids": ids, "action": "nonsense"}).status_code in (400, 200)


# ---------- managed lists (form routes) ----------
def test_manage_form_add_rename_delete_propagates(client, project):
    client.post("/manage/telescope/add", data={"name": "ScopeR"})
    client.post(f"/projects/{project}/sessions", data={"date": "05-05-2026", "telescope": "ScopeR"})
    cid = int(client.post("/calibration/new", data={"frame_type": "dark", "name": "Dk", "telescope": "ScopeR"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        it = db.scalars(select(CatalogItem).where(CatalogItem.kind == "telescope", CatalogItem.name == "ScopeR")).one()
    client.post(f"/manage/items/{it.id}/edit", data={"name": "ScopeR2", "notes": ""})
    with SessionLocal() as db:
        assert db.scalars(select(ImagingSession.telescope).where(ImagingSession.project_id == project)).all() == ["ScopeR2"]
        assert db.get(CalibrationSet, cid).telescope == "ScopeR2"
    client.post("/manage/telescope/add", data={"name": "Other"})
    client.post(f"/manage/items/{it.id}/edit", data={"name": "Other"})           # duplicate name -> refused
    with SessionLocal() as db:
        assert db.get(CatalogItem, it.id).name == "ScopeR2"
    r = client.post(f"/manage/items/{it.id}/delete", follow_redirects=True)      # still used -> refused
    assert "cannot be deleted" in r.text and "1 session" in r.text and "1 calibration set" in r.text
    with SessionLocal() as db:
        assert db.get(CatalogItem, it.id) is not None
        assert db.scalars(select(ImagingSession.telescope).where(ImagingSession.project_id == project)).all() == ["ScopeR2"]
    assert client.get("/manage?kind=evil").status_code == 404
    client.post(f"/calibration/{cid}/delete")


# ---------- calibration set lifecycle ----------
def test_calibration_set_edit_zip_delete(client, tmp_path):
    cid = int(client.post("/calibration/new", data={"frame_type": "bias", "name": "B1", "exposure_s": "abc"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    client.post(f"/calibration/{cid}/edit", data={"frame_type": "bias", "name": "B2", "gain": "100",
                                                 "capture_date": "01-01-2026", "exposure_s": "0,001"})
    with SessionLocal() as db:
        c = db.get(CalibrationSet, cid)
        assert (c.name, c.gain, c.exposure_s) == ("B2", "100", 0.001)
    upload(client, write_fits(tmp_path / "b.fits", data=star_frame(seed=21)), "bias", calib_set_id=cid)
    drain()
    names = zipfile.ZipFile(io.BytesIO(client.get(f"/calibration/{cid}/zip").content)).namelist()
    assert "bias/b.fits" in names
    with SessionLocal() as db:
        stored = DATA_DIR / db.scalars(select(FrameFile.stored_path).where(FrameFile.calib_set_id == cid)).one()
    assert client.get("/calibration?type=bias").status_code == 200
    client.post(f"/calibration/{cid}/delete")
    assert not stored.exists() and client.get(f"/calibration/{cid}").status_code == 404


# ---------- sessions automation / reprocess ----------
def test_sessions_auto_and_reprocess(client, project, tmp_path):
    with SessionLocal() as db:                       # a project without sessions yet
        sid = default_session(project)
    for i, d in enumerate(["2025-12-01T21:00:00", "2025-12-02T01:30:00", "2025-12-05T22:00:00"]):
        upload(client, write_fits(tmp_path / f"a{i}.fits", data=star_frame(seed=30 + i), DATE_OBS=d), "light",
               project_id=project, session_id=sid)
    drain()
    client.post("/api/files/bulk", json={"ids": [f.id for f in files_of(project)], "action": "session", "value": ""})
    client.post(f"/projects/{project}/sessions/auto")
    with SessionLocal() as db:
        nights = sorted(str(s.date) for s in db.scalars(select(ImagingSession).where(ImagingSession.project_id == project)))
    assert "2025-12-01" in nights and "2025-12-05" in nights and "2025-12-02" not in nights   # after midnight = previous night
    assert all(f.session_id for f in files_of(project))
    client.post(f"/projects/{project}/reprocess")
    assert {f.status for f in files_of(project)} == {"pending"}
    drain()


@pytest.mark.parametrize("comp", ["none", "fast", "best"])
def test_zip_compression_modes(client, project, tmp_path, comp):
    one_file(client, project, tmp_path, f"z_{comp}.fits")
    client.post("/settings", data={"accent_color": "#3b5bdb", "zip_compression": comp, "show_stats": "on"})
    r = client.get(f"/projects/{project}/zip?types=light")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert any(n.endswith(f"z_{comp}.fits") for n in z.namelist()) and z.testzip() is None
    assert ("content-length" in r.headers) == (comp == "none")
    client.post("/settings", data={"reset": "1"})


# ---------- background cleanup ----------
def test_cleanup_uploads_and_cache(client):
    from app import worker
    up, cache = DATA_DIR / ".uploads", DATA_DIR / ".cache" / "999999"
    up.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    new_up, old_c = up / "fresh.part", cache / "old.jpg"
    old_up = up / ("0" * 32 + ".part")
    for p in (old_up, new_up, old_c):
        p.write_bytes(b"x")
    past = time.time() - 40 * 86400
    os.utime(old_up, (past, past))
    os.utime(old_c, (past, past))
    worker.cleanup_uploads()
    worker.cleanup_cache()
    assert not old_up.exists() and new_up.exists() and not old_c.exists()
    new_up.unlink()


def test_stats_page_with_data(client, project, tmp_path):
    one_file(client, project, tmp_path, "s.fits", EXPTIME=600.0, DATE_OBS="2025-08-10T22:00:00")
    t = client.get("/stats").text
    assert "10m" in t and "imaging nights" in t


# ---------- regression tests from the 2nd review ----------
def test_render_size_cannot_escape_cache(client, project, tmp_path):
    f = one_file(client, project, tmp_path, "sz.fits")
    r = client.get(f"/files/{f.id}/render", params={"size": "/../../../../../../tmp/pwned"})
    assert r.status_code == 200
    assert not list((DATA_DIR.parent).rglob("pwned*")) and not list(DATA_DIR.rglob("pwned*"))
    names = [p.name for p in (DATA_DIR / ".cache" / str(f.id)).iterdir()]
    assert all(("_full_" in n or "_preview_" in n or n.startswith("hist_")) for n in names)


def test_histogram_cache_recovers_from_damaged_file(client, project, tmp_path):
    f = one_file(client, project, tmp_path, "hc.fits")
    assert client.get(f"/files/{f.id}/histogram").status_code == 200
    [c] = list((DATA_DIR / ".cache" / str(f.id)).glob("hist_*.json"))
    c.write_text('{"broken')
    assert client.get(f"/files/{f.id}/histogram").status_code == 200


def test_view_opts_of_wrong_type_do_not_crash(client, project, tmp_path):
    f = one_file(client, project, tmp_path, "vo.fits")
    with SessionLocal() as db:
        db.get(FrameFile, f.id).view_opts = "[1, 2]"
        db.commit()
    assert client.get(f"/files/{f.id}/view").status_code == 200
    client.post("/api/files/bulk", json={"ids": [f.id], "action": "reprocess"})
    drain()
    assert files_of(project)[0].status == "done"


@pytest.mark.parametrize("shape", [(2, 40, 50), (5, 40, 50), (1, 3, 40, 50)])
def test_fits_cubes_render_as_images(client, project, tmp_path, shape):
    from PIL import Image
    data = np.random.default_rng(3).random(shape).astype(np.float32) * 1000
    f = one_file(client, project, tmp_path, "cube%s.fits" % "x".join(map(str, shape)), data=data)
    assert f.thumb_path and not f.error
    im = Image.open(io.BytesIO(client.get(f"/files/{f.id}/render").content))
    assert im.size == (50, 40)                       # width × height of the planes, not of the cube axes


def test_bayer_offset_as_float_and_one_row_cfa(client, project, tmp_path):
    f = one_file(client, project, tmp_path, "off.fits", data=star_frame(60, 80, seed=4), BAYERPAT="RGGB",
                 XBAYROFF="1.0", YBAYROFF=0)
    assert f.thumb_path and not f.error
    g = one_file(client, project, tmp_path, "row1.fits", data=np.arange(80, dtype=np.uint16).reshape(1, 80),
                 BAYERPAT="RGGB")
    for x in (f, g):
        assert client.get(f"/files/{x.id}/render").status_code == 200
        assert client.get(f"/files/{x.id}/histogram").status_code in (200, 404)


def test_suspect_flag_cleared_after_type_change(client, project, tmp_path):
    for i in range(4):
        one_file(client, project, tmp_path, f"g{i}.fits", data=star_frame(seed=200 + i), FILTER="L", EXPTIME=60.0)
    bad = one_file(client, project, tmp_path, "blurry.fits", data=star_frame(sigma=4.5, seed=210), FILTER="L",
                   EXPTIME=60.0)
    assert [f.original_name for f in files_of(project) if f.suspect] == ["blurry.fits"]
    client.post("/api/files/bulk", json={"ids": [bad.id], "action": "type", "value": "other"})
    assert not any(f.suspect for f in files_of(project))


def test_catalog_names_case_insensitive_and_length_limited(client):
    assert client.post("/api/catalog/camera", json={"name": "CaseCam"}).status_code == 200
    r = client.post("/api/catalog/camera", json={"name": "casecam"})
    assert r.status_code == 400 and "already exists" in r.json()["error"]
    r = client.post("/api/catalog/object_type", json={"name": "x" * 150})
    assert r.status_code == 400 and "too long" in r.json()["error"]
    r = client.post("/manage/telescope/add", data={"name": "y" * 300}, follow_redirects=True)
    assert r.status_code == 200 and "too long" in r.text


def test_too_long_values_give_message_not_500(client, project):
    r = client.post(f"/projects/{project}/sessions", data={"date": "01-02-2026", "seeing": "s" * 80},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/calibration/new", data={"frame_type": "dark", "name": "Long", "temperature": "t" * 40},
                    follow_redirects=True)
    assert r.status_code == 200 and "too long" in r.text


def test_nan_exposure_is_not_stored(client):
    cid = int(client.post("/calibration/new", data={"frame_type": "dark", "name": "N", "exposure_s": "nan"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        assert db.get(CalibrationSet, cid).exposure_s is None
    client.post(f"/calibration/{cid}/delete")


def test_search_treats_wildcards_literally_and_normalises_spaces(client):
    ids = []
    for name, notes in (("Wild_1", "M 31  core"), ("Wild%2", ""), ("WildAB", "")):
        r = client.post("/projects/new", data={"name": name, "notes": notes}, follow_redirects=False)
        ids.append(int(r.headers["location"].rsplit("/", 1)[1]))
    t = client.get("/", params={"q": "Wild_"}).text
    assert "<h3>Wild_1</h3>" in t and "<h3>WildAB</h3>" not in t
    t = client.get("/", params={"q": "%"}).text
    assert "<h3>Wild%2</h3>" in t and "<h3>WildAB</h3>" not in t
    t = client.get("/", params={"q": "M  31"}).text
    assert "<mark>M 31</mark>" in t
    for pid in ids:
        client.post(f"/projects/{pid}/delete")


# ---------- CSRF, catalog delete protection, debayer, CMYK ----------
def test_csrf_token_required_for_changes(client, project):
    from fastapi.testclient import TestClient
    from app.main import app
    tok = client.headers.pop("X-CSRF-Token")
    try:
        r = client.post(f"/projects/{project}/sessions", data={"date": "01-01-2026"}, follow_redirects=False)
        assert r.status_code == 403
        assert client.post("/api/catalog/camera", json={"name": "NoTok"}).status_code == 403
        assert client.put("/api/uploads/" + "0" * 32 + "?offset=0", content=b"x").status_code == 403
        r = client.post(f"/projects/{project}/sessions?csrf=wrong", data={"date": "01-01-2026"}, follow_redirects=False)
        assert r.status_code == 403
        r = client.post(f"/projects/{project}/sessions?csrf={tok}", data={"date": "01-01-2026"}, follow_redirects=False)
        assert r.status_code == 303                       # token in the form action (how the browser sends it)
        assert client.get("/").status_code == 200         # reading is never blocked
    finally:
        client.headers["X-CSRF-Token"] = tok
    with TestClient(app) as anon:                         # login itself needs no token
        assert anon.post("/login", data={"username": "admin", "password": "x"}, follow_redirects=False).status_code == 303
    assert 'name="csrf-token"' in client.get("/").text


def test_logout_is_post_only(client):
    from fastapi.testclient import TestClient
    from app.main import app
    from conftest import csrf_of
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin"})
        assert c.get("/logout", follow_redirects=False).headers["location"] == "/"   # GET does nothing
        assert c.get("/", follow_redirects=False).status_code == 200
        assert c.post("/logout", follow_redirects=False).status_code == 403          # no token
        c.post(f"/logout?csrf={csrf_of(c)}")
        assert c.get("/", follow_redirects=False).status_code == 303                 # logged out


def test_catalog_item_in_use_cannot_be_deleted_via_api(client, project):
    client.post("/api/catalog/mount", json={"name": "BusyMount"})
    client.post(f"/projects/{project}/sessions", data={"date": "02-02-2026", "mount": "BusyMount"})
    item = next(i for i in client.get("/api/catalog/mount").json()["items"] if i["name"] == "BusyMount")
    assert item["in_use"] == "1 session"
    r = client.delete(f"/api/catalog/items/{item['id']}")
    assert r.status_code == 409 and "cannot be deleted" in r.json()["error"]
    client.post("/api/catalog/mount", json={"name": "FreeMount"})
    free = next(i for i in client.get("/api/catalog/mount").json()["items"] if i["name"] == "FreeMount")
    assert free["in_use"] is None and client.delete(f"/api/catalog/items/{free['id']}").status_code == 200


def test_debayer_lean_matches_reference():
    from app.preview import debayer_bilinear, _conv3
    def ref(a, pattern):                                  # previous (memory-hungry) implementation
        h, w = a.shape
        yy, xx = np.mgrid[0:h, 0:w]
        idx = (yy % 2) * 2 + (xx % 2)
        ks = {"R": [[.25, .5, .25], [.5, 1, .5], [.25, .5, .25]], "G": [[0, .25, 0], [.25, 1, .25], [0, .25, 0]]}
        ks["B"] = ks["R"]
        return np.stack([_conv3(a * np.isin(idx, [i for i, c in enumerate(pattern) if c == col]).astype(np.float32),
                                ks[col]) for col in "RGB"], axis=-1)
    a = np.random.default_rng(5).random((61, 83)).astype(np.float32) * 1000
    for p in ("RGGB", "BGGR", "GRBG", "GBRG"):
        assert np.allclose(debayer_bilinear(a, p), ref(a, p), atol=1e-3), p


def test_cmyk_jpeg_renders_with_correct_colours(client, project, tmp_path):
    from PIL import Image
    p = tmp_path / "cmyk.jpg"
    Image.new("RGB", (40, 30), (200, 30, 30)).convert("CMYK").save(p)
    upload(client, p, "stack", project_id=project)
    drain()
    [f] = files_of(project)
    im = Image.open(io.BytesIO(client.get(f"/files/{f.id}/render?stretch=none").content)).convert("RGB")
    r, g, b = im.getpixel((20, 15))
    assert r > 150 and g < 90 and b < 90                    # still red, not inverted / washed out
