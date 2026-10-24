#!/bin/bash
# Fetch the fixture images (tests/pack-fixtures.sh made the asset) into tests/fixtures and check them against
# tests/fixtures/SHA256SUMS.
#
# Usage: tests/fetch-fixtures.sh <url-or-file>
#   e.g. https://github.com/<owner>/<repo>/releases/download/fixtures-1/fixtures.tar   (in CI: see .github/workflows/ci.yml)
set -euo pipefail
SRC="${1:?usage: fetch-fixtures.sh <url or file of fixtures.tar>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
case "$SRC" in
    http://*|https://*) curl -fsSL --retry 3 -o "$TMP" "$SRC" ;;
    *) cp "$SRC" "$TMP" ;;
esac
tar -xf "$TMP" -C "$HERE/fixtures"
(cd "$HERE/fixtures" && sha256sum -c SHA256SUMS --quiet) || { echo "the images do not match SHA256SUMS"; exit 1; }
echo "fixtures ready: $(ls -d "$HERE"/fixtures/*/ | wc -l) variants"
