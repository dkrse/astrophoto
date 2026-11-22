"""Session name (default: the project name); the uploader's session for the night most dropped files were taken in."""
from sqlalchemy import select

from app.models import SessionLocal, ImagingSession


def sessions_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(ImagingSession).where(ImagingSession.project_id == pid)
                          .order_by(ImagingSession.id)).all()


def test_session_name_is_saved_shown_and_optional(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "name": "Ha test"})
    client.post(f"/projects/{project}/sessions", data={"date": "13-10-2025"})
    a, b = sessions_of(project)[-2:]
    assert a.name == "Ha test" and b.name == "Test project"      # default: the project name
    html = client.get(f"/projects/{project}").text
    assert "Ha test" in html and 'data-key="name"' in html
    client.post(f"/projects/{project}/sessions", data={"sid": str(a.id), "date": "12-10-2025", "name": ""})
    assert sessions_of(project)[-2].name is None


def test_sessions_sort_by_name(client, project):
    for d, n in (("12-10-2025", "Beta"), ("13-10-2025", "alpha")):
        client.post(f"/projects/{project}/sessions", data={"date": d, "name": n})
    html = client.get(f"/projects/{project}?so=name").text
    assert html.index("alpha") < html.index("Beta")


def test_for_night_reuses_the_existing_session(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "camera": "ZWO", "name": "N1"})
    [s] = [x for x in sessions_of(project) if str(x.date) == "2025-10-12"]
    r = client.post(f"/api/projects/{project}/sessions/for-night", json={"date": "2025-10-12"})
    j = r.json()
    assert j["id"] == s.id and not j["created"] and j["label"].startswith("N1 · 12-10-2025")


def test_for_night_creates_a_session_with_the_equipment_of_the_source(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "location": "Home", "seeing": "3"})
    src = [x for x in sessions_of(project) if str(x.date) == "2025-10-12"][0]
    n = len(sessions_of(project))
    j = client.post(f"/api/projects/{project}/sessions/for-night",
                    json={"date": "2025-10-20", "from": src.id}).json()
    assert j["created"]
    new = [x for x in sessions_of(project) if x.id == j["id"]][0]
    assert len(sessions_of(project)) == n + 1
    assert str(new.date) == "2025-10-20" and new.location == "Home" and new.seeing is None
    assert new.moon_illum is not None


def test_for_night_rejects_bad_dates(client, project):
    assert client.post(f"/api/projects/{project}/sessions/for-night", json={"date": "12-10-2025"}).status_code == 400
    assert client.post(f"/api/projects/{project}/sessions/for-night", json={}).status_code == 400


# ---------- defaults, settings, fixing sessions from their files ----------
from app.models import FrameFile, Setting  # noqa: E402
from app.core import invalidate_settings  # noqa: E402
from conftest import drain, write_fits, upload, star_frame  # noqa: E402


def set_setting(k, v):
    with SessionLocal() as db:
        row = db.get(Setting, k)
        if row:
            row.value = v
        else:
            db.add(Setting(key=k, value=v))
        db.commit()
    invalidate_settings()


def light(tmp_path, name, seed, when):
    return write_fits(tmp_path / name, data=star_frame(seed=seed), IMAGETYP="Light Frame", EXPTIME=60.0,
                      DATE_OBS=when)


def test_sessions_follow_a_renamed_project_unless_renamed_themselves(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025"})
    client.post(f"/projects/{project}/sessions", data={"date": "13-10-2025", "name": "Own"})
    client.post(f"/projects/{project}/edit", data={"name": "Renamed", "target": "M42"})
    names = sorted(x.name for x in sessions_of(project))
    assert "Own" in names and "Test project" not in names and "Renamed" in names


def test_session_columns_setting(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025"})
    try:
        r = client.post("/settings", data={"session_columns": ["", "night", "files", "bogus"]})
        assert r.status_code in (200, 303)
        with SessionLocal() as db:
            assert db.get(Setting, "session_columns").value == "night,files"
        html = client.get(f"/projects/{project}").text
        assert 'data-key="night"' in html and 'data-key="name"' not in html and 'data-key="moon"' not in html
    finally:
        set_setting("session_columns", "name,night,set,equipment,scale,moon,measured,files")


def test_fix_sessions_names_and_majority_night(client, project, tmp_path):
    client.post(f"/projects/{project}/sessions", data={"date": "01-10-2025"})
    [s] = sessions_of(project)
    with SessionLocal() as db:
        db.get(ImagingSession, s.id).name = None
        db.commit()
    for i, when in enumerate(["2025-10-12T22:00:00", "2025-10-13T01:00:00", "2025-10-12T23:00:00",
                              "2025-10-20T22:00:00"]):
        upload(client, light(tmp_path, f"l{i}.fits", i, when), project_id=project, session_id=s.id)
    drain()
    j = client.post(f"/api/projects/{project}/sessions/fix").json()
    assert j["named"] == 1
    ss = sessions_of(project)
    assert [str(x.date) for x in ss] == ["2025-10-12"] and ss[0].name == "Test project"
    assert client.post(f"/api/projects/{project}/sessions/fix").json() == {"named": 0, "redated": 0, "merged": 0}


def test_fix_sessions_merges_into_the_twin_of_that_night(client, project, tmp_path):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "exposure_s": "60"})
    client.post(f"/projects/{project}/sessions", data={"date": "01-10-2025", "exposure_s": "60", "seeing": "2"})
    a, b = sessions_of(project)
    for i in range(2):
        upload(client, light(tmp_path, f"m{i}.fits", 10 + i, "2025-10-12T22:00:00"), project_id=project,
               session_id=b.id)
    drain()
    with SessionLocal() as db:                    # worker may have placed them already; force them into b
        for f in db.scalars(select(FrameFile).where(FrameFile.project_id == project)):
            f.session_id = b.id
        db.commit()
    j = client.post(f"/api/projects/{project}/sessions/fix").json()
    assert j["merged"] == 1
    [only] = sessions_of(project)
    assert only.id == a.id and only.seeing == "2"
    with SessionLocal() as db:
        assert {f.session_id for f in db.scalars(select(FrameFile).where(FrameFile.project_id == project))} == {a.id}


def test_worker_fixes_sessions_when_switched_on(client, project, tmp_path):
    client.post(f"/projects/{project}/sessions", data={"date": "01-10-2025"})
    [s] = sessions_of(project)
    set_setting("sessions_auto_fix", "1")
    try:
        upload(client, light(tmp_path, "w.fits", 7, "2025-10-15T22:00:00"), project_id=project, session_id=s.id)
        drain()
        assert [str(x.date) for x in sessions_of(project)] == ["2025-10-15"]
    finally:
        set_setting("sessions_auto_fix", "0")


def test_analyse_fix_renames_every_session(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "name": "Own"})
    j = client.post(f"/api/projects/{project}/sessions/fix").json()
    assert j["named"] == 1 and [x.name for x in sessions_of(project)] == ["Test project"]
