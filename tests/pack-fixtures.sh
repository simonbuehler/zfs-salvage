#!/bin/bash
# Pack the fixture images for a release asset (the CI downloads it, tests/fetch-fixtures.sh). Not bit-reproducible
# (the images come from a VM), so the asset is the reference: tests/fixtures/SHA256SUMS says which images belong
# to the sidecars in git.
#
# Usage: tests/pack-fixtures.sh [output=build/fixtures.tar]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/../build/fixtures.tar}"
mkdir -p "$(dirname "$OUT")"
cd "$HERE/fixtures"
(cd "$HERE/fixtures" && sha256sum -c SHA256SUMS --quiet) || { echo "the images do not match SHA256SUMS"; exit 1; }
tar -cf "$OUT" --exclude='*.json' --exclude=SHA256SUMS */*.xz
echo "$OUT ($(du -h "$OUT" | cut -f1)): $(tar -tf "$OUT" | wc -l) images"
