#!/bin/bash
# Build a release tarball: the generic tool only (no case files, logs or case documents),
# with prebuilt zscan and libzsalvage.so so that users need no compiler or Docker.
#
# Usage: dev/make-bundle.sh            -> build/zfs-salvage-<version>.tar.gz
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

for f in tools/zscan/zscan tools/libzsalvage/libzsalvage.so; do
    [ -f "$f" ] || { echo "missing $f – run ${f%/*}/build.sh first"; exit 1; }
done
tools/check-portable.sh || { echo "the binaries are not portable – run tools/build-portable.sh"; exit 1; }
VERSION="$(git describe --tags --match 'v[0-9]*' --always --dirty 2>/dev/null || date +%Y%m%d)"     # release tags only (fixtures-N tags share commits)
NAME="zfs-salvage-$VERSION"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME"

FILES=(zfs-salvage README.md AGENTS.md docs/design.md cases/example.json
       scripts/*.py scripts/*.sh scripts/ui scripts/diag
       tools/build-portable.sh tools/check-portable.sh
       tools/zscan/zscan.c tools/zscan/build.sh tools/zscan/zscan
       tools/libzsalvage/zsalvage.c tools/libzsalvage/build.sh tools/libzsalvage/libzsalvage.so)
[ -f LICENSE ] && FILES+=(LICENSE)
tar -c --exclude=__pycache__ "${FILES[@]}" | tar -x -C "$STAGE/$NAME"
echo "$VERSION" > "$STAGE/$NAME/VERSION"

mkdir -p build
tar -C "$STAGE" -czf "build/$NAME.tar.gz" "$NAME"
echo "build/$NAME.tar.gz ($(du -h "build/$NAME.tar.gz" | cut -f1))"
tar -tzf "build/$NAME.tar.gz" | sed "s#^$NAME/##" | grep -v '/$' | sort
