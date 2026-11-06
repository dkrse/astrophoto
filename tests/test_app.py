"""HTTP-level tests: auth, pages, projects, lookup lists, settings."""
from conftest import csrf_of
from app.routes_main import _FAILS


PAGES = ["/", "/gallery", "/stats", "/calibration", "/calibration/new", "/settings", "/manage", "/projects/new",
         "/password"]


def test_login_required(anon):
    for url in ["/", "/projects/new", "/files/1/download", "/settings"]:
        r = anon.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", url
    assert anon.get("/api/catalog/camera").status_code == 401
    assert anon.get("/static/style.css").status_code == 200


def test_wrong_password_and_lockout(anon):
    _FAILS.clear()
    for _ in range(5):
        anon.post("/login", data={"username": "admin", "password": "nope"})
    r = anon.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    assert "Too many failed attempts" in r.text            # locked even with the right password
    # a spoofed X-Forwarded-For must not bypass the lock
    r = anon.post("/login", data={"username": "admin", "password": "admin"},
                  headers={"X-Forwarded-For": "1.2.3.4"}, follow_redirects=True)
    assert "Too many failed attempts" in r.text
    _FAILS.clear()


def test_default_password_banner_and_change(anon):
    _FAILS.clear()
    anon.post("/login", data={"username": "admin", "password": "admin"})
    anon.headers["X-CSRF-Token"] = csrf_of(anon)
    assert "default password" in anon.get("/").text
    r = anon.post("/password", data={"old": "admin", "new": "secret1", "new2": "secret1"}, follow_redirects=True)
    assert "Password changed" in r.text and "default password" not in r.text
    anon.post("/password", data={"old": "secret1", "new": "admin", "new2": "admin"})   # restore for other tests


def test_all_pages_render(client, project):
    for url in PAGES + [f"/projects/{project}", f"/projects/{project}/edit", f"/import?project={project}"]:
        assert client.get(url).status_code == 200, url


def test_invalid_input_does_not_crash(client):
    for url in ["/?page=-5", "/?size_from=abc", "/?date_from=99-99-9999", "/?sort=xxx", "/?q=%25_%27",
                "/gallery?page=9999", "/calibration?type=evil"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/projects/999999").status_code == 404
    assert client.get("/manage?kind=evil").status_code == 404


def test_project_crud_dates_and_escaping(client):
    new = client.get("/projects/new").text
    assert 'name="capture_date"' not in new and 'name="telescope"' not in new   # these live in sessions now
    r = client.post("/projects/new", data={"name": "<b>x</b> Mliečna", "capture_date": "24-12-2025"},
                    follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/projects/{pid}").text
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page
    assert "Created" in page
    client.post(f"/projects/{pid}/edit", data={"name": "Renamed", "capture_date": "01-01-2026"})
    assert "Renamed" in client.get(f"/projects/{pid}").text
    client.post(f"/projects/{pid}/delete")
    assert client.get(f"/projects/{pid}").status_code == 404


def test_filters_and_pagination(client):
    ids = []
    for i, d in enumerate(["01-01-2024", "01-06-2024", "01-01-2025"]):
        r = client.post("/projects/new", data={"name": f"Filter{i}"}, follow_redirects=False)
        pid = int(r.headers["location"].rsplit("/", 1)[1])
        ids.append(pid)
        client.post(f"/projects/{pid}/sessions", data={"date": d, "telescope": "ScopeX" if i else ""})
    client.post(f"/projects/{ids[2]}/sessions", data={"date": "01-07-2024"})        # 2nd night of Filter2
    t = client.get("/?q=Filter&date_from=01-03-2024&date_to=31-12-2024").text
    assert "Filter1" in t and "Filter2" in t and "Filter0" not in t
    t = client.get("/?q=Filter&telescope=__none__").text
    assert "Filter0" in t and "Filter1" not in t
    t = client.get("/?q=Filter&telescope=ScopeX").text
    assert "Filter1" in t and "Filter0" not in t
    t = client.get("/?q=ScopeX").text                                          # search covers sessions
    assert "Filter1" in t and "Filter0" not in t
    t = client.get("/?q=Filter&sort=date_desc").text
    assert t.index("Filter2") < t.index("Filter1") < t.index("Filter0")
    assert "01-07-2024 – 01-01-2025" in t and "2 nights" in t
    client.post("/settings", data={"page_size": "1", "accent_color": "#3b5bdb"})
    t = client.get("/?q=Filter&sort=name_asc").text
    assert "1–1 of 3" in t and "Filter0" in t and "page=2" in t
    client.post("/settings", data={"reset": "1"})
    for pid in ids:
        client.post(f"/projects/{pid}/delete")


def test_catalog_rename_propagates(client, project):
    r = client.post("/api/catalog/camera", json={"name": "Cam A"})
    item = next(i for i in r.json()["items"] if i["name"] == "Cam A")
    assert client.post("/api/catalog/camera", json={"name": "Cam A"}).status_code == 400   # duplicate
    client.post(f"/projects/{project}/edit", data={"name": "Test project", "camera": "Cam A"})
    r = client.put(f"/api/catalog/items/{item['id']}", json={"name": "Cam B"})
    assert r.json()["renamed"] == {"from": "Cam A", "to": "Cam B"}
    assert "Cam B" in client.get(f"/projects/{project}").text
    client.delete(f"/api/catalog/items/{item['id']}")
    assert "Cam B" not in client.get(f"/projects/{project}").text


def test_settings_roundtrip(client):
    client.post("/settings", data={"app_title": "My Sky", "page_size": "7", "accent_color": "#e03131",
                                   "slideshow_seconds": "9", "zip_compression": "fast"})
    t = client.get("/settings").text
    assert "My Sky" in t and 'value="7"' in t and "#e03131" in t
    client.post("/settings", data={"reset": "1"})
    assert "My Sky" not in client.get("/").text


def test_sessions(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "20-10-2025", "seeing": "2.1", "notes": "windy"})
    t = client.get(f"/projects/{project}").text
    assert "20-10-2025" in t and "windy" in t and "1%" in t           # new moon -> ~1 %


def test_double_delete_redirects(client):
    r = client.post("/projects/new", data={"name": "Gone"}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    client.post(f"/projects/{pid}/delete")
    r = client.post(f"/projects/{pid}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.get("/projects/999999", headers={"accept": "text/html"})
    assert r.status_code == 404 and "<html" in r.text


def test_user_menu(client):
    t = client.get("/").text
    assert 'class="usermenu"' in t and 'action="/logout"' in t and 'href="/password"' in t


def test_equipment_specs_and_optics(client, project):
    client.post("/manage/telescope/add", data={"name": "Scope 80", "spec_aperture_mm": "80",
                                               "spec_focal_mm": "480", "spec_telescope_type": "Refractor"})
    client.post("/manage/camera/add", data={"name": "Cam 533", "spec_width_px": "3008", "spec_height_px": "3008",
                                            "spec_pixel_um": "3,76", "spec_sensor": "bogus"})
    j = client.get("/api/catalog/camera").json()
    cam = next(i for i in j["items"] if i["name"] == "Cam 533")
    assert cam["specs"] == {"width_px": 3008, "height_px": 3008, "pixel_um": 3.76}
    assert "11.3×11.3 mm" in cam["summary"]
    assert "f/6.0" in client.get("/manage?kind=telescope").text
    client.put(f"/api/catalog/items/{cam['id']}", json={"name": "Cam 533", "notes": "x"})   # specs kept
    assert next(i for i in client.get("/api/catalog/camera").json()["items"] if i["id"] == cam["id"])["specs"]
    client.post(f"/projects/{project}/sessions", data={"date": "01-11-2025", "telescope": "Scope 80", "camera": "Cam 533"})
    t = client.get(f"/projects/{project}").text
    assert "1.62″/px" in t and "81′×81′" in t


def test_project_has_no_software_and_upload_needs_session(client, project):
    assert 'name="software"' not in client.get("/projects/new").text
    r = client.post("/api/uploads", json={"project_id": project, "filename": "a.fits", "size": 10,
                                          "frame_type": "light"})
    assert r.status_code == 400 and "session" in r.json()["error"]
    client.post(f"/projects/{project}/sessions", data={"date": "02-02-2026", "software": "Siril"})
    t = client.get(f"/projects/{project}").text
    assert "Siril" in t and "⬆ Add files" in t and 'class="up-session"' in t
    assert "Filter2" not in client.get("/?software=Siril").text and "Test project" in client.get("/?software=Siril").text


def test_add_files_button_markup(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "03-03-2026"})
    t = client.get(f"/projects/{project}").text
    assert 'onclick="uploadTo(this.dataset.sid, this.dataset.label)"' in t and 'data-label="03-03-2026"' in t


def test_session_table_compact_with_info_dialog(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "04-04-2026", "seeing": "2.4", "sqm": "21.1",
                                                       "notes": "clear sky"})
    t = client.get(f"/projects/{project}").text
    head = t.split('id="sessions"')[1].split("</thead>")[0]
    assert "<th>Seeing</th>" not in head and "<th>Night</th>" in head and "<th>Files</th>" in head
    assert 'id="sinfo-' in t and "21.1" in t and "clear sky" in t


def test_search_shows_where_it_was_found(client, project):
    client.post(f"/projects/{project}/edit", data={"name": "Test project", "notes": "guiding was bad near the Zorkon edge"})
    client.post(f"/projects/{project}/sessions", data={"date": "07-07-2026", "notes": "Zorkon again <b>x</b>"})
    t = client.get("/?q=zorkon").text
    assert "Found in 2 places" in t and "<b>Notes</b>" in t and "Session 07-07-2026 · notes" in t
    assert "<mark>Zorkon</mark>" in t and "&lt;b&gt;x&lt;/b&gt;" in t          # highlighted, still escaped


def test_panel_defaults_from_settings(client, project):
    client.post(f"/projects/{project}/sessions", data={"date": "08-08-2026"})
    assert 'id="sessions" >' in client.get(f"/projects/{project}").text.replace('id="sessions" open', "X")  # default closed
    client.post("/settings", data={"accent_color": "#3b5bdb", "show_stats": "on", "open_sessions": "on", "open_file_groups": "on"})
    t = client.get(f"/projects/{project}").text
    assert 'id="sessions" open' in t and '<details class="sessblock" id="s' in t and 'class="sessblock" id="s' in t
    assert 'id="ovstats" hidden' in client.get("/").text          # summary unchecked -> hidden
    client.post("/settings", data={"reset": "1"})
    assert 'id="ovstats" hidden' not in client.get("/").text
