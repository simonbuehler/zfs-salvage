#!/bin/bash
# Install the recovery-host tools: zscan (static), libzsalvage.so and zsread.py.
# The recovery host usually runs from RAM (e.g. SystemRescue), so repeat this after every reboot.
#
# Usage: scripts/deploy-tools.sh [host=$ZFS_SALVAGE_HOST or recovery-host]
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${1:-"$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1] + "/scripts"); from common import HOST; print(HOST)' "$REPO")"}"

if [ "$HOST" = local ]; then
    for f in tools/zscan/zscan tools/libzsalvage/libzsalvage.so; do
        [ -f "$REPO/$f" ] || { echo "missing $f – build it first (${f%/*}/build.sh) or use a release bundle"; exit 1; }
    done
    echo "local mode: the tools are used from $REPO, nothing to install"
    exit 0
fi
for f in tools/zscan/zscan tools/libzsalvage/libzsalvage.so; do
    [ -f "$REPO/$f" ] || { echo "missing $f – build it first (${f%/*}/build.sh) or use a release bundle"; exit 1; }
done
ssh "$HOST" 'command -v python3 >/dev/null' || { echo "$HOST has no python3"; exit 1; }
scp -q "$REPO/tools/zscan/zscan" "$HOST:/usr/local/bin/zscan"
scp -q "$REPO/tools/libzsalvage/libzsalvage.so" "$HOST:/usr/local/lib/libzsalvage.so"
scp -q "$REPO/scripts/zsread.py" "$HOST:/usr/local/bin/zsread.py"
ssh "$HOST" 'chmod 755 /usr/local/bin/zscan /usr/local/bin/zsread.py'
echo "installed on $HOST: zscan, libzsalvage.so, zsread.py"
