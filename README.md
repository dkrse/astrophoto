# Astrophoto

**Self-hosted archive and analysis platform for astrophotography.** Astrophoto keeps all your raw frames (light,
dark, bias, flat, dark flat), final stacks, imaging nights, equipment and a shared calibration library in one place –
and measures what is actually in the frames: it plate-solves them locally with ASTAP (real focal length, field,
objects in the field, pointing), inspects the optics (tilt, field curvature, coma, guiding), and derives the
**observing conditions** of every night (seeing, transparency, sky brightness like an SQM meter). Results are
collected per night and shown as trends per telescope · camera over time.

It is made for growing archives: whole folder trees are imported at once, analyses run in the background on the
server, duplicates are reported and the archive's integrity can be verified file by file.

Built with FastAPI, PostgreSQL and plain HTML/JS; runs in Docker, fully offline.

> Original files are **never modified**. Everything you change in the viewer (stretch, levels, Bayer pattern,
> brightness, north up …) is stored as view settings; thumbnails, renders and measurements are separate data.

**Contents:** [Screenshots](#screenshots) · [Features](#features) · [Requirements](#requirements) ·
[Installation](#installation) · [Usage](#usage) · [Storage layout](#storage-layout) · [Development](#development) ·
[Tests](#tests) · [License](#license)

## Screenshots

**Overview** – summary, filters on nights / equipment, project cards with nights and integration; the activity
indicator next to the name and **⬇ Metadata** (export of all projects) in the header

![Overview](screenshots/01.png)

**Project** – one object with its sessions (nights). The **Measured** column shows the plate-solved focal length
(vs. the equipment), seeing 🌬, sky brightness 🌃 and limiting magnitude per night; sessions and files are paged

![Project detail](screenshots/02.png)

**Session details** – measured values (focal length, scale, rotation, limiting magnitude, FWHM, tilt) and the
**observing conditions** (seeing, transparency, sky brightness) with *🌡 Measure conditions* / *Use for Seeing / SQM*

![Session dialog with conditions](screenshots/03.png)

**Viewer – FITS with manual levels** – histogram with black / midtone / white markers and live preview,
FITS header and frame quality in the side panel

![Viewer with histogram levels](screenshots/04.png)

**✨ Objects + ⬆ North up** – objects in the field (OpenNGC), RA / Dec grid and N / E after plate solving; the
frame turned north up / east left like a sky map

![Objects overlay, north up – M51](screenshots/05.png)

**🎯 Plate solve** – focal length vs. equipment, scale, field, centre, rotation, limiting magnitude, sky
brightness, zero point, pointing check, objects in the field, annotated JPEG export

![Plate solve dialog](screenshots/06.png)

**🔬 Optics inspector** – findings (tilt, curvature, coma / spacing, guiding) and a 3×3 mosaic of corners, edges
and centre with FWHM and elongation

![Optics inspector](screenshots/07.png)

**📦 Import archive** – a folder tree becomes projects × nights: project from FITS `OBJECT` or the folders, night
from `DATE-OBS` / EXIF / path; existing projects and nights are recognised before anything is copied

![Import archive](screenshots/08.png)

**Statistics – Overview** – integration per month / year, objects, filters, equipment, disk usage

![Statistics](screenshots/09.png)

**Statistics – Optics & conditions** – per telescope · camera and night: seeing, transparency, sky brightness,
FWHM, limiting magnitude, tilt and elongation

![Optics and conditions over time](screenshots/10.png)

**Statistics – Archive health** – duplicate warning and the quick / full (SHA-256) integrity check

![Archive health](screenshots/11.png)

**Settings** – tabs; *Background processing* switches every automatic job on / off

![Settings – background processing](screenshots/12.png)

**Gallery** of final images with slideshow, and the **calibration library**

![Gallery](screenshots/13.png)
![Calibration library](screenshots/14.png)

## Features

### Projects, sessions and equipment
- A **project is one object** (name, object, object type, tags, description, notes, cover image); duplicate
  warning when creating / renaming (`NGC 123`, `ngc123`, `NGC  123` are the same)
- **Sessions (nights)** carry everything that changes between nights: date, location, telescope, camera, mount,
  software, seeing, SQM, temperature, humidity and computed moon illumination; new nights inherit the equipment
  of the latest one; ⚡ automatic sessions from `DATE-OBS` / EXIF (frames after midnight belong to the evening)
- Integration time per session and filter (rejected frames excluded), total per project
- Image scale and field of view from the equipment parameters; **measured** values per night next to them
- **Equipment lists** with technical parameters (telescope aperture / focal length / type, camera resolution /
  pixel size / sensor / cooling, mount); renames propagate, used items cannot be deleted; measured focal lengths
  can be stored with one click
- Details of sessions and files in compact **i** dialogs; clicking a session row jumps to its files
- **Pagination** of sessions (table + file blocks together), of every file group and of the calibration library;
  links to a session on another page open the right page

### Files, upload and import
- Resumable chunked upload (8 MB chunks, retry, pause) with drag & drop of files and folders, pre-check for files
  already present
- **📁 Import** of a server folder into a session, **📦 Import archive** of a whole folder tree into many
  projects and sessions at once (reviewed per project × night before copying)
- Frame type detection (FITS `IMAGETYP`, file and folder names); duplicate detection by SHA-256 content hash
- Bulk operations: type, move, session, metadata, reject / restore, reprocess, delete, 🎯 Solve, 🔬 Optics
- Frame quality statistics for lights (FWHM, stars, background, noise) with automatic “suspect” flags
- Unreadable / corrupt files are marked with the reason

### Viewer
- FITS (mono, OSC / Bayer, RGB cubes), camera RAW (CR2, CR3, NEF, ARW, DNG, RAF, ORF, RW2 …), JPEG / PNG / TIFF
  incl. 16-bit
- Fit / 1:1 / zoom / pan, fullscreen, Bayer pattern, flip, channels, linked / unlinked auto stretch, histogram
  levels with live preview, brightness; settings saved per file or per frame type
- FITS header and EXIF side panel; back to the frame's session (🌙)
- **✨ Objects** overlay, **⬆ North up**, **🎯 Solve** and **🔬 Optics** right in the viewer

### Plate solving and sky (ASTAP, local)
- Real **image scale** and **focal length** (compared with the telescope; resized exports, drizzle and binning
  recognised), field of view, centre, rotation
- **Objects in the field** (OpenNGC: NGC, IC, Messier + addendum) – list, overlay with ellipses, RA / Dec grid and
  N / E; click an object to show only it
- **Pointing check** – where the project's object lies in the frame
- **Limiting magnitude** and photometric **zero point** from the stars matched to the local Gaia star database
- **⬇ Annotated JPEG** export (also north up); warning when a stored solve is outdated by an equipment change

### Optics inspector
- Star size and shape across the field, 3×3 mosaic, overlay on the image
- Findings: **sensor tilt** (and the worse side), **field curvature**, radial / tangential edge elongation
  (**coma, corrector spacing**), elongation everywhere (**guiding, wind, collimation**)
- For many frames: what repeats (= optics) and what changes (= conditions)

### Observing conditions
- **Seeing** – star FWHM in the centre without the telescope's diffraction (upper limit; warnings for
  undersampled or defocused stars)
- **Transparency** – zero point per second compared with the best night of the same telescope · camera · gain
- **Sky brightness** – mag/arcsec² like an SQM meter; camera offset from RAW black level, FITS keyword or a
  linked bias / dark
- Per night in the session dialog and the sessions table; **manual** (🌡 Measure conditions, Use for Seeing / SQM)
  or **automatic** for new nights, filling the session's Seeing / SQM without touching values you typed

### Statistics and trends
- Integration per month / year, top objects, filters, object types, equipment, disk usage
- **Optics & conditions over time** per telescope · camera: seeing, transparency, sky brightness, FWHM, limiting
  magnitude, tilt, elongation (charts with tooltips → click opens the night, plus a table)

### Large archives and archive health
- **🎯🔬 Analyse** a project or the whole archive in the background (all frames or 1 / 3 / 10 per night,
  only missing results), progress and stop in the processing bar
- **Activity indicator** in the header while anything runs in the background
- **Duplicate warning** (project page, Stats) – files with the same content stored more than once
- **Archive health**: quick check (existence, size, unknown files, processing errors) or full check (SHA-256 of
  every file), with a stored report
- Page speed guarded by tests (3000-frame project, query counts); lists use small cached thumbnails, FITS headers
  are loaded only when needed, memory caches are bounded

### Downloads and export
- Streamed ZIP downloads (start immediately, no temporary files): all sessions as `01_<date>/<type>/…` (the layout
  PixInsight WBPP / Siril expect), one session, selected types, calibration sets; rejected frames excluded by default
- `INFO.txt` and `metadata.json` in every project ZIP; **📄 Export metadata** of a project and **⬇ Metadata** of
  all projects + calibration library (incl. all FITS headers, EXIF, plate solves and optics results)

### Overview, search and gallery
- Search across projects, sessions, equipment, file names, descriptions and FITS headers with highlighted matches
- Filters on nights and equipment, sorting, grid / list view, summary strip
- Gallery of final images with capture date, sorting, tile sizes and a fullscreen slideshow

### Settings, security and administration
- Settings in tabs: **General & appearance** (title, accent colour, theme, view, page sizes of projects /
  sessions / files / calibration sets), **Downloads**, **Gallery**, **Background processing** (thumbnails, frame
  statistics, suspect marks, background analyses, integrity checks, automatic conditions measuring and filling –
  each can be switched off) and **Panels** (expanded by default)
- Single admin account (`admin` / `admin` on first start, banner until changed), login throttling, CSRF protection
  of every change, warning for a weak `SECRET_KEY`
- Light / dark mode, keyboard shortcuts in the viewer

## Requirements

- Docker with Docker Compose
- A PostgreSQL server with an existing database (tables are created automatically on start)
- RAM: about 2 GB free is enough for normal use; full-resolution viewing of large colour (Bayer) FITS needs
  roughly 0.55 GB per 24 MP frame (max. 2 such renders run at the same time)
- Optional for plate solving / sky analysis: about 1 GB of disk for an ASTAP star database (see step 4)
- Everything runs locally – no internet connection or online service is needed after the installation

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

4. Plate solving (optional): download an ASTAP star database into `DATA_DIR/astap` (host:
   `/mnt/data/media/astrophoto/astap`). The ASTAP CLI and the OpenNGC catalogue are built into the image.

   ```bash
   mkdir -p /mnt/data/media/astrophoto/astap && cd /mnt/data/media/astrophoto/astap
   curl -L -o d50.zip https://sourceforge.net/projects/astap-program/files/star_databases/d50_star_database.zip/download
   unzip d50.zip && rm d50.zip
   ```

   D50 (~0.9 GB) covers fields of view from about 0.2°; for wider fields D20 / D05 are smaller, for very
   small fields use D80. Paths can be changed with `ASTAP_DB` / `ASTAP_BIN` / `OPENNGC_DIR`.

5. Open <http://localhost:8010>, sign in with `admin` / `admin` and **change the password** (account menu → Change password).

## Usage

**Typical workflow**
1. **+ New project** (= one object) → **+ Add session** (night, location, equipment) → **⬆ Add files** on the
   session. Or bring a whole archive with **📦 Import archive** (account menu).
2. Previews, metadata and frame statistics are made in the background; with *measure new nights automatically* on,
   3 frames per night are plate-solved and analysed and the session's Seeing / SQM are filled in.
3. Open frames in the **viewer**: 🎯 Solve, ✨ Objects, ⬆ North up, 🔬 Optics.
4. Link **calibration** sets to the sessions (*🔗 Calibration library…*), reject suspect frames, then download the
   **ZIP** for stacking.
5. Follow your equipment and sky over time on **Stats → Optics & conditions**; check **Archive health** now and then.

**More**
- **📁 Import** on a session: browse the server's import folder, scan, correct the types, import.
- **⚡ Auto from dates** creates one session per night from the observation dates.
- **🎯🔬 Analyse** (project page, Stats → *Analyse archive…*) runs solves / optics in the background.
- **Session dialog → Conditions**: *🌡 Measure conditions*, *Use for Seeing / SQM*.
- Viewer shortcuts: `F` fit, `1` 100 %, `+` / `-` zoom, `←` / `→` previous / next file, `I` info panel,
  `Shift+F` fullscreen, `Esc` close levels, `O` objects overlay, `N` north up.

## Storage layout

```
DATA_DIR/
  archive-data/                        ← back up this folder (+ the database)
    projects/<id>/<type>/<file>        original files of a project
    projects/<id>/.thumbs/<type>__<file>.jpg
    calibration/<id>/<type>/<file>     calibration library
  .cache/<file id>/…                   rendered views (disposable; max 30 days / 5 GB)
  .uploads/                            unfinished chunked uploads (removed after 48 h)
  astap/                               ASTAP star database (plate solving, downloaded once)
```

## Development

```
app/
  main.py            entry point, startup (migrations, seeding, worker)
  core.py            app object, settings, auth, helpers, storage, pagination
  models.py          SQLAlchemy models + idempotent migrations
  worker.py          background processing (imports, hashes, previews, statistics, analysis queue)
  preview.py         loading / stretching / histogram / metadata for FITS, RAW, images
  framestats.py      FWHM, star count, background, noise
  platesolve.py      ASTAP plate solving, WCS, OpenNGC catalogue, overlays, pointing check
  photometry.py      ASTAP star database reader, limiting magnitude / zero point
  optics.py          optics inspector (star FWHM / elongation across the field, tilt, curvature, coma)
  conditions.py      seeing, transparency, sky brightness, camera offset
  measured.py        measured values per session (medians of solves / optics, transparency reference)
  analysis.py        solve / optics runs shared by endpoints and the queue, conditions measuring / filling
  annotate.py        annotated JPEG export (also north up)
  integrity.py       duplicate detection, archive integrity check
  routes_*.py        HTTP routes (main, projects, files, upload, calibration, archive import / health)
  templates/         Jinja2 templates
  static/            CSS, JS (uploader, bulk actions, catalogue dialog, slideshow), flatpickr
tests/               pytest suite
```

## Tests

```bash
./run-tests.sh            # all tests (218, ≈ 50 s, no warnings)
./run-tests.sh -k upload  # any pytest arguments
```

The tests run inside the application image against a separate database **`astrophoto_test`** (dropped and
recreated on every run, on the same server as `DATABASE_URL`) and temporary data folders. Production data is never
touched; the suite refuses to run if the test database name equals the production one. ASTAP's star database is not
needed – plate solving is simulated where required.

| File | Covers |
|---|---|
| `test_app.py`, `test_features.py`, `test_files.py`, `test_units.py` | authentication / CSRF, pages, projects, sessions, filters, lists, uploads / imports, odd files (NaN, empty, cubes, corrupt, 8/16-bit, CMYK, Bayer), viewer endpoints, bulk actions, ZIP / export, calibration, statistics, settings, migrations, cleanup |
| `test_sky.py` | catalogue, WCS overlays, pointing, resize detection, star database, limiting magnitude, optics on synthetic star fields, north up, annotated export, all-projects export, duplicates |
| `test_archive.py` | background analysis queue, archive import, duplicate warnings, integrity check, background switches, activity indicator, page speed with 3000 frames |
| `test_conditions.py` | seeing / transparency / sky brightness, offsets, session values, manual and automatic measuring / filling, tabs |
| `test_coverage.py` | full plate-solve flow with a simulated ASTAP, star database search across RA 0/360, upload edge cases, import / worker error paths |
| `test_review.py` | regression tests for the bugs found in the code reviews |
| `test_paging.py` | pagination of sessions, file groups and the calibration library, page-size settings |
| `test_perf.py` | small cached thumbnails, render slots never blocking the server, star-tile cache budget, settings cache, deferred FITS headers (query counts), large uploads |

Coverage report (optional, ≈ 88 %):

```bash
docker run --rm --env-file .env -v "$PWD/tests:/srv/tests:ro" -v "$PWD/pytest.ini:/srv/pytest.ini:ro" \
  -v "$PWD/requirements-dev.txt:/srv/requirements-dev.txt:ro" -w /srv astrophoto-astrophoto \
  sh -c 'pip install -q -r requirements-dev.txt pytest-cov && python -m pytest -q --cov=app --cov-report=term'
```

## License

[MIT](LICENSE) © 2026 krse
