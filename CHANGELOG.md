# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.4.5] – 2026-11-08

### Added
- **Sky map** (menu): every project on a whole-sky map – exact field outlines from the plate solves, catalogue
  position for unsolved projects, Messier / NGC / IC (OpenNGC, magnitude limit), ecliptic and galactic equator,
  constellation of each field (IAU boundaries, astropy); zoom with automatic re-centring, search, tooltips, project
  list. Tests in `test_skymap.py`.
- 🔬 Optics inspector: choose which masks are drawn in the tiles – values, stretch direction, measured stars,
  star shapes (enlarged 3×) and a crosshair (bright, outlined marks), plus **stretch** and **contrast** sliders for
  the sky background with reset; the choice is remembered in the browser.

### Changed
- **⬇ Download metadata** (export of all projects) moved from the header to the 👤 menu.
- 🔬 Optics inspector: the 3×3 tiles fill the dialog width (sharper 320 px tiles).
- README: screenshots of the sky map and the optics inspector with masks.

### Fixed
- Sky map: a solve with missing / non-finite WCS values (or a CDELT-only solve) no longer breaks the whole map – such
  fields are skipped; the map data refresh right away when a file is re-solved or a project renamed / retargeted
  (the cache stamp covers solve sizes and project names / targets); panning only moves the view (redraw on release).
- Plate solve: `NaN` / `inf` in FITS keywords are treated as unknown, and stored solves never contain `NaN` /
  `Infinity` (they broke the SQL JSON casts of the sky map and the transparency reference).
- Optics inspector: tile star marks are the brightest stars of the crop (up to 300), the caption says so correctly.
- Viewer pan / zoom: the image transform and the overlays (objects, optics) are updated at most once per animation
  frame (wheel and pointer events used to redraw the overlays several times per frame), the full-resolution image is
  decoded before it replaces the preview (no hitch, no one-frame size jump), mouse-wheel / trackpad zoom is
  proportional to the scroll amount, window resize is debounced, and from 200 % the real pixels are shown sharp
  instead of blurred. Measured: steady 60 fps while zooming and panning a 3008 × 3008 frame with the objects overlay.
- ⬇ Download metadata from the 👤 menu: progress is shown on the menu button and a second click while exporting
  is ignored.

### Tests
- 233 tests: viewer pan / zoom frame coalescing; `test_skymap.py` (TAN outlines vs. astropy, catalogue, reference lines, constellations, broken solves,
  cache refresh), NaN-free solve storage, optics tile stars.

## [1.4.1] – 2026-11-08

From an archive to an analysis platform: local plate solving, optics inspection, observing conditions, trends,
tools for large archives – plus three code reviews, a performance pass and a much larger test suite.

### Added – plate solving and sky (ASTAP, local)
- **🎯 Solve** in the viewer: real image scale and **focal length** (vs. the session's telescope), field of view,
  centre, rotation; stored per file and in the metadata export. ASTAP CLI is built into the image, the star
  database lives in `DATA_DIR/astap`.
- **Resized exports** (JPEG at 50 %, drizzle) and **binning** recognised from the camera's sensor size; crops left
  unchanged.
- **Objects in the field** (OpenNGC: NGC, IC, Messier + addendum); click one to show only it on the image.
- **✨ Objects** overlay (`O`): object ellipses and names, RA / Dec grid, N / E arrows.
- **⬆ North up** (`N`): the displayed image turned (and un-mirrored) to north up / east left, overlays included;
  toggle, remembered between frames, cleared by ↺ Reset, savable as a view setting.
- **Pointing check**: where the project's object lies in the frame.
- **Limiting magnitude** and photometric **zero point** from the stars matched to the local Gaia star database.
- **⬇ Annotated JPEG** export (objects, grid, N / E; also north up).
- **Use measured focal length** writes it to the telescope (solve dialog, bulk solve, session dialog); warning when
  a stored solve is outdated by a camera change.

### Added – optics inspector
- **🔬 Optics** in the viewer: star FWHM and elongation across the field, 3×3 mosaic of corners / edges / centre,
  overlay, findings for **sensor tilt**, **field curvature**, radial / tangential edge elongation (**coma,
  corrector spacing**) and elongation everywhere (**guiding, wind, collimation**); summary stored per file.
- **🔬 Optics** in the bulk bar: per-frame table and what repeats across frames (optics) vs. what changes
  (conditions).

### Added – observing conditions and trends
- **Seeing** (centre FWHM without diffraction; warnings when undersampled or defocused), **transparency** (zero
  point vs. the best night of the same telescope · camera · gain) and **sky brightness** (mag/arcsec² like SQM;
  camera offset from RAW black level, FITS keyword or a linked bias / dark).
- Session dialog **Conditions** with *🌡 Measure conditions* and *Use for Seeing / SQM*; **Measured** column in the
  sessions table (focal length, seeing, sky brightness, transparency, limiting magnitude).
- Settings: **measure new nights automatically** and **fill in Seeing / SQM** (values typed by the user are kept).
- **Stats → Optics & conditions**: seeing, transparency, sky brightness, FWHM, limiting magnitude, tilt and
  elongation per night and telescope · camera (charts with tooltips, click opens the night, table).

### Added – large archives
- **📦 Import archive**: a whole folder tree into many projects and sessions at once (project from `OBJECT` or the
  folders, night from `DATE-OBS` / EXIF / path), reviewed per project × night; existing projects and nights
  recognised by name or object; telescope / camera prefilled from the header.
- **Background analysis queue**: **🎯🔬 Analyse** a project or the whole archive (all frames or 1 / 3 / 10 per
  night, only missing results), progress and *Stop analysis*; **🎯 Solve** in the bulk bar with a per-telescope
  summary; for more than 20 frames the bulk bar offers the background mode; interrupted tasks are re-queued.
- **Activity indicator** next to the app name while anything runs in the background (hover for details).
- **Duplicate warning** (project page, Stats) for files stored more than once.
- **Archive health** (Stats): quick or full (SHA-256) integrity check in the background with a stored report.
- **Pagination** of sessions (table + file blocks; links to a session on another page find it), of every file group
  and of the calibration library; page sizes in Settings (0 = all).

### Added – other
- **⬇ Metadata** in the header: metadata export of all projects + calibration library.
- **Duplicate project warning** when creating / renaming (`NGC 123` = `ngc123` = `NGC  123`).
- Clicking a session row jumps to its files; **🌙 session** button in the viewer back to the frame's night.
- **Settings → Background processing**: switches for thumbnails, frame statistics, suspect marks, the analysis
  queue (paused while off), integrity checks and the automatic conditions measuring / filling.
- **Tabs** on the Stats page (Overview · Optics & conditions · Archive health) and the Settings page.

### Changed
- Viewer toolbar: ✨ Objects, ⬆ North up, 🔬 Optics and 🎯 Solve next to the info button; wide result dialogs.
- Plate solve / optics logic in `analysis.py`, shared by the endpoints and the background queue; new modules
  `platesolve.py`, `photometry.py`, `optics.py`, `conditions.py`, `measured.py`, `annotate.py`, `integrity.py`,
  `routes_archive.py`.
- Docker image: ASTAP CLI, OpenNGC catalogue (v20260501) and DejaVu fonts (annotated export).
- File lists, project cards and the gallery use small cached thumbnails (240 / 640 px).
- Static files are referenced with a content version (`?v=…`) – browsers never keep an outdated CSS / JS.
- README rewritten for the current scope (introduction, contents, features by area, workflow, modules, test table)
  with 14 new screenshots.

### Fixed – speed, memory and blocking
- **Server freeze**: with two optics analyses / annotated exports running, a third request blocked the whole server
  (a thread semaphore was taken on the event loop).
- **Thumbnails**: lists loaded the 1600 px previews (~770 kB each) – a project page with 208 frames now loads
  ~0.7 MB of images instead of ~160 MB.
- **FITS headers** are no longer read with every file row (deferred column; exports load them in one query).
- **Star database cache** limited to 150 MB (LRU, float32).
- **Uploads**: writing and hashing in 4 MB blocks outside the event loop; state of abandoned uploads is freed.
- Settings cached for 1 s (invalidated on save); OpenNGC catalogue loaded in the background at start (first solved
  frame 0.9 s → 0.02 s); transparency reference reads only the needed values; waiting for the plate solver is
  limited; heavy archive-import work no longer blocks the server; new database indexes for frequent filters.
- Every background analysis re-read all plate solves of the archive; automatic measuring could queue the same night
  again and again; starting a new batch could remove queued tasks.

### Fixed – plate solve, optics, conditions
- **Binned** frames got a doubled focal length; the FOV hint used the unbinned sensor.
- Session **FWHM in ″** was halved for resized (50 % JPEG) frames.
- An **ASTAP timeout** or missing binary caused a server error and blocked other solves.
- Limiting magnitude: the match radius now grows with the image scale (wide fields matched almost nothing).
- Conditions were also computed from **stacks** (calibrated / normalised) – now light frames only.
- Seeing of colour (CFA) cameras was not flagged as undersampled; values below the diffraction limit are not
  reported; defocused nights (> 6″) are not filled in as seeing automatically.
- Gain “101” vs. “101.0” split the transparency reference; the zero point uses the most common gain of the night.
- Camera offset: the shortest linked dark is used; a FITS keyword that could never match was removed.
- Optics: the background was 0 along the image edges (re-run 🔬 Optics on frames analysed before); radial /
  tangential classification on non-square frames; tiny frames crashed the mosaic; `/optics` uses the viewer
  orientation.
- Palette / alpha PNGs were measured from palette indices / with the alpha channel; NaN / ±inf pixels (damaged FITS)
  overflowed the statistics and previews.
- Objects within half a pixel of the frame edge were counted on the wrong side.

### Fixed – import, worker, integrity
- The integrity check reported files imported / deleted / moved during the check; a stopped check showed “OK”.
- Archive import: dangling or outside symlinks broke the scan; a deleted matched project created a project with an
  empty name; equipment of existing sessions was overwritten; file-name dates after midnight started a new night;
  failed starts left empty placeholder files; free disk space is checked.
- Server import: invalid items / failed starts leave no placeholder files; broken symlinks are skipped.
- Uploads: cancelling while finishing returned a server error; locks were not released on cancel.

### Fixed – pages and settings
- Settings reset deleted the stored integrity report; Save did nothing visible when a field on another tab was
  invalid.
- Changing a session's date kept the frames of the old night in it; “Untitled” projects warned about duplicates of
  each other; deleting an already deleted session redirected to the Referer.
- Session dialog failed with optics results but no plate solve, tilt or elongation value.
- Activity indicator could start several polling loops; a failed upload kept it “uploading” forever.
- Bulk solve / optics dialogs showed rows of a previous run; a malformed result stopped the run.
- Viewer: ⬆ North up appeared only after a reload; North up / Objects buttons without a rendered image; endless
  retry when the image failed; saved view settings now win over the remembered north-up toggle; failed optics
  results were cached; Reset refreshes the optics overlay; Reject shows an error instead of flipping on failure.
- Pages no longer reload by themselves while a dialog is open.
- Archive import page: real date validation, empty names, errors re-enable Start; project form duplicate warning
  used out-of-order answers; Stats panels survive network errors; tabs follow links to elements inside a pane;
  trend charts show “No data yet”.

### Tests
- 218 tests (from about 100), code coverage ≈ 88 %; the suite runs without warnings.
- New: `test_sky.py` (catalogue, WCS, pointing, photometry, optics on synthetic star fields, north up, exports),
  `test_archive.py` (analysis queue, archive import, duplicates, integrity, background switches, activity, page
  speed with 3000 frames), `test_conditions.py` (seeing / transparency / sky brightness, measuring / filling, tabs),
  `test_coverage.py` (full plate solve with a simulated ASTAP, upload / import / worker error paths),
  `test_review.py` (regressions of the code reviews), `test_paging.py` (pagination), `test_perf.py` (thumbnails,
  render slots, cache budgets, settings cache, query counts, large uploads).

## [1.2.0] – 2026-11-06

### Changed – projects are objects, sessions carry everything else
- **A project is one object** (name, object, object type, tags, description, notes, cover, created / modified).
  Capture date, location, telescope, camera, mount, software, filters and total exposure were removed from the
  project.
- **Sessions (nights)** hold date, location, telescope, camera, mount, software, seeing, SQM, temperature,
  humidity and moon illumination. A new session inherits the equipment of the latest one.
- Every file (lights and calibration frames) belongs to a session. Files are added with **⬆ Add files** /
  **📁 Import** on a session; uploads and server imports into a project without a session are rejected.
- Calibration library sets are linked **per session** (best matches: same camera, closest date).
- Integration time is shown per session and per filter; the project header shows the total.
- Existing data is migrated automatically on startup: a session is created from the old project date and
  equipment, equipment / software are copied to sessions, project calibration links go to every session and
  single-session projects get all their files assigned. Old project columns stay in the database (unused).

### Changed – project page
- Sessions table shows only night, equipment, scale / FOV, moon and files; **i** opens a session detail
  dialog (conditions, equipment with technical parameters, files per type, integration per filter,
  linked calibration sets, notes).
- Files panel is grouped by session; each session block can be collapsed and has its own Add files,
  Import, Calibration library and ZIP buttons.
- File tables show only name, size, date added and capture date (DATE-OBS / EXIF); **i** opens a dialog with
  all details (exposure, gain, temperature, filter, FWHM / stars vs. median, background, noise, hash, storage
  path, FITS header), editing and single-file actions (type, session, move). Checkboxes + bulk bar remain.
- Final stacks are shown as a compact gallery at the top of the project page.
- Resizable description / notes fields.

### Changed – downloads
- **⬇ ZIP all sessions**: every file of every session, one folder per session `<nr>_<date>/<type>/…`
  (unique even when sessions share a date), incl. each session’s linked library sets.
- **⬇ ZIP session**: all files of one session. **⬇ ZIP selected types**: only the checked types.
- `INFO.txt` / `metadata.json` list sessions with equipment and their calibration links.

### Changed – overview, gallery, statistics
- Project cards show the range of nights and their count; date filter, equipment / location / software filters,
  search and date sorting work on sessions. Gallery and statistics use session dates and equipment.
- Archivable data moved to `DATA_DIR/archive-data/{projects,calibration}`; `.cache` and `.uploads` stay outside
  (moved automatically on startup).
- More professional look: neutral palette, refined header with active section, joined stat strip, tinted
  badges, underlined tabs, consistent controls; full-width layout.

### Added
- Technical parameters for equipment (Manage lists): telescope aperture, focal length, type (f-ratio derived);
  camera resolution, pixel size, sensor type, cooling (sensor size derived); mount type, payload, GoTo.
  Image scale (″/px) and field of view per session.
- Account dropdown menu (manage lists, settings, change password, log out).
- Upload pre-check: files that already exist (same name and size) or are already in the upload list are
  reported immediately; *Start upload* is hidden when nothing is left to upload.
- Search results show **where** the text was found: each project appears once with a “Found in N places”
  list (project fields, session notes / equipment, file names, descriptions, FITS headers) with highlighted
  snippets; the first 3 are shown, the rest summarized.
- Gallery: pagination with range info, first / last, page-size and sort switch (photo date, project, recently
  added); new settings *Images per page*, *Tile size* and *Default sort* in Settings → Gallery.
- Gallery and slideshow show the capture date of each image (DATE-OBS / EXIF), else its session date.
- Settings → *Panels – expanded by default*: choose whether the overview summary and filters, the project’s
  Sessions panel, session blocks in Files, file type groups and the statistics table start expanded.
- Overview summary can be hidden / shown with a button.
- Deleting a session deletes all its files (stored files, thumbnails, cache); linked calibration library
  sets are only unlinked. The confirmation shows the number of files.
- Readable HTML error page for browser requests instead of bare JSON.

### Security
- **CSRF protection:** every change (forms, API calls, uploads) requires a per-login token; other websites can
  no longer submit actions on your behalf. Log out is a POST button (a plain `/logout` link does nothing).

### Fixed
- Lookup list items that are still used (sessions, projects, calibration sets) cannot be deleted; the delete
  button is disabled and the message says where the item is used. Renaming is still possible.
- Full-resolution colour (Bayer) FITS rendering needs about half the memory (24 MP: 1.16 GB → 0.54 GB).
- CMYK JPEGs are converted to RGB (they were shown with wrong colours).
- Slideshow still starts when its captions cannot be loaded.
- A lookup-list name containing an apostrophe broke the delete confirmation on the Manage lists page.
- **Security:** the viewer’s `size` parameter went unchecked into the render-cache file name and allowed writing
  files outside the cache (`../`); now only `full` / `preview` are accepted.
- Housekeeping errors (cache / upload cleanup) could stop the background worker permanently.
- FITS cubes with 2 or more than 3 planes and 4-D data (e.g. 1×3×H×W) rendered as garbage or not at all;
  `XBAYROFF` written as `1.0` and 1-pixel-high colour frames crashed rendering.
- Unreadable files are reported (“⚠ unreadable” + reason) instead of silently showing no preview.
- “Suspect” flags are recomputed after type change, move, delete and session change, compared within the
  same night and filter, and cleared for non-light frames.
- Histogram cache is written atomically; a damaged cache file is recomputed. Saved view settings of an
  unexpected type no longer crash the viewer or processing.
- Too long values (session conditions, calibration fields, catalog names) show a message instead of a 500;
  catalog names are limited to 100 characters and unique regardless of upper / lower case.
- Exposure values `nan` / `inf` are not stored.
- Search: `%` and `_` are matched literally, repeated spaces are ignored; same in the calibration list.
- Warning at startup when `SECRET_KEY` is missing or weak.
- Overview filters for nights (date range, location, telescope, camera, mount, software) are combined with AND
  on the same session – telescope A + camera B only matches a night where both were used.
- “ZIP session” contains `INFO.txt` and `metadata.json` for that session only (with a `scope` field).
- Duplicate check and insert are atomic across uploads and imports: the same content uploaded / imported
  concurrently is stored once.
- Project page performance: calibration sets are loaded once (file counts in one query) and one shared
  calibration dialog is filled per session in the browser instead of one dialog per session with all sets.
- **Security:** stored XSS in the file detail dialog – a description / filter / FITS value starting with
  `<span` was inserted as HTML. All values are now escaped.
- Saving a session with an empty or invalid date caused a server error (500); now a validation message.
- Bulk “Session” with an unknown id silently removed files from their session; now rejected (404).
- Files moved to another project are put into that project’s latest session instead of none.
- Removed the unused legacy multipart upload endpoint, which bypassed the session and duplicate rules.
- Upload API: non-numeric ids / invalid body returned 500 (now 400); uploads larger than the free disk space
  are refused (507); a repeated or concurrent “finish” no longer leaves empty orphan files; a project / set /
  session deleted during an upload is handled (410, temporary data removed); long-running resumable uploads
  are no longer removed by the 48 h cleanup while chunks are still arriving.
- Metadata export dropped FITS headers of equally named files from different sessions; headers are now stored
  per session folder and ZIP entries are never silently skipped.
- Statistics counted an “imaging night” (and month) from the upload date for lights without any date.
- Migrations: legacy projects without a date no longer break the session migration; the archive folder
  migration only repoints files that were actually moved.
- Calibration link message showed the number of submitted ids instead of linked sets.
- Dropping files on a project page before choosing a session queued them invisibly; now a hint is shown.
  Switching “Add files” to another session asks before re-targeting files already in the list.
- “Show all N files” links did not expand a collapsed session; slideshow could start at an invalid position
  for images beyond the first 2000.
- Deleting a project / calibration set twice (double click, resubmitted form) showed an empty page.
- “Add files” button did nothing (broken inline handler).
- Adding a file already in the upload list showed a misleading “No supported files” message.

## [1.0.0] – 2026-11-05

First complete release.

### Added
- Projects with equipment, object, location, description, notes, tags, cover image picker,
  created / modified timestamps.
- Imaging sessions (nights) with conditions and computed moon illumination; automatic creation from
  observation dates.
- Calibration library (darks, bias, flats, dark flats) reusable across projects and included in stacking ZIPs.
- Resumable chunked upload with drag & drop of files and folders, pause and retry; incremental SHA-256.
- Server-side folder import (background copy, read-only source).
- Frame type auto-detection (FITS `IMAGETYP`, file and folder names).
- Duplicate detection by content hash.
- Bulk operations on files (type, move, session, metadata, reject, reprocess, delete).
- Background worker for imports, hashing, previews, metadata extraction and frame statistics.
- Frame quality statistics (FWHM, stars, background, noise) with suspect flagging and rejection.
- Integration time per filter.
- Image viewer for FITS, camera RAW and images: fit / 1:1 / zoom, fullscreen, Bayer pattern, flip, channels,
  stretch modes, histogram levels with live preview, brightness, reset, per-file / per-type view settings,
  FITS header and EXIF panel.
- Gallery of final images with fullscreen slideshow.
- Statistics page (integration per month / year, objects, filters, equipment, disk usage).
- Streamed ZIP downloads with `INFO.txt` and `metadata.json`; metadata export incl. all FITS headers and EXIF.
- Search, filters (date, size, equipment, location, object type), sorting, pagination, grid / list view.
- Managed lookup lists with modal editor; renames propagate to projects, sessions and calibration sets.
- Settings page (title, accent color, theme, view, card size, page size, sort, ZIP compression, slideshow, duplicates).
- Light / dark mode, dd-mm-yyyy dates with date picker (defaults to today for new records).
- Login throttling and default-password warning.
- Test suite (`run-tests.sh`) running against a separate test database.
- `.env.example` configuration template.

### Security
- Original files are never modified; view settings only affect derived thumbnails and renders.
- Path traversal protection for imports, uploads and ZIP entry names (zip-slip).
- Atomic file name reservation; deletes never remove a path still referenced by another file.
- Upload size limits and per-upload locking.
