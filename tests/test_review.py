"""Regression tests for the bugs found in the full code review (one test per fixed problem where possible)."""
import io
import json
import os
import subprocess
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select

from app import analysis, integrity, measured, photometry, platesolve
from app.core import IMPORT_DIR, DATA_DIR, ARCHIVE_DIR
from app.models import SessionLocal, FrameFile, ImagingSession, Project, Setting, CatalogItem
from app.routes_archive import path_night
from app.routes_projects import similar_projects
from conftest import drain, write_fits, upload, star_frame, default_session
from test_sky import fake_solve


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


# ---------- settings / projects / sessions ----------
def test_settings_reset_keeps_integrity_report(client):
    with SessionLocal() as db:
        db.merge(Setting(key=integrity.REPORT_KEY, value=json.dumps({"ok": True, "mode": "quick"})))
        db.merge(Setting(key="page_size", value="7"))
        db.commit()
    client.post("/settings", data={"reset": "1"})
    with SessionLocal() as db:
        assert db.get(Setting, "page_size") is None
        assert json.loads(db.get(Setting, integrity.REPORT_KEY).value)["mode"] == "quick"


def test_untitled_projects_are_not_duplicates(client):
    with SessionLocal() as db:
        db.add(Project(name="Untitled"))
        db.commit()
        assert similar_projects(db, "Untitled") == []
    r = client.post("/projects/new", data={"name": "", "target": ""}, follow_redirects=False)
    assert r.status_code == 303                       # no duplicate warning for the placeholder name
    client.post(r.headers["location"] + "/delete")
    with SessionLocal() as db:
        for p in db.scalars(select(Project).where(Project.name == "Untitled")):
            db.delete(p)
        db.commit()


def test_changing_session_date_releases_frames_of_the_old_night(client, project, tmp_path):
    sid = default_session(project)                    # night 12-10-2025
    upload(client, write_fits(tmp_path / "old.fits", data=star_frame(seed=51), DATE_OBS="2025-10-12T22:00:00"),
           "light", project_id=project)
    upload(client, write_fits(tmp_path / "nodate.fits", data=star_frame(seed=52)), "light", project_id=project)
    drain()
    r = client.post(f"/projects/{project}/sessions", data={"sid": str(sid), "date": "13-10-2025"},
                    follow_redirects=False)
    assert r.status_code == 303
    by = {f.original_name: f for f in files_of(project)}
    assert by["old.fits"].session_id is None          # belongs to the night of 12-10 -> unassigned
    assert by["nodate.fits"].session_id == sid        # no observation date -> stays


def test_session_delete_twice_redirects_home(client, project):
    with SessionLocal() as db:
        s = ImagingSession(project_id=project, date=date(2025, 1, 1))
        db.add(s)
        db.commit()
        sid = s.id
    client.post(f"/sessions/{sid}/delete")
    r = client.post(f"/sessions/{sid}/delete", headers={"referer": "https://evil.example/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


# ---------- plate solve ----------
def test_astap_timeout_is_a_failed_solve_not_a_crash(monkeypatch, tmp_path):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="astap_cli", timeout=1)
    monkeypatch.setattr(platesolve, "available", lambda: None)
    monkeypatch.setattr(platesolve.subprocess, "run", hang)
    src = write_fits(tmp_path / "t.fits", data=star_frame())
    res = platesolve.solve(src, None, 3.76, 500, None)
    assert res["ok"] is False and "did not finish" in res["error"]


def test_missing_astap_binary_is_a_failed_solve(monkeypatch, tmp_path):
    monkeypatch.setattr(platesolve, "available", lambda: None)
    monkeypatch.setattr(platesolve, "ASTAP_BIN", "/nonexistent/astap_cli")
    res = platesolve.solve(write_fits(tmp_path / "m.fits", data=star_frame()), None, None, None, None)
    assert res["ok"] is False and "could not be started" in res["error"]


def test_binned_frame_is_not_taken_for_a_resized_export(monkeypatch, tmp_path):
    """bin 2: 1920 px wide frame of a 3840 px camera -> pixel 2×, no 'resized' correction of the focal length."""
    def fake(src, header, pixel_um, focal, sensor_px):
        return {"ok": True, "image_px": [1920, 1080], "scale_arcsec_px": 4.5, "binning": 2,
                "focal_mm": round(206.265 * pixel_um * 2 / 4.5, 1), "pixel_um": pixel_um * 2,
                "ra_deg": 10.0, "dec_deg": 20.0}
    monkeypatch.setattr(platesolve, "solve", fake)
    inp = {"src": write_fits(tmp_path / "b.fits"), "header": None, "pixel_um": 2.9, "focal": 266.0,
           "sensor": (3840, 2160), "telescope": "T", "camera": "C"}
    res = analysis.solve_compute(inp)
    assert res["focal_mm"] == round(206.265 * 5.8 / 4.5, 1) and "resize_factor" not in res and "note" not in res


def test_fov_hint_uses_binned_sensor(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(platesolve, "available", lambda: None)
    monkeypatch.setattr(platesolve, "_run", lambda f, fov, ra, dec: seen.append(fov) or {})
    hdr = "XBINNING= 2\nXPIXSZ  = 5.8\n"
    platesolve.solve(write_fits(tmp_path / "h.fits", data=star_frame(h=300, w=400)), hdr, 2.9, 500, (800, 600))
    expected = 300 * 5.8 / 500 * 206.265 / 3600                    # binned sensor height × binned pixel
    assert seen and abs(seen[0] - expected) < 1e-3


def test_stale_solve_after_camera_change(client, project, tmp_path):
    with SessionLocal() as db:
        db.add(CatalogItem(kind="camera", name="Stale cam", specs=json.dumps({"pixel_um": 3.76})))
        s = db.get(ImagingSession, default_session(project))
        s.camera = "Stale cam"
        db.commit()
    upload(client, write_fits(tmp_path / "st.fits"), "light", project_id=project)
    drain()
    f = files_of(project)[-1]
    with SessionLocal() as db:
        db.get(FrameFile, f.id).solve = json.dumps(fake_solve(pixel_um=3.76))
        db.commit()
    assert "stale" not in client.get(f"/api/files/{f.id}/solved").json()
    with SessionLocal() as db:
        it = db.scalars(select(CatalogItem).where(CatalogItem.name == "Stale cam")).one()
        it.specs = json.dumps({"pixel_um": 2.9})
        db.commit()
    assert "pixel size changed" in client.get(f"/api/files/{f.id}/solved").json()["stale"]
    assert "pixel size changed" in client.get(f"/files/{f.id}/view").text
    with SessionLocal() as db:
        db.get(ImagingSession, default_session(project)).camera = None
        db.delete(db.scalars(select(CatalogItem).where(CatalogItem.name == "Stale cam")).one())
        db.commit()


def test_object_half_a_pixel_inside_the_edge_counts_as_inside():
    """0-based pixel centres: the frame spans -0.5 … W-0.5."""
    res = fake_solve(w=1000, h=1000)
    w = platesolve._wcs(res)
    ra, dec = w.all_pix2world([[-0.4, 500]], 0)[0]           # just inside the left edge
    t = platesolve.target_check(fake_solve(w=1000, h=1000, ra=2 * res["ra_deg"] - ra, dec=res["dec_deg"]), "M51")
    assert t["inside"]


# ---------- photometry / measured values / framestats ----------
def test_photometry_match_radius_follows_image_scale():
    rng = np.random.default_rng(3)
    n = 200
    ra, dec = 50 + rng.uniform(-0.5, 0.5, n), 30 + rng.uniform(-0.5, 0.5, n)
    mag = rng.uniform(8, 15, n)
    flux = 10 ** ((25 - mag) / 2.5)
    snr = flux / 10 ** ((25 - 16.5) / 2.5) * 5
    off = 8 / 3600                                         # detections 8″ off (wide-field centroid error)
    k = snr >= 5
    assert photometry.limiting_magnitude(snr[k], flux[k], ra[k] + off, dec[k], ra, dec, mag) is None
    res = photometry.limiting_magnitude(snr[k], flux[k], ra[k] + off, dec[k], ra, dec, mag, match_arcsec=12)
    assert res and abs(res["limit_mag"] - 16.5) < 0.3


def test_measured_fwhm_arcsec_for_resized_export():
    """50 % JPEG: FWHM in image pixels × arcsec per IMAGE pixel (not per sensor pixel)."""
    sol = fake_solve(scale_arcsec=4.5, sensor_scale_arcsec_px=2.25)
    f = SimpleNamespace(id=1, solve=json.dumps(sol), optics=json.dumps({"fwhm_px": 2.0, "elong": 1.1, "metrics": {}}))
    m = measured.session_measured([f])
    assert m["fwhm_arcsec"] == 9.0 and m["scale"] == 2.25


def test_palette_and_alpha_png_luminance(tmp_path):
    from PIL import Image
    from app.framestats import _luminance
    rgba = np.zeros((20, 30, 4), np.uint8)
    rgba[..., :3] = 100
    rgba[..., 3] = 255
    Image.fromarray(rgba, "RGBA").save(tmp_path / "a.png")
    lum, b = _luminance(tmp_path / "a.png")
    assert b == 1 and lum.shape == (20, 30) and abs(float(lum.mean()) - 100) < 1   # alpha not averaged in
    Image.fromarray(np.full((20, 30, 3), 200, np.uint8)).convert("P").save(tmp_path / "p.png")
    lum, _ = _luminance(tmp_path / "p.png")
    assert abs(float(lum.mean()) - 200) < 3                                      # brightness, not palette index


# ---------- integrity ----------
def test_cancelled_integrity_check_is_not_ok():
    integrity.state["cancel"] = True
    try:
        rep = integrity.run_check(full=False)
    finally:
        integrity.state["cancel"] = False
    assert rep["cancelled"] and rep["ok"] is None


def test_orphan_that_gets_registered_during_the_check_is_not_reported(client, project, tmp_path, monkeypatch):
    upload(client, write_fits(tmp_path / "late.fits"), "light", project_id=project)
    drain()
    f = files_of(project)[-1]
    real_select = integrity.select
    calls = {"n": 0}

    def hide_first_read(*cols):                        # the first row list does not contain the new file yet
        q = real_select(*cols)
        calls["n"] += 1
        return q.where(FrameFile.id != f.id) if calls["n"] == 1 else q
    monkeypatch.setattr(integrity, "select", hide_first_read)
    rep = integrity.run_check(full=False)
    assert f.stored_path not in {o["path"] for o in rep["orphans"]}


# ---------- archive import ----------
def test_path_night_prefers_folder_date_and_handles_after_midnight_names():
    assert path_night("M51/2024-03-15/Light_2024-03-16_01-30-00.fits") == date(2024, 3, 15)
    assert path_night("M51/Light_2024-03-16_01-30-00.fits") == date(2024, 3, 15)    # after midnight
    assert path_night("M51/Light_2024-03-16_21-30-00.fits") == date(2024, 3, 16)
    assert path_night("M51/Light_20240316.fits") == date(2024, 3, 16)


def test_archive_scan_skips_dangling_and_outside_symlinks(client):
    root = IMPORT_DIR / "links"
    write_fits(root / "ok" / "a.fits", data=star_frame(seed=61), OBJECT="Linktest", DATE_OBS="2024-01-01T21:00:00")
    (root / "ok" / "dangling.fits").symlink_to(root / "nope.fits")
    outside = DATA_DIR / "outside.fits"
    write_fits(outside, data=star_frame(seed=62))
    (root / "ok" / "out.fits").symlink_to(outside)
    scan = client.post("/api/import/archive/scan", json={"path": "links"}).json()
    assert scan["files"] == 1
    outside.unlink()


def test_archive_start_validation_and_existing_session_equipment(client, project):
    root = IMPORT_DIR / "arch2"
    write_fits(root / "x" / "e.fits", data=star_frame(seed=63), OBJECT="M 42", DATE_OBS="2025-10-12T21:00:00")
    sid = default_session(project)
    with SessionLocal() as db:
        db.get(ImagingSession, sid).telescope = "My curated scope"
        db.commit()
    bad = [{"project": "X", "night": "12-10-2025", "files": ["not-a-dict"]}]
    assert client.post("/api/import/archive/start", json={"path": "arch2", "groups": bad}).status_code == 400
    # stale project_id (deleted project) without a name is refused, not created with an empty name
    stale = [{"project": "", "project_id": 999999, "night": "12-10-2025", "files": [{"rel": "x/e.fits"}]}]
    assert client.post("/api/import/archive/start", json={"path": "arch2", "groups": stale}).status_code == 400
    ok = [{"project": "M 42", "project_id": project, "night": "12-10-2025", "telescope": "Guessed scope",
           "files": [{"rel": "x/e.fits", "type": "light"}]}]
    r = client.post("/api/import/archive/start", json={"path": "arch2", "groups": ok, "skip_dups": False}).json()
    assert r["ok"] and r["new_sessions"] == 0
    drain()
    with SessionLocal() as db:
        assert db.get(ImagingSession, sid).telescope == "My curated scope"     # not overwritten


def test_archive_start_removes_placeholders_on_failure(client, monkeypatch):
    import app.routes_archive as ra
    root = IMPORT_DIR / "arch3"
    write_fits(root / "y" / "f1.fits", data=star_frame(seed=64))
    write_fits(root / "y" / "f2.fits", data=star_frame(seed=65))
    made = []
    real = ra.unique_dest

    def flaky(folder, name):
        if made:
            raise OSError("disk full")
        d = real(folder, name)
        made.append(d)
        return d
    monkeypatch.setattr(ra, "unique_dest", flaky)
    groups = [{"project": "Placeholder test", "night": "01-01-2024",
               "files": [{"rel": "y/f1.fits", "type": "light"}, {"rel": "y/f2.fits", "type": "light"}]}]
    with pytest.raises(OSError):
        client.post("/api/import/archive/start", json={"path": "arch3", "groups": groups})
    assert made and not made[0].exists()
    with SessionLocal() as db:
        assert not db.scalars(select(Project).where(Project.name == "Placeholder test")).first()


# ---------- pages render with partial measured values ----------
def test_session_dialog_with_optics_but_no_solve(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "po.fits"), "light", project_id=project)
    drain()
    f = files_of(project)[-1]
    with SessionLocal() as db:
        db.get(FrameFile, f.id).optics = json.dumps({"fwhm_px": 3.0, "elong": None, "metrics": {}})
        db.commit()
    r = client.get(f"/projects/{project}")
    assert r.status_code == 200 and "Star FWHM" in r.text
