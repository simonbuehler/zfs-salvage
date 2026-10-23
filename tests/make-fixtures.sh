#!/bin/bash
# Create the test fixtures (see tests/make_fixtures.py). Needs root and the ZFS kernel module.
# On a machine without ZFS (e.g. WSL), run it inside the test VM:
#   tests/vm/run.sh tests/make-fixtures.sh [variant ...]
set -euo pipefail
cd "$(dirname "$0")/.."
modprobe zfs
exec python3 -I tests/make_fixtures.py "$@"
