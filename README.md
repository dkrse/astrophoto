# Astrophoto

**A self-hosted archive and analysis platform for astrophotography.**

Astrophoto is the place where an imager's whole body of work lives: every raw frame (lights, darks, bias, flats,
dark flats), every final stack, every imaging night with its equipment and sky, and a shared calibration library.
It does not stop at storing files – it **measures what is in them**. Frames are plate-solved locally, their stars
are matched against star catalogues, the optics are inspected across the field, and the observing conditions of
every night are derived from the data itself. Over months and years this turns an archive into a record of how
your equipment and your sky actually perform.

It is built for real archives: thousands of frames per project, whole folder trees imported at once, analyses
running in the background, duplicate detection and file-by-file integrity checks. It runs in Docker on your own
server and works **completely offline** – no cloud service, no account, no data leaves the machine.

> **Originals are never modified.** View settings (stretch, levels, Bayer pattern, north up …), thumbnails,
> renders and measurements are stored separately from the files.

**Contents:** [Who it is for](#who-it-is-for) · [Highlights](#highlights) · [Screenshots](#screenshots) ·
[Features](#features) · [How it works](#how-it-works) · [Requirements](#requirements) ·
[Installation](#installation) · [Usage](#usage) · [Data and backups](#data-and-backups) ·
[Development](#development) · [Tests](#tests) · [Data sources and licences](#data-sources-and-licences) ·
[License](#license)

---

## Who it is for

- **Deep-sky imagers** with a growing collection of nights, targets and calibration frames who want one
  organised, searchable place for all of it – instead of folder trees spread over disks.
- **Anyone tuning a setup** – backfocus, tilt, collimation, guiding – who wants measured answers across many
  nights rather than impressions from a single frame.
- **Observers who care about their site**: seeing, transparency and sky brightness per night, comparable over
  time and between setups.

## Highlights

| | |
|---|---|
| **Organised by object and night** | A project is one object; a session is one night with one acquisition set. Frames are sorted into nights and sets automatically from their headers. |
| **Local plate solving** | ASTAP solves frames on the server: real focal length and image scale, field, rotation, pointing check against the project's object. |
| **Objects in the field** | Deep-sky objects (OpenNGC + Sharpless, Barnard, LBN, LDN, vdB …) and named / HD stars with spectral type, colour, variability and distance, drawn over the image. |
| **Photometry** | Limiting magnitude, zero point and a photometric quality check – scatter, linearity, saturation onset, field uniformity – against Gaia. |
| **Optics inspector** | Star size and shape in every part of the frame: tilt, field curvature, coma / corrector spacing, guiding errors. |
| **Observing conditions** | Seeing, transparency and SQM-like sky brightness per night, measured from the frames. |
| **Planning and checks** | Mosaic coverage per panel, calibration check of every night, optics recommendations from your own history. |
| **Built for large archives** | Background processing, bulk actions, streamed ZIP export in stacking-ready layout, duplicate and integrity checks. |
| **Private and offline** | Self-hosted, single user, no internet needed – not even to rebuild the image. |

## Screenshots

**Projects** – every object as a card with its nights, integration and frame counts; archive summary and filters
by night and equipment.

![Projects overview](screenshots/01.png)

**Project** – one object with all its nights. Per night the equipment, image scale and field, moon and the
**measured** values: plate-solved focal length, seeing, transparency and limiting magnitude.

![Project with its sessions](screenshots/02.png)

**Objects in the field** – after plate solving: deep-sky objects with their true size and orientation, named and
HD stars, RA / Dec grid and north / east. Here the Horsehead and Flame nebulae (IC 434, B 33, NGC 2024).

![Objects overlay on IC 434](screenshots/03.png)

**Object list** – catalogue designations and cross-identifications (IC 432 = vdB 51 = LBN 946 …), type, magnitude,
size, distance and the brightness of each star measured in the frame; filter by deep-sky / stars / magnitude.

![Objects in the field](screenshots/04.png)

**Photometric quality** – every matched star compared with Gaia: zero point and its uncertainty, scatter,
linearity, saturation onset (red line) and field uniformity, with plain-language verdicts.

![Photometric quality check](screenshots/05.png)

**Optics inspector** – corners, edges and centre side by side with FWHM, elongation and its direction; findings
for tilt, curvature, corrector spacing and guiding.

![Optics inspector](screenshots/06.png)

**Mosaic** – a panel grid around the object; each panel's coverage is computed from the exact fields of the
solved frames, with integration per panel and J2000 centres for framing.

![Mosaic coverage](screenshots/07.png)

**Sky map** – all projects on a whole-sky map, fields drawn exactly from their plate solves, with Messier / NGC
objects, the ecliptic and the galactic equator.

![Sky map](screenshots/08.png)

**Statistics** – integration per month and year, top objects, filters, object types and equipment.

![Statistics](screenshots/09.png)

**Optics & conditions over time** – per telescope · camera and night: seeing, transparency, sky brightness, FWHM,
limiting magnitude, tilt and elongation, followed by recommendations drawn from the whole history.

![Optics and conditions over time](screenshots/10.png)

---

## Features

### Archive
- **Projects** (one object: name, object, type, tags, description, notes, cover) with duplicate-name detection
  (`NGC 123` = `ngc123` = `NGC  123`).
- **Sessions** – one night with one acquisition set (exposure, gain / ISO, filter, sensor temperature). Frames with
  another set go to a sibling session of the same night automatically; equipment, conditions and linked
  calibration are copied. Nights run noon to noon in the site's time zone; FITS `DATE-OBS` (UTC) is converted,
  `DATE-LOC` / EXIF are taken as local.
- **Equipment lists** with technical parameters (aperture, focal length, pixel size, sensor, cooling); renames
  propagate everywhere, measured focal lengths can be stored with one click.
- **Calibration library** shared by all projects – darks, bias, flats, dark flats linked to the nights they
  calibrate.
- Moon illumination per night (Meeus, ~0.5 %), integration per session, filter and project.

### Import and files
- Resumable chunked upload (drag & drop of files and folders, pause / retry, pre-check for files already stored).
- **Import from the server**: a folder into one session, or a whole archive tree into many projects and nights
  at once – project from FITS `OBJECT` or the folder names, night from `DATE-OBS`, EXIF or the path; existing
  projects and nights are recognised and the result reviewed before anything is copied.
- Frame type detection (FITS `IMAGETYP`, file and folder names), SHA-256 duplicate detection.
- Frame statistics for every light (FWHM, stars, background, noise) with automatic *suspect* marks.
- Bulk actions: type, move, session, reject / restore, reprocess, delete, plate solve, optics.

### Viewer
- FITS (mono, OSC / Bayer, RGB cubes, 8–32 bit), camera RAW (CR2, CR3, NEF, ARW, DNG, RAF, ORF, RW2 …),
  JPEG / PNG / TIFF including 16-bit.
- Zoom and pan, Bayer pattern, flip, channels, linked / unlinked auto stretch, histogram levels with live
  preview, FITS header and EXIF panel, keyboard shortcuts.
- Objects overlay, **north up**, plate solve and optics inspector directly in the viewer; annotated JPEG export.

### Plate solving and photometry
- **ASTAP** on the server: image scale, focal length (resized exports, drizzle and binning recognised), field,
  centre, rotation, and a pointing check against the project's object.
- **Objects in the field** from OpenNGC (NGC, IC, Messier) and the Stellarium catalogue (Sharpless, Barnard,
  LBN, LDN, van den Bergh, RCW, Cederblad, Collinder, Trumpler, Arp, HCG, PK) with cross-identifications and
  distances; **stars** from HYG with HD number, proper / Bayer–Flamsteed name, spectral type, B–V colour,
  variability and distance.
- **Measured brightness** of the listed stars (zero-point calibrated, Gaia BP) with the difference to the
  catalogue and a saturation mark.
- **Limiting magnitude** and **zero point** from the stars matched to the local Gaia star database.
- **Photometric quality**: zero-point uncertainty, robust scatter, linearity (slope), saturation onset, field
  uniformity (gradient with its direction in the displayed image, vignetting), residual plot and verdicts.

### Optics and conditions
- **Optics inspector**: star FWHM and elongation across the field with a 3×3 tile mosaic and masks; findings for
  sensor tilt (and its side), field curvature, radial / tangential edge elongation (coma, corrector spacing) and
  uniform elongation (guiding, wind, flexure). Star sizes use adaptive Gaussian-windowed moments – unbiased by
  noise and by the wings of real stars.
- **Seeing** (centre FWHM without the telescope's diffraction), **transparency** (zero point per second against
  the best night of the same telescope · camera · gain) and **sky brightness** in mag/arcsec² like an SQM meter
  (camera offset from RAW black level, FITS keyword or a linked bias / dark).
- Measured manually per night or automatically for new nights; the session's Seeing / SQM can be filled in.

### Planning and checks
- **Mosaic** – plan a grid of camera fields (columns × rows, overlap, rotation, centre; defaults from the latest
  solve). Coverage per panel from the exact TAN fields of the solved frames, integration per panel, missing
  panels and their J2000 centres to copy into NINA, ASIAIR or Ekos.
- **Calibration check** of every night – linked sets against its lights: camera, gain, offset, binning,
  telescope, exposure (scalable with a bias?), sensor temperature, flats per filter, flat and dark age, flats
  without bias / dark flats. Unknown values are never reported as mismatches.
- **Optics recommendations** per setup from the night-by-night analyses: a tilt towards the same side of the
  sensor (mechanical – tilt plate) versus a changing side (sag, flexure); the best corrector spacing fitted from
  the spacings logged in the sessions; lasting changes between nights; guiding hints.

### Overview, statistics and sky map
- Full-text search over projects, nights, equipment, file names, notes and FITS headers with highlighted hits;
  filters, sorting, card and table views; gallery of final images with slideshow.
- Statistics: integration per month and year, objects, filters, object types, equipment, disk usage; trends of
  optics and conditions per setup with recommendations.
- Whole-sky map (Hammer–Aitoff, J2000) with every project's solved fields, catalogue objects, ecliptic, galactic
  equator and constellation of each field.

### Large archives and data safety
- Background analysis of a project or the whole archive (all frames or a few per night, only missing results),
  with progress, stop and an activity indicator.
- Duplicate report and **archive health** check – quick (existence, size, unknown files, errors) or full
  (SHA-256 of every file) – with a stored report.
- Streamed ZIP downloads in the layout PixInsight WBPP / Siril expect (`01_<date>/<type>/…`), metadata export of
  a project or the whole archive (FITS headers, EXIF, solves and optics results included).
- Bounded caches, deferred FITS headers, decimated previews and SQL aggregation keep pages fast with thousands of
  frames; long disk operations never block the server.

### Security and administration
- Single admin account (a banner stays until the default password is changed), login throttling per client, CSRF protection on every
  change, path-traversal and symlink checks on server imports, request size limits.
- Settings in tabs: appearance, page sizes, downloads, site time zone, gallery and every background job can be
  switched on or off individually. Light and dark theme.

---

## How it works

```
 browser ──► FastAPI (server-rendered pages + JSON API)
               │
               ├─ PostgreSQL ─ projects, sessions, frames, equipment, calibration, results (JSON)
               ├─ worker thread ─ imports, hashes, previews, frame statistics, analysis queue
               │      └─ ASTAP (plate solving)  ·  numpy / scipy / astropy (stars, optics, photometry)
               └─ DATA_DIR ─ original files, thumbnails, render cache, ASTAP star database
```

- **Server-rendered UI** (Jinja2 templates, plain JavaScript, no build step) on a **FastAPI** backend with
  **SQLAlchemy** on **PostgreSQL**.
- A single **background worker** processes the queue: copies of server imports, SHA-256, previews and EXIF,
  frame statistics, plate solves and optics analyses – lowest priority after interactive work.
- **Disk-friendly when idle:** the hourly housekeeping (expired uploads, render cache) runs only after
  activity (a web request or processed files); an idle server never touches `DATA_DIR`, so a data disk
  with a spindown timeout (e.g. `hdparm -S`) can stay asleep.
- Measurements are stored as compact JSON next to each frame and aggregated per night on demand; nothing is
  derived from the originals in place.
- All catalogues are local files loaded once per process; the application never opens a network connection.

## Requirements

- Docker with Docker Compose
- A PostgreSQL server with an existing, empty database (tables are created and migrated on start)
- RAM: about 2 GB free is enough for normal use; full-resolution viewing of large colour (Bayer) FITS needs about
  0.55 GB per 24 MP frame (at most two such renders run at the same time)
- Optional, for plate solving and photometry: about 1 GB of disk for an ASTAP star database
- For the first build only (once, with internet): `curl` and `python3` on the host to fill `vendor/`

## Installation

1. **Configuration** – create `.env` from the template:

   ```bash
   cp .env.example .env && chmod 600 .env
   ```

   | Variable | Meaning |
   |---|---|
   | `DATABASE_URL` | PostgreSQL connection, e.g. `postgresql://USER:PASSWORD@HOST:5432/astrophoto` |
   | `SECRET_KEY` | session signing key – `python3 -c 'import secrets; print(secrets.token_hex(32))'` |
   | `DATA_DIR` | data folder inside the container (default `/data`) |
   | `TEST_DATABASE_URL` | optional, database for `./run-tests.sh` (default `astrophoto_test` on the same server) |
   | `FORWARDED_ALLOW_IPS` | optional, behind a reverse proxy: proxy IP(s) whose `X-Forwarded-For` is trusted |

2. **Third-party build inputs** – fill `vendor/` once (ASTAP, catalogues, fonts, Python wheels; see
   [Data sources and licences](#data-sources-and-licences)):

   ```bash
   scripts/fetch-vendor.sh
   ```

   From then on the image builds without internet: the Dockerfile only copies from `vendor/`, and the tests
   install their packages from `vendor/wheels`. To install on a machine without internet, copy the project folder
   including `vendor/` and transfer the base image (`docker save python:3.12-slim` → `docker load`).

3. **Volumes** – adjust the paths in `docker-compose.yml`:

   | Host path | Container | Purpose |
   |---|---|---|
   | `/mnt/data/media/astrophoto` | `/data` | stored files, thumbnails, render cache, star database |
   | `/mnt/data/media/astrophoto_import` | `/import` (read-only) | folders offered by *Import from server* |

4. **Start**:

   ```bash
   docker compose up -d --build
   ```

5. **Plate solving** (optional) – put an ASTAP star database into `DATA_DIR/astap`. It is data in the volume, not
   part of the image; without internet, copy the unpacked files there.

   ```bash
   mkdir -p /mnt/data/media/astrophoto/astap && cd /mnt/data/media/astrophoto/astap
   curl -L -o d50.zip https://sourceforge.net/projects/astap-program/files/star_databases/d50_star_database.zip/download
   unzip d50.zip && rm d50.zip
   ```

   D50 (~0.9 GB) covers fields from about 0.2°; D20 / D05 are smaller for wide fields, D80 suits very small
   fields. Paths can be overridden with `ASTAP_DB`, `ASTAP_BIN`, `OPENNGC_DIR`, `HYG_FILE`, `STELLARIUM_DIR`.

6. Open <http://localhost:8010>, sign in with `admin` / `admin` and **change the password** (account menu).

7. *Settings → Files & downloads → Time zone of the observing site* (e.g. `Europe/Bratislava`) so that capture
   times and nights are local.

## Usage

**Typical workflow**

1. **+ New project** for an object, **+ Add session** for a night, then **⬆ Add files** – or bring an existing
   archive with **📦 Import archive** (account menu). Upload a whole night at once; frames with another set end
   up in their own session of that night.
2. Previews, metadata and frame statistics are produced in the background. With *measure new nights
   automatically* enabled, a few frames per night are plate-solved and analysed and the night's seeing and sky
   brightness are filled in.
3. In the **viewer**: 🎯 Solve, ✨ Objects, ⬆ North up, 🔬 Optics.
4. Link **calibration** sets to the nights (*🔗 Calibration library…*), review the calibration check, reject
   suspect frames, and download the **ZIP** for stacking.
5. Plan the next nights with the **Mosaic** panel; follow equipment and sky on **Stats → Optics & conditions**;
   run the **archive health** check now and then.

**Viewer shortcuts:** `F` fit · `1` 100 % · `+` / `-` zoom · `←` / `→` previous / next frame · `I` info panel ·
`Shift+F` fullscreen · `O` objects overlay · `N` north up · `Esc` close levels.

## Data and backups

```
DATA_DIR/
  archive-data/                        ← back up this folder together with the database
    projects/<id>/<type>/<file>        original files of a project
    projects/<id>/.thumbs/…            thumbnails
    calibration/<id>/<type>/<file>     calibration library
  .cache/<file id>/…                   rendered views (disposable; max 30 days / 5 GB)
  .uploads/                            unfinished chunked uploads (removed after 48 h)
  astap/                               ASTAP star database
```

A complete backup consists of `DATA_DIR/archive-data/` and a dump of the PostgreSQL database. Everything else is
regenerated automatically.

## Development

```
app/
  main.py            entry point: migrations, seeding, worker start
  core.py            app object, settings, auth, helpers, storage, pagination
  models.py          SQLAlchemy models and idempotent migrations
  sessets.py         one acquisition set per session: matching, sibling sessions
  worker.py          background processing (imports, hashes, previews, statistics, analysis queue)
  preview.py         FITS / RAW / image loading, debayer, stretch, histogram, metadata
  framestats.py      frame statistics (windowed-moment FWHM, stars, background, noise)
  optics.py          optics inspector (FWHM / elongation across the field, tilt, curvature, coma)
  platesolve.py      ASTAP plate solving, WCS, catalogue, objects in the field, pointing check
  skycat.py          star (HYG) and extra deep-sky (Stellarium) catalogues
  photometry.py      ASTAP star database reader, limiting magnitude, zero point, photometric quality
  conditions.py      seeing, transparency, sky brightness, camera offset
  measured.py        measured values per night, transparency reference
  analysis.py        solve / optics runs shared by the endpoints and the queue
  mosaic.py          mosaic plan, panel geometry (gnomonic), coverage and integration per panel
  calibcheck.py      calibration check of a night
  opticsadvice.py    optics recommendations per setup
  annotate.py        annotated JPEG export
  skymap.py          whole-sky map data
  integrity.py       duplicates, archive integrity check
  routes_*.py        HTTP routes
  templates/, static/
tests/               pytest suite
scripts/             fetch-vendor.sh
vendor/              third-party build inputs (not committed)
```

## Tests

```bash
./run-tests.sh            # whole suite, offline (test packages from vendor/wheels)
./run-tests.sh -k mosaic  # any pytest arguments
```

The suite (≈ 600 tests) runs inside the application image against a separate database **`astrophoto_test`**,
dropped and recreated on every run, and temporary data folders – production data is never touched, and the run is
refused if the test database name equals the production one. Plate solving is simulated where the ASTAP star
database would be needed.

It covers the web application end to end (authentication, CSRF, pages, uploads and imports, bulk actions, ZIP and
metadata export, settings, migrations), odd and broken input files, the astronomy (WCS and overlays, catalogues,
photometry, unbiased star measurement, optics, conditions, mosaic geometry, calibration rules, optics
recommendations), performance guards (query counts, a 3000-frame project, caches) and regression tests for every
bug found in the code reviews.

Coverage report (optional; add `pytest-cov` to `requirements-dev.txt` and re-run `scripts/fetch-vendor.sh` to have
it offline):

```bash
docker run --rm --env-file .env -v "$PWD/tests:/srv/tests:ro" -v "$PWD/pytest.ini:/srv/pytest.ini:ro" \
  -v "$PWD/requirements-dev.txt:/srv/requirements-dev.txt:ro" -v "$PWD/vendor/wheels:/srv/wheels:ro" \
  -w /srv astrophoto-astrophoto \
  sh -c 'pip install -q --no-index --find-links wheels -r requirements-dev.txt pytest-cov && python -m pytest -q --cov=app --cov-report=term'
```

## Data sources and licences

Third-party programs and data are fetched once into `vendor/` by `scripts/fetch-vendor.sh` and built into the
image; they are not part of this repository.

| Component | Used for | Licence |
|---|---|---|
| [ASTAP](https://www.hnsky.org/astap.htm) command-line solver | plate solving | MPL-2.0 / LGPL |
| ASTAP star database (D05–D80, Gaia DR3) | solving, photometry | see ASTAP |
| [OpenNGC](https://github.com/mattiaverga/OpenNGC) v20260501 | NGC / IC / Messier objects | CC-BY-SA 4.0 |
| [HYG database](https://codeberg.org/astronexus/hyg) v4.1 | named and HD stars | CC-BY-SA 4.0 |
| [Stellarium](https://github.com/Stellarium/stellarium) DSO catalogue v3.21 | Sharpless, Barnard, LBN, LDN … , distances | GPL-2.0+ |
| DejaVu fonts | annotated JPEG | Bitstream Vera / public domain |
| Python packages (`requirements.txt`) | application | per package |

## License

[MIT](LICENSE) © 2026 krse
