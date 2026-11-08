"""Pagination of sessions, file groups and the calibration library, with the page sizes from Settings."""
from datetime import date

import pytest
from sqlalchemy import select

from app.core import paginate
from app.models import SessionLocal, FrameFile, ImagingSession, CalibrationSet, Setting
from conftest import drain


def _set(**kv):
    with SessionLocal() as db:
        for k, v in kv.items():
            db.merge(Setting(key=k, value=str(v)))
        db.commit()


@pytest.fixture()
def small_pages():
    _set(sessions_page_size=2, files_page_size=3, calib_page_size=2)
    yield
    with SessionLocal() as db:
        db.query(Setting).filter(Setting.key.in_(["sessions_page_size", "files_page_size", "calib_page_size"])).delete()
        db.commit()


def test_paginate():
    assert paginate(10, 0, 3) == {"page": 1, "pages": 1, "start": 0, "end": 10, "total": 10, "window": [1]}
    p = paginate(45, 10, 3)
    assert (p["pages"], p["start"], p["end"]) == (5, 20, 30) and p["window"] == [1, 2, 3, 4, 5]
    assert paginate(45, 10, 99)["page"] == 5 and paginate(45, 10, "x")["page"] == 1 and paginate(45, 10, -2)["page"] == 1
    assert paginate(100, 10, 1)["window"] == [1, 2, 3, 10]


def _project_with(client, nights=5, files=7, name="Paging"):
    r = client.post("/projects/new", data={"name": name, "confirm_dup": "1"}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        sids = []
        for n in range(nights):
            s = ImagingSession(project_id=pid, date=date(2024, 1, 1 + n))
            db.add(s)
            db.flush()
            sids.append(s.id)
        for i in range(files):
            db.add(FrameFile(project_id=pid, session_id=sids[0], frame_type="light", status="done", size=1,
                             original_name=f"pg_{i:02d}.fits", stored_path=f"archive-data/pg/{pid}/{i}.fits"))
        db.commit()
    return pid, sids


def test_sessions_and_files_are_paged(client, small_pages):
    pid, sids = _project_with(client)
    html = client.get(f"/projects/{pid}").text
    assert html.count('class="sessrow"') == 2 and "1–2 of 5" in html
    assert f'data-target="s{sids[0]}_"' in html and f'data-target="s{sids[2]}_"' not in html
    assert "SESSION_PAGE" in html and f'"{sids[4]}": 3' in html               # where each session lives
    page3 = client.get(f"/projects/{pid}?sp=3").text
    assert page3.count('class="sessrow"') == 1 and f'data-target="s{sids[4]}_"' in page3
    # files of a group: 3 per page, own page parameter, other groups / sessions unaffected
    key = f"p_s{sids[0]}_light"
    assert html.count('/view">pg_0') == 3 and "1–3 of 7" in html and key in html
    p3 = client.get(f"/projects/{pid}?{key}=3").text
    assert "pg_06" in p3 and "pg_00" not in p3
    allf = client.get(f"/projects/{pid}?show=s{sids[0]}_light").text
    assert all(f"pg_{i:02d}" in allf for i in range(7)) and "Show in pages" in allf
    client.post(f"/projects/{pid}/delete")


def test_zero_means_everything(client):
    _set(sessions_page_size=0, files_page_size=0)
    try:
        pid, sids = _project_with(client, nights=3, files=4, name="Paging all")
        html = client.get(f"/projects/{pid}").text
        assert html.count('class="sessrow"') == 3 and 'class="pager' not in html
        client.post(f"/projects/{pid}/delete")
    finally:
        with SessionLocal() as db:
            db.query(Setting).filter(Setting.key.in_(["sessions_page_size", "files_page_size"])).delete()
            db.commit()


def test_calibration_library_is_paged(client, small_pages):
    with SessionLocal() as db:
        ids = []
        for n in range(5):
            c = CalibrationSet(name=f"Paged set {n}", frame_type="dark", capture_date=date(2024, 1, 1 + n))
            db.add(c)
            db.flush()
            ids.append(c.id)
        db.commit()
    try:
        html = client.get("/calibration?q=Paged+set").text
        assert html.count("<b>Paged set") == 2 and "of 5" in html and "Paged set 4" in html   # newest first
        p3 = client.get("/calibration?q=Paged+set&page=3").text
        assert "Paged set 0" in p3 and "Paged set 4" not in p3
        assert "q=Paged+set" in html and "page=2" in html                # pager keeps the filters
    finally:
        with SessionLocal() as db:
            for i in ids:
                db.delete(db.get(CalibrationSet, i))
            db.commit()


def test_settings_page_sizes(client):
    html = client.get("/settings").text
    for k in ("sessions_page_size", "files_page_size", "calib_page_size"):
        assert f'name="{k}"' in html
    client.post("/settings", data={"sessions_page_size": "7", "files_page_size": "0", "calib_page_size": "5000"})
    from app.core import load_settings
    cfg = load_settings()
    assert cfg["sessions_page_size"] == 7 and cfg["files_page_size"] == 0 and cfg["calib_page_size"] == 1000
    client.post("/settings", data={"reset": "1"})
