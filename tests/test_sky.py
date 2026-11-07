"""Plate solving helpers, deep-sky catalogue, overlays, photometry, optics inspector, measured values per session,
duplicate projects and the metadata export of all projects."""
import io
import json
import math
import zipfile
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select

from app import annotate, measured, optics, photometry, platesolve
from app.models import SessionLocal, FrameFile, ImagingSession, CatalogItem
from app.routes_projects import name_key
from conftest import drain, write_fits, upload, star_frame

M51 = (202.4696, 47.1952)


def fake_solve(ra=M51[0], dec=M51[1], scale_arcsec=1.5, w=1504, h=1504, **extra):
    """A stored plate-solve result like platesolve.solve() returns (north up, east left)."""
    s = scale_arcsec / 3600
    return {"ok": True, "scale_arcsec_px": scale_arcsec, "ra_deg": ra, "dec_deg": dec, "rotation_deg": 0.0,
            "fov_w_arcmin": w * scale_arcsec / 60, "fov_h_arcmin": h * scale_arcsec / 60, "image_px": [w, h],
            "pixel_um": 3.76, "focal_mm": 517.0, "orient": 0,
            "wcs": {"crpix": [w / 2 + 0.5, h / 2 + 0.5], "crval": [ra, dec], "cd": [[-s, 0.0], [0.0, -s]], "w": w, "h": h},
            **extra}


def star_field(w=1200, h=900, n=300, sigma=lambda x, y: 1.8, seed=3):
    rng = np.random.default_rng(seed)
    img = np.full((h, w), 1000.0)
    pos = np.random.default_rng(11)
    for _ in range(n):
        x, y = pos.uniform(10, w - 10), pos.uniform(10, h - 10)
        s = sigma(x / w, y / h)
        r = int(s * 5) + 2
        y0, y1, x0, x1 = int(max(y - r, 0)), int(min(y + r + 1, h)), int(max(x - r, 0)), int(min(x + r + 1, w))
        yy, xx = np.mgrid[y0:y1, x0:x1]
        img[y0:y1, x0:x1] += pos.uniform(3000, 20000) * np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * s * s))
    return np.clip(rng.poisson(img), 0, 65535).astype(np.uint16)


# ---------- names / duplicates ----------
@pytest.mark.parametrize("a,b", [("NGC 123", "ngc123"), ("NGC  123", "ngc 123"), ("NGC-123", "N G C 123"),
                                  ("M 31", "m31")])
def test_name_key_ignores_case_spaces_punctuation(a, b):
    assert name_key(a) == name_key(b)
    assert name_key("NGC 123") != name_key("NGC 1234")


def test_ident_key_strips_leading_zeros():
    assert platesolve.ident_key("NGC0224") == platesolve.ident_key("ngc 224") == "ngc224"
    assert platesolve.ident_key("M 031") == "m31"
    assert platesolve.ident_key("IC 1396") == "ic1396"


def test_duplicate_project_warning_and_confirmation(client, project):
    r = client.get("/api/projects/similar", params={"name": "m  42"})
    assert any(x["id"] == project for x in r.json())                # target "M42" of the fixture project
    r = client.post("/projects/new", data={"name": "m 42"}, follow_redirects=False)
    assert r.status_code == 200 and "already exists" in r.text and 'name="confirm_dup"' in r.text
    r = client.post("/projects/new", data={"name": "m 42", "confirm_dup": "1"}, follow_redirects=False)
    assert r.status_code == 303
    client.post(r.headers["location"] + "/delete")
    # editing without changing name / object does not ask again
    r = client.post(f"/projects/{project}/edit", data={"name": "Test project", "target": "M42"}, follow_redirects=False)
    assert r.status_code == 303


def test_export_all_projects_metadata(client, project):
    r = client.get("/export/projects")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    data = json.loads(z.read("all_projects.json"))
    assert data["format"] == "astrophoto-metadata-all"
    assert any(p["project"]["id"] == project for p in data["projects"])
    assert any(n.startswith("projects/") and n.endswith("/metadata.json") for n in z.namelist())


def test_session_rows_link_to_files(client, project):
    from conftest import default_session
    sid = default_session(project)
    html = client.get(f"/projects/{project}").text
    assert f'data-target="s{sid}_"' in html and "sessrow" in html


# ---------- catalogue, WCS, overlays, pointing ----------
def test_catalogue_finds_messier_and_common_names():
    o = platesolve.find_object("M51")
    assert o and o["name"] == "NGC 5194" and o["messier"] == "M51" and "Whirlpool" in (o["common"] or "")
    assert platesolve.find_object("ngc5194")["name"] == "NGC 5194"
    assert platesolve.find_object("Whirlpool Galaxy")["name"] == "NGC 5194"
    assert platesolve.find_object("no such thing") is None


def test_annotations_and_target_check():
    res = fake_solve()
    ann = platesolve.annotations(res)
    m51 = next(o for o in ann["objects"] if o["name"] == "NGC 5194")
    assert abs(m51["u"] - 0.5) < 0.01 and abs(m51["v"] - 0.5) < 0.01 and m51["inside"]
    assert any(o["name"] == "NGC 5195" for o in ann["objects"])
    assert ann["grid"] and all(len(g["pts"]) > 1 for g in ann["grid"])
    n, e, c = ann["compass"]["n"], ann["compass"]["e"], ann["compass"]["c"]
    assert n[1] < c[1] and e[0] < c[0]                             # north up, east left
    t = platesolve.target_check(res, "M 51")
    assert t["found"] and t["inside"] and t["sep_arcmin"] < 0.5
    far = platesolve.target_check(fake_solve(dec=M51[1] + 2), "M51")
    assert far["found"] and not far["inside"] and far["direction"] == "S" and abs(far["sep_arcmin"] - 120) < 1
    assert platesolve.target_check(res, "Unknown thing") == {"target": "Unknown thing", "found": False}


@pytest.mark.parametrize("orient,flip,expect", [(0, "none", (0.2, 0.1)), (180, "none", (0.8, 0.9)),
                                                (90, "none", (0.9, 0.2)), (270, "none", (0.1, 0.8)),
                                                (0, "v", (0.2, 0.9)), (0, "h", (0.8, 0.1)), (0, "vh", (0.8, 0.9))])
def test_overlay_orientation_mapping(orient, flip, expect):
    u, v = annotate.to_display(0.2, 0.1, orient, flip)
    assert (round(u, 6), round(v, 6)) == expect


def test_resize_check_corrects_resized_export_but_not_crop():
    res = {"image_px": [1920, 1080], "focal_mm": 133.0, "scale_arcsec_px": 4.5}
    platesolve.resize_check(res, (3840, 2160), 266)
    assert res["focal_mm"] == 266.0 and res["focal_image_mm"] == 133.0 and res["resize_factor"] == 0.5
    crop = {"image_px": [3000, 2000], "focal_mm": 265.0, "scale_arcsec_px": 2.25}
    platesolve.resize_check(crop, (3840, 2160), 266)
    assert crop["focal_mm"] == 265.0 and "cropped" in crop["note"]
    same = {"image_px": [3840, 2160], "focal_mm": 266.0, "scale_arcsec_px": 2.25}
    platesolve.resize_check(same, (3840, 2160), 260)
    assert "note" not in same


def test_solve_reports_missing_star_database(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "s.fits"), "light", project_id=project)
    drain()
    with SessionLocal() as db:
        f = db.scalars(select(FrameFile).where(FrameFile.project_id == project)).first()
    r = client.post(f"/api/files/{f.id}/solve").json()
    assert r["ok"] is False and ("ASTAP" in r["error"] or "database" in r["error"])


# ---------- photometry (ASTAP .1476 star database) ----------
def write_tile(path, stars):
    """stars: (ra, dec, mag), written in the ASTAP 5-byte record format (sorted by magnitude)."""
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


def test_star_database_roundtrip_and_ring(tmp_path):
    stars = [(10.0, 20.5, 8.3), (10.2, 20.6, 12.1), (11.0, 21.0, 15.4)]
    write_tile(tmp_path / "d50_2201.1476", stars)
    ra, dec, mag = photometry.read_tile(tmp_path / "d50_2201.1476")
    for (r0, d0, m0), r1, d1, m1 in zip(sorted(stars, key=lambda s: s[2]), ra, dec, mag):
        assert abs(r0 - r1) < 1e-4 and abs(d0 - d1) < 1e-4 and abs(m0 - m1) < 0.051
    assert photometry._ring_of(-89) == 1 and photometry._ring_of(89.5) == 36 and photometry._ring_of(0.1) == 19


def test_limiting_magnitude_from_synthetic_field():
    rng = np.random.default_rng(5)
    n = 300
    ra = 100 + rng.uniform(-0.3, 0.3, n)
    dec = 10 + rng.uniform(-0.3, 0.3, n)
    mag = rng.uniform(9, 16, n)
    zp = 25.0
    flux = 10 ** ((zp - mag) / 2.5)
    snr = flux / 10 ** ((zp - 17.0) / 2.5) * 5        # background limited: SNR 5 at magnitude 17
    keep = snr >= 5
    res = photometry.limiting_magnitude(snr[keep], flux[keep], ra[keep], dec[keep], ra, dec, mag)
    assert res and abs(res["zero_point"] - zp) < 0.05 and abs(res["limit_mag"] - 17.0) < 0.15
    assert res["matched"] >= 0.9 * keep.sum()


# ---------- optics inspector ----------
def test_optics_on_files(tmp_path, monkeypatch):
    def run(img):
        monkeypatch.setattr(optics, "_luminance", lambda src: (img.astype(np.float32), 1))
        return optics.analyse(tmp_path / "x.fits")
    tilted = run(star_field(sigma=lambda u, v: 1.5 + 2.0 * (1 - u)))
    assert tilted["ok"] and tilted["stars"] > 80 and len(tilted["tiles"]) == 9
    tilt = next(d for d in tilted["diagnosis"] if d["key"] == "tilt")
    assert "left" in tilt["dir"] and tilted["metrics"]["tilt_pct"] > 20
    flat = run(star_field())
    keys = {d["key"] for d in flat["diagnosis"]}
    assert "notilt" in keys and "tilt" not in keys and "round" in keys and flat["elong"] < 1.15
    curved = run(star_field(sigma=lambda u, v: 1.5 + 3.0 * math.hypot(u - 0.5, v - 0.5)))
    assert any(d["key"] == "curvature" for d in curved["diagnosis"])
    # orientation: flipping the image horizontally moves the worse side to the right
    monkeypatch.setattr(optics, "_luminance", lambda src: (star_field(sigma=lambda u, v: 1.5 + 2.0 * (1 - u))
                                                           .astype(np.float32), 1))
    flipped = optics.analyse(tmp_path / "x.fits", flip="h", with_tiles=False)
    assert "right" in next(d for d in flipped["diagnosis"] if d["key"] == "tilt")["dir"] and not flipped["tiles"]


def test_optics_endpoint_stores_summary_and_session_measured(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "o.fits", data=star_field()), "light", project_id=project)
    drain()
    with SessionLocal() as db:
        f = db.scalars(select(FrameFile).where(FrameFile.project_id == project)).first()
    r = client.get(f"/api/files/{f.id}/optics", params={"flip": "auto"}).json()
    assert r["ok"] and r["regions"] and r["tiles"]
    with SessionLocal() as db:
        f = db.get(FrameFile, f.id)
        summ = json.loads(f.optics)
        assert summ["fwhm_px"] == r["fwhm_px"] and "tiles" not in summ
        f.solve = json.dumps(fake_solve(photometry={"limit_mag": 17.2}, focal_mm=520.0))
        db.commit()
    html = client.get(f"/projects/{project}").text
    assert "Measured" in html and "17.2 mag" in html
    trend = client.get("/api/stats/optics").json()
    row = next(x for x in trend if x["project_id"] == project)
    assert row["solved"] == 1 and row["analysed"] == 1 and row["limit_mag"] == 17.2
    assert abs(row["fwhm_arcsec"] - r["fwhm_px"] * 1.5) < 0.01


def test_session_measured_medians():
    files = [SimpleNamespace(id=i, solve=json.dumps(fake_solve(focal_mm=f, photometry={"limit_mag": m})),
                             optics=json.dumps({"fwhm_px": p, "elong": 1.1, "metrics": {"tilt_pct": t},
                                                "tilt_dir": d}))
             for i, (f, m, p, t, d) in enumerate([(510, 16.0, 4.0, 30, "left"), (520, 16.5, 5.0, 10, None),
                                                   (530, 17.0, 6.0, 40, "left")])]
    m = measured.session_measured(files, nominal_focal=480, nominal_scale=1.6)
    assert m["focal_mm"] == 520 and m["limit_mag"] == 16.5 and m["fwhm_px"] == 5.0
    assert m["fwhm_arcsec"] == 7.5 and m["tilt_dir"] == "left" and m["tilt_frames"] == 2
    assert m["focal_diff_pct"] == round((520 / 480 - 1) * 100, 1)
    assert measured.session_measured([SimpleNamespace(id=1, solve=None, optics=None)]) is None


# ---------- solved file: annotations, export, apply focal ----------
def test_solved_file_endpoints(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "a.fits", data=star_frame(h=400, w=400)), "light", project_id=project)
    drain()
    with SessionLocal() as db:
        f = db.scalars(select(FrameFile).where(FrameFile.project_id == project)).first()
        assert client.get(f"/api/files/{f.id}/annotations").status_code == 404
        f.solve = json.dumps(fake_solve(w=400, h=400))
        db.commit()
        fid = f.id
    ann = client.get(f"/api/files/{fid}/annotations").json()
    assert any(o["name"] == "NGC 5194" for o in ann["objects"]) and ann["orient"] == 0
    s = client.get(f"/api/files/{fid}/solved").json()
    assert s["ok"] and s["target_check"]["target"] == "M42" and not s["target_check"]["inside"]
    r = client.get(f"/files/{fid}/annotated.jpg")
    assert r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff"
    from PIL import Image
    assert Image.open(io.BytesIO(r.content)).size == (400, 400)
    view = client.get(f"/files/{fid}/view").text
    assert 'id="skybtn"' in view and 'id="opticsbtn"' in view


def test_apply_measured_focal_to_equipment(client):
    with SessionLocal() as db:
        db.add(CatalogItem(kind="telescope", name="Focal test scope", specs=json.dumps({"focal_mm": 800})))
        db.commit()
    r = client.post("/api/equipment/focal", json={"telescope": "Focal test scope", "focal_mm": 1003.6}).json()
    assert r["ok"] and r["old"] == 800 and r["focal_mm"] == 1004
    with SessionLocal() as db:
        it = db.scalars(select(CatalogItem).where(CatalogItem.name == "Focal test scope")).one()
        assert it.spec["focal_mm"] == 1004
        db.delete(it)
        db.commit()
    assert client.post("/api/equipment/focal", json={"telescope": "nope", "focal_mm": 500}).status_code == 404
    assert client.post("/api/equipment/focal", json={"telescope": "x", "focal_mm": 2}).status_code == 400


def test_bulk_bar_has_solve_and_optics(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "b.fits"), "light", project_id=project)
    drain()
    html = client.get(f"/projects/{project}").text
    assert 'data-bulk="solve"' in html and 'data-bulk="optics"' in html
