"""Tests for paths that were not exercised before (found with a coverage run and the second code review):
the full plate-solve flow with a simulated ASTAP, the star database search, upload edge cases, the worker's
import / analysis error paths, and the fixes of the second review (conditions from lights only, gain keys,
undersampling of colour cameras, defocused nights, failed optics, queue wipe, tabs / validation)."""
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select

from app import analysis, conditions, measured, photometry, platesolve, worker
from app.core import DATA_DIR, IMPORT_DIR
from app.models import (SessionLocal, FrameFile, ImagingSession, ImportJob, AnalysisTask, Project, Setting,
                        CatalogItem)
from conftest import drain, write_fits, upload, star_frame, default_session
from test_sky import fake_solve, star_field


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


def tile(path, stars):
    """ASTAP 5-byte record star database tile (see photometry.py)."""
    out = bytearray(b"test".ljust(110))
    last = None
    for ra, dec, mag in sorted(stars, key=lambda s: s[2]):
        d = round(dec / 90 * (128 * 65536 - 1))
        hi, lo = d >> 16, d & 0xFFFF
        key = (hi, round(mag * 10))
        if key != last:
            out += bytes([255, 255, 255, hi + 128, round(mag * 10) + 16])
            last = key
        r = round(ra / 360 * (2 ** 24 - 1))
        out += bytes([r & 255, r >> 8 & 255, r >> 16, lo & 255, lo >> 8])
    path.write_bytes(bytes(out))


# ---------- full plate solve with a simulated ASTAP ----------
@pytest.fixture()
def fake_astap(tmp_path, monkeypatch):
    """ASTAP replaced by a function that writes .ini / .csv like astap_cli; a 1-tile star database."""
    db = tmp_path / "astapdb"
    db.mkdir()
    ra0, dec0, scale = 202.47, 47.2, 1.5
    rng = np.random.default_rng(1)
    cat = [(ra0 + rng.uniform(-0.15, 0.15), dec0 + rng.uniform(-0.1, 0.1), rng.uniform(9, 16)) for _ in range(150)]
    tile(db / f"d50_{photometry._ring_of(dec0):02d}01.1476", cat)
    (db / "dummy.1476").write_bytes(b"")
    calls = []

    def run(cmd, **kw):
        f = Path(cmd[cmd.index("-f") + 1])
        fov = cmd[cmd.index("-fov") + 1]
        calls.append({"fov": fov, "ra": "-ra" in cmd})
        if len(calls) == 1 and fov != "0":                      # the hint attempt fails, the blind one works
            f.with_suffix(".ini").write_text("PLTSOLVD=F\nERROR=No solution\n")
            return
        s = scale / 3600
        f.with_suffix(".ini").write_text(
            f"PLTSOLVD=T\nCRPIX1=200.5\nCRPIX2=150.5\nCRVAL1={ra0}\nCRVAL2={dec0}\nCDELT1={s}\nCDELT2={s}\n"
            f"CROTA2=12.0\nCD1_1={-s}\nCD1_2=0\nCD2_1=0\nCD2_2={-s}\n")
        zp = 25.0
        rows = ["x,y,hfd,snr,flux,ra[0..360],dec[0..360]"]
        for ra, dec, mag in cat:
            flux = 10 ** ((zp - mag) / 2.5)
            snr = flux / 10 ** ((zp - 17) / 2.5) * 5
            if snr >= 5:
                rows.append(f"1,1,3,{snr:.1f},{flux:.1f},{ra:.7f},{dec:.7f}")
        f.with_suffix(".csv").write_text("\n".join(rows))
    monkeypatch.setattr(platesolve, "ASTAP_DB", str(db))
    monkeypatch.setattr(platesolve, "available", lambda: None)
    monkeypatch.setattr(platesolve.subprocess, "run", run)
    return SimpleNamespace(calls=calls, ra=ra0, dec=dec0, scale=scale)


def test_full_solve_flow(fake_astap, tmp_path):
    src = write_fits(tmp_path / "s.fits", data=star_frame(h=300, w=400), RA=202.47, DEC=47.2)
    hdr = "RA      = 202.47\nDEC     = 47.2\n"
    res = platesolve.solve(src, hdr, 3.76, 600.0, None)
    assert res["ok"], res
    assert len(fake_astap.calls) == 2 and fake_astap.calls[0]["fov"] != "0" and fake_astap.calls[0]["ra"]
    assert abs(res["scale_arcsec_px"] - 1.5) < 1e-6 and res["focal_mm"] == round(206.265 * 3.76 / 1.5, 1)
    assert res["wcs"]["w"] == 400 and res["lum_bin"] == 1 and res["sky_adu"] > 0
    ph = res["photometry"]
    assert ph["matched"] > 20 and abs(ph["zero_point"] - 25.0) < 0.1 and abs(ph["limit_mag"] - 17.0) < 0.3


def test_solve_no_solution(fake_astap, tmp_path, monkeypatch):
    monkeypatch.setattr(platesolve.subprocess, "run",
                        lambda cmd, **kw: Path(cmd[cmd.index("-f") + 1]).with_suffix(".ini").write_text("PLTSOLVD=F\n"))
    res = platesolve.solve(write_fits(tmp_path / "n.fits", data=star_frame()), None, None, None, None)
    assert res["ok"] is False and "No solution" in res["error"]


def test_ra_dec_hints_from_header():
    assert platesolve._ra_dec_hint({"RA": "150.0", "DEC": "-20.5"}) == (10.0, -20.5)
    ra, dec = platesolve._ra_dec_hint({"OBJCTRA": "05 35 17", "OBJCTDEC": "-00 30 00"})
    assert abs(ra - (5 + 35 / 60 + 17 / 3600)) < 1e-6 and abs(dec + 0.5) < 1e-6      # "-00 …" keeps the sign
    assert platesolve._ra_dec_hint({}) == (None, None)


def test_catalogue_stars_wraps_ra_and_rings(tmp_path):
    tile(tmp_path / f"d50_{photometry._ring_of(10.0):02d}01.1476", [(359.9, 10.0, 10.0), (0.1, 10.05, 11.0), (180, 10, 9)])
    ra, dec, mag = photometry.catalogue_stars(tmp_path, 0.0, 10.0, 0.5)
    assert sorted(np.round(mag, 1)) == [10.0, 11.0]                        # across 0/360, not the far star


def test_annotations_near_the_pole_and_ra_zero():
    for ra, dec in ((0.05, 30.0), (100.0, 88.5)):
        ann = platesolve.annotations(fake_solve(ra=ra, dec=dec, w=800, h=600))
        assert ann["grid"] and "compass" in ann


# ---------- uploads ----------
def test_upload_edge_cases(client, project, tmp_path):
    sid = default_session(project)
    data = b"x" * 1000
    r = client.post("/api/uploads", json={"project_id": project, "session_id": sid, "filename": "e.fits",
                                          "size": len(data), "frame_type": "light"})
    uid = r.json()["id"]
    assert client.put(f"/api/uploads/{uid}?offset=5", content=data[:10]).status_code == 409   # wrong offset
    assert client.put(f"/api/uploads/{uid}?offset=0", content=data + b"y").status_code == 400  # too much data
    assert client.get(f"/api/uploads/{uid}").json()["offset"] == 0                           # truncated back
    client.put(f"/api/uploads/{uid}?offset=0", content=data[:400])
    assert client.post(f"/api/uploads/{uid}/finish").status_code == 409                    # incomplete
    assert client.delete(f"/api/uploads/{uid}").json()["ok"]
    assert client.get(f"/api/uploads/{uid}").status_code == 404
    assert client.post(f"/api/uploads/{uid}/finish").status_code == 404


def test_upload_finish_after_project_deleted(client, tmp_path):
    p = client.post("/projects/new", data={"name": "Gone soon"}, follow_redirects=False)
    pid = int(p.headers["location"].rsplit("/", 1)[1])
    sid = default_session(pid)
    data = b"z" * 100
    uid = client.post("/api/uploads", json={"project_id": pid, "session_id": sid, "filename": "g.fits",
                                            "size": 100, "frame_type": "light"}).json()["id"]
    client.put(f"/api/uploads/{uid}?offset=0", content=data)
    client.post(f"/projects/{pid}/delete")
    assert client.post(f"/api/uploads/{uid}/finish").status_code == 410


# ---------- worker: import / analysis error paths ----------
def test_import_duplicates_and_failed_copies(client, project, tmp_path):
    sid = default_session(project)
    a = write_fits(IMPORT_DIR / "dupe" / "a.fits", data=star_frame(seed=301))
    (IMPORT_DIR / "dupe" / "b.fits").write_bytes(a.read_bytes())                 # same content
    write_fits(IMPORT_DIR / "dupe" / "c.fits", data=star_frame(seed=302))
    r = client.post("/api/import/start", json={"project_id": project, "session_id": sid, "path": "dupe",
                                               "skip_dups": True, "files": [{"rel": "a.fits"}, {"rel": "b.fits"},
                                                                             {"rel": "c.fits"}]}).json()
    (IMPORT_DIR / "dupe" / "c.fits").unlink()                                       # vanishes before copying
    drain()
    with SessionLocal() as db:
        job = db.get(ImportJob, r["job"])
        assert job.status == "done" and job.done == 1 and job.skipped == 1 and job.failed == 1
        failed = db.scalars(select(FrameFile).where(FrameFile.import_job_id == job.id,
                                                    FrameFile.status == "error")).one()
        assert failed.stored_path.startswith("__failed__/") and "Import failed" in failed.error


def test_run_analysis_on_unavailable_file(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "u.fits"), "light", project_id=project)
    drain()
    f = files_of(project)[-1]
    with SessionLocal() as db:
        db.query(AnalysisTask).delete()                      # tasks of the automatic measuring
        db.get(FrameFile, f.id).status = "error"
        db.add(AnalysisTask(file_id=f.id, kind="optics"))
        db.commit()
    drain()
    with SessionLocal() as db:
        t = db.scalars(select(AnalysisTask).where(AnalysisTask.file_id == f.id)).one()
        assert t.status == "error" and "not available" in t.error


def test_interrupted_tasks_are_requeued(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "r.fits"), "light", project_id=project)
    drain()
    f = files_of(project)[-1]
    with SessionLocal() as db:
        db.query(AnalysisTask).delete()
        db.add(AnalysisTask(file_id=f.id, kind="solve", status="running"))
        db.add(AnalysisTask(file_id=f.id, kind="optics", status="done",
                            finished_at=datetime.now() - timedelta(days=2)))
        db.commit()
    worker.cleanup_tasks()
    with SessionLocal() as db:
        ts = db.scalars(select(AnalysisTask).where(AnalysisTask.file_id == f.id)).all()
        assert [t.status for t in ts] == ["queued"]                               # old done removed
        db.query(AnalysisTask).delete()
        db.commit()


# ---------- second review fixes ----------
def test_conditions_use_light_frames_only():
    light = SimpleNamespace(id=1, frame_type="light", solve=json.dumps(fake_solve(sky={"sqm": 20.0, "zp_s": 20.0, "gain": "100"})),
                            optics=json.dumps({"fwhm_px": 2.0, "fwhm_centre_px": 2.0, "metrics": {}}))
    stack = SimpleNamespace(id=2, frame_type="stack", solve=json.dumps(fake_solve(sky={"sqm": 23.0, "zp_s": 26.0, "gain": "100"})),
                            optics=json.dumps({"fwhm_px": 1.0, "fwhm_centre_px": 1.0, "metrics": {}}))
    m = measured.session_measured([light, stack], aperture_mm=None)
    assert m["sqm"] == 20.0 and m["zp_s"] == 20.0 and m["seeing"] == 3.0      # 2 px × 1.5″


def test_gain_normalisation_and_modal_gain():
    assert measured.norm_gain("101.0") == measured.norm_gain(101) == "101" and measured.norm_gain(" ") is None
    assert measured.norm_gain("ISO 800") == "ISO 800"
    assert measured.equipment_key("T", "C", "101.0") == measured.equipment_key("T", "C", "101")
    fs = [SimpleNamespace(id=i, frame_type="light", optics=None,
                          solve=json.dumps(fake_solve(sky={"zp_s": zp, "gain": g})))
          for i, (zp, g) in enumerate([(20.0, "100"), (20.2, "100.0"), (25.0, "300")])]
    m = measured.session_measured(fs)
    assert m["gain"] == "100" and m["zp_s"] == 20.1                            # other gains not mixed in


def test_undersampled_detection_for_colour_cameras():
    o = {"fwhm_px": 3.0, "fwhm_centre_px": 3.0, "metrics": {}, "bin": 2}       # 3 sensor px = 1.5 measured px
    f = SimpleNamespace(id=1, frame_type="light", solve=json.dumps(fake_solve(scale_arcsec=1.0)), optics=json.dumps(o))
    assert measured.session_measured([f])["seeing_undersampled"] is True
    o["bin"] = 1
    f.optics = json.dumps({**o, "fwhm_px": 4.0, "fwhm_centre_px": 4.0})
    assert measured.session_measured([f])["seeing_undersampled"] is False


def test_seeing_below_diffraction_is_none():
    assert conditions.seeing(0.5, 127) is None


def test_defocused_night_is_not_auto_filled(client, project, tmp_path):
    sid = default_session(project)
    upload(client, write_fits(tmp_path / "foc.fits"), "light", project_id=project, session_id=sid)
    drain()
    f = files_of(project)[-1]
    with SessionLocal() as db:
        x = db.get(FrameFile, f.id)
        x.solve = json.dumps(fake_solve(scale_arcsec=1.5, sky={"sqm": 19.0}))
        x.optics = json.dumps({"fwhm_px": 8.0, "fwhm_centre_px": 8.0, "metrics": {}})    # 12″ – focus, not seeing
        db.commit()
        s = db.get(ImagingSession, sid)
        s.seeing = s.sqm = None
        done = analysis.fill_conditions(db, s)
        assert "seeing" not in done and done["sqm"].startswith("19.00")
        assert analysis.fill_conditions(db, s, force=True)["seeing"].startswith("12.0")   # manual = as measured
        db.rollback()
    html = client.get(f"/projects/{project}").text
    assert "rarely the atmosphere alone" in html


def test_failed_optics_is_remembered_and_stops_auto_measure(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "few.fits", data=np.full((200, 200), 500, np.uint16)), "light",
           project_id=project)
    drain()
    f = files_of(project)[-1]
    with SessionLocal() as db:
        x = db.get(FrameFile, f.id)
        analysis.optics_store(x, {"ok": False, "error": "Too few usable stars (0)"})
        db.commit()
        assert json.loads(x.optics)["ok"] is False
        assert analysis.auto_measure_project(db, project) == 0          # the session counts as measured
    m = client.get(f"/api/sessions/{f.session_id}/conditions").json()
    assert m.get("analysed", 0) == 0                                            # failure not in the medians


def test_new_batch_never_drops_queued_tasks(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "w1.fits"), "light", project_id=project)
    drain()
    f = files_of(project)[-1]
    with SessionLocal() as db:
        db.add(AnalysisTask(file_id=f.id, kind="optics", status="done", finished_at=datetime.now()))
        db.commit()
    # a queued task appears between the "nothing running?" check and the wipe: only finished rows go
    with SessionLocal() as db:
        db.add(AnalysisTask(file_id=f.id, kind="solve", status="queued"))
        db.commit()
    client.post("/api/analysis/queue", json={"scope": "files", "ids": [f.id], "kind": "optics"})
    with SessionLocal() as db:
        kinds = sorted((t.kind, t.status) for t in db.scalars(select(AnalysisTask).where(AnalysisTask.file_id == f.id)))
        assert ("solve", "queued") in kinds
        db.query(AnalysisTask).delete()
        db.commit()


def test_stack_solve_has_no_sky_values(monkeypatch, tmp_path):
    monkeypatch.setattr(platesolve, "solve", lambda *a, **k: fake_solve(sky_adu=900.0, lum_bin=1,
                                                                         photometry={"zero_point": 23.0}))
    inp = {"src": write_fits(tmp_path / "st.fits"), "header": "PEDESTAL= 100\n", "pixel_um": None, "focal": None,
           "sensor": None, "telescope": None, "camera": None, "exposure": 600, "gain": "100", "calib": None,
           "calib_kind": None, "frame_type": "stack"}
    assert "sky" not in analysis.solve_compute(inp)
    inp["frame_type"] = "light"
    assert analysis.solve_compute(inp)["sky"]["gain"] == "100"


# ---------- pages ----------
def test_settings_saved_from_all_tabs(client):
    r = client.post("/settings", data={"app_title": "Astro test", "gallery_page_size": "30", "zip_compression": "fast",
                                       "bg_previews": "on", "cond_auto_fill": "on", "open_filters": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    from app.core import load_settings
    cfg = load_settings()
    assert cfg["app_title"] == "Astro test" and cfg["gallery_page_size"] == 30 and cfg["zip_compression"] == "fast"
    assert cfg["cond_auto_fill"] is True and cfg["cond_auto_measure"] is False and cfg["open_filters"] is True
    client.post("/settings", data={"reset": "1"})
    assert load_settings()["cond_auto_measure"] is True
    assert "invalid" in client.get("/settings").text                         # hidden-tab validation helper


def test_stats_page_without_measurements(client):
    r = client.get("/stats")
    assert r.status_code == 200 and "Could not load the measurements" in r.text
    assert client.get("/api/stats/optics").status_code == 200
