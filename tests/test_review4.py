"""Third review round (2026-11-20): frames without metadata yet, settings, gallery search, query counts,
calibration page, prefetch exclusions, upload form without filter / description."""
from datetime import date

from sqlalchemy import select, event

from app.models import SessionLocal, FrameFile, ImagingSession, Setting, engine
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


def new_project(client, name):
    r = client.post("/projects/new", data={"name": name}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1])


# ---------- lights whose metadata is not known yet ----------
def test_moving_pending_lights_is_refused_and_creates_no_sessions(client, project, tmp_path):
    other = new_project(client, "Pending target")
    try:
        client.post(f"/projects/{other}/sessions", data={"date": "12-10-2025", "exposure_s": "120"})
        [target] = sessions_of(other)
        upload(client, light(tmp_path, "p.fits", 1, EXPTIME=120.0), project_id=project)
        [f] = files_of(project)
        assert f.status == "pending"                # not processed yet
        r = client.post("/api/files/bulk", json={"ids": [f.id], "action": "move", "value": str(other)})
        assert r.status_code == 409                 # still processing: moved later, when its set is known
        assert len(sessions_of(other)) == 1
        drain()
        r = client.post("/api/files/bulk", json={"ids": [f.id], "action": "move", "value": str(other)})
        assert r.status_code == 200, r.text
        [f] = files_of(other)
        assert f.session_id == target.id and len(sessions_of(other)) == 1
    finally:
        client.post(f"/projects/{other}/delete")


def test_bulk_session_accepts_pending_lights_then_worker_places_them(client, project, tmp_path):
    sid = default_session(project)
    upload(client, light(tmp_path, "a.fits", 1, EXPTIME=60.0), project_id=project, session_id=sid)
    drain()
    client.post(f"/projects/{project}/sessions", data={"date": "13-10-2025"})
    empty = sessions_of(project)[-1]
    upload(client, light(tmp_path, "b.fits", 2, EXPTIME=300.0), project_id=project, session_id=sid)
    b = files_of(project)[-1]
    assert b.status == "pending"
    r = client.post("/api/files/bulk", json={"ids": [b.id], "action": "session", "value": str(empty.id)})
    assert r.status_code == 200, r.text            # set unknown yet -> not refused as "another set"
    drain()
    b = files_of(project)[-1]
    assert b.session_id == empty.id
    with SessionLocal() as db:
        assert db.get(ImagingSession, empty.id).exposure_s == 300.0


def test_session_set_locked_while_lights_are_still_processing(client, project, tmp_path):
    sid = default_session(project)
    upload(client, light(tmp_path, "w.fits", 1, EXPTIME=120.0), project_id=project, session_id=sid)
    assert files_of(project)[0].status == "pending"
    client.post(f"/projects/{project}/sessions", data={"sid": str(sid), "date": "12-10-2025",
                                                       "exposure_s": "30", "gain": "", "filter": "",
                                                       "cam_temp": ""})
    with SessionLocal() as db:
        assert db.get(ImagingSession, sid).exposure_s is None      # same rule as the dialog (locked)
    drain()
    with SessionLocal() as db:
        assert db.get(ImagingSession, sid).exposure_s == 120.0
    assert len(sessions_of(project)) == 1


# ---------- settings ----------
def test_unknown_time_zone_keeps_the_current_zone(client):
    try:
        client.post("/settings", data={"timezone": "Europe/Bratislava"})
        r = client.post("/settings", data={"timezone": "Europe/Bratislavaa"}, follow_redirects=True)
        assert "Unknown time zone" in r.text and "kept Europe/Bratislava" in r.text
        with SessionLocal() as db:
            assert db.get(Setting, "timezone").value == "Europe/Bratislava"   # not reset to UTC
    finally:
        client.post("/settings", data={"timezone": "UTC"})
        client.post("/settings", data={"reset": "1"})


# ---------- gallery / slides ----------
def test_gallery_search_treats_wildcards_literally(client, project, tmp_path):
    from PIL import Image
    for i, name in enumerate(("M131_final.png", "M_31_final.png")):
        p = tmp_path / name
        Image.new("RGB", (40, 30), (40 + i * 100, 20, 30)).save(p)
        upload(client, p, "stack", project_id=project)
    drain()
    fs = {f.original_name: f.id for f in files_of(project)}
    html = client.get("/gallery?q=M_31").text
    assert f"/files/{fs['M_31_final.png']}/thumb" in html and f"/files/{fs['M131_final.png']}/thumb" not in html


def test_slides_load_sessions_in_one_query(client, project, tmp_path):
    from PIL import Image
    ids = []
    for i in range(6):
        p = tmp_path / f"s{i}.png"
        Image.new("RGB", (40, 30), (i * 30, 10, 10)).save(p)
        upload(client, p, "stack", project_id=project)
    drain()
    ids = [f.id for f in files_of(project)]
    n = [0]
    count = lambda *a: n.__setitem__(0, n[0] + 1)
    event.listen(engine, "before_cursor_execute", count)
    try:
        r = client.get("/api/slides?ids=" + ",".join(map(str, ids)))
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert r.status_code == 200 and len(r.json()) == 6
    assert n[0] <= 8                                # not one session query per frame without a date


# ---------- calibration page ----------
def test_calibration_page_move_targets_are_light_rows(client, tmp_path):
    ids = []
    for i in range(2):
        r = client.post("/calibration/new", data={"name": f"Move set {i}", "frame_type": "dark"},
                        follow_redirects=False)
        ids.append(int(r.headers["location"].rstrip("/").rsplit("/", 1)[1]))
    try:
        upload(client, write_fits(tmp_path / "d.fits", data=star_frame(seed=5), IMAGETYP="Dark Frame"), "dark",
               calib_set_id=ids[0])
        drain()
        html = client.get(f"/calibration/{ids[0]}").text
        assert f'value="calib:{ids[1]}"' in html and "Move set 1" in html
        with SessionLocal() as db:
            fid = db.scalar(select(FrameFile.id).where(FrameFile.calib_set_id == ids[0]))
        r = client.post("/api/files/bulk", json={"ids": [fid], "action": "move", "value": f"calib:{ids[1]}"})
        assert r.status_code == 200
        with SessionLocal() as db:
            assert db.get(FrameFile, fid).calib_set_id == ids[1]
    finally:
        for cid in ids:
            client.post(f"/calibration/{cid}/delete")


# ---------- prefetch / upload form ----------
def test_prefetch_excludes_heavy_and_changing_urls(client, project):
    html = client.get(f"/projects/{project}").text
    for pat in ('"/logout"', '"/export/*"', '"/*/zip*"', '"/files/*/view*"', '"/files/*/download*"', '"/api/*"',
                '"/*/delete*"'):
        assert pat in html


def test_upload_and_import_forms_have_no_filter_or_description(client, project):
    page = client.get(f"/projects/{project}").text
    assert "up-filter" not in page and "up-desc" not in page
    imp = client.get(f"/import?project={project}&session={default_session(project)}").text
    assert 'id="filter"' not in imp and 'id="desc"' not in imp
