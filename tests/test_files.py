"""Uploads, background processing, bulk operations, downloads, import, calibration library."""
import hashlib
import io
import json
import zipfile

from sqlalchemy import select

from datetime import date

from app.models import SessionLocal, FrameFile, ImportJob, ImagingSession, Project, Setting
from conftest import drain, write_fits, upload, star_frame, default_session, DATA_DIR, IMPORT_DIR


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------- upload ----------
def test_chunked_upload_processing_and_duplicates(client, project, tmp_path):
    f = write_fits(tmp_path / "L_001.fits", IMAGETYP="Light Frame", EXPTIME=120.0, FILTER="Ha",
                   DATE_OBS="2025-10-12T21:00:00")
    r = upload(client, f, project_id=project)
    assert r["ok"]
    drain()
    [ff] = files_of(project)
    assert ff.status == "done" and ff.thumb_path and ff.sha256 == sha(f)
    assert ff.exposure_s == 120.0 and ff.filter == "Ha" and ff.date_obs.year == 2025
    assert ff.stat_fwhm and ff.stat_stars > 10
    # same content again -> skipped
    assert upload(client, f, project_id=project).get("duplicate") is True
    assert len(files_of(project)) == 1


def test_upload_validation(client, project):
    assert client.post("/api/uploads", json={"project_id": project, "filename": "a.fits", "size": 0,
                                             "frame_type": "light"}).status_code == 400
    assert client.post("/api/uploads", json={"project_id": project, "filename": "a.fits", "size": 5,
                                             "frame_type": "../x"}).status_code == 400
    uid = client.post("/api/uploads", json={"project_id": project, "filename": "a.fits", "size": 5,
                                            "frame_type": "light", "session_id": default_session(project)}).json()["id"]
    assert client.put(f"/api/uploads/{uid}?offset=3", content=b"abc").status_code == 409      # wrong offset
    assert client.put(f"/api/uploads/{uid}?offset=0", content=b"0123456789").status_code == 400   # too much
    assert client.put(f"/api/uploads/{uid}?offset=0", content=b"012").json()["offset"] == 3
    assert client.post(f"/api/uploads/{uid}/finish").status_code == 409                      # incomplete
    assert client.get("/api/uploads/..%2F..%2Fetc").status_code == 404


def test_upload_filename_cannot_escape(client, project, tmp_path):
    p = tmp_path / "x.fits"
    p.write_bytes(b"SIMPLE")
    data = p.read_bytes()
    uid = client.post("/api/uploads", json={"project_id": project, "filename": "../../../etc/evil.fits",
                                            "size": len(data), "frame_type": "other",
                                            "session_id": default_session(project)}).json()["id"]
    client.put(f"/api/uploads/{uid}?offset=0", content=data)
    assert client.post(f"/api/uploads/{uid}/finish").json()["ok"]
    [ff] = files_of(project)
    assert ff.original_name == "evil.fits" and ".." not in ff.stored_path
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/projects/{project}/zip?types=other").content))
    assert all(".." not in n for n in z.namelist())


# ---------- originals are never modified ----------
def test_view_settings_never_touch_original(client, project, tmp_path):
    f = write_fits(tmp_path / "keep.fits", IMAGETYP="Light Frame")
    upload(client, f, project_id=project)
    drain()
    [ff] = files_of(project)
    stored = DATA_DIR / ff.stored_path
    before = sha(stored)
    client.post(f"/files/{ff.id}/opts", data={"bayer": "RGGB", "stretch": "manual", "bp": "0.05", "mp": "0.1",
                                             "wp": "0.7", "flip": "v", "channels": "bgr", "brightness": "40"})
    for q in ("size=preview", "size=full&stretch=manual&bp=0.1&mp=0.2&wp=0.9"):
        assert client.get(f"/files/{ff.id}/render?{q}").headers["content-type"] == "image/jpeg"
    h = client.get(f"/files/{ff.id}/histogram").json()
    assert h["bins"] == 256 and "auto" in h
    assert sha(stored) == before == sha(f)
    downloaded = client.get(f"/files/{ff.id}/download").content
    assert hashlib.sha256(downloaded).hexdigest() == before


# ---------- bulk / delete safety ----------
def test_bulk_type_change_moves_file_and_thumb(client, project, tmp_path):
    a = write_fits(tmp_path / "same.fits", IMAGETYP="Light Frame", data=star_frame(seed=1))
    b = write_fits(tmp_path / "d" / "same.fits", IMAGETYP="Dark Frame", data=star_frame(seed=2))
    upload(client, a, "light", project_id=project)
    upload(client, b, "dark", project_id=project)
    drain()
    la, da = files_of(project)
    assert la.thumb_path != da.thumb_path                  # same name, different type -> separate thumbnails
    r = client.post("/api/files/bulk", json={"ids": [da.id], "action": "type", "value": "light"})
    assert r.json()["ok"]
    la, da = files_of(project)
    assert da.frame_type == "light" and da.stored_path != la.stored_path
    assert (DATA_DIR / da.stored_path).is_file() and (DATA_DIR / la.stored_path).is_file()
    assert sha(DATA_DIR / da.stored_path) == sha(b)
    # move into own project = no-op (no renaming)
    before = [f.stored_path for f in files_of(project)]
    client.post("/api/files/bulk", json={"ids": [la.id, da.id], "action": "move", "value": str(project)})
    assert [f.stored_path for f in files_of(project)] == before


def test_delete_never_removes_shared_path(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "a.fits", data=star_frame(seed=3)), project_id=project)
    upload(client, write_fits(tmp_path / "b.fits", data=star_frame(seed=4)), project_id=project)
    drain()
    a, b = files_of(project)
    with SessionLocal() as db:                              # simulate a corrupted reference
        db.get(FrameFile, b.id).stored_path = a.stored_path
        db.commit()
    client.post(f"/files/{b.id}/delete")
    assert (DATA_DIR / a.stored_path).is_file()
    assert client.get(f"/files/{a.id}/download").status_code == 200


def test_bulk_blocked_while_processing(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "p.fits"), project_id=project)
    [ff] = files_of(project)
    assert ff.status == "pending"
    r = client.post("/api/files/bulk", json={"ids": [ff.id], "action": "delete"})
    assert r.status_code == 409
    drain()


def test_missing_file_returns_404(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "m.fits"), project_id=project)
    drain()
    [ff] = files_of(project)
    (DATA_DIR / ff.stored_path).unlink()
    for u in ("download", "histogram", "render"):
        assert client.get(f"/files/{ff.id}/{u}").status_code == 404, u
    assert client.get(f"/files/{ff.id}/view").status_code == 200


# ---------- rejection, zip, export ----------
def test_reject_suspects_zip_and_metadata(client, project, tmp_path):
    for i in range(5):
        upload(client, write_fits(tmp_path / f"L{i}.fits", data=star_frame(seed=10 + i), IMAGETYP="Light Frame",
                                  EXPTIME=60.0, FILTER="L", DATE_OBS=f"2025-10-12T2{i}:00:00"), project_id=project)
    upload(client, write_fits(tmp_path / "blur.fits", data=star_frame(sigma=4.5, seed=20), IMAGETYP="Light Frame",
                              EXPTIME=60.0, FILTER="L", DATE_OBS="2025-10-13T01:00:00"), project_id=project)
    drain()
    assert [f.original_name for f in files_of(project) if f.suspect] == ["blur.fits"]
    client.post(f"/projects/{project}/rejectsuspects")
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/projects/{project}/zip").content))
    names = z.namelist()
    assert "01_2025-10-12/light/blur.fits" not in names
    assert len([n for n in names if n.startswith("01_2025-10-12/light/")]) == 5
    assert "INFO.txt" in names and "metadata.json" in names
    assert "5m" in z.read("INFO.txt").decode()             # 5 × 60 s, rejected frame not counted
    # sessions from DATE-OBS: 2 frames after midnight belong to the same night
    client.post(f"/projects/{project}/sessions/auto")
    with SessionLocal() as db:
        from app.models import ImagingSession
        nights = db.scalars(select(ImagingSession.date).where(ImagingSession.project_id == project)).all()
    assert len(nights) == 1
    # metadata export
    ez = zipfile.ZipFile(io.BytesIO(client.get(f"/projects/{project}/export").content))
    meta = json.loads(ez.read("metadata.json"))
    assert meta["project"]["name"] == "Test project" and len(meta["files"]) == 6
    f0 = meta["files"][0]
    assert any(c["key"] == "EXPTIME" and c["value"] == 60.0 for c in f0["fits_header"])
    assert f0["stats"]["fwhm_px"] and any(n.startswith("headers/01_2025-10-12/light/") for n in ez.namelist())


# ---------- server import ----------
def test_import_scan_start_and_duplicates(client, project):
    root = IMPORT_DIR / "M42"
    write_fits(root / "lights" / "a.fits", IMAGETYP="Light Frame", data=star_frame(seed=30))
    write_fits(root / "darks" / "d.fits", data=star_frame(seed=31))
    write_fits(root / "biases" / "b.fits", IMAGETYP="Bias Frame", data=star_frame(seed=32))
    assert client.get("/api/import/browse?path=../..").status_code == 400
    assert client.post("/api/import/scan", json={"path": "../../etc"}).status_code == 400
    scan = client.post("/api/import/scan", json={"path": "M42", "recursive": True}).json()["files"]
    assert {f["rel"]: f["type"] for f in scan} == {"biases/b.fits": "bias", "darks/d.fits": "dark",
                                                   "lights/a.fits": "light"}
    body = {"project_id": project, "path": "M42", "files": scan, "skip_dups": True}
    assert client.post("/api/import/start", json=body).status_code == 400          # session required
    body["session_id"] = default_session(project)
    assert client.post("/api/import/start", json=body).json()["total"] == 3
    drain()
    got = files_of(project)
    assert sorted(f.frame_type for f in got) == ["bias", "dark", "light"] and all(f.status == "done" for f in got)
    by_name = {f.original_name: f for f in got}
    assert sha(DATA_DIR / by_name["a.fits"].stored_path) == sha(root / "lights" / "a.fits")
    # importing again: everything skipped as duplicate, sources untouched
    job = client.post("/api/import/start", json=body).json()["job"]
    drain()
    with SessionLocal() as db:
        j = db.get(ImportJob, job)
        assert (j.skipped, j.done, j.status) == (3, 0, "done")
    assert len(files_of(project)) == 3 and (root / "lights" / "a.fits").is_file()


# ---------- calibration library ----------
def test_calibration_set_link_and_zip(client, project, tmp_path):
    r = client.post("/calibration/new", data={"frame_type": "dark", "capture_date": "10-10-2025",
                                              "temperature": "-10", "exposure_s": "120"}, follow_redirects=False)
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    assert "Dark" in client.get(f"/calibration/{cid}").text
    client.post(f"/projects/{project}/sessions", data={"date": "11-10-2025", "camera": "CamZ"})
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025"})
    with SessionLocal() as db:
        s1, s2 = db.scalars(select(ImagingSession).where(ImagingSession.project_id == project)
                            .order_by(ImagingSession.date)).all()
    upload(client, write_fits(tmp_path / "md.fits", data=star_frame(seed=40)), "dark", calib_set_id=cid)
    upload(client, write_fits(tmp_path / "l.fits", data=star_frame(seed=41)), "light", project_id=project,
           session_id=s1.id)
    upload(client, write_fits(tmp_path / "l2.fits", data=star_frame(seed=42)), "light", project_id=project,
           session_id=s2.id)
    upload(client, write_fits(tmp_path / "fl.fits", data=star_frame(seed=43)), "flat", project_id=project,
           session_id=s2.id)
    drain()
    client.post(f"/sessions/{s1.id}/calibration", data={"set_id": str(cid)})
    names = set(zipfile.ZipFile(io.BytesIO(client.get(f"/projects/{project}/zip").content)).namelist())
    assert {"01_2025-10-11/dark/md.fits", "01_2025-10-11/light/l.fits", "02_2025-10-12/light/l2.fits",
            "02_2025-10-12/flat/fl.fits"} <= names
    names = zipfile.ZipFile(io.BytesIO(client.get(f"/projects/{project}/zip?session={s2.id}").content)).namelist()
    assert "02_2025-10-12/light/l2.fits" in names and not any(n.startswith("01_") for n in names)
    t = client.get(f"/projects/{project}").text
    assert '"label": "11-10-2025"' in t and 'id="s%d_light"' % s1.id in t
    assert "Test project" in client.get(f"/calibration/{cid}").text
    assert client.post(f"/calibration/{cid}/delete", follow_redirects=False).status_code == 303
    assert client.get(f"/calibration/{cid}").status_code == 404


def test_legacy_project_fields_migrate_to_session(client):
    from app.main import migrate_project_to_sessions
    with SessionLocal() as db:
        p = Project(name="Legacy", capture_date=date(2024, 3, 5), telescope="OldScope", camera="OldCam")
        db.add(p)
        db.flush()
        db.add(FrameFile(project_id=p.id, frame_type="light", original_name="x.fits", stored_path="__x", size=1))
        db.execute(Setting.__table__.delete().where(Setting.key == "migr_sessions_v1"))
        db.commit()
        migrate_project_to_sessions(db)
        db.refresh(p)
        [s] = p.sessions
        assert (s.date, s.telescope, s.camera) == (date(2024, 3, 5), "OldScope", "OldCam")
        assert all(f.session_id == s.id for f in p.files)
        pid = p.id
    client.post(f"/projects/{pid}/delete")


def test_gallery_and_slides(client, project, tmp_path):
    from PIL import Image
    p = tmp_path / "final.png"
    Image.new("RGB", (60, 40), (10, 20, 120)).save(p)
    upload(client, p, "stack", project_id=project)
    drain()
    [ff] = files_of(project)
    assert f"/files/{ff.id}/thumb" in client.get("/gallery").text
    assert client.get(f"/api/slides?ids={ff.id}").json()[0]["title"] == "Test project"
    client.post(f"/projects/{project}/cover", data={"file_id": str(ff.id)})
    assert f"/files/{ff.id}/thumb" in client.get("/").text


def test_archive_dir_layout_and_migration(client, project):
    from app.core import ARCHIVE_DIR, project_dir, calib_dir
    from app.main import migrate_to_archive_dir
    assert project_dir(project).parent.parent == ARCHIVE_DIR and calib_dir(1).parent.parent == ARCHIVE_DIR
    old = DATA_DIR / "projects" / "424242" / "light"
    old.mkdir(parents=True)
    (old / "x.fits").write_bytes(b"x")
    migrate_to_archive_dir()
    assert (ARCHIVE_DIR / "projects" / "424242" / "light" / "x.fits").is_file()
    assert not (DATA_DIR / "projects").exists()


def test_file_table_compact_with_info_modal(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "it.fits", data=star_frame(seed=60), IMAGETYP="Light Frame",
                              EXPTIME=30.0, FILTER="Ha", DATE_OBS="2025-10-12T22:15:00"), project_id=project)
    drain()
    t = client.get(f"/projects/{project}").text
    assert 'name="gain"' not in t.split('id="info-dlg"')[0]          # no inline edit fields in the table
    assert "12-10-2025 22:15" in t and 'onclick="fileInfo(this)"' in t and 'id="info-dlg"' in t
    import html as _h, re as _re
    raw = _h.unescape(_re.search(r"data-info='([^']*)'", t).group(1))
    d = json.loads(raw)
    assert d["filter"] == "Ha" and d["exposure_s"] == "30" and d["name"] == "it.fits"


def test_info_dialog_assigns_session_and_bulk_bar_present(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "s.fits", data=star_frame(seed=70)), "light", project_id=project)
    drain()
    t = client.get(f"/projects/{project}").text
    assert 'class="selfile"' in t and 'id="bulkbar"' in t and 'data-fi="session"' in t
    [ff] = files_of(project)
    r = client.post("/api/files/bulk", json={"ids": [ff.id], "action": "session", "value": ""})
    assert r.status_code == 200 and files_of(project)[0].session_id is None


def test_upload_precheck_reports_existing(client, project, tmp_path):
    p = write_fits(tmp_path / "dup.fits", data=star_frame(seed=80))
    upload(client, p, "light", project_id=project)
    r = client.post("/api/uploads/check", json={"project_id": project, "files": [
        {"name": "dup.fits", "size": p.stat().st_size}, {"name": "dup.fits", "size": 1}, {"name": "new.fits", "size": 5}]})
    ex = r.json()["existing"]
    assert ex[0] == "session 12-10-2025" and ex[1] is None and ex[2] is None
    t = client.get(f"/projects/{project}").text
    assert '<details class="sessblock"' in t and 'class="resizable"' in t


def test_zip_sessions_with_same_date_do_not_collide(client, project, tmp_path):
    for _ in range(2):
        client.post(f"/projects/{project}/sessions", data={"date": "05-05-2025"})
    with SessionLocal() as db:
        ss = db.scalars(select(ImagingSession).where(ImagingSession.project_id == project)
                        .order_by(ImagingSession.id)).all()
    for i, s in enumerate(ss):
        upload(client, write_fits(tmp_path / f"d{i}" / "same.fits", data=star_frame(seed=90 + i)), "light",
               project_id=project, session_id=s.id)
    names = zipfile.ZipFile(io.BytesIO(client.get(f"/projects/{project}/zip").content)).namelist()
    assert "01_2025-05-05/light/same.fits" in names and "02_2025-05-05/light/same.fits" in names


def test_zip_all_sessions_includes_every_type(client, project, tmp_path):
    from PIL import Image
    for d in ("05-11-2026", "06-11-2026", "06-11-2026"):
        client.post(f"/projects/{project}/sessions", data={"date": d})
    with SessionLocal() as db:
        ss = db.scalars(select(ImagingSession).where(ImagingSession.project_id == project)
                        .order_by(ImagingSession.id)).all()
    upload(client, write_fits(tmp_path / "l.fits", data=star_frame(seed=95)), "light", project_id=project, session_id=ss[0].id)
    upload(client, write_fits(tmp_path / "l2.fits", data=star_frame(seed=96)), "light", project_id=project, session_id=ss[1].id)
    png = tmp_path / "final.png"
    Image.new("RGB", (30, 20), (1, 2, 3)).save(png)
    upload(client, png, "stack", project_id=project, session_id=ss[2].id)
    t = client.get(f"/projects/{project}").text
    url = t.split('class="btn primary" href="')[1].split('"')[0].replace("&amp;", "&")
    names = zipfile.ZipFile(io.BytesIO(client.get(url).content)).namelist()
    folders = sorted({n.split("/")[0] for n in names if "/" in n})
    assert folders == ["01_2026-11-05", "02_2026-11-06", "03_2026-11-06"]
    assert "03_2026-11-06/stack/final.png" in names


def test_gallery_shows_photo_date_else_session_date(client, project, tmp_path):
    from PIL import Image
    png = tmp_path / "g.png"
    Image.new("RGB", (30, 20), (9, 9, 9)).save(png)
    upload(client, png, "stack", project_id=project)                         # no EXIF date -> session date
    upload(client, write_fits(tmp_path / "g.fits", data=star_frame(seed=99), DATE_OBS="2024-02-03T21:00:00"),
           "stack", project_id=project)
    drain()
    t = client.get("/gallery").text
    assert "M42 · 12-10-2025" in t and "M42 · 03-02-2024" in t


def test_gallery_pagination_and_settings(client, project, tmp_path):
    from PIL import Image
    for i in range(5):
        png = tmp_path / f"p{i}.png"
        Image.new("RGB", (20 + i, 20), (i, 0, 0)).save(png)
        upload(client, png, "stack", project_id=project)
    drain()
    client.post("/settings", data={"gallery_page_size": "4", "gallery_tile": "small", "accent_color": "#3b5bdb"})
    t = client.get("/gallery").text
    assert "1–4 of 5" in t and "tiles-small" in t and "page=2" in t
    assert "5–5 of 5" in client.get("/gallery?page=2").text
    assert "1–5 of 5" in client.get("/gallery?per=12&sort=added_desc").text
    assert 'name="gallery_page_size"' in client.get("/settings").text
    client.post("/settings", data={"reset": "1"})


def test_delete_session_deletes_its_files(client, project, tmp_path):
    client.post(f"/projects/{project}/sessions", data={"date": "09-09-2026"})
    client.post(f"/projects/{project}/sessions", data={"date": "10-09-2026"})
    with SessionLocal() as db:
        keep, s = db.scalars(select(ImagingSession).where(ImagingSession.project_id == project)
                             .order_by(ImagingSession.date)).all()
        keep_sid = keep.id
    upload(client, write_fits(tmp_path / "gone.fits", data=star_frame(seed=111)), "light", project_id=project, session_id=s.id)
    upload(client, write_fits(tmp_path / "keep.fits", data=star_frame(seed=112)), "light", project_id=project, session_id=keep_sid)
    drain()
    gone = next(f for f in files_of(project) if f.original_name == "gone.fits")
    assert (DATA_DIR / gone.stored_path).is_file()
    r = client.post(f"/sessions/{s.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert [f.original_name for f in files_of(project)] == ["keep.fits"]
    assert not (DATA_DIR / gone.stored_path).exists() and not (DATA_DIR / gone.thumb_path).exists()
    assert client.post(f"/sessions/{s.id}/delete", follow_redirects=False).status_code == 303   # double submit ok
    assert "including all its 1 file(s)" in client.get(f"/projects/{project}").text


# ---------- regression tests from the code review ----------
def _sessions(pid):
    with SessionLocal() as db:
        return db.scalars(select(ImagingSession).where(ImagingSession.project_id == pid)
                          .order_by(ImagingSession.date, ImagingSession.id)).all()


def test_session_requires_valid_date(client, project):
    for bad in ("", "31-02-2025", "abc"):
        r = client.post(f"/projects/{project}/sessions", data={"date": bad}, follow_redirects=False)
        assert r.status_code == 303
    assert _sessions(project) == []
    assert "valid night date" in client.get(f"/projects/{project}").text


def test_bulk_session_validation_and_move_keeps_session(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "b.fits", data=star_frame(seed=120)), "light", project_id=project)
    [ff] = files_of(project)
    sid = ff.session_id
    r = client.post("/api/files/bulk", json={"ids": [ff.id], "action": "session", "value": "999999"})
    assert r.status_code == 404 and files_of(project)[0].session_id == sid          # unchanged
    r = client.post("/api/files/bulk", json={"ids": [ff.id], "action": "session", "value": "abc"})
    assert r.status_code == 404 and files_of(project)[0].session_id == sid
    other = int(client.post("/projects/new", data={"name": "Target"}, follow_redirects=False)
                .headers["location"].rsplit("/", 1)[1])
    client.post(f"/projects/{other}/sessions", data={"date": "01-01-2026"})
    client.post(f"/projects/{other}/sessions", data={"date": "02-01-2026"})
    drain()
    r = client.post("/api/files/bulk", json={"ids": [ff.id], "action": "move", "value": str(other)})
    assert r.status_code == 200
    [moved] = files_of(other)
    assert moved.session_id == _sessions(other)[-1].id                               # latest night of target
    client.post(f"/projects/{other}/delete")


def test_legacy_multipart_upload_removed(client, project):
    r = client.post(f"/projects/{project}/upload", data={"frame_type": "light"},
                    files={"files": ("x.fits", b"SIMPLE")})
    assert r.status_code in (404, 405)


def test_upload_api_rejects_bad_input(client, project):
    assert client.post("/api/uploads", json={"project_id": "abc", "filename": "a.fits", "size": 5,
                                             "frame_type": "light"}).status_code == 400
    assert client.post("/api/uploads", json=[1, 2]).status_code == 400
    assert client.post("/api/uploads/check", json={"project_id": "1.5", "files": []}).status_code == 400
    r = client.post("/api/uploads", json={"project_id": project, "filename": "huge.fits", "size": 10 ** 18,
                                          "frame_type": "light", "session_id": default_session(project)})
    assert r.status_code == 507 and "disk space" in r.json()["error"]


def test_upload_finish_after_session_deleted_leaves_no_orphan(client, project):
    sid = default_session(project)
    data = b"0123456789"
    uid = client.post("/api/uploads", json={"project_id": project, "filename": "late.fits", "size": len(data),
                                            "frame_type": "light", "session_id": sid}).json()["id"]
    client.put(f"/api/uploads/{uid}?offset=0", content=data)
    client.post(f"/sessions/{sid}/delete")
    r = client.post(f"/api/uploads/{uid}/finish")
    assert r.status_code == 410 and files_of(project) == []
    assert not list((DATA_DIR / "archive-data" / "projects" / str(project)).rglob("late*"))
    assert client.post(f"/api/uploads/{uid}/finish").status_code == 404              # repeated finish


def test_export_keeps_headers_of_equal_names_from_all_sessions(client, project, tmp_path):
    client.post(f"/projects/{project}/sessions", data={"date": "01-03-2026"})
    client.post(f"/projects/{project}/sessions", data={"date": "02-03-2026"})
    s1, s2 = _sessions(project)
    for i, s in enumerate((s1, s2)):
        upload(client, write_fits(tmp_path / f"n{i}" / "Light_001.fits", data=star_frame(seed=130 + i)), "light",
               project_id=project, session_id=s.id)
    drain()
    names = zipfile.ZipFile(io.BytesIO(client.get(f"/projects/{project}/export").content)).namelist()
    assert "headers/01_2026-03-01/light/Light_001.fits.txt" in names
    assert "headers/02_2026-03-02/light/Light_001.fits.txt" in names


def test_stats_do_not_invent_nights(client, project, tmp_path):
    from app.models import FrameFile as FF
    upload(client, write_fits(tmp_path / "nd.fits", data=star_frame(seed=140), EXPTIME=10.0), "light",
           project_id=project)
    drain()
    with SessionLocal() as db:                       # simulate a light without session and without DATE-OBS
        f = db.scalars(select(FF).where(FF.project_id == project)).one()
        f.session_id, f.date_obs = None, None
        db.commit()
    t = client.get("/stats").text
    assert '<b>0</b><span>imaging nights</span>' in t


def test_migration_handles_legacy_project_without_date(client):
    from app.main import migrate_project_to_sessions
    with SessionLocal() as db:
        p = Project(name="NoDate", telescope="T1")
        db.add(p)
        db.execute(Setting.__table__.delete().where(Setting.key == "migr_sessions_v1"))
        db.commit()
        migrate_project_to_sessions(db)              # must not violate NOT NULL on session.date
        db.refresh(p)
        assert len(p.sessions) == 1 and p.sessions[0].date is not None and p.sessions[0].telescope == "T1"
        pid = p.id
    client.post(f"/projects/{pid}/delete")


def test_file_info_dialog_escapes_all_values(client, project, tmp_path):
    upload(client, write_fits(tmp_path / "x.fits", data=star_frame(seed=150)), "light", project_id=project)
    drain()
    [ff] = files_of(project)
    client.post(f"/files/{ff.id}/edit", data={"description": "<span><img src=x onerror=alert(1)></span>"})
    t = client.get(f"/projects/{project}").text
    assert "/^<span/" not in t                       # no "trusted if it looks like HTML" shortcut any more
    assert "<img src=x" not in t                     # stored description never rendered raw


def test_filters_are_combined_on_the_same_night(client):
    r = client.post("/projects/new", data={"name": "AndTest"}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    client.post(f"/projects/{pid}/sessions", data={"date": "01-01-2025", "telescope": "TelA", "camera": "CamX"})
    client.post(f"/projects/{pid}/sessions", data={"date": "01-06-2025", "telescope": "TelB", "camera": "CamY"})
    q = "/?q=AndTest"
    assert "<h3>AndTest</h3>" in client.get(q + "&telescope=TelA&camera=CamX").text          # same night
    assert "<h3>AndTest</h3>" not in client.get(q + "&telescope=TelA&camera=CamY").text      # different nights
    assert "<h3>AndTest</h3>" in client.get(q + "&telescope=TelB&date_from=01-05-2025").text
    assert "<h3>AndTest</h3>" not in client.get(q + "&telescope=TelA&date_from=01-05-2025").text
    client.post(f"/projects/{pid}/delete")


def test_session_zip_metadata_only_for_that_session(client, project, tmp_path):
    client.post(f"/projects/{project}/sessions", data={"date": "01-04-2026"})
    client.post(f"/projects/{project}/sessions", data={"date": "02-04-2026"})
    s1, s2 = _sessions(project)
    upload(client, write_fits(tmp_path / "a.fits", data=star_frame(seed=160), EXPTIME=60.0), "light",
           project_id=project, session_id=s1.id)
    upload(client, write_fits(tmp_path / "b.fits", data=star_frame(seed=161), EXPTIME=30.0), "light",
           project_id=project, session_id=s2.id)
    drain()
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/projects/{project}/zip?session={s2.id}").content))
    meta = json.loads(z.read("metadata.json"))
    assert [f["original_name"] for f in meta["files"]] == ["b.fits"]
    assert [s["id"] for s in meta["sessions"]] == [s2.id] and meta["scope"]["session_id"] == s2.id
    info = z.read("INFO.txt").decode()
    assert "30s (1 lights)" in info and "01-04-2026" not in info and "session 02-04-2026 only" in info
    full = json.loads(zipfile.ZipFile(io.BytesIO(client.get(f"/projects/{project}/zip").content)).read("metadata.json"))
    assert len(full["files"]) == 2 and full["scope"] == "project"


def test_concurrent_duplicate_uploads_store_one_copy(client, project, tmp_path):
    import threading
    sid = default_session(project)
    data = write_fits(tmp_path / "c.fits", data=star_frame(seed=170)).read_bytes()
    uids = []
    for i in range(2):
        uid = client.post("/api/uploads", json={"project_id": project, "filename": f"c{i}.fits", "size": len(data),
                                                "frame_type": "light", "session_id": sid, "skip_dups": True}).json()["id"]
        client.put(f"/api/uploads/{uid}?offset=0", content=data)
        uids.append(uid)
    res = []
    ts = [threading.Thread(target=lambda u=u: res.append(client.post(f"/api/uploads/{u}/finish").json())) for u in uids]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(bool(r.get("duplicate")) for r in res) == [False, True]
    assert len(files_of(project)) == 1


def test_calibration_dialog_rendered_once(client, project):
    for d in ("01-05-2026", "02-05-2026", "03-05-2026"):
        client.post(f"/projects/{project}/sessions", data={"date": d})
    client.post("/calibration/new", data={"frame_type": "dark", "name": "D1"})
    t = client.get(f"/projects/{project}").text
    assert t.count('<dialog id="calib-dlg"') == 1 and "var CALIB_LIB" in t and t.count("openCalib(") >= 3
