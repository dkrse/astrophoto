# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
