"""Mosaic planning / coverage, calibration check of sessions and optics recommendations from the history."""
import json
import math
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select

from app import calibcheck, mosaic, opticsadvice
from app.models import SessionLocal, FrameFile, ImagingSession, Project
from conftest import upload, write_fits, star_frame, drain, default_session


# ---------- mosaic: geometry ----------
def plan(**k):
    return mosaic.normalize({"cols": 2, "rows": 1, "overlap": 0, "fov_w": 60, "fov_h": 40, "rot": 0,
                             "ra": 180.0, "dec": 0.0, **k})


def sep(a, b):
    r = np.radians
    c = np.sin(r(a[1])) * np.sin(r(b[1])) + np.cos(r(a[1])) * np.cos(r(b[1])) * np.cos(r(a[0] - b[0]))
    return float(np.degrees(np.arccos(np.clip(c, -1, 1))))


def test_plane_roundtrip():
    for ra0, dec0 in ((10, 20), (359.5, -60), (0.2, 88.0)):
        ra, dec = mosaic.from_plane([0.3, -0.7], [0.5, -0.2], ra0, dec0)
        xi, eta = mosaic.to_plane(ra, dec, ra0, dec0)
        assert np.allclose(xi, [0.3, -0.7], atol=1e-9) and np.allclose(eta, [0.5, -0.2], atol=1e-9)


def test_panels_north_up_east_left():
    p1, p2 = mosaic.panels(plan())
    assert p1["n"] == 1 and p2["n"] == 2
    assert p1["ra"] > p2["ra"]                                    # panel 1 is left = east = larger RA
    assert sep((p1["ra"], p1["dec"]), (p2["ra"], p2["dec"])) == pytest.approx(1.0, abs=1e-3)   # one field width
    assert abs(p1["dec"]) < 1e-6 and len(p1["corners"]) == 4


def test_overlap_and_rotation():
    p1, p2 = mosaic.panels(plan(overlap=20))
    assert sep((p1["ra"], p1["dec"]), (p2["ra"], p2["dec"])) == pytest.approx(0.8, abs=1e-3)
    q1, q2 = mosaic.panels(plan(rot=90))        # "up" points east -> "right" (= up − 90°) points north
    assert q1["dec"] < q2["dec"] and abs(q1["ra"] - q2["ra"]) < 1e-3
    r1, r2 = mosaic.panels(plan(rows=2, cols=1))
    assert r1["dec"] > r2["dec"]                                  # row 1 north


@pytest.mark.parametrize("bad", [{"cols": 0}, {"rows": 11}, {"overlap": 70}, {"fov_w": 0}, {"dec": 95},
                                 {"ra": "abc"}, {"rot": float("nan")}])
def test_normalize_rejects(bad):
    assert plan(**bad) is None


def test_normalize_wraps_ra_and_rounds_grid():
    p = plan(ra=-10, cols=2.6)
    assert p["ra"] == pytest.approx(350) and p["cols"] == 3
    assert mosaic.normalize(None) is None and mosaic.load("not json") is None


def field_wcs(ra, dec, fov_w=60, fov_h=40, w=600, h=400, rot=0.0):
    """A solved field (north up, east left for rot 0) like platesolve stores it."""
    sx, sy = fov_w / 60 / w, fov_h / 60 / h
    c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    cd = [[-sx * c, sy * s], [sx * s, sy * c]]
    return {"crpix": [w / 2 + 0.5, h / 2 + 0.5], "crval": [ra, dec], "cd": cd, "w": w, "h": h}


def test_coverage_and_integration():
    pl = plan()
    p1, p2 = mosaic.panels(pl)
    frames = [{"session": 1, "wcs": field_wcs(p2["ra"], p2["dec"])}] * 3 \
        + [{"session": 2, "wcs": field_wcs(p1["ra"], p1["dec"])}, {"session": 2, "wcs": field_wcs(p2["ra"], p2["dec"])}]
    sess = {1: {"date": "2026-01-01", "integration": 3600.0, "lights": 30},
            2: {"date": "2026-01-02", "integration": 1200.0, "lights": 10}}
    out = mosaic.coverage(pl, frames, sess)
    a, b = out["panels"]
    assert a["covered"] > 0.95 and b["covered"] > 0.95 and a["status"] == b["status"] == "done"
    assert b["integration"] == pytest.approx(3600 + 600) and a["integration"] == pytest.approx(600)   # night 2 split
    assert [s["id"] for s in b["sessions"]] == [1, 2]
    assert out["summary"]["done"] == 2 and out["summary"]["min_integration"] == 600
    assert len(out["fields"]) == 2                                # distinct pointings drawn once


def test_partial_and_missing_panels():
    pl = plan(cols=3)
    p1, p2, p3 = mosaic.panels(pl)
    half = field_wcs(p2["ra"] + 0.25, p2["dec"])                 # half of panel 2, a bit of panel 1
    out = mosaic.coverage(pl, [{"session": 1, "wcs": half}], {1: {"integration": 60.0}})
    st = [p["status"] for p in out["panels"]]
    assert st[2] == "missing" and st[1] == "partial" and 0.3 < out["panels"][1]["covered"] < 0.8


def test_defaults_from_a_solve():
    w = field_wcs(100.0, 20.0, fov_w=90, fov_h=60, rot=30.0)
    d = mosaic.defaults({"wcs": w}, {"ra": 101.0, "dec": 21.0})
    assert d["fov_w"] == pytest.approx(90, abs=0.2) and d["fov_h"] == pytest.approx(60, abs=0.2)
    assert d["rot"] == pytest.approx(30, abs=0.2) and d["ra"] == 101.0 and d["cols"] == 1
    assert mosaic.defaults(None, None) is None


def test_mosaic_api(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "mz.fits", data=star_frame(seed=950), EXPTIME=120.0), "light",
           project_id=project)
    drain()
    r = client.get(f"/api/projects/{project}/mosaic").json()
    assert r["plan"] is None and "Solve" in r["error"]
    with SessionLocal() as db:
        f = db.scalars(select(FrameFile).where(FrameFile.project_id == project)).first()
        f.solve = json.dumps({"ok": True, "wcs": field_wcs(83.82, -5.39)})      # M42 (project target)
        db.commit()
    r = client.get(f"/api/projects/{project}/mosaic").json()
    assert not r["stored"] and r["plan"]["cols"] == 1 and r["summary"]["done"] == 1
    assert r["target"]["name"] == "M42" and r["panels"][0]["integration"] == 120.0
    new = {**r["plan"], "cols": 2, "rows": 2, "overlap": 10}
    s = client.post(f"/api/projects/{project}/mosaic", json={"plan": new}).json()
    assert s["stored"] and s["summary"]["panels"] == 4 and s["summary"]["done"] == 0
    assert client.post(f"/api/projects/{project}/mosaic", json={"plan": {**new, "cols": 0}}).status_code == 400
    with SessionLocal() as db:
        assert json.loads(db.get(Project, project).mosaic)["rows"] == 2
    client.post(f"/api/projects/{project}/mosaic", json={"clear": True})
    with SessionLocal() as db:
        assert db.get(Project, project).mosaic is None
    assert 'id="mosaic"' in client.get(f"/projects/{project}").text


# ---------- calibration check ----------
def S(**k):
    return SimpleNamespace(**{"camera": "ASI2600", "telescope": "RC8", "gain": "100", "exposure_s": 300.0,
                              "filter": "Ha", "cam_temp": -10.0, "date": date(2026, 3, 10), "calib_sets": [], **k})


def C(t, **k):
    return SimpleNamespace(**{"name": f"{t} set", "frame_type": t, "camera": "ASI2600", "telescope": "RC8",
                              "gain": "100", "offset": "50", "exposure_s": 300.0 if t == "dark" else
                              (2.0 if t in ("flat", "darkflat") else 0.001), "temperature": "-10",
                              "filter": "Ha" if t == "flat" else None, "capture_date": date(2026, 3, 10), **k})


def L(**k):
    return SimpleNamespace(**{"frame_type": "light", "exposure_s": 300.0, "gain": "100", "filter": "Ha",
                              "temperature": "-10", "rejected": False, **k})


def titles(f):
    return {x["title"].split(":")[0] for x in f}


def test_calibration_fits():
    f = calibcheck.check(S(), [L()], [C("dark"), C("flat"), C("bias")])
    assert [x["level"] for x in f] == ["ok"] and calibcheck.summary(f) == {"level": "ok", "problems": 0}


def test_calibration_missing_sets():
    f = calibcheck.check(S(), [L()], [])
    assert {"No darks linked", "No flats linked"} <= titles(f)
    assert calibcheck.summary(f)["level"] == "warn"
    assert calibcheck.check(S(), [], []) == []                    # no lights: nothing to check


def test_dark_mismatches():
    f = calibcheck.check(S(), [L()], [C("dark", exposure_s=120.0, gain="0", temperature="5", camera="ASI294",
                                        capture_date=date(2024, 1, 1)), C("flat"), C("bias")])
    t = titles(f)
    assert {"Other exposure", "Other gain", "Other sensor temperature", "Other camera", "Old darks"} <= t
    assert next(x for x in f if x["title"].startswith("Other exposure"))["level"] == "warn"   # scalable with bias
    nobias = calibcheck.check(S(), [L()], [C("dark", exposure_s=120.0), C("flat"), C("darkflat")])
    assert next(x for x in nobias if x["title"].startswith("Other exposure"))["level"] == "bad"


def test_gain_and_temperature_tolerances():
    f = calibcheck.check(S(gain="100.0"), [L()], [C("dark", temperature="-11.5"), C("flat"), C("bias")])
    assert [x["level"] for x in f] == ["ok"]                       # 100 = 100.0, 1.5 °C within tolerance


def test_flat_rules():
    f = calibcheck.check(S(), [L(), L(filter="OIII")], [C("dark"), C("flat", telescope="Esprit", capture_date=date(2026, 1, 1))])
    t = titles(f)
    assert {"Other telescope", "Flats from another month", "Flats missing for a filter",
            "Flats without bias / dark flats"} <= t
    near = calibcheck.check(S(), [L()], [C("dark"), C("flat", capture_date=date(2026, 3, 7)), C("bias")])
    assert "Flats from another night" in titles(near) and calibcheck.summary(near)["level"] == "info"


def test_unknown_values_are_not_mismatches():
    s = S(camera=None, gain=None, cam_temp=None, telescope=None)
    f = calibcheck.check(s, [L(gain=None, temperature=None)], [C("dark", camera="X", gain="7", temperature="20"),
                                                               C("flat", telescope="Y"), C("bias")])
    assert [x["level"] for x in f] == ["ok"]


def test_darkflat_exposure():
    f = calibcheck.check(S(), [L()], [C("dark"), C("flat"), C("darkflat", exposure_s=5.0)])
    assert "Dark flats with another exposure" in titles(f)


def test_project_page_shows_the_calibration_check(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "cc.fits", data=star_frame(seed=960)), "light", project_id=project)
    drain()
    html = client.get(f"/projects/{project}").text
    assert "chip cc-warn" in html and "No darks linked" in html and "No flats linked" in html


# ---------- optics recommendations ----------
def R(d, eq="RC8 · ASI2600", **k):
    return {"equipment": eq, "date": d, "date_dmy": d, "session_id": hash(d) % 1000, "project_id": 1,
            "project": "P", "corners_pct": 10, "tilt_pct": 5, "tilt_dir": None, "elong": 1.05,
            "backfocus_mm": None, "findings_share": {"uniform": 0, "radial": 0, "tangential": 0}, **k}


def adv(rows):
    out = opticsadvice.advise(rows)
    assert len(out) == 1
    return out[0], [x["title"] for x in out[0]["items"]]


def test_clean_history():
    g, t = adv([R("2026-01-0%d" % i) for i in range(1, 5)])
    assert t == ["No recurring optical problem"] and g["nights"] == 4


def test_mechanical_tilt_same_side():
    _, t = adv([R("2026-01-01"), R("2026-01-02", tilt_pct=35, tilt_dir="top left"),
                R("2026-01-03", tilt_pct=40, tilt_dir="top left"), R("2026-01-04", tilt_pct=30, tilt_dir="top left")])
    assert "Mechanical tilt towards the top left of the sensor" in t


def test_tilt_changing_side():
    _, t = adv([R("2026-01-02", tilt_pct=35, tilt_dir="left"), R("2026-01-03", tilt_pct=40, tilt_dir="right"),
                R("2026-01-04", tilt_pct=30, tilt_dir="top")])
    assert "Tilt that changes side" in t


def test_nights_are_aggregated():
    g, t = adv([R("2026-01-02", tilt_pct=35, tilt_dir="left", project="A"),
                R("2026-01-02", tilt_pct=37, tilt_dir="left", project="B"), R("2026-01-03")])
    assert g["nights"] == 2 and not any(x.startswith("Mechanical tilt") for x in t)   # one tilted night only


def test_best_spacing_fitted():
    rows = [R("2026-01-0%d" % i, backfocus_mm=bf, corners_pct=c)
            for i, (bf, c) in enumerate([(54.0, 40), (55.0, 18), (56.0, 30)], 1)]
    _, t = adv(rows)
    best = next(x for x in t if x.startswith("Best spacing"))
    x0 = float(best.split("≈")[1].split()[0])
    a, b, _ = np.polyfit([54, 55, 56], [40, 18, 30], 2)
    assert x0 == pytest.approx(-b / (2 * a), abs=0.05) and 54.5 < x0 < 55.5


def test_spacing_direction_at_the_edge():
    rows = [R("2026-01-01", backfocus_mm=55.0, corners_pct=45), R("2026-01-02", backfocus_mm=56.0, corners_pct=35)]
    _, t = adv(rows)
    assert "Try a longer spacing" in t


def test_soft_corners_without_spacing_asks_for_it():
    g, t = adv([R("2026-01-0%d" % i, corners_pct=40, findings_share={"radial": 0.8}) for i in range(1, 4)])
    item = next(x for x in g["items"] if x["title"] == "Soft corners on several nights")
    assert "radial" in item["text"] and "spacing" in item["text"]


def test_lasting_change_only():
    blip = [R("2026-01-01", corners_pct=10), R("2026-01-02", corners_pct=50), R("2026-01-03", corners_pct=10),
            R("2026-01-04", corners_pct=12)]
    _, t = adv(blip)
    assert not any(x.startswith("Corners") for x in t)            # one bad night is not a change
    fixed = [R("2026-01-01", corners_pct=45, backfocus_mm=55.0), R("2026-01-02", corners_pct=48, backfocus_mm=55.0),
             R("2026-01-03", corners_pct=12, backfocus_mm=56.0), R("2026-01-04", corners_pct=10, backfocus_mm=56.0)]
    g, t = adv(fixed)
    item = next(x for x in g["items"] if x["title"].startswith("Corners better since"))
    assert "55 → 56 mm" in item["text"] and item["level"] == "ok"


def test_guiding_hint():
    _, t = adv([R("2026-01-0%d" % i, findings_share={"uniform": 0.7}) for i in range(1, 4)])
    assert "Stars elongated in one direction on several nights" in t


def test_sessions_without_optics_are_ignored():
    assert opticsadvice.advise([R("2026-01-01", corners_pct=None, tilt_pct=None, elong=None)]) == []


def test_backfocus_saved_and_advice_endpoint(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "backfocus_mm": "55,5"})
    client.post(f"/projects/{project}/sessions", data={"date": "13-10-2025", "backfocus_mm": "abc"})
    with SessionLocal() as db:
        ss = db.scalars(select(ImagingSession).where(ImagingSession.project_id == project)
                        .order_by(ImagingSession.id)).all()
        vals = {str(s.date): s.backfocus_mm for s in ss}
    assert vals["2025-10-12"] == 55.5 and vals["2025-10-13"] is None        # decimal comma accepted
    r = client.get("/api/stats/optics-advice")
    assert r.status_code == 200 and isinstance(r.json(), list)
    assert 'id="advice"' in client.get("/stats").text


def test_adjacent_sides_are_one_side():
    _, t = adv([R("2026-01-02", tilt_pct=40, tilt_dir="top left"), R("2026-01-03", tilt_pct=30, tilt_dir="left"),
                R("2026-01-04", tilt_pct=35, tilt_dir="top left")])
    assert any(x.startswith("Mechanical tilt towards the") and "left" in x for x in t)


def test_night_with_opposite_sessions_has_no_side():
    g, _ = adv([R("2026-01-02", tilt_pct=30, tilt_dir="left", project="A"),
                R("2026-01-02", tilt_pct=30, tilt_dir="right", project="B")])
    assert g["series"][0]["tilt_dir"] is None
    assert opticsadvice._mean_dir([("left", 1), ("right", 1)])[1] == pytest.approx(0, abs=1e-9)
    assert opticsadvice._mean_dir([("top", 2)]) == ("top", 1.0)


def test_unknown_tilt_sides_are_not_a_changing_side():
    g, t = adv([R("2026-01-02", tilt_pct=35), R("2026-01-03", tilt_pct=40), R("2026-01-04", tilt_pct=30)])
    assert "Tilt on several nights – side unknown" in t and "Tilt that changes side" not in t
