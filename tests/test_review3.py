"""Second review round: EXIF cache, streamed full export, viewer navigation by SQL, decimated FITS previews,
camera temperature only for cooled sensors."""
import io
import json
import zipfile

import numpy as np
from PIL import Image
from sqlalchemy import select

from app import sessets, preview
from app.models import SessionLocal, FrameFile, ImagingSession
from conftest import drain, write_fits, upload, star_frame, default_session


def files_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(FrameFile).where(FrameFile.project_id == pid).order_by(FrameFile.id)).all()


def sessions_of(pid):
    with SessionLocal() as db:
        return db.scalars(select(ImagingSession).where(ImagingSession.project_id == pid)
                          .order_by(ImagingSession.id)).all()


def jpeg_with_exif(path):
    im = Image.new("RGB", (64, 48), (120, 80, 40))
    ex = Image.Exif()
    ex[0x010F] = "TestMaker"                       # Make
    ex[0x0110] = "TestCam"                         # Model
    im.save(path, exif=ex.tobytes())
    return path


# ---------- EXIF cache ----------
def test_exif_is_stored_at_processing_and_used_by_exports(client, project, tmp_path, monkeypatch):
    upload(client, jpeg_with_exif(tmp_path / "e.jpg"), project_id=project)
    drain()
    [f] = files_of(project)
    with SessionLocal() as db:
        stored = json.loads(db.get(FrameFile, f.id).exif)
    assert stored.get("Image Make") == "TestMaker"
    monkeypatch.setattr(preview, "read_exif", lambda src: (_ for _ in ()).throw(AssertionError("read again")))
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/projects/{project}/export").content))
    md = json.loads(z.read("metadata.json"))
    assert md["files"][0]["exif"]["Image Model"] == "TestCam"


def test_exif_of_older_files_is_read_once_then_cached(client, project, tmp_path):
    upload(client, jpeg_with_exif(tmp_path / "o.jpg"), project_id=project)
    drain()
    [f] = files_of(project)
    with SessionLocal() as db:                      # as stored by an older version: nothing cached
        db.get(FrameFile, f.id).exif = None
        db.commit()
    client.get(f"/projects/{project}/zip?types=light")
    with SessionLocal() as db:
        assert "TestMaker" in db.get(FrameFile, f.id).exif


# ---------- streamed export of everything ----------
def test_export_all_is_streamed_and_complete(client, project, tmp_path):
    sid = default_session(project)
    upload(client, write_fits(tmp_path / "h1.fits", data=star_frame(seed=71), OBJECT="M42", EXPTIME=30.0), project_id=project, session_id=sid)
    upload(client, write_fits(tmp_path / "h2.fits", data=star_frame(seed=72), OBJECT="M42", EXPTIME=30.0), project_id=project, session_id=sid)
    drain()
    client.post("/api/files/bulk", json={"ids": [files_of(project)[1].id], "action": "session", "value": ""})
    r = client.get("/export/projects")
    assert r.status_code == 200 and "content-length" not in r.headers
    z = zipfile.ZipFile(io.BytesIO(r.content))
    allp = json.loads(z.read("all_projects.json"))
    assert allp["format"] == "astrophoto-metadata-all" and allp["project_count"] == len(allp["projects"])
    mine = next(p for p in allp["projects"] if p["project"]["id"] == project)
    assert {x["original_name"] for x in mine["files"]} == {"h1.fits", "h2.fits"}
    assert isinstance(allp["calibration_library"], list)
    names = z.namelist()
    folder = next(n.split("/")[1] for n in names if n.startswith(f"projects/{project:04d}_"))
    assert f"projects/{folder}/metadata.json" in names and f"projects/{folder}/INFO.txt" in names
    h1 = [n for n in names if n.startswith(f"projects/{folder}/headers/01_") and n.endswith("h1.fits.txt")]
    h2 = [n for n in names if n.startswith(f"projects/{folder}/headers/unassigned/") and n.endswith("h2.fits.txt")]
    assert h1 and h2 and "OBJECT" in z.read(h1[0]).decode()
    assert json.loads(z.read(f"projects/{folder}/metadata.json"))["project"]["id"] == project


def test_export_all_with_several_calibration_sets(client, tmp_path):
    ids = []
    for i in range(3):
        r = client.post("/calibration/new", data={"name": f"Exp set {i}", "frame_type": "dark"},
                        follow_redirects=False)
        ids.append(int(r.headers["location"].rstrip("/").rsplit("/", 1)[1]))
    upload(client, write_fits(tmp_path / "d.fits", data=star_frame(seed=91), IMAGETYP="Dark Frame"), "dark",
           calib_set_id=ids[1])
    drain()
    z = zipfile.ZipFile(io.BytesIO(client.get("/export/projects").content))
    lib = {c["id"]: c for c in json.loads(z.read("all_projects.json"))["calibration_library"]}
    assert set(ids) <= set(lib) and [f["original_name"] for f in lib[ids[1]]["files"]] == ["d.fits"]
    for cid in ids:
        client.post(f"/calibration/{cid}/delete")


# ---------- viewer ----------
def test_viewer_prev_next_and_position_by_sql(client, project, tmp_path):
    sid = default_session(project)
    for n in ("b.fits", "a.fits", "c.fits"):
        upload(client, write_fits(tmp_path / n, data=star_frame(seed=ord(n[0]))), project_id=project, session_id=sid)
    drain()
    fs = {f.original_name: f for f in files_of(project)}
    page = client.get(f"/files/{fs['b.fits'].id}/view").text
    assert f"/files/{fs['a.fits'].id}/view" in page and f"/files/{fs['c.fits'].id}/view" in page
    assert "2 / 3" in page or "2/3" in page
    first = client.get(f"/files/{fs['a.fits'].id}/view").text
    assert "1 / 3" in first or "1/3" in first


def test_view_settings_for_all_of_a_type(client, project, tmp_path):
    for i in range(2):
        upload(client, write_fits(tmp_path / f"v{i}.fits", data=star_frame(seed=i)), project_id=project)
    drain()
    f0, f1 = files_of(project)
    r = client.post(f"/files/{f0.id}/opts", data={"stretch": "linear", "all": "1"}, follow_redirects=False)
    assert r.status_code in (200, 303)
    assert all(f.view_opts and "linear" in f.view_opts for f in files_of(project))


# ---------- decimated FITS previews ----------
def test_decimate_keeps_the_bayer_pattern():
    raw = np.zeros((40, 60), np.uint16)
    raw[0::2, 0::2] = 1000                          # R of an RGGB mosaic
    d = preview._decimate(raw, 3, cfa=True)
    assert d.shape == (14, 20)
    assert (d[0::2, 0::2] == 1000).all() and (d[1::2, :] == 0).all() and (d[:, 1::2] == 0).all()
    m = preview._decimate(raw, 4, cfa=False)
    assert m.shape == (10, 15)
    assert preview._decimate(raw, 1, cfa=True) is raw


def test_preview_of_large_cfa_fits_is_decimated_before_float(tmp_path, monkeypatch):
    raw = np.full((2000, 3000), 100, np.uint16)
    raw[0::2, 0::2] = 4000                          # strong red
    p = write_fits(tmp_path / "cfa.fits", data=raw, BAYERPAT="RGGB")
    seen = []
    real = np.array
    def spy(a, *k, **kw):
        out = real(a, *k, **kw)
        if getattr(out, "dtype", None) == np.float32 and out.ndim == 2:
            seen.append(out.shape)
        return out
    monkeypatch.setattr(preview.np, "array", spy)
    data, _ = preview.load_fits(p, dict(preview.DEFAULT_OPTS) if hasattr(preview, "DEFAULT_OPTS")
                                else {"bayer": "auto", "flip": "none"}, 200)
    monkeypatch.undo()
    assert seen and max(seen[0]) <= 1000            # float copy made of the decimated data only
    assert data.ndim == 3 and max(data.shape[:2]) <= 500     # same preview size as before (≤ 2.5 × max_side)
    r, g, b = (float(data[..., i].mean()) for i in range(3))
    assert r > 3 * g and r > 3 * b                  # colours intact (pattern not shifted)


# ---------- camera temperature ----------
def test_unknown_camera_uses_temperature_only_with_a_set_point(client, project, tmp_path):
    client.post(f"/projects/{project}/sessions", data={"date": "12-10-2025", "camera": "Mystery cam"})
    [s0] = sessions_of(project)
    for i, t in enumerate((24.0, 18.0)):            # drifting sensor, no cooler
        upload(client, write_fits(tmp_path / f"u{i}.fits", data=star_frame(seed=i), IMAGETYP="Light Frame",
                                  EXPTIME=10.0, CCD_TEMP=t, DATE_OBS="2025-10-12T22:00:00"),
               project_id=project, session_id=s0.id)
    drain()
    assert len(sessions_of(project)) == 1
    for i, t in enumerate((-10.0, -20.0)):          # cooled: SET-TEMP in the header
        upload(client, write_fits(tmp_path / f"k{i}.fits", data=star_frame(seed=10 + i), IMAGETYP="Light Frame",
                                  EXPTIME=10.0, CCD_TEMP=t, **{"SET-TEMP": t}, DATE_OBS="2025-10-12T22:00:00"),
               project_id=project, session_id=s0.id)
    drain()
    temps = sorted((s.cam_temp is not None, s.cam_temp or 0) for s in sessions_of(project))
    assert len(temps) == 3 and temps[0] == (False, 0)


def test_ignore_temp_rules():
    f = FrameFile(header="SET-TEMP= -10.0 / cooler set point\n")
    assert sessets.ignore_temp(None, f) is False and sessets.ignore_temp(False, f) is True
    assert sessets.ignore_temp(True, FrameFile(header="")) is False
    assert sessets.ignore_temp(None, FrameFile(header="CCD-TEMP= 20\n")) is True


def test_project_export_loads_headers_in_one_query(client, project, tmp_path):
    from sqlalchemy import event
    from app.models import engine
    for i in range(12):
        upload(client, write_fits(tmp_path / f"q{i}.fits", data=star_frame(seed=200 + i), OBJECT="M1"),
               project_id=project)
    drain()
    n = [0]
    count = lambda *a: n.__setitem__(0, n[0] + 1)
    event.listen(engine, "before_cursor_execute", count)
    try:
        r = client.get(f"/projects/{project}/export")
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert r.status_code == 200
    md = json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read("metadata.json"))
    assert len(md["files"]) == 12 and all(f["fits_header"] for f in md["files"])
    assert n[0] < 30                                # not 2 queries per file (header + EXIF)


# ---------- no flashing between pages ----------
def test_pages_block_first_paint_until_scripts_ran(client, project):
    for url in (f"/projects/{project}", "/", "/settings", "/calibration", "/gallery"):
        html = client.get(url).text
        assert '<link rel="expect" href="#page-ready" blocking="render">' in html
        marker = html.index('<div id="page-ready" hidden></div>')
        assert marker > html.rindex("<script")         # after every script of the page
        assert '"prefetch"' in html and "/logout" in html


def test_session_zip_dialog_replaces_type_form(client, project, tmp_path):
    sid = default_session(project)
    upload(client, write_fits(tmp_path / "z.fits", data=star_frame(seed=301)), project_id=project, session_id=sid)
    drain()
    html = client.get(f"/projects/{project}").text
    assert "ZIP selected types" not in html and 'id="zip-dlg"' in html
    assert f'data-sid="{sid}"' in html and "openZip(this)" in html and "&#34;light&#34;: 1" in html
    assert 'class="cat-dialog wide sess-dlg"' in html
    r = client.get(f"/projects/{project}/zip?session={sid}&types=light")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
