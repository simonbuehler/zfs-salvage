#!/bin/bash
# Run a script inside a throwaway Ubuntu 24.04 VM that has the ZFS kernel module.
# Needed where the host has no ZFS module (e.g. WSL): creating fixture pools needs `zpool create`.
# The VM runs with QEMU/KVM inside a Docker container; the repository is shared at /mnt/work.
#
# Usage: tests/vm/run.sh <script relative to the repository> [args...]
#   e.g. tests/vm/run.sh tests/make-fixtures.sh
# Output of the script: tests/vm/run.log, exit code: tests/vm/exit-code, VM console: tests/vm/console.log
# Requires: Docker and /dev/kvm. The Ubuntu cloud image (~600 MB) is cached in ~/.cache/zfs-salvage-vm.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/zfs-salvage-vm"
IMG_URL="https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img"
SCRIPT="${1:?usage: run.sh <script> [args]}"; shift

[ -e /dev/kvm ] || { echo "no /dev/kvm – a VM would be far too slow"; exit 1; }
mkdir -p "$CACHE"
if [ ! -f "$CACHE/base.img" ]; then
    echo "downloading $IMG_URL"
    curl -fL --progress-bar -o "$CACHE/base.img.part" "$IMG_URL" && mv "$CACHE/base.img.part" "$CACHE/base.img"
fi
docker build -q -t zfs-salvage-vm "$REPO/tests/vm" >/dev/null
rm -f "$REPO/tests/vm/run.log" "$REPO/tests/vm/exit-code"
docker run --rm --device /dev/kvm -v "$REPO:/work" -v "$CACHE:/cache:ro" zfs-salvage-vm \
    bash /work/tests/vm/inside-container.sh "$SCRIPT" "$(id -u):$(id -g)" "$@"
rc="$(cat "$REPO/tests/vm/exit-code" 2>/dev/null || echo 'none (VM did not finish the script)')"
echo "script exit code: $rc  (log: tests/vm/run.log)"
[ "$rc" = 0 ]
