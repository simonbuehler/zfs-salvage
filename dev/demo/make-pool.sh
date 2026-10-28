#!/bin/bash
# Build the demo pool (see dev/demo/make_pool.py). Inside the test VM: tests/vm/run.sh dev/demo/make-pool.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
modprobe zfs
exec python3 -I dev/demo/make_pool.py "$@"
