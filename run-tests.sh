#!/usr/bin/env bash
# Run the test suite inside the application image.
# Uses a separate database "astrophoto_test" (dropped & recreated every run) on the same PostgreSQL
# server as DATABASE_URL in .env, and temporary data folders – production data is never touched.
#
#   ./run-tests.sh              run all tests
#   ./run-tests.sh -k upload    pass any pytest arguments
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "Missing .env (needs DATABASE_URL)" >&2
  exit 1
fi

IMAGE=astrophoto-astrophoto
echo "▸ building image…"
docker compose build -q astrophoto

echo "▸ running tests…"
docker run --rm \
  --env-file .env \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD/tests:/srv/tests:ro" \
  -v "$PWD/pytest.ini:/srv/pytest.ini:ro" \
  -v "$PWD/requirements-dev.txt:/srv/requirements-dev.txt:ro" \
  -w /srv \
  "$IMAGE" \
  sh -c 'pip install -q --disable-pip-version-check --root-user-action=ignore -r requirements-dev.txt && python -m pytest "$@"' _ "$@"
