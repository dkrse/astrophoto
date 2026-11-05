"""Uploads, background processing, bulk operations, downloads, import, calibration library."""
import hashlib
import io
import json
import zipfile

from sqlalchemy import select

from app.models import SessionLocal, FrameFile, ImportJob
from conftest import drain, write_fits, upload, star_frame, DATA_DIR, IMPORT_DIR


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
                                            "frame_type": "light"}).json()["id"]
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
                                            "size": len(data), "frame_type": "other"}).json()["id"]
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
    assert "light/blur.fits" not in names and len([n for n in names if n.startswith("light/")]) == 5
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
    assert f0["stats"]["fwhm_px"] and any(n.startswith("headers/light/") for n in ez.namelist())


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
    upload(client, write_fits(tmp_path / "md.fits", data=star_frame(seed=40)), "dark", calib_set_id=cid)
    upload(client, write_fits(tmp_path / "l.fits", data=star_frame(seed=41)), "light", project_id=project)
    drain()
    client.post(f"/projects/{project}/calibration", data={"set_id": str(cid)})
    names = zipfile.ZipFile(io.BytesIO(client.get(f"/projects/{project}/zip").content)).namelist()
    assert "dark/md.fits" in names and "light/l.fits" in names
    assert client.post(f"/calibration/{cid}/delete", follow_redirects=False).status_code == 303
    assert client.get(f"/calibration/{cid}").status_code == 404


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
