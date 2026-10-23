#!/bin/bash
# Pipeline test: scan -> verify candidates -> tree -> extract -> compare SHA-256, per fixture variant.
# Runs locally on fixture images, no SSH, no web UI. Needs python3, gcc (builds zscan and
# libzsalvage.so on first use) and the fixture images (tests/vm/run.sh tests/make-fixtures.sh).
#
# Usage: tests/run.sh [-v] [--keep] [--no-lib] [variant ...]       (default: every variant)
#   variant   <name> (undamaged) or <name>-damaged (objset block destroyed in every copy)
# Output:   one line per variant: PASS | FAIL | XFAIL | XPASS; exit code = number of FAIL + XPASS.
set -euo pipefail
exec python3 -I "$(dirname "$0")/run.py" "$@"
