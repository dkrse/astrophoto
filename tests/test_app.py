"""HTTP-level tests: auth, pages, projects, lookup lists, settings."""
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
    assert 'name="capture_date" value="' in new                       # today pre-filled
    r = client.post("/projects/new", data={"name": "<b>x</b> Mliečna", "capture_date": "24-12-2025"},
                    follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/projects/{pid}").text
    assert "24-12-2025" in page and "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page
    assert "Created" in page
    client.post(f"/projects/{pid}/edit", data={"name": "Renamed", "capture_date": "01-01-2026"})
    assert "Renamed" in client.get(f"/projects/{pid}").text
    client.post(f"/projects/{pid}/delete")
    assert client.get(f"/projects/{pid}").status_code == 404


def test_filters_and_pagination(client):
    ids = []
    for i, d in enumerate(["01-01-2024", "01-06-2024", "01-01-2025"]):
        r = client.post("/projects/new", data={"name": f"Filter{i}", "capture_date": d, "telescope": "ScopeX" if i else ""},
                        follow_redirects=False)
        ids.append(int(r.headers["location"].rsplit("/", 1)[1]))
    t = client.get("/?q=Filter&date_from=01-03-2024&date_to=31-12-2024").text
    assert "Filter1" in t and "Filter0" not in t and "Filter2" not in t
    t = client.get("/?q=Filter&telescope=__none__").text
    assert "Filter0" in t and "Filter1" not in t
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
