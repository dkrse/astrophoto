# vendor/ – everything the Docker build needs, kept locally

The image builds and the application runs **without internet access**. This folder holds the third-party files
that the build copies into the image (filled by `scripts/fetch-vendor.sh`, run once with internet):

| Folder | Content | Licence |
|---|---|---|
| `astap/` | ASTAP command-line plate solver (`astap_cli`, Linux amd64) – https://www.hnsky.org/astap.htm | MPL-2.0 / LGPL |
| `openngc/` | OpenNGC v20260501 (`NGC.csv`, `addendum.csv`) – https://github.com/mattiaverga/OpenNGC | CC-BY-SA 4.0 |
| `hyg/` | HYG star database v4.1 (`hygdata_v41.csv`) – https://codeberg.org/astronexus/hyg | CC-BY-SA 4.0 |
| `stellarium/` | Stellarium DSO catalogue v3.21 + names (`catalog.txt`, `names.dat`) – https://github.com/Stellarium/stellarium | GPL-2.0+ |
| `fonts/` | DejaVu fonts (annotated JPEG) | Bitstream Vera / public domain |
| `wheels/` | Python packages of `requirements.txt` and `requirements-dev.txt` for python:3.12-slim | see each package |

The ASTAP star database (D50 …) is not here: it lives in the data volume (`DATA_DIR/astap`).
The base image `python:3.12-slim` must be in the local Docker cache (`docker pull python:3.12-slim` once).
The files are not committed (size, third-party licences) – see `.gitignore`.
