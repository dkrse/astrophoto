# Astrophoto

Self-hosted web application for managing astrophotography data: raw frames (light, dark, bias, flat,
dark flat), final stacked images, imaging sessions, a shared calibration library and all metadata —
with a FITS/RAW-aware image viewer.

Built with FastAPI, PostgreSQL and plain HTML/JS. Runs in Docker.

> Original files are **never modified**. Everything you change in the viewer (stretch, levels, Bayer pattern,
> brightness …) is stored as view settings; thumbnails and renders are separate derived files.

## Screenshots

**Gallery** – all final (stacked) images, with a fullscreen slideshow

![Gallery](screenshots/05.png)

**Project** – collapsible integration time, sessions and calibration panels; files with frame quality
statistics (FWHM, stars, background) and inline-editable metadata

![Project detail](screenshots/02.png)

**Viewer – FITS with manual levels** – histogram with black / midtone / white markers and live preview,
channel selection, FITS header and frame quality in the side panel

![Viewer with histogram levels](screenshots/03.png)

**Viewer – images** – fit / 1:1 / zoom, channels, stretch and brightness controls (info panel hidden)

![Viewer – M31 (stack)](screenshots/01.png)

![Viewer – M42 (light)](screenshots/04.png)

## Features

**Projects & sessions**
- Projects (target, object type, location, telescope, camera, mount, filters, software, tags, description, notes)
- Imaging sessions (nights) with seeing, SQM, temperature, humidity and computed moon illumination;
  automatic creation from `DATE-OBS` / EXIF dates (frames after midnight belong to the previous evening)
- Integration time per filter, excluding rejected frames
- Created / modified timestamps for projects, files, sessions and calibration sets
- Project cover image picker

**Files**
- Resumable chunked upload (8 MB chunks, retry, pause) with drag & drop of files and whole folders
- Import of folders already on the server (copied in the background, sources untouched)
- Frame type auto-detection: FITS `IMAGETYP`, then file and folder names (`lights/`, `darks/`, `biases/` …)
- Duplicate detection by SHA-256 content hash
- Bulk operations: change type, move to another project or the calibration library, assign session,
  edit metadata, reject / restore, reprocess, delete
- Frame quality statistics for lights (FWHM, star count, background, noise) with automatic
  “suspect” flagging and one-click rejection

**Calibration library**
- Reusable sets of darks, bias, flats and dark flats (camera, telescope, temperature, gain, offset,
  exposure, filter, binning, date)
- Link sets to any number of projects; linked frames are included in “ZIP for stacking”

**Viewer**
- FITS (mono, OSC/Bayer, RGB), camera RAW (CR2, CR3, NEF, ARW, DNG, RAF, ORF, RW2 …), JPEG/PNG/TIFF incl. 16-bit
- Fit / 1:1 / zoom (wheel, drag, keyboard), fullscreen
- Bayer pattern, flip, channels (RGB, BGR, R, G, B, luminance), auto stretch (linked / unlinked), linear
- Histogram with draggable black / midtone / white levels, live preview, log scale, reset
- Brightness; save settings per file or per frame type
- FITS header and EXIF side panel

**Downloads & export**
- Streamed ZIP downloads (start immediately, no temporary files): frames for stacking, single types,
  calibration sets; rejected frames excluded by default
- `INFO.txt` summary and `metadata.json` in every project ZIP
- Metadata export: everything about a project incl. all FITS headers and EXIF as JSON

**Overview**
- Search across project fields, file names, descriptions and FITS headers
- Filters: date range, size range, telescope, camera, mount, software, object type, location
- Managed lookup lists (add / rename / delete in a modal dialog; renames propagate everywhere)
- Pagination, sorting, grid / list view
- Gallery of final images with fullscreen slideshow
- Statistics: integration per month / year, top objects, filters, equipment, disk usage

**Admin**
- Single admin account (`admin` / `admin` on first start; a banner warns until the password is changed)
- Login throttling (5 failed attempts → 10 min lock per IP)
- Settings: title, accent color, default theme, view, card size, page size, sort, ZIP compression,
  slideshow interval, duplicate handling
- Light / dark mode

## Requirements

- Docker with Docker Compose
- A PostgreSQL server with an existing database (tables are created automatically on start)

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

4. Open <http://localhost:8010>, sign in with `admin` / `admin` and **change the password** (🔑).

## Usage

- **New project** → fill in the details (capture date defaults to today) → **⬆ Add files**: drop files or folders,
  check the detected types, **Start upload**. Previews, metadata and statistics are generated in the background.
- **📁 Import from server**: put data into the import folder on the server, browse to it, scan, correct the
  types and start the import.
- **Sessions**: *⚡ Auto from dates* creates one session per night from the frames’ timestamps.
- **Calibration**: create a set in the *Calibration* library, add frames, then *🔗 Link sets…* in a project.
- **Downloads**: *⬇ ZIP for stacking* (lights + darks + bias + flats incl. linked library sets),
  per-type ZIPs, *⬇ Download stack*, *📄 Export metadata*.
- **Viewer** shortcuts: `F` fit, `1` 100 %, `+`/`-` zoom, `←`/`→` previous / next file, `I` info panel,
  `Shift+F` fullscreen, `Esc` close levels.

## Storage layout

```
DATA_DIR/
  projects/<id>/<type>/<file>          original files of a project
  projects/<id>/.thumbs/<type>__<file>.jpg
  calibration/<id>/<type>/<file>       calibration library
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

The tests run inside the application image against a separate database **`astrophoto_test`**
(dropped and recreated on every run, on the same server as `DATABASE_URL`) and temporary data folders.
Production data is never touched; the suite refuses to run if the test database name equals the production one.

## License

[MIT](LICENSE) © 2026 krse
