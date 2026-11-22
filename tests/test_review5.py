"""Fixes of the review 2026-11-22: session sorting, medians, project page / search / stats / calibration list
queries, session merging, stale session edits, paging params, import / worker robustness, bulk limits, failed
moves, JSON size limit, unbiased star FWHM, saturation, measured scale, RA hints, Dec formatting, 32-bit FITS."""
import json
import math
import os
import re
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select

from app import core, framestats, measured, optics, platesolve, preview, routes_projects, sessets, worker
from app.models import SessionLocal, FrameFile, ImagingSession, ImportJob, Project, CalibrationSet
from conftest import IMPORT_DIR, DATA_DIR, drain, upload, write_fits, star_frame, default_session


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


def sessions_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(ImagingSession).where(ImagingSession.project_id == pid)
                          .order_by(ImagingSession.id)).all()


# ---------- projects / sessions ----------
def test_sort_sessions_by_set_without_exposure(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "exposure_s": "120"})
    client.post(f"/projects/{project}/sessions", data={"date": "13-10-2025", "exposure_s": "", "gain": "100"})
    for d in ("1", "-1"):
        r = client.get(f"/projects/{project}?so=set&sd={d}")
        assert r.status_code == 200


def test_project_median_ignores_rejected_frames(client, project, tmp_path):
    for i in range(3):
        upload(client, write_fits(tmp_path / f"m{i}.fits", data=star_frame(seed=500 + i)), "light", project_id=project)
    drain()
    with SessionLocal() as db:
        fs = db.scalars(select(FrameFile).where(FrameFile.project_id == project).order_by(FrameFile.id)).all()
        for f, v in zip(fs, (2.0, 3.0, 50.0)):
            f.stat_fwhm = v
        fs[2].rejected = True                                   # the blurred one was thrown out
        db.commit()
    html = client.get(f"/projects/{project}").text
    vals = set(re.findall(r"med_fwhm\W+([\d.]+)", html))
    assert vals == {"2.50"}                                     # median of 2 and 3, not 3 (or with the rejected 50)


def test_cover_choices_are_capped():
    files = [SimpleNamespace(id=i, frame_type="light", thumb_path="t") for i in range(200)]
    files += [SimpleNamespace(id=1000 + i, frame_type="stack", thumb_path="t") for i in range(5)]
    files.append(SimpleNamespace(id=999, frame_type="light", thumb_path=None))
    p = SimpleNamespace(files=files, cover_file_id=150)
    out = routes_projects.cover_choices(p)
    ids = {f.id for f in out}
    assert sum(1 for f in out if f.frame_type == "light") == routes_projects.COVER_LIGHTS + 1   # + the cover
    assert 150 in ids and {1000, 1004} <= ids and 999 not in ids


def test_project_page_shows_linked_calibration_counts(client, project, tmp_path):
    r = client.post("/calibration/new", data={"name": "Darks 120", "frame_type": "dark"}, follow_redirects=False)
    cid = int(r.headers["location"].rstrip("/").rsplit("/", 1)[1])
    for i in range(2):
        upload(client, write_fits(tmp_path / f"d{i}.fits", data=star_frame(seed=600 + i)), "dark", calib_set_id=cid)
    sid = default_session(project)
    with SessionLocal() as db:
        s = db.get(ImagingSession, sid)
        s.calib_sets.append(db.get(CalibrationSet, cid))
        db.commit()
    html = client.get(f"/projects/{project}").text
    assert "Darks 120 (2)" in html
    lst = client.get("/calibration").text
    assert re.search(r"Darks 120.*?<td>2</td>", lst, re.S)
    client.post(f"/calibration/{cid}/delete")


def test_session_merge_rules():
    S = lambda **k: SimpleNamespace(**{"exposure_s": 120.0, "gain": "101", "filter": "Ha", "cam_temp": -10.0,
                                       "telescope": "RC8", "camera": "ASI2600", **k})
    assert sessets.same_set(S(), S(gain="101.0", cam_temp=-9.0, filter="ha"))      # tolerances like matches()
    assert not sessets.same_set(S(), S(exposure_s=60.0))
    assert sessets.same_rig(S(), S(camera="asi2600")) and sessets.same_rig(S(), S(telescope=None))
    assert not sessets.same_rig(S(), S(telescope="Esprit 100"))                     # another rig: never merged


def test_saving_a_deleted_session_does_not_create_a_new_one(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025"})
    s = sessions_of(project)[-1]
    n = len(sessions_of(project))
    client.post(f"/sessions/{s.id}/delete")
    r = client.post(f"/projects/{project}/sessions", data={"sid": str(s.id), "date": "14-10-2025"},
                    follow_redirects=False)
    assert r.status_code == 303 and len(sessions_of(project)) == n - 1


@pytest.mark.parametrize("path", ["/?page=abc", "/?page=", "/gallery?page=x", "/?page=-3", "/gallery?page=99999"])
def test_bad_page_parameter_falls_back(client, path):
    assert client.get(path).status_code == 200


def test_search_hits_come_from_sql(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "zetaquux_1.fits", data=star_frame(seed=801), OBJECT="zetaquux target"),
           "light", project_id=project)
    upload(client, write_fits(tmp_path / "plain.fits", data=star_frame(seed=802), OBJECT="zetaquux target"),
           "light", project_id=project)
    drain()
    from app.routes_main import file_matches, header_matches, search_hits
    with SessionLocal() as db:
        fm = file_matches(db, [project], "zetaquux")
        hm = header_matches(db, [project], "zetaquux")
        p = db.get(Project, project)
        hits = search_hits(p, "zetaquux", hm.get(project), fm.get(project, []))
    assert [r[0] for r in fm[project]] == ["zetaquux_1.fits"]
    assert hm[project][0] == 2 and hm[project][1][0] in ("plain.fits", "zetaquux_1.fits")
    wheres = [h["where"] for h in hits]
    assert "File zetaquux_1.fits" in wheres and "FITS headers" in wheres
    assert "zetaquux" in client.get("/?q=zetaquux").text


def test_stats_count_a_night_once(client, tmp_path):
    from app.routes_main import stats_page                        # noqa: F401  (route exists)
    pids = []
    for name in ("Night A", "Night B"):
        pid = int(client.post("/projects/new", data={"name": name}, follow_redirects=False)
                  .headers["location"].rsplit("/", 1)[1])
        pids.append(pid)
        client.post(f"/projects/{pid}/sessions", data={"date": "03-03-2021"})
        upload(client, write_fits(tmp_path / f"{name}.fits", data=star_frame(seed=len(pids)), EXPTIME=60.0),
               "light", project_id=pid)
    drain()
    with SessionLocal() as db:
        nights = {s.date for pid in pids for s in db.scalars(select(ImagingSession)
                                                              .where(ImagingSession.project_id == pid))}
    html = client.get("/stats").text
    assert client.get("/stats").status_code == 200 and "imaging nights" in html
    for pid in pids:
        client.post(f"/projects/{pid}/delete")
    assert len(nights) == 1


# ---------- import / worker / bulk / storage / JSON ----------
def test_import_without_existing_files_is_refused(client, project):
    sid = default_session(project)
    (IMPORT_DIR / "empty").mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        before = db.scalar(select(ImportJob.id).order_by(ImportJob.id.desc()).limit(1))
    r = client.post("/api/import/start", json={"project_id": project, "session_id": sid, "path": "empty",
                                               "files": [{"rel": "gone.fits"}, {"rel": "../../etc/passwd"}]})
    assert r.status_code == 400
    with SessionLocal() as db:
        assert db.scalar(select(ImportJob.id).order_by(ImportJob.id.desc()).limit(1)) == before
    big = client.post("/api/import/start", json={"project_id": project, "session_id": sid, "path": "empty",
                                                 "files": [{"rel": "x"}] * 20001})
    assert big.status_code == 400


def test_worker_rejects_a_source_swapped_for_a_symlink(client, project, tmp_path):
    sid = default_session(project)
    src = write_fits(IMPORT_DIR / "swap" / "a.fits", data=star_frame(seed=700))
    r = client.post("/api/import/start", json={"project_id": project, "session_id": sid, "path": "swap",
                                               "files": [{"rel": "a.fits"}]}).json()
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET_KEY=abc")
    src.unlink()
    os.symlink(secret, src)                                       # replaced after the import started
    drain()
    with SessionLocal() as db:
        f = db.scalars(select(FrameFile).where(FrameFile.import_job_id == r["job"])).one()
        assert f.status == "error" and "import folder" in f.error
        assert db.get(ImportJob, r["job"]).status == "done"       # the queue went on
    assert not any(p.read_bytes() == b"SECRET_KEY=abc" for p in DATA_DIR.rglob("a*.fits") if p.is_file())


def test_worker_finishes_jobs_left_running(client, project):
    with SessionLocal() as db:
        job = ImportJob(project_id=project, source_dir="/x", total=0, status="running")
        db.add(job)
        db.commit()
        jid = job.id
    drain()
    with SessionLocal() as db:
        assert db.get(ImportJob, jid).status == "done"


def test_bulk_action_limits(client):
    r = client.post("/api/files/bulk", json={"ids": list(range(5001)), "action": "delete"})
    assert r.status_code == 400
    assert client.post("/api/files/bulk", json={"ids": "1,2", "action": "delete"}).status_code == 400


def test_failed_move_leaves_no_placeholder(client, project, tmp_path, monkeypatch):
    upload(client, write_fits(tmp_path / "mv.fits", data=star_frame(seed=710)), "light", project_id=project)
    drain()
    f = files_of(project)[-1]
    other = int(client.post("/projects/new", data={"name": "Move target"}, follow_redirects=False)
                .headers["location"].rsplit("/", 1)[1])

    def boom(a, b):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(core.os, "replace", boom)
    r = client.post("/api/files/bulk", json={"ids": [f.id], "action": "move", "value": str(other)})
    monkeypatch.undo()
    assert r.status_code == 409
    assert not list((core.owner_dir(other, None)).rglob("mv*.fits"))     # no empty reserved file
    assert (DATA_DIR / files_of(project)[-1].stored_path).is_file()
    client.post(f"/projects/{other}/delete")


def test_json_body_size_limit(client):
    r = client.post("/api/files/bulk", content=b'{"ids": [' + b"1," * 10 + b'1], "pad": "' +
                    b"x" * (core.JSON_MAX_BYTES + 10) + b'"}', headers={"content-type": "application/json"})
    assert r.status_code == 413


# ---------- computations ----------
def _gauss_star(n, amp, sx, sy, noise, rng, x0=None, y0=None):
    yy, xx = np.mgrid[0:n, 0:n]
    x0 = n // 2 + rng.uniform(-.5, .5) if x0 is None else x0
    y0 = n // 2 + rng.uniform(-.5, .5) if y0 is None else y0
    return amp * np.exp(-((xx - x0) ** 2 / (2 * sx * sx) + (yy - y0) ** 2 / (2 * sy * sy))) + rng.normal(0, noise, (n, n))


@pytest.mark.parametrize("amp", [150, 400, 3000])
@pytest.mark.parametrize("sx,sy", [(2.5, 2.5), (3.0, 2.0)])
def test_windowed_moments_are_unbiased(amp, sx, sy):
    rng = np.random.default_rng(1)
    fw, el = [], []
    for _ in range(120):
        m = framestats.windowed_moments(_gauss_star(31, amp, sx, sy, 10, rng), 15, 15, 2.0)
        assert m is not None
        fw.append(2.3548 * math.sqrt((m[2] + m[3]) / 2))
        el.append(math.sqrt(max(m[2], m[3]) / min(m[2], m[3])))
    true = 2.3548 * math.sqrt((sx * sx + sy * sy) / 2)
    assert np.median(fw) == pytest.approx(true, rel=0.02)               # box moments read 7–16 % too large
    assert np.median(el) == pytest.approx(max(sx, sy) / min(sx, sy), abs=0.05)


def test_windowed_moments_reject_empty_patches():
    rng = np.random.default_rng(2)
    assert framestats.windowed_moments(np.zeros((15, 15)), 7, 7, 2.0) is None
    assert framestats.windowed_moments(-np.ones((15, 15)), 7, 7, 2.0) is None


def test_frame_fwhm_matches_the_true_value(tmp_path):
    rng = np.random.default_rng(3)
    h = w = 600
    img = np.full((h, w), 1000.0)
    yy, xx = np.mgrid[0:h, 0:w]
    for y in range(30, h - 30, 60):
        for x in range(30, w - 30, 60):                          # all stars equally bright, none saturated
            img += 2000 * np.exp(-((xx - x - 0.3) ** 2 + (yy - y + 0.2) ** 2) / (2 * 2.5 ** 2))
    img = np.clip(img + rng.normal(0, 10, img.shape), 0, 65535).astype(np.uint16)
    p = write_fits(tmp_path / "grid.fits", data=img)
    st = framestats.frame_stats(p)
    assert st["stars"] >= 80                                     # 9 × 9 grid; the old 0.97 × max cut dropped them all
    assert st["fwhm"] == pytest.approx(2.3548 * 2.5, rel=0.03)


def test_saturation_level_only_with_clipped_pixels():
    a = np.zeros((50, 50))
    a[10, 10] = 1000.0
    assert framestats.saturation_level(a, 0.97) == math.inf
    a[20:22, 20:22] = 1000.0
    assert framestats.saturation_level(a, 0.97) == pytest.approx(970.0)
    assert framestats.saturation_level(np.zeros((3, 3)), 0.9) == math.inf


def test_measured_uses_per_image_scale_for_unsolved_frames():
    sol = {"ok": True, "scale_arcsec_px": 2.0, "sensor_scale_arcsec_px": 1.0}     # 50 % export
    solved = SimpleNamespace(id=1, frame_type="light", solve=json.dumps(sol),
                             optics=json.dumps({"fwhm_px": 3.0, "fwhm_centre_px": 3.0}))
    unsolved = SimpleNamespace(id=2, frame_type="light", solve=None,
                               optics=json.dumps({"fwhm_px": 3.0, "fwhm_centre_px": 3.0}))
    out = measured.session_measured([solved, unsolved])
    assert out["scale"] == 1.0 and out["fwhm_arcsec"] == 6.0      # both frames at 2″ per image pixel


@pytest.mark.parametrize("hdr,expect", [
    ({"RA": "83.82", "DEC": "-5.39"}, (83.82 / 15, -5.39)),                       # degrees (convention)
    ({"RA": "5.588", "DEC": "-5.39", "OBJCTRA": "05 35 17"}, (5.588, -5.39)),     # hours, OBJCTRA agrees
    ({"RA": "83.82", "DEC": "-5.39", "OBJCTRA": "05 35 17"}, (83.82 / 15, -5.39)),
])
def test_ra_hint_units(hdr, expect):
    ra, dec = platesolve._ra_dec_hint(hdr)
    assert ra == pytest.approx(expect[0], abs=1e-3) and dec == pytest.approx(expect[1])


def test_fmt_dec_has_no_negative_zero():
    assert platesolve.fmt_dec(-0.0001, short=True) == "+0°00′"
    assert platesolve.fmt_dec(-0.00001) == "+00° 00′ 00.0″"
    assert platesolve.fmt_dec(-0.5, short=True) == "-0°30′"


def test_find_object_index():
    assert platesolve.find_object("M 51")["name"] == "NGC 5194"
    assert platesolve.find_object("sh2-229")["name"] == "IC 405"
    assert platesolve.find_object("") is None and platesolve.find_object("nothing at all") is None


def test_32bit_fits_with_bzero_keeps_precision(tmp_path):
    from astropy.io import fits
    data = (np.arange(64, dtype=np.int64).reshape(8, 8) * 3 + 70000)          # needs exact low bits
    hdu = fits.PrimaryHDU((data - 2 ** 31).astype(np.int32))
    hdu.header["BZERO"] = 2 ** 31
    hdu.header["BSCALE"] = 1
    p = tmp_path / "i32.fits"
    hdu.writeto(p)
    arr, _ = preview.load_fits_raw(p)
    assert np.array_equal(np.asarray(arr, dtype=np.int64), data)


def test_old_fwhm_values_are_re_measured_in_the_background(client, project, tmp_path):
    from app.models import Setting
    upload(client, write_fits(tmp_path / "old.fits", data=star_frame(seed=900)), "light", project_id=project)
    drain()
    f = files_of(project)[-1]
    with SessionLocal() as db:
        db.get(FrameFile, f.id).stat_fwhm = 99.0                 # stored by the former (box moment) method
        s = db.get(Setting, worker.RESTAT_KEY) or Setting(key=worker.RESTAT_KEY)
        s.value = f"{f.id - 1}/{f.id}"
        db.merge(s)
        db.commit()
    drain()
    with SessionLocal() as db:
        assert db.get(FrameFile, f.id).stat_fwhm < 10
        assert db.get(Setting, worker.RESTAT_KEY).value == "done"
    drain()                                                       # nothing left: no further work
