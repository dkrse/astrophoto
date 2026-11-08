"""Observing conditions: seeing, transparency, sky brightness (SQM) – maths, storage, session values,
manual and automatic measuring / filling, settings and the tabbed Stats / Settings pages."""
import json
import math
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select

from app import analysis, conditions, measured, platesolve
from app.models import SessionLocal, FrameFile, ImagingSession, CatalogItem, CalibrationSet, AnalysisTask, Setting
from conftest import drain, write_fits, upload, star_frame, default_session
from test_sky import fake_solve, star_field


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


def _set(**kv):
    with SessionLocal() as db:
        for k, v in kv.items():
            db.merge(Setting(key=k, value=v))
        db.commit()


# ---------- maths ----------
def test_diffraction_and_seeing():
    d = conditions.diffraction_arcsec(127)
    assert abs(d - 0.92) < 0.02                                     # 1.03 λ/D for 127 mm
    se = conditions.seeing(2.0, 127, 0.5)
    assert abs(se["seeing"] - math.sqrt(4 - d * d)) < 0.01 and not se.get("undersampled")
    assert conditions.seeing(3.0, None, 1.5)["undersampled"]        # 2 px per FWHM
    assert conditions.seeing(8.0, 80, 1.0)["likely_focus"]
    assert conditions.seeing(None) is None and conditions.seeing(0) is None
    assert [conditions.seeing_label(x) for x in (1.2, 2.0, 3.0, 4.0, 6.0)] == \
        ["excellent", "good", "average", "poor", "bad"]


def test_sky_brightness_and_zero_point():
    # zero point 25 mag, sky 1500 ADU, offset 500 → 1000 ADU per pixel of 2″ → 25 − 2.5·log10(1000 / 4)
    assert conditions.sky_brightness(25.0, 1500, 500, 2.0) == round(25 - 2.5 * math.log10(250), 2)
    assert conditions.sky_brightness(25.0, 400, 500, 2.0) is None        # below the offset
    assert conditions.sky_brightness(None, 1500, 500, 2.0) is None
    assert conditions.zp_per_second(25.0, 100) == 20.0
    assert conditions.zp_per_second(25.0, None) is None
    assert conditions.sqm_label(17.5) == "city" and conditions.sqm_label(21.6) == "very dark"


def test_sky_level_ignores_stars():
    img = np.full((400, 400), 1000.0)
    img[::10, ::10] = 60000                                          # many bright "stars"
    assert abs(conditions.sky_level(img) - 1000) < 1


def test_pedestal_sources(tmp_path):
    src = write_fits(tmp_path / "l.fits", data=star_frame())
    assert conditions.pedestal(src, "PEDESTAL= 512 / offset\n") == (512.0, "FITS header")
    bias = write_fits(tmp_path / "b.fits", data=np.full((100, 120), 300, np.uint16))
    v, srcname = conditions.pedestal(src, None, bias, "bias")
    assert v == 300.0 and "bias" in srcname
    assert conditions.pedestal(src, None) == (None, None)


def test_sky_result_needs_offset_and_photometry():
    res = fake_solve(scale_arcsec=1.0, sky_adu=1500.0, lum_bin=2, photometry={"zero_point": 24.0})
    r = conditions.sky_result(res, 500.0, "FITS header", exposure_s=60, gain="100")
    assert r["sqm"] == round(24 - 2.5 * math.log10(1000 / 4), 2)          # 2×2 binned: 2″ per measured pixel
    assert r["zp_s"] == round(24 - 2.5 * math.log10(60), 3) and r["gain"] == "100"
    assert "bias or dark" in conditions.sky_result(res, None, None)["sqm_note"]
    assert conditions.sky_result(fake_solve(), 500, "x")["sqm_note"] == "no photometry"


def test_solve_compute_adds_sky_values(monkeypatch, tmp_path):
    bias = write_fits(tmp_path / "bias.fits", data=np.full((100, 100), 200, np.uint16))
    monkeypatch.setattr(platesolve, "solve", lambda *a, **k: fake_solve(
        scale_arcsec=1.5, sky_adu=900.0, lum_bin=1, photometry={"zero_point": 23.0}))
    inp = {"src": write_fits(tmp_path / "x.fits"), "header": None, "pixel_um": None, "focal": None, "sensor": None,
           "telescope": None, "camera": None, "exposure": 30.0, "gain": "120", "calib": bias, "calib_kind": "bias"}
    res = analysis.solve_compute(inp)
    assert res["sky"]["pedestal"] == 200.0 and res["sky"]["sqm"] is not None and res["sky"]["zp_s"] is not None


# ---------- session values ----------
def test_session_values_seeing_sqm_transparency():
    def f(i, zp, sqm, fwhm):
        sol = fake_solve(scale_arcsec=1.0, sky={"sqm": sqm, "zp_s": zp, "gain": "100", "pedestal_src": "FITS header"})
        return SimpleNamespace(id=i, solve=json.dumps(sol),
                               optics=json.dumps({"fwhm_px": fwhm + 0.5, "fwhm_centre_px": fwhm, "elong": 1.1, "metrics": {}}))
    m = measured.session_measured([f(1, 20.0, 20.5, 2.0), f(2, 20.2, 20.7, 2.4), f(3, 20.4, 20.9, 2.2)],
                                  aperture_mm=200)
    assert m["sqm"] == 20.7 and m["zp_s"] == 20.2 and m["gain"] == "100"
    d = conditions.diffraction_arcsec(200)
    assert abs(m["seeing"] - math.sqrt(2.2 ** 2 - d * d)) < 0.01       # centre FWHM, diffraction removed
    ref = {measured.equipment_key("T", "C", "100"): 20.7}
    measured.add_transparency(m, "T", "C", ref)
    assert m["transparency_mag"] == 0.5 and m["transparency_pct"] == round(10 ** (-0.2) * 100)


def test_zero_point_reference_is_best_night_of_same_setup(client, project, tmp_path):
    s1 = default_session(project)
    with SessionLocal() as db:
        s2 = ImagingSession(project_id=project, date=date(2025, 10, 20))
        db.add(s2)
        db.commit()
        s2id = s2.id
        for sid in (s1, s2id):
            s = db.get(ImagingSession, sid)
            s.telescope, s.camera = "Ref scope", "Ref cam"
        db.commit()
    for n, sid in (("z1.fits", s1), ("z2.fits", s2id)):
        upload(client, write_fits(tmp_path / n, data=star_frame(seed=len(n) + sid)), "light", project_id=project,
               session_id=sid)
    drain()
    with SessionLocal() as db:
        for f in db.scalars(select(FrameFile).where(FrameFile.project_id == project)):
            zp = 21.0 if f.session_id == s1 else 20.25
            f.solve = json.dumps(fake_solve(sky={"zp_s": zp, "gain": "100"}))
        db.commit()
        ref = measured.zp_reference(db, max_age_s=0)
        assert ref[measured.equipment_key("Ref scope", "Ref cam", "100")] == 21.0
    html = client.get(f"/projects/{project}").text
    assert "of the best night of this setup" in html and "−0.75" in html.replace("-0.75", "−0.75")


# ---------- filling the session fields ----------
def _session_with_values(client, project, tmp_path):
    with SessionLocal() as db:
        for kind, name, spec in (("telescope", "Cond scope", {"aperture_mm": 80, "focal_mm": 1500}),
                                 ("camera", "Cond cam", {"pixel_um": 3.76})):
            if not db.scalars(select(CatalogItem).where(CatalogItem.kind == kind, CatalogItem.name == name)).first():
                db.add(CatalogItem(kind=kind, name=name, specs=json.dumps(spec)))
        s = db.get(ImagingSession, default_session(project))
        s.telescope, s.camera, s.seeing, s.sqm = "Cond scope", "Cond cam", None, None
        db.commit()
        sid = s.id
    upload(client, write_fits(tmp_path / "c.fits", data=star_field(seed=7)), "light", project_id=project, session_id=sid)
    drain()
    f = files_of(project)[-1]
    with SessionLocal() as db:
        x = db.get(FrameFile, f.id)
        x.optics = json.dumps({"fwhm_px": 3.2, "fwhm_centre_px": 3.0, "elong": 1.1, "metrics": {}})
        x.solve = json.dumps(fake_solve(scale_arcsec=0.6, sky={"sqm": 20.4, "zp_s": 20.0, "gain": "1"}))
        db.commit()
    return sid


def test_fill_conditions_respects_user_values(client, project, tmp_path):
    sid = _session_with_values(client, project, tmp_path)
    with SessionLocal() as db:
        s = db.get(ImagingSession, sid)
        done = analysis.fill_conditions(db, s)
        assert s.seeing.endswith("″ (measured)") and s.sqm == "20.40 (measured)" and set(done) == {"seeing", "sqm"}
        s.seeing = "3/5 Pickering"                                   # typed by the user
        s.sqm = "20.40 (measured)"
        assert analysis.fill_conditions(db, s) == {} and s.seeing == "3/5 Pickering"
        assert analysis.fill_conditions(db, s, force=True)["seeing"].endswith("(measured)")
        db.rollback()


def test_conditions_api_manual(client, project, tmp_path):
    sid = _session_with_values(client, project, tmp_path)
    c = client.get(f"/api/sessions/{sid}/conditions").json()
    assert c["sqm"] == 20.4 and c["seeing"] > 0
    with SessionLocal() as db:
        db.get(ImagingSession, sid).seeing = "my note"
        db.commit()
    r = client.post(f"/api/sessions/{sid}/conditions/apply").json()
    assert r["ok"] and r["set"]["seeing"].endswith("(measured)")              # manual = overwrite
    r = client.post(f"/api/sessions/{sid}/conditions/measure").json()
    assert r["ok"] and r["queued"] == 2                                       # solve + optics of the one light
    client.post("/api/analysis/cancel", json={})
    assert client.post("/api/sessions/999999/conditions/measure").status_code == 404
    html = client.get(f"/projects/{project}").text
    assert "Measure conditions" in html and "Sky brightness" in html and "Use for Seeing / SQM" in html


def test_optics_endpoint_reports_seeing(client, project, tmp_path):
    sid = _session_with_values(client, project, tmp_path)
    f = files_of(project)[-1]
    r = client.get(f"/api/files/{f.id}/optics", params={"tiles": 0}).json()
    assert r["ok"] and r["seeing"]["seeing"] > 0 and r["seeing"]["label"]


# ---------- automatic measuring / filling (Settings) ----------
def test_auto_measure_new_night_and_auto_fill(client, project, tmp_path):
    try:
        _set(cond_auto_measure="1", cond_auto_fill="1", bg_analysis="1")
        upload(client, write_fits(tmp_path / "auto.fits", data=star_field(seed=9)), "light", project_id=project)
        # processing the frame queues solve + optics for the new night; the worker then runs them
        drain()
        with SessionLocal() as db:
            tasks = db.scalars(select(AnalysisTask)).all()
            f = db.scalars(select(FrameFile).where(FrameFile.original_name == "auto.fits")).one()
            assert {t.kind for t in tasks if t.file_id == f.id} == {"solve", "optics"}
            assert f.optics                                          # optics ran (solve fails without ASTAP DB)
    finally:
        _set(cond_auto_measure="1", cond_auto_fill="1")


def test_auto_measure_switched_off(client, project, tmp_path):
    try:
        _set(cond_auto_measure="0")
        upload(client, write_fits(tmp_path / "off2.fits", data=star_field(seed=10)), "light", project_id=project)
        drain()
        with SessionLocal() as db:
            f = db.scalars(select(FrameFile).where(FrameFile.original_name == "off2.fits")).one()
            assert not db.scalars(select(AnalysisTask).where(AnalysisTask.file_id == f.id)).first()
    finally:
        _set(cond_auto_measure="1")


def test_auto_fill_after_background_analysis(client, project, tmp_path):
    sid = _session_with_values(client, project, tmp_path)
    f = files_of(project)[-1]
    with SessionLocal() as db:
        db.get(ImagingSession, sid).seeing = None
        db.get(ImagingSession, sid).sqm = None
        db.commit()
    client.post("/api/analysis/queue", json={"scope": "files", "ids": [f.id], "kind": "optics"})
    drain()
    with SessionLocal() as db:
        s = db.get(ImagingSession, sid)
        assert s.seeing and s.seeing.endswith("(measured)")
    try:
        _set(cond_auto_fill="0")
        with SessionLocal() as db:
            db.get(ImagingSession, sid).seeing = None
            db.commit()
        client.post("/api/analysis/queue", json={"scope": "files", "ids": [f.id], "kind": "optics"})
        drain()
        with SessionLocal() as db:
            assert db.get(ImagingSession, sid).seeing is None
    finally:
        _set(cond_auto_fill="1")


# ---------- tabs ----------
def test_stats_and_settings_tabs(client):
    st = client.get("/stats").text
    for pane in ("t-overview", "t-conditions", "t-health"):
        assert f'id="{pane}"' in st and f'href="#{pane}"' in st
    assert "'seeing','Seeing'" in st and "'sqm','Sky brightness'" in st
    se = client.get("/settings").text
    for pane in ("t-general", "t-files", "t-gallery", "t-processing", "t-panels"):
        assert f'id="{pane}"' in se
    assert 'name="cond_auto_measure"' in se and 'name="cond_auto_fill"' in se
    # every tab stays inside the one settings form (saved together)
    a = se.index('class="card settings"')
    end = se.index("</form>", a)
    assert all(a < se.index(f'id="{p}"') < end for p in ("t-general", "t-processing", "t-panels"))
