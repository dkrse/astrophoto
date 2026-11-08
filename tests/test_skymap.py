"""Sky map: exact field outlines (TAN WCS), catalogue positions, reference lines, page and API."""
import json

import numpy as np
import pytest

from app import skymap
from app.models import SessionLocal, FrameFile
from conftest import write_fits, upload, drain
from test_sky import fake_solve


@pytest.mark.parametrize("ra,dec,rot", [(10.68, 41.27, 0), (148.9, 69.07, 33), (359.9, -5, 120), (80, 89.2, 10),
                                        (200, -60, 250)])
def test_tan_matches_astropy(ra, dec, rot):
    from astropy.wcs import WCS
    s, r = 2.5 / 3600, np.radians(rot)
    wcs = {"crpix": [600.5, 400.5], "crval": [ra, dec], "w": 1200, "h": 800,
           "cd": [[-s * np.cos(r), s * np.sin(r)], [s * np.sin(r), s * np.cos(r)]]}
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crpix, w.wcs.crval, w.wcs.cd = wcs["crpix"], wcs["crval"], np.array(wcs["cd"])
    for x, y in [(0.5, 0.5), (1200.5, 800.5), (1, 800), (333, 77)]:
        a, d = w.all_pix2world(x, y, 1)
        b = skymap.tan_to_radec(wcs, x, y)
        assert abs(((float(a) - b[0] + 180) % 360) - 180) * np.cos(np.radians(float(d))) * 3600 < 0.5
        assert abs(float(d) - b[1]) * 3600 < 0.5


def test_outline_closed_ring_around_centre():
    sv = fake_solve(ra=202.47, dec=47.2, w=1000, h=600)
    pts = skymap.outline(sv["wcs"])
    assert len(pts) == 16
    ras, decs = [p[0] for p in pts], [p[1] for p in pts]
    # 1000 × 600 px at 1.5″ = 25′ × 15′; east left: RA decreases to the right of the image
    assert abs(max(decs) - min(decs) - 0.25) < 0.01
    assert abs((max(ras) - min(ras)) * np.cos(np.radians(47.2)) - 25 / 60) < 0.01
    assert min(ras) < 202.47 < max(ras) and min(decs) < 47.2 < max(decs)


def test_reference_lines_and_catalogue():
    ln = skymap.reference_lines()
    gc = ln["galactic_centre"]                              # Sgr A*: 17h45.6m −28°56′
    assert abs(gc[0] - 266.40) < 0.05 and abs(gc[1] + 28.94) < 0.05
    assert max(abs(p[1]) for p in ln["ecliptic"]) == pytest.approx(23.44, abs=0.05)
    assert skymap.constellation(10.68, 41.27) == "Andromeda"
    assert skymap.constellation(83.82, -5.39) == "Orion"
    cat = {o["n"]: o for o in skymap.catalogue_points(8)}
    assert abs(cat["M31"]["ra"] - 10.685) < 0.01 and abs(cat["M31"]["dec"] - 41.269) < 0.01
    assert cat["M42"]["alt"] == "NGC 1976"
    assert all(o["m"] or o["mag"] <= 8 for o in cat.values())


def test_skymap_api_and_page(client, project, tmpdir_path):
    upload(client, write_fits(tmpdir_path / "sm.fits", EXPTIME=60.0), project_id=project)
    drain()
    with SessionLocal() as db:
        f = db.query(FrameFile).filter_by(project_id=project).first()
        f.solve = json.dumps(fake_solve(ra=83.82, dec=-5.39))
        db.commit()
    skymap._CACHE.clear()
    d = client.get("/api/skymap?mag=8").json()
    p = next(x for x in d["projects"] if x["id"] == project)
    assert p["con"] == "Orion" and len(p["fields"]) == 1 and len(p["fields"][0]["outline"]) == 16
    assert p["obj"]["n"] == "M42" and abs(p["ra"] - 83.82) < 1e-3
    assert d["ecliptic"] and d["galactic"] and any(o["n"] == "M42" for o in d["catalogue"])
    assert client.get("/api/skymap?mag=99").status_code == 422
    html = client.get("/skymap").text
    assert 'id="sm"' in html and "Sky map" in html


def test_skymap_unsolved_project_uses_catalogue(client, project):
    skymap._CACHE.clear()
    p = next(x for x in client.get("/api/skymap").json()["projects"] if x["id"] == project)
    assert p["fields"] == [] and p["obj"]["n"] == "M42" and p["con"] == "Orion"


def test_skymap_requires_login(anon):
    assert anon.get("/api/skymap", follow_redirects=False).status_code in (302, 303, 401)


def test_valid_wcs_rejects_broken_solves():
    good = fake_solve()["wcs"]
    assert skymap.valid_wcs(good)
    assert not skymap.valid_wcs({**good, "crval": [None, None]})
    assert not skymap.valid_wcs({**good, "crpix": [float("nan"), 1]})
    assert not skymap.valid_wcs({**good, "cd": [[0, 0], [0, 0]]})          # CDELT-only solve: no field size
    assert not skymap.valid_wcs({k: v for k, v in good.items() if k != "w"})
    assert not skymap.valid_wcs(None)


def test_bad_solve_does_not_break_map_and_cache_follows_changes(client, project, tmpdir_path):
    for i in range(2):
        upload(client, write_fits(tmpdir_path / f"b{i}.fits", data=None, EXPTIME=30.0 + i), project_id=project)
    drain()
    with SessionLocal() as db:
        fs = db.query(FrameFile).filter_by(project_id=project).order_by(FrameFile.id).all()
        bad = fake_solve()
        bad["wcs"]["crval"] = [None, None]
        fs[0].solve = json.dumps(bad)
        fs[1].solve = json.dumps(fake_solve(ra=83.82, dec=-5.39))
        db.commit()
    d = client.get("/api/skymap").json()                    # no 500 – the bad field is skipped
    p = next(x for x in d["projects"] if x["id"] == project)
    assert len(p["fields"]) <= 1
    # rename + re-solve (same number of solves) -> fresh data, not the 5-minute cache
    from app.models import Project
    with SessionLocal() as db:
        db.get(Project, project).name = "Renamed sky project"
        f = db.get(FrameFile, fs[1].id)
        f.solve = json.dumps(fake_solve(ra=10.68, dec=41.27))
        db.commit()
    p = next(x for x in client.get("/api/skymap").json()["projects"] if x["id"] == project)
    assert p["name"] == "Renamed sky project" and p["con"] == "Andromeda"


def test_solve_store_never_writes_nan():
    from types import SimpleNamespace
    from app.analysis import solve_store
    from app.platesolve import _float
    f = SimpleNamespace(solve=None)
    solve_store(f, {"ok": True, "sky": {"sqm": float("nan"), "zp_s": float("inf")}, "x": [1.0, float("-inf")]})
    assert "NaN" not in f.solve and "Infinity" not in f.solve
    assert json.loads(f.solve)["sky"] == {"sqm": None, "zp_s": None}
    assert _float("nan") is None and _float("inf") is None and _float("2.5") == 2.5


def test_tile_stars_brightest_first(tmpdir_path):
    from app import optics
    from test_sky import star_field
    p = write_fits(tmpdir_path / "of.fits", data=star_field())
    r = optics.analyse(p)
    assert r["ok"] and len(r["tile_stars"]) == 9
    assert all(len(t) <= 300 for t in r["tile_stars"])
