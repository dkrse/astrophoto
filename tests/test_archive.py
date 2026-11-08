"""Background analysis queue, archive import (many projects / sessions at once), duplicate warnings,
integrity check and page speed with a large archive."""
import json
import os
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import integrity, worker
from app.core import DATA_DIR, IMPORT_DIR, ARCHIVE_DIR
from app.models import SessionLocal, FrameFile, ImagingSession, Project, AnalysisTask
from app.routes_archive import path_project, path_night, clean_object
from app.routes_files import pick_per_session
from conftest import drain, write_fits, upload, star_frame, default_session
from test_sky import star_field


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


# ---------- background analysis ----------
def test_pick_per_session_spreads_over_the_night():
    t0 = datetime(2025, 1, 1, 20)
    fs = [SimpleNamespace(id=i, session_id=1, date_obs=t0 + timedelta(minutes=i)) for i in range(10)] + \
         [SimpleNamespace(id=100 + i, session_id=2, date_obs=t0 + timedelta(minutes=i)) for i in range(2)]
    ids = sorted(f.id for f in pick_per_session(fs, 3))
    assert ids == [0, 5, 9, 100, 101] or ids == [0, 4, 9, 100, 101]
    assert len(pick_per_session(fs, 1)) == 2


def test_analysis_queue_runs_in_worker(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "q1.fits", data=star_field(seed=4)), "light", project_id=project)
    upload(client, write_fits(tmp_path / "q2.fits", data=star_frame(seed=9)), "light", project_id=project)
    drain()
    r = client.post("/api/analysis/queue", json={"scope": "project", "project_id": project, "kind": "both"}).json()
    assert r["queued"] == 4                                     # 2 files × (solve + optics)
    st = client.get(f"/api/projects/{project}/status").json()
    assert st["analysis"]["queued"] == 4
    # queuing again does not duplicate waiting tasks
    assert client.post("/api/analysis/queue", json={"scope": "project", "project_id": project,
                                                    "kind": "both"}).json()["queued"] == 0
    drain()                                                     # the worker runs the queue after previews
    with SessionLocal() as db:
        tasks = db.scalars(select(AnalysisTask)).all()
        assert tasks and all(t.status in ("done", "error") for t in tasks)
        f1 = db.scalars(select(FrameFile).where(FrameFile.original_name == "q1.fits")).one()
        assert json.loads(f1.optics)["fwhm_px"] > 0             # optics result stored
        assert json.loads(f1.solve)["ok"] is False              # no star database in tests -> error stored
        assert any(t.kind == "solve" and t.status == "error" for t in tasks)
    assert client.get(f"/api/projects/{project}/status").json()["analysis"] is None
    # only_missing skips files that already have an optics result
    missing = sum(1 for f in files_of(project) if not f.optics)
    r = client.post("/api/analysis/queue", json={"scope": "project", "project_id": project, "kind": "optics",
                                                 "only_missing": True}).json()
    assert r["queued"] == missing < 2


def test_analysis_cancel_and_validation(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "c.fits"), "light", project_id=project)
    drain()
    ids = [f.id for f in files_of(project)]
    assert client.post("/api/analysis/queue", json={"scope": "files", "ids": ids, "kind": "solve"}).json()["queued"] == 1
    assert client.post("/api/analysis/cancel", json={"project_id": project}).json()["cancelled"] == 1
    assert client.post("/api/analysis/queue", json={"scope": "x", "kind": "solve"}).status_code == 400
    assert client.post("/api/analysis/queue", json={"scope": "all", "kind": "nope"}).status_code == 400
    html = client.get(f"/projects/{project}").text
    assert "openAnalysis()" in html and 'id="an-dlg"' in html


# ---------- archive import ----------
@pytest.mark.parametrize("rel,expect", [("M51/2023-09-15/Light/a.fits", "M51"), ("2023-09-15/M 51/a.fits", "M 51"),
                                        ("Lights/a.fits", "Root"), ("a.fits", "Root"),
                                        ("NGC 7000/darks/x.fits", "NGC 7000")])
def test_path_project(rel, expect):
    assert path_project(rel, "Root") == expect


def test_path_night_and_object():
    assert path_night("M51/2023-09-15/Light/a.fits") == date(2023, 9, 15)
    assert path_night("x/20190816/a.cr2") == date(2019, 8, 16)
    assert path_night("x/no-date/a.cr2") is None
    assert clean_object(" 'M 51 ' ") == "M 51" and clean_object("Unknown") == ""


def test_archive_import_creates_projects_and_sessions(client, project):
    default_session(project)                                # night 12-10-2025 exists in the fixture project
    root = IMPORT_DIR / "arch"
    # project from OBJECT + night from DATE-OBS (after midnight -> previous evening)
    write_fits(root / "night1" / "Light" / "a.fits", data=star_frame(seed=21), OBJECT="NGC  7000",
               DATE_OBS="2024-05-02T01:30:00", IMAGETYP="Light Frame")
    write_fits(root / "night1" / "Light" / "b.fits", data=star_frame(seed=22), OBJECT="ngc7000",
               DATE_OBS="2024-05-01T23:30:00", IMAGETYP="Light Frame")
    # project from the folder, night from the path, calibration frame by name
    write_fits(root / "Veil" / "2024-06-10" / "Darks" / "d.fits", data=star_frame(seed=23))
    # existing project (fixture: name "Test project", object "M42") matched by its object
    write_fits(root / "x" / "c.fits", data=star_frame(seed=24), OBJECT="M 42", DATE_OBS="2025-10-12T21:00:00")
    scan = client.post("/api/import/archive/scan", json={"path": "arch"}).json()
    assert scan["files"] == 4, [f["rel"] for g in scan["groups"] for f in g["files"]]
    g = {(x["project"], x["night"]): x for x in scan["groups"]}
    ngc = next(v for k, v in g.items() if k[1] == "2024-05-01")
    assert len(ngc["files"]) == 2 and ngc["match"] is None       # "NGC  7000" and "ngc7000" -> one group
    veil = g[("Veil", "2024-06-10")]
    assert veil["counts"] == {"dark": 1}
    m42 = next(v for v in scan["groups"] if v["match"])
    assert m42["match"]["id"] == project and m42["session_exists"]      # fixture session 12-10-2025
    groups = [{"project": x["project"], "project_id": x["match"]["id"] if x["match"] else None,
               "night": x["night_dmy"], "files": [{"rel": f["rel"], "type": f["type"]} for f in x["files"]]}
              for x in scan["groups"]]
    r = client.post("/api/import/archive/start", json={"path": "arch", "groups": groups, "skip_dups": False}).json()
    assert r["ok"] and r["files"] == 4 and r["new_projects"] == 2 and r["new_sessions"] == 2
    drain()
    with SessionLocal() as db:
        from app.routes_projects import name_key
        p = next(x for x in db.scalars(select(Project)) if name_key(x.name) == "ngc7000")
        s = db.scalars(select(ImagingSession).where(ImagingSession.project_id == p.id)).one()
        assert s.date == date(2024, 5, 1) and s.moon_illum is not None
        assert {f.original_name for f in p.files} == {"a.fits", "b.fits"}
        assert all(f.session_id == s.id and f.status == "done" for f in p.files)
        veil_p = db.scalars(select(Project).where(Project.name == "Veil")).one()
        assert veil_p.files[0].frame_type == "dark"
        assert any(f.original_name == "c.fits" for f in db.get(Project, project).files)
        for x in (p, veil_p):
            db.delete(x)
        db.commit()
    # a group without night date is refused
    bad = [{"project": "X", "night": "", "files": [{"rel": "x/c.fits", "type": "light"}]}]
    assert client.post("/api/import/archive/start", json={"path": "arch", "groups": bad}).status_code == 400
    assert client.get("/import/archive").status_code == 200


# ---------- duplicates (warning only) ----------
def test_duplicate_warning(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "d1.fits", data=star_frame(seed=31)), "light", project_id=project)
    upload(client, write_fits(tmp_path / "d2.fits", data=star_frame(seed=32)), "light", project_id=project)
    drain()
    a, b = files_of(project)[-2:]
    assert integrity.project_duplicates(SessionLocal(), project) is None
    with SessionLocal() as db:                      # simulate the same content stored twice
        db.get(FrameFile, b.id).sha256 = a.sha256
        db.commit()
        d = integrity.project_duplicates(db, project)
        assert d["files"] == 2 and d["where"][0]["name"] == "this project"
        assert integrity.duplicate_summary(db)["extra_files"] >= 1
    assert "stored more than once" in client.get(f"/projects/{project}").text
    assert "stored more than once" in client.get("/stats").text


# ---------- integrity ----------
def test_integrity_check_finds_problems(client, project, tmp_path):
    for n in ("i1.fits", "i2.fits", "i3.fits"):
        upload(client, write_fits(tmp_path / n, data=star_frame(seed=hash(n) % 1000)), "light", project_id=project)
    drain()
    f1, f2, f3 = files_of(project)[-3:]
    (DATA_DIR / f1.stored_path).unlink()                                    # missing
    with open(DATA_DIR / f2.stored_path, "ab") as fh:                       # wrong size
        fh.write(b"xx")
    p3 = DATA_DIR / f3.stored_path                                          # same size, other content
    data = bytearray(p3.read_bytes())
    data[-1] ^= 0xFF
    p3.write_bytes(bytes(data))
    orphan = ARCHIVE_DIR / "projects" / str(project) / "light" / "stray.fits"
    orphan.write_bytes(b"stray")
    quick = integrity.run_check(full=False)
    names = lambda key: {x.get("name") or x.get("path") for x in quick[key]}
    assert "i1.fits" in names("missing") and "i2.fits" in names("size") and not quick["ok"]
    assert any(o["path"].endswith("stray.fits") for o in quick["orphans"])
    assert "i3.fits" not in {x["name"] for x in quick["hash"]}              # quick check does not hash
    full = integrity.run_check(full=True)
    assert "i3.fits" in {x["name"] for x in full["hash"]}
    st = client.get("/api/integrity/status").json()
    assert not st["running"] and st["report"]["mode"] == "full"
    assert client.post("/api/integrity/start", json={"full": False}).json()["ok"]
    for _ in range(100):
        if not client.get("/api/integrity/status").json()["running"]:
            break
        time.sleep(0.05)
    orphan.unlink()
    for f in (f1, f2, f3):
        client.post(f"/files/{f.id}/delete")


# ---------- page speed with a large archive ----------
def test_pages_stay_fast_with_a_large_archive(client):
    """3000 light frames in one project over 30 nights (≈ the expected archive size per big project)."""
    with SessionLocal() as db:
        p = Project(name="Big archive project", target="M 101")
        db.add(p)
        db.flush()
        rows = []
        for n in range(30):
            s = ImagingSession(project_id=p.id, date=date(2024, 1, 1) + timedelta(days=n), telescope="T", camera="C")
            db.add(s)
            db.flush()
            for i in range(100):
                rows.append(FrameFile(project_id=p.id, session_id=s.id, frame_type="light", status="done",
                                      original_name=f"L_{n}_{i}.fits", stored_path=f"archive-data/big/{n}_{i}.fits",
                                      size=17_000_000, exposure_s=120.0, filter="L", stat_fwhm=3.0, stat_stars=500,
                                      header="SIMPLE  = T\n" * 40,
                                      date_obs=datetime(2024, 1, 1, 21) + timedelta(days=n, minutes=i)))
        db.add_all(rows)
        db.commit()
        pid = p.id
    try:
        for url, limit in ((f"/projects/{pid}", 6.0), ("/", 2.0), ("/stats", 3.0), ("/api/stats/optics", 2.0)):
            t = time.time()
            r = client.get(url)
            dt = time.time() - t
            assert r.status_code == 200, url
            assert dt < limit, f"{url} took {dt:.2f}s"
    finally:
        with SessionLocal() as db:
            db.query(FrameFile).filter(FrameFile.project_id == pid).delete()
            db.delete(db.get(Project, pid))
            db.commit()


# ---------- background processing switches (Settings) ----------
def _set(**kv):
    from app.models import Setting
    with SessionLocal() as db:
        for k, v in kv.items():
            row = db.get(Setting, k) or Setting(key=k)
            row.value = v
            db.merge(row)
        db.commit()


def test_background_switches(client, project, tmp_path):
    try:
        _set(bg_previews="0", bg_frame_stats="0", bg_analysis="0", bg_integrity="0")
        upload(client, write_fits(tmp_path / "off.fits", data=star_frame(seed=77), EXPTIME=30.0), "light",
               project_id=project)
        drain()
        f = [x for x in files_of(project) if x.original_name == "off.fits"][0]
        assert f.status == "done" and f.thumb_path is None            # no thumbnail …
        assert f.header and f.exposure_s == 30.0 and f.sha256          # … but metadata and hash
        assert f.stat_fwhm is None and f.stat_stars is None            # no frame statistics
        assert client.post("/api/analysis/queue", json={"scope": "files", "ids": [f.id], "kind": "optics"}).json()["queued"] == 1
        drain()                                                         # analysis switched off -> waits
        st = client.get(f"/api/projects/{project}/status").json()["analysis"]
        assert st["queued"] == 1 and st["paused"] is True
        assert client.post("/api/integrity/start", json={"full": False}).status_code == 403
        page = client.get("/settings").text
        assert 'name="bg_analysis"' in page and 'name="bg_previews"' in page
    finally:
        _set(bg_previews="1", bg_frame_stats="1", bg_analysis="1", bg_integrity="1")
    drain()                                                             # switched on again -> the queue runs
    assert client.get(f"/api/projects/{project}/status").json()["analysis"] is None
    client.post(f"/projects/{project}/reprocess")                       # ↻ Reprocess fills in what was skipped
    drain()
    f = [x for x in files_of(project) if x.original_name == "off.fits"][0]
    assert f.thumb_path and f.stat_stars is not None


def test_header_activity_indicator(client, project, tmp_path):
    drain()
    idle = client.get("/api/activity").json()
    assert idle["busy"] is False and idle["paused"] is False and idle["items"] == []
    assert 'id="activity"' in client.get("/").text
    upload(client, write_fits(tmp_path / "act.fits"), "light", project_id=project)
    a = client.get("/api/activity").json()
    assert a["busy"] and any("Processing" in x for x in a["items"])
    drain()
    f = files_of(project)[-1]
    try:
        _set(bg_analysis="0")
        client.post("/api/analysis/queue", json={"scope": "files", "ids": [f.id], "kind": "optics"})
        a = client.get("/api/activity").json()
        assert not a["busy"] and a["paused"] and "paused" in a["items"][0]
    finally:
        _set(bg_analysis="1")
    a = client.get("/api/activity").json()
    assert a["busy"] and "optics" in a["items"][0]
    drain()
    assert client.get("/api/activity").json()["busy"] is False
