#!/bin/bash
# Build the demo pool (see demo/make_pool.py). Inside the test VM: tests/vm/run.sh demo/make-pool.sh
set -euo pipefail
cd "$(dirname "$0")/.."
modprobe zfs
exec python3 -I demo/make_pool.py "$@"
