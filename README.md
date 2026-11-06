# Astrophoto

Self-hosted web application for managing astrophotography data: raw frames (light, dark, bias, flat,
dark flat), final stacked images, imaging sessions, a shared calibration library and all metadata —
with a FITS/RAW-aware image viewer.

Built with FastAPI, PostgreSQL and plain HTML/JS. Runs in Docker.

> Original files are **never modified**. Everything you change in the viewer (stretch, levels, Bayer pattern,
> brightness …) is stored as view settings; thumbnails and renders are separate derived files.

## Screenshots

**Overview** – summary, filters on nights / equipment, project cards with nights and integration

![Overview](screenshots/01.png)

**Project** – one object with its sessions (nights); files grouped by session, details of each file and
session in an **i** dialog

![Project detail](screenshots/02.png)

**Viewer – FITS with manual levels** – histogram with black / midtone / white markers and live preview,
FITS header and frame quality in the side panel

![Viewer with histogram levels](screenshots/03.png)

**Viewer** – fit / 1:1 / zoom, Bayer, flip, channels, stretch and brightness controls (info panel hidden)

![Viewer – M101 (light)](screenshots/04.png)

**Statistics** – integration per month / year, objects, filters, equipment, disk usage

![Statistics](screenshots/05.png)

## Features

**Projects & sessions**
- A **project is one object** (name, object, object type, tags, description, notes, cover image)
- **Sessions (nights)** carry everything that changes between nights: date, location, telescope, camera,
  mount, software, seeing, SQM, temperature, humidity and computed moon illumination
- Every file – lights and calibration frames – belongs to a session (deleting a session deletes its files);
  calibration library sets are linked
  per session
- Automatic session creation from `DATE-OBS` / EXIF dates (frames after midnight belong to the previous
  evening); new sessions inherit the equipment of the latest one
- Integration time per session and filter (rejected frames excluded), total per project
- Image scale (″/px) and field of view per session from the equipment parameters
- Session and file details in compact **i** dialogs; created / modified timestamps everywhere
- Final stacks shown as a compact gallery on the project page; cover image picker

**Equipment**
- Managed lists with technical parameters: telescope aperture / focal length / type (f-ratio derived),
  camera resolution / pixel size / sensor / cooling (sensor size derived), mount type / payload / GoTo

**Files**
- Files are added into a specific session (⬆ Add files / 📁 Import on the session)
- Resumable chunked upload (8 MB chunks, retry, pause) with drag & drop of files and whole folders
- Upload pre-check: files already present (same name + size) are reported before anything is sent
- Import of folders already on the server (copied in the background, sources untouched)
- Frame type auto-detection: FITS `IMAGETYP`, then file and folder names (`lights/`, `darks/`, `biases/` …)
- Duplicate detection by SHA-256 content hash (atomic across concurrent uploads and imports)
- Compact file tables (name, size, added, captured); full details, editing and single-file actions in a dialog
- Bulk operations: change type, move to another project or the calibration library, assign session,
  edit metadata, reject / restore, reprocess, delete
- Frame quality statistics for lights (FWHM, star count, background, noise) with automatic
  “suspect” flagging (compared within the same night and filter) and one-click rejection
- Unreadable / corrupt files are marked “⚠ unreadable” with the reason

**Calibration library**
- Reusable sets of darks, bias, flats and dark flats (camera, telescope, temperature, gain, offset,
  exposure, filter, binning, date)
- Link sets to any number of sessions; linked frames are included in that session’s ZIP folder

**Viewer**
- FITS (mono, OSC/Bayer, RGB cubes incl. 4-D data), camera RAW (CR2, CR3, NEF, ARW, DNG, RAF, ORF, RW2 …), JPEG/PNG/TIFF incl. 16-bit
- Fit / 1:1 / zoom (wheel, drag, keyboard), fullscreen
- Bayer pattern, flip, channels (RGB, BGR, R, G, B, luminance), auto stretch (linked / unlinked), linear
- Histogram with draggable black / midtone / white levels, live preview, log scale, reset
- Brightness; save settings per file or per frame type
- FITS header and EXIF side panel

**Downloads & export**
- Streamed ZIP downloads (start immediately, no temporary files); rejected frames excluded by default;
  `INFO.txt` / `metadata.json` describe exactly the downloaded scope (whole project or one session)
  - **ZIP all sessions** – one folder per session `01_<date>/<type>/…`, `02_<date>/…` (unique even for equal
    dates), the layout multi-session stacking (PixInsight WBPP, Siril) expects
  - **ZIP session**, **ZIP selected types**, single type, calibration sets
- `INFO.txt` summary and `metadata.json` in every project ZIP
- Metadata export: everything about a project incl. all FITS headers and EXIF as JSON (headers stored per
  session folder, so equal file names from different nights are all kept)

**Overview**
- Search across project fields, session notes and equipment, file names, descriptions and FITS headers;
  each result shows where it matched (“Found in N places”) with highlighted snippets (`%` and `_` are
  matched literally)
- Summary strip (projects, files, total size, integration, frames per type) – can be hidden / shown
- Project cards with range and number of nights
- Filters: night date range, size range, telescope, camera, mount, software, location, object type – night
  filters are combined with AND on the same session (e.g. telescope A + camera B = a night with both)
- Managed lookup lists (add / rename / delete in a modal dialog; renames propagate everywhere; names are
  unique regardless of case, max. 100 characters; items still used by a session, project or calibration
  set cannot be deleted)
- Pagination, sorting (last / first night, name, recently added, size), grid / list view
- Gallery of final images with the capture date (DATE-OBS / EXIF, else session date), pagination, sorting
  (photo date, project, recently added), tile sizes and a fullscreen slideshow
- Statistics: integration per month / year, top objects, filters, equipment, disk usage

**Admin**
- Single admin account (`admin` / `admin` on first start; a banner warns until the password is changed)
- Login throttling (5 failed attempts → 10 min lock per IP)
- CSRF protection: every change (forms, API calls, uploads) carries a per-login token; log out is a POST button
- Warning at startup when `SECRET_KEY` is missing or weak
- Settings
  - General / appearance: title, accent color, default theme, project view and card size
  - Projects overview: page size, default sort, summary on / off
  - Downloads: ZIP compression, duplicate handling
  - Gallery: images per page, tile size, default sort, slideshow interval
  - Panels – expanded by default: overview summary and filters, project Sessions panel, session blocks in
    Files, file type groups, statistics table (each can still be toggled on the page)
- Light / dark mode, account menu in the header, date pickers (also inside dialogs)

## Requirements

- Docker with Docker Compose
- A PostgreSQL server with an existing database (tables are created automatically on start)
- RAM: about 2 GB free is enough for normal use; full-resolution viewing of large colour (Bayer) FITS needs
  roughly 0.55 GB per 24 MP frame (max. 2 such renders run at the same time)

## Installation

1. Create `.env` from the template and fill in the values:

   ```bash
   cp .env.example .env && chmod 600 .env
   ```

   | Variable | Meaning |
   |---|---|
   | `DATABASE_URL` | PostgreSQL connection, e.g. `postgresql://USER:PASSWORD@HOST:5432/astrophoto` |
   | `SECRET_KEY` | session signing key – `python3 -c 'import secrets; print(secrets.token_hex(32))'` |
   | `DATA_DIR` | data folder inside the container (default `/data`) |
   | `TEST_DATABASE_URL` | optional, database for `./run-tests.sh` (default `astrophoto_test` on the same server) |

2. Adjust the volumes in `docker-compose.yml`:

   | Host path | Container | Purpose |
   |---|---|---|
   | `/mnt/data/media/astrophoto` | `/data` | all stored files, thumbnails, render cache |
   | `/mnt/data/media/astrophoto_import` | `/import` (read-only) | folders available for “Import from server” |

3. Start:

   ```bash
   docker compose up -d --build
   ```

4. Open <http://localhost:8010>, sign in with `admin` / `admin` and **change the password** (account menu → Change password).

## Usage

- **New project** (= one object) → **+ Add session** (night: date, location, equipment, conditions) →
  **⬆ Add files** on that session: drop files or folders, check the detected types, **Start upload**.
  Previews, metadata and statistics are generated in the background.
- **📁 Import** on a session: put data into the import folder on the server, browse to it, scan, correct the
  types and start the import.
- **⚡ Auto from dates** creates one session per night from the frames’ observation dates and assigns them.
- **Calibration**: create a set in the *Calibration* library, add frames, then *🔗 Calibration library…*
  on a session in the project's Files panel.
- **Downloads**: *⬇ ZIP all sessions*, *⬇ ZIP session*, *⬇ ZIP selected types*, per-type ZIPs,
  *📄 Export metadata*.
- **Details**: the **i** button on a session or a file opens all its information.
- **Viewer** shortcuts: `F` fit, `1` 100 %, `+`/`-` zoom, `←`/`→` previous / next file, `I` info panel,
  `Shift+F` fullscreen, `Esc` close levels.

## Storage layout

```
DATA_DIR/
  archive-data/                        ← back up this folder (+ the database)
    projects/<id>/<type>/<file>        original files of a project
    projects/<id>/.thumbs/<type>__<file>.jpg
    calibration/<id>/<type>/<file>     calibration library
  .cache/<file id>/…                   rendered views (disposable; max 30 days / 5 GB)
  .uploads/                            unfinished chunked uploads (removed after 48 h)
```

## Development

```
app/
  main.py          entry point, startup (migrations, seeding, worker)
  core.py          app object, settings, auth, helpers, storage
  models.py        SQLAlchemy models + idempotent migrations
  worker.py        background processing (imports, hashes, previews, metadata, statistics)
  preview.py       loading / stretching / histogram / metadata for FITS, RAW, images
  framestats.py    FWHM, star count, background, noise
  routes_*.py      HTTP routes
  templates/       Jinja2 templates
  static/          CSS, JS (uploader, bulk actions, catalog dialog, slideshow), flatpickr
tests/             pytest suite
```

### Tests

```bash
./run-tests.sh            # all tests
./run-tests.sh -k upload  # any pytest arguments
```

About 100 tests cover authentication and CSRF, pages, projects / sessions / filters, managed lists,
uploads and imports (incl. duplicates, races, invalid input), file processing of normal and odd files
(NaN, empty, 1×1, cubes, corrupt, 8/16-bit, CMYK, Bayer), viewer endpoints, bulk actions, ZIP / export,
calibration library, statistics, settings, migrations and background cleanup.

Coverage report (optional):

```bash
docker run --rm --env-file .env -v "$PWD/tests:/srv/tests:ro" -v "$PWD/pytest.ini:/srv/pytest.ini:ro" \
  -v "$PWD/requirements-dev.txt:/srv/requirements-dev.txt:ro" -w /srv astrophoto-astrophoto \
  sh -c 'pip install -q -r requirements-dev.txt pytest-cov && python -m pytest -q --cov=app --cov-report=term'
```

The tests run inside the application image against a separate database **`astrophoto_test`**
(dropped and recreated on every run, on the same server as `DATABASE_URL`) and temporary data folders.
Production data is never touched; the suite refuses to run if the test database name equals the production one.

## License

[MIT](LICENSE) © 2026 krse
