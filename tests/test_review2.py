"""Fixes from the review of 2026-11-20: session sets edge cases, downloads, guards, bounded loops."""
import time
from urllib.parse import quote

import numpy as np
from sqlalchemy import select

from app import platesolve, sessets
from app.models import SessionLocal, FrameFile, ImagingSession
from conftest import drain, write_fits, upload, star_frame, default_session


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


def sessions_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(ImagingSession).where(ImagingSession.project_id == pid)
                          .order_by(ImagingSession.id)).all()


def light(tmp_path, name, seed, **hdr):
    hdr.setdefault("DATE_OBS", "2025-10-12T22:00:00")
    return write_fits(tmp_path / name, data=star_frame(seed=seed), IMAGETYP="Light Frame", **hdr)


def test_frames_without_metadata_share_one_sibling(client, project, tmp_path):
    sid = default_session(project)
    upload(client, light(tmp_path, "set.fits", 1, EXPTIME=120.0, FILTER="L"), project_id=project, session_id=sid)
    drain()
    for i in range(3):                              # no EXPTIME / FILTER / GAIN / temperature
        upload(client, light(tmp_path, f"bare{i}.fits", 10 + i), project_id=project, session_id=sid)
    drain()
    ss = sessions_of(project)
    assert len(ss) == 2                             # not one session per bare frame
    bare = {f.session_id for f in files_of(project) if f.original_name.startswith("bare")}
    assert len(bare) == 1 and sid not in bare


def test_open_session_takes_the_majority_not_the_first_frame(client, project, tmp_path):
    sid = default_session(project)
    upload(client, light(tmp_path, "focus.fits", 1, EXPTIME=5.0, FILTER="L"), project_id=project, session_id=sid)
    for i in range(4):
        upload(client, light(tmp_path, f"real{i}.fits", 10 + i, EXPTIME=300.0, FILTER="L"),
               project_id=project, session_id=sid)
    drain()
    fs = {f.original_name: f for f in files_of(project)}
    assert all(fs[f"real{i}.fits"].session_id == sid for i in range(4))
    assert fs["focus.fits"].session_id != sid
    with SessionLocal() as db:
        assert db.get(ImagingSession, sid).exposure_s == 300.0


def test_split_skips_frames_still_waiting_for_metadata(client, project, tmp_path):
    sid = default_session(project)
    upload(client, light(tmp_path, "w0.fits", 1, EXPTIME=60.0), project_id=project, session_id=sid)
    drain()
    upload(client, light(tmp_path, "w1.fits", 2, EXPTIME=90.0), project_id=project, session_id=sid)
    with SessionLocal() as db:                      # w1 not processed yet: pending, no metadata
        from app.models import Project
        assert sessets.split_project(db, db.get(Project, project)) == 0
        db.commit()
    assert len(sessions_of(project)) == 1
    drain()
    assert len(sessions_of(project)) == 2           # placed by the worker once its metadata is known


def test_annotated_download_unicode_name_and_missing_file(client, project, tmp_path, monkeypatch):
    from app import annotate
    p = light(tmp_path, "Туманность ✨.fits", 1)
    upload(client, p, project_id=project)
    drain()
    [f] = files_of(project)
    with SessionLocal() as db:
        db.get(FrameFile, f.id).solve = ('{"ok": true, "wcs": {"crpix": [1, 1], "crval": [0, 0], '
                                         '"cd": [[0.001, 0], [0, 0.001]], "w": 400, "h": 300}}')
        db.commit()
    monkeypatch.setattr(annotate, "annotated_jpeg", lambda *a, **k: b"\xff\xd8jpeg")
    r = client.get(f"/files/{f.id}/annotated.jpg")
    assert r.status_code == 200, r.text
    cd = r.headers["content-disposition"]
    assert "filename*=UTF-8''" + quote("Туманность ✨_annotated.jpg") in cd
    from conftest import DATA_DIR
    (DATA_DIR / f.stored_path).unlink()
    assert client.get(f"/files/{f.id}/annotated.jpg").status_code == 409


def test_upload_check_reports_session_night(client, project, tmp_path):
    p = light(tmp_path, "c.fits", 1)
    upload(client, p, project_id=project)
    drain()
    r = client.post("/api/uploads/check", json={"project_id": project, "files": [
        {"name": "c.fits", "size": p.stat().st_size}, {"name": "c.fits", "size": 1}]})
    assert r.status_code == 200
    res = r.json()["existing"]
    assert "12-10-2025" in str(res[0]) and res[1] is None


def test_solve_without_scale_is_an_error_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(platesolve, "available", lambda: None)
    monkeypatch.setattr(platesolve, "_run", lambda f, fov, ra, dec: {"PLTSOLVD": "T", "CRVAL1": "10", "CRVAL2": "20"})
    res = platesolve.solve(write_fits(tmp_path / "z.fits", data=star_frame()), "", 3.76, 500, (400, 300))
    assert res["ok"] is False and "scale" in res["error"]


def test_star_measurement_is_bounded_on_pure_noise(tmp_path):
    from app import framestats, optics
    noise = np.random.default_rng(3).normal(1000, 30, (1500, 2000)).astype(np.float32)
    t = time.time()
    optics._stars(noise - 1000, 1.0)                # tiny sigma -> a huge number of labels
    st = framestats.frame_stats(write_fits(tmp_path / "n.fits", data=noise.astype(np.uint16)))
    assert time.time() - t < 30 and st is not None


def test_image_file_handle_is_closed(tmp_path):
    from PIL import Image
    from app.preview import load_image
    p = tmp_path / "x.png"
    Image.fromarray((np.random.default_rng(1).random((50, 60)) * 255).astype(np.uint8)).save(p)
    import os
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(30):
        load_image(p, {}, None)
    assert len(os.listdir("/proc/self/fd")) - before < 5
