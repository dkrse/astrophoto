#!/usr/bin/env bash
# Fills vendor/ with everything the Docker build needs, so that the image can later be built offline.
# Run once with internet access (and again only to update a component).
set -euo pipefail
cd "$(dirname "$0")/.."
V=vendor
mkdir -p $V/astap $V/openngc $V/hyg $V/stellarium $V/fonts $V/wheels
get() { echo "▸ $2"; curl -fsSL --retry 3 -o "$2" "$1"; }

get "https://sourceforge.net/projects/astap-program/files/linux_installer/astap_command-line_version_Linux_amd64.zip/download" /tmp/astap.zip
python3 -c "import zipfile;zipfile.ZipFile('/tmp/astap.zip').extract('astap_cli','$V/astap')" && rm /tmp/astap.zip
chmod +x $V/astap/astap_cli

get https://raw.githubusercontent.com/mattiaverga/OpenNGC/v20260501/database_files/NGC.csv $V/openngc/NGC.csv
get https://raw.githubusercontent.com/mattiaverga/OpenNGC/v20260501/database_files/addendum.csv $V/openngc/addendum.csv
get https://raw.githubusercontent.com/astronexus/HYG-Database/main/hyg/CURRENT/hygdata_v41.csv $V/hyg/hygdata_v41.csv
get https://raw.githubusercontent.com/Stellarium/stellarium/v25.3/nebulae/default/catalog.txt $V/stellarium/catalog.txt
get https://raw.githubusercontent.com/Stellarium/stellarium/v25.3/nebulae/default/names.dat $V/stellarium/names.dat

echo "▸ fonts (DejaVu) and Python wheels for python:3.12-slim"
docker pull -q python:3.12-slim >/dev/null
docker run --rm -v "$PWD/$V/fonts:/f" python:3.12-slim sh -c \
  "apt-get update -qq && apt-get install -y -qq --no-install-recommends fonts-dejavu-core >/dev/null && cp /usr/share/fonts/truetype/dejavu/*.ttf /f/"
docker run --rm -v "$PWD/requirements.txt:/r.txt:ro" -v "$PWD/$V/wheels:/w" python:3.12-slim \
  pip download -q --disable-pip-version-check -d /w -r /r.txt
# test dependencies too, so ./run-tests.sh works offline
docker run --rm -v "$PWD/requirements-dev.txt:/r.txt:ro" -v "$PWD/$V/wheels:/w" python:3.12-slim \
  pip download -q --disable-pip-version-check -d /w -r /r.txt
echo "✓ vendor/ complete – the image now builds without internet"
