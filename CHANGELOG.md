# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.3.1] – 2026-11-07

### Added
- **Plate solving** with ASTAP (local, on demand): **🎯 Solve** in the viewer measures image scale, focal
  length (vs. the session's telescope), field of view, centre and rotation; stored per file and exported in
  the metadata. ASTAP CLI is built into the image, the star database lives in `DATA_DIR/astap`.
- Plate solve detects **resized images** (e.g. JPEG exports at 50 %, drizzle) by comparing the image with
  the session camera's sensor size and corrects the focal length; crops are recognised and left unchanged.
  Only when the camera's resolution and pixel size are set.
- **Objects in the field** (OpenNGC catalogue, NGC / IC / Messier + addendum) listed in the solve dialog.
- **✨ Objects** overlay in the viewer (`O`): object ellipses and names, RA / Dec grid, N / E arrows.
- **Pointing check**: position of the project's object relative to the frame centre.
- **🔬 Optics inspector** in the viewer: star FWHM and elongation across the field, 3×3 mosaic of corners /
  edges / centre, overlay on the image and findings for sensor tilt, field curvature, radial / tangential
  edge elongation (coma, spacing), uniform elongation (guiding, wind, collimation).
- **🔬 Optics** in the bulk bar for several frames: per-frame table and a summary of findings that repeat.
- **🎯 Solve** in the bulk bar for many frames with a summary per telescope (median focal length).
- **Measured values per session** (sessions table + session dialog): focal length vs. equipment, scale,
  rotation, limiting magnitude, star FWHM (″), elongation, tilt.
- **Use measured focal length** button (solve dialog, bulk solve, session dialog) writes it to the equipment.
- **Limiting magnitude** per solved frame from the ASTAP star list matched to the local Gaia star database.
- **⬇ Annotated JPEG** export with objects, RA / Dec grid and N / E.
- **Optics & sky over time** charts on the Stats page (FWHM, limiting magnitude, tilt, elongation per night
  and equipment).
- Optics analyses are stored per file (summary only).
- Tests for all of the above (`tests/test_sky.py`).
- **⬇ Metadata** in the header: full metadata export of all projects + calibration library (ZIP); asks where
  to save where the browser allows it (HTTPS / localhost).
- Duplicate project warning (name / object compared ignoring case, spaces and punctuation); saving a
  duplicate needs an explicit confirmation.
- Clicking a session row jumps to that session's files.

### Changed
- Viewer toolbar: **✨ Objects**, **🔬 Optics** and **🎯 Solve** next to the info button; solve and optics
  results open in wide dialogs.
- Sessions table: new **Measured** column; the session dialog shows the measured values.
- Statistics page: new **Optics & sky over time** section.
- Docker image: ASTAP CLI, OpenNGC catalogue (v20260501) and DejaVu fonts (annotated export) are included.
- README screenshots updated; new screenshots of the session dialog, objects overlay, plate solve, optics
  inspector and the optics trend.

### Fixed
- Optics inspector: the smooth background was 0 along the image edges (interpolation), which distorted
  stars near the edges – re-run 🔬 Optics on frames analysed before.

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
