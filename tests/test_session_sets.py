"""One session = one night with one acquisition set (exposure, gain / ISO, filter, camera temperature)."""
import json
from datetime import date

from sqlalchemy import select

from app.models import SessionLocal, FrameFile, ImagingSession, CatalogItem, Project
from app import sessets
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
    if "CCD_TEMP" in hdr and "nocool" not in hdr:  # cooled camera: the header carries the set point
        hdr["SET-TEMP"] = float(round(hdr["CCD_TEMP"]))
    hdr.pop("nocool", None)
    return write_fits(tmp_path / name, data=star_frame(seed=seed), IMAGETYP="Light Frame", **hdr)


def by_name(pid):
    return {f.original_name: f for f in files_of(pid)}


# ---------- unit ----------
def test_normalisation_and_tolerances():
    assert sessets.norm_exposure(10.024) == sessets.norm_exposure(10.045) == 10.0
    assert sessets.norm_exposure("") is None and sessets.norm_exposure("x") is None
    assert sessets.norm_gain("101.0") == "101" and sessets.norm_gain(" ISO 800 ") == "ISO 800"
    assert sessets.norm_gain("") is None and sessets.norm_gain("0.5") == "0.5"
    assert sessets.norm_filter("  Ha ") == "Ha" and sessets.norm_filter("") is None
    assert sessets.parse_temp("-10,2 °C") == -10.2 and sessets.parse_temp("abc") is None
    s = ImagingSession(exposure_s=120.0, gain="101", filter="L", cam_temp=-10.0)
    f = FrameFile(exposure_s=120.0, gain="101.0", filter="l", temperature="-9.7", frame_type="light")
    assert sessets.matches(s, sessets.frame_key(f))                  # gain / case / temperature jitter
    f.temperature = "-5.0"
    assert not sessets.matches(s, sessets.frame_key(f))              # another set point
    assert sessets.matches(s, sessets.frame_key(f, ignore_temp=True)) is False   # session has a temperature
    f.temperature, f.exposure_s = "-10", 60.0
    assert not sessets.matches(s, sessets.frame_key(f))
    assert not sessets.matches(ImagingSession(), sessets.frame_key(f))
    assert sessets.matches(ImagingSession(), sessets.frame_key(FrameFile()))   # no metadata == no set
    assert sessets.set_label(s) == "120 s · gain 101 · L · -10 °C"
    assert sessets.set_label(ImagingSession(gain="ISO 800", exposure_s=0.5)) == "0.5 s · ISO 800"
    assert sessets.set_label(ImagingSession()) == ""


# ---------- upload / worker ----------
def test_upload_splits_mixed_sets_into_sibling_sessions(client, project, tmp_path):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "telescope": "Scope A",
                                                       "camera": "Cam A", "seeing": "3"})
    [s0] = sessions_of(project)
    frames = [("a1.fits", 1, dict(EXPTIME=120.0, GAIN=101, FILTER="L", CCD_TEMP=-10.1)),
              ("a2.fits", 2, dict(EXPTIME=120.0, GAIN=101, FILTER="L", CCD_TEMP=-9.8)),
              ("b1.fits", 3, dict(EXPTIME=60.0, GAIN=101, FILTER="L", CCD_TEMP=-10.0)),
              ("c1.fits", 4, dict(EXPTIME=120.0, GAIN=101, FILTER="Ha", CCD_TEMP=-10.0)),
              ("d1.fits", 5, dict(EXPTIME=120.0, GAIN=200, FILTER="L", CCD_TEMP=-10.0)),
              ("e1.fits", 6, dict(EXPTIME=120.0, GAIN=101, FILTER="L", CCD_TEMP=-5.0))]
    for name, seed, h in frames:
        upload(client, light(tmp_path, name, seed, **h), project_id=project, session_id=s0.id)
    drain()
    ss = sessions_of(project)
    assert len(ss) == 5 and all(s.date == date(2025, 10, 12) for s in ss)
    assert all(s.telescope == "Scope A" and s.camera == "Cam A" and s.seeing == "3" for s in ss)   # copied
    fs = by_name(project)
    assert fs["a1.fits"].session_id == fs["a2.fits"].session_id == s0.id
    others = {fs[n].session_id for n in ("b1.fits", "c1.fits", "d1.fits", "e1.fits")}
    assert len(others) == 4 and s0.id not in others
    first = next(s for s in ss if s.id == s0.id)
    assert (first.exposure_s, first.gain, first.filter, first.cam_temp) == (120.0, "101", "L", -10.0)
    # every session holds one set only
    for s in ss:
        keys = {tuple(sessets.frame_key(f).values()) for f in files_of(project) if f.session_id == s.id}
        assert len(keys) == 1
    # a second upload of a known set lands in the existing sibling, not a new one
    upload(client, light(tmp_path, "b2.fits", 7, EXPTIME=60.0, GAIN=101, FILTER="L", CCD_TEMP=-10.3),
           project_id=project, session_id=s0.id)
    drain()
    assert len(sessions_of(project)) == 5
    fs = by_name(project)
    assert fs["b2.fits"].session_id == fs["b1.fits"].session_id
    page = client.get(f"/projects/{project}").text
    assert "60 s · gain 101 · L · -10 °C" in page and "120 s · gain 101 · Ha · -10 °C" in page


def test_uncooled_camera_ignores_temperature(client, project, tmp_path):
    with SessionLocal() as db:
        if not db.scalars(select(CatalogItem).where(CatalogItem.kind == "camera",
                                                    CatalogItem.name == "Warm cam")).first():
            db.add(CatalogItem(kind="camera", name="Warm cam", specs=json.dumps({"cooled": "Uncooled"})))
            db.commit()
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "camera": "Warm cam"})
    [s0] = sessions_of(project)
    for i, t in enumerate((25.2, 23.1, 17.4)):
        upload(client, light(tmp_path, f"w{i}.fits", 10 + i, EXPTIME=10.0, GAIN=200, FILTER="IRCUT", CCD_TEMP=t),
               project_id=project, session_id=s0.id)
    drain()
    assert len(sessions_of(project)) == 1
    assert sessions_of(project)[0].cam_temp is None
    assert {f.session_id for f in files_of(project)} == {s0.id}


def test_calibration_frames_and_stacks_are_not_split(client, project, tmp_path):
    sid = default_session(project)
    upload(client, light(tmp_path, "l.fits", 1, EXPTIME=120.0, FILTER="L"), project_id=project, session_id=sid)
    upload(client, write_fits(tmp_path / "d.fits", data=star_frame(seed=2), IMAGETYP="Dark Frame", EXPTIME=300.0),
           "dark", project_id=project, session_id=sid)
    upload(client, write_fits(tmp_path / "f.fits", data=star_frame(seed=3), IMAGETYP="Flat Frame", EXPTIME=1.0,
                              FILTER="Ha"), "flat", project_id=project, session_id=sid)
    drain()
    assert len(sessions_of(project)) == 1
    assert {f.session_id for f in files_of(project)} == {sid}


def test_unassigned_frame_goes_to_the_session_of_its_set(client, project, tmp_path):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "exposure_s": "60", "gain": "101",
                                                       "filter": "L", "cam_temp": "-10"})
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "exposure_s": "120", "gain": "101",
                                                       "filter": "L", "cam_temp": "-10"})
    s60, s120 = sessions_of(project)
    assert (s60.exposure_s, s120.exposure_s) == (60.0, 120.0)
    upload(client, light(tmp_path, "x.fits", 1, EXPTIME=120.0, GAIN=101, FILTER="L", CCD_TEMP=-10.0),
           project_id=project, session_id=s60.id)
    drain()
    assert files_of(project)[0].session_id == s120.id                 # predefined set wins, no new session
    assert len(sessions_of(project)) == 2


# ---------- manual changes ----------
def test_manual_session_assignment_respects_the_set(client, project, tmp_path):
    sid = default_session(project)
    upload(client, light(tmp_path, "a.fits", 1, EXPTIME=120.0, FILTER="L"), project_id=project, session_id=sid)
    upload(client, light(tmp_path, "b.fits", 2, EXPTIME=60.0, FILTER="L"), project_id=project, session_id=sid)
    drain()
    fs = by_name(project)
    a, b = fs["a.fits"], fs["b.fits"]
    assert a.session_id != b.session_id
    r = client.post("/api/files/bulk", json={"ids": [b.id], "action": "session", "value": str(a.session_id)})
    assert r.status_code == 400 and "one set" in r.json()["error"]
    assert by_name(project)["b.fits"].session_id == b.session_id       # unchanged
    # an empty session takes the set of the first light moved into it
    client.post(f"/projects/{project}/sessions", data={"date": "13-10-2025"})
    empty = sessions_of(project)[-1]
    assert empty.exposure_s is None
    r = client.post("/api/files/bulk", json={"ids": [b.id, a.id], "action": "session", "value": str(empty.id)})
    assert r.status_code == 200 and "left where they were" in r.json()["message"]
    fs = by_name(project)
    assert fs["b.fits"].session_id == empty.id and fs["a.fits"].session_id == a.session_id
    with SessionLocal() as db:
        assert db.get(ImagingSession, empty.id).exposure_s == 60.0
    # "(none)" always works
    r = client.post("/api/files/bulk", json={"ids": [a.id], "action": "session", "value": ""})
    assert r.status_code == 200 and by_name(project)["a.fits"].session_id is None


def test_file_set_values_are_not_editable(client, project, tmp_path):
    sid = default_session(project)
    upload(client, light(tmp_path, "m0.fits", 0, EXPTIME=120.0, FILTER="L"), project_id=project, session_id=sid)
    drain()
    [f] = files_of(project)
    r = client.post(f"/files/{f.id}/edit", data={"exposure_s": "300", "filter": "Ha", "note": "n"},
                    follow_redirects=False)
    assert r.status_code == 303
    [f] = files_of(project)
    assert (f.exposure_s, f.filter, f.note, f.session_id) == (120.0, "L", "n", sid)
    assert len(sessions_of(project)) == 1
    page = client.get(f"/projects/{project}").text
    assert 'name="note"' in page and 'name="exposure_s" inputmode="decimal"></label>\n        <label>Gain' not in page


def test_session_form_set_is_locked_once_it_holds_lights(client, project, tmp_path):
    sid = default_session(project)
    upload(client, light(tmp_path, "s.fits", 1, EXPTIME=120.0, FILTER="L"), project_id=project, session_id=sid)
    drain()
    r = client.post(f"/projects/{project}/sessions", data={"sid": str(sid), "date": "12-10-2025",
                                                           "exposure_s": "30", "gain": "", "filter": "Ha",
                                                           "cam_temp": ""})
    assert r.status_code == 200 and "unchanged" in r.text               # redirect followed, flash shown
    with SessionLocal() as db:
        s = db.get(ImagingSession, sid)
        assert (s.exposure_s, s.filter) == (120.0, "L")
    # an empty session's set can be edited
    client.post(f"/projects/{project}/sessions", data={"date": "14-10-2025", "exposure_s": "30"})
    new = sessions_of(project)[-1]
    client.post(f"/projects/{project}/sessions", data={"sid": str(new.id), "date": "14-10-2025",
                                                       "exposure_s": "45", "gain": "ISO 800", "filter": "",
                                                       "cam_temp": "-9.6"})
    with SessionLocal() as db:
        s = db.get(ImagingSession, new.id)
        assert (s.exposure_s, s.gain, s.filter, s.cam_temp) == (45.0, "ISO 800", None, -10.0)
    # a form without the set fields (older clients) leaves the set alone
    client.post(f"/projects/{project}/sessions", data={"sid": str(new.id), "date": "14-10-2025"})
    with SessionLocal() as db:
        assert db.get(ImagingSession, new.id).exposure_s == 45.0


def test_sessions_auto_creates_one_session_per_set(client, project, tmp_path):
    sid = default_session(project)
    for i, e in enumerate((60.0, 60.0, 180.0)):
        upload(client, light(tmp_path, f"n{i}.fits", i, EXPTIME=e, DATE_OBS="2025-11-02T21:00:00"),
               project_id=project, session_id=sid)
    drain()
    client.post("/api/files/bulk", json={"ids": [f.id for f in files_of(project)], "action": "session", "value": ""})
    r = client.post(f"/projects/{project}/sessions/auto", follow_redirects=False)
    assert r.status_code == 303
    nov = [s for s in sessions_of(project) if s.date == date(2025, 11, 2)]
    assert sorted(s.exposure_s for s in nov) == [60.0, 180.0]
    fs = by_name(project)
    assert fs["n0.fits"].session_id == fs["n1.fits"].session_id != fs["n2.fits"].session_id


# ---------- migration of existing (mixed) sessions ----------
def test_split_project_sorts_legacy_mixed_session(client, project, tmp_path):
    sid = default_session(project)
    specs = [(120.0, "-10.0"), (120.0, "-10.1"), (120.0, "-9.9"), (20.0, "-10.0"), (120.0, "-5.0")]
    for i, (e, t) in enumerate(specs):
        upload(client, light(tmp_path, f"g{i}.fits", i, EXPTIME=e, GAIN=101, CCD_TEMP=float(t)),
               project_id=project, session_id=sid)
    drain()
    # simulate data of an older version: everything in one session, no set stored
    with SessionLocal() as db:
        for f in db.scalars(select(FrameFile).where(FrameFile.project_id == project)):
            f.session_id = sid
        for s in db.scalars(select(ImagingSession).where(ImagingSession.project_id == project,
                                                         ImagingSession.id != sid)):
            db.delete(s)
        s = db.get(ImagingSession, sid)
        sessets.apply_key(s, dict.fromkeys(sessets.SET_FIELDS))
        db.commit()
    with SessionLocal() as db:
        moved = sessets.split_project(db, db.get(Project, project))
        db.commit()
    assert moved == 2
    ss = sessions_of(project)
    assert len(ss) == 3
    main = next(s for s in ss if s.id == sid)
    assert (main.exposure_s, main.cam_temp) == (120.0, -10.0)          # the largest set stays
    fs = by_name(project)
    assert {fs[f"g{i}.fits"].session_id for i in range(3)} == {sid}
    assert len({fs["g3.fits"].session_id, fs["g4.fits"].session_id, sid}) == 3
    with SessionLocal() as db:                                          # running it again changes nothing
        assert sessets.split_project(db, db.get(Project, project)) == 0
        db.commit()


def test_export_and_session_table_show_the_set(client, project, tmp_path):
    sid = default_session(project)
    upload(client, light(tmp_path, "x.fits", 1, EXPTIME=90.0, GAIN=120, FILTER="OIII", CCD_TEMP=-15.2),
           project_id=project, session_id=sid)
    drain()
    from app.routes_projects import metadata_json_bytes
    with SessionLocal() as db:
        meta = json.loads(metadata_json_bytes(db.get(Project, project)))
    [s] = meta["sessions"]
    assert (s["exposure_s"], s["gain"], s["filter"], s["cam_temp"]) == (90.0, "120", "OIII", -15.0)
    page = client.get(f"/projects/{project}?so=set").text
    assert "90 s · gain 120 · OIII · -15 °C" in page and 'name="cam_temp"' in page
