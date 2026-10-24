#!/bin/bash
# Run zscan on the recovery host chunk by chunk and keep the finds on this machine
# (<outdir>/chunk_NNNN.tsv.gz). Resumable: finished chunks (file + .done) are skipped,
# failed ones are retried.
#
# Usage: scripts/scan-device.sh <device> [chunk_gib=64] [outdir=logs/raw/scan_<name>] >> logs/scan_<name>.log
#   device   read-only block device on the recovery host, e.g. /dev/mapper/ro_sdX4
# The recovery host is ZFS_SALVAGE_HOST (default recovery-host); install the tools first
# with scripts/deploy-tools.sh. The status page expects the paths given in the case file (scan.*).
# zscan gets --max-txg = newest uberblock TXG of the case + 1000 (blocks cannot be younger than the
# newest uberblock); a case without a timeline scans without a limit. Needs a zscan with --max-txg and
# --max-vdev on the host (scripts/deploy-tools.sh installs it).
# Indirect block sizes: zscan tries 128 KiB and 16 KiB by default. The case's profile names one size, but it
# is measured on a readable dataset, and datasets created by different ZFS versions can differ, so it is
# not used to restrict the scan. ZFS_SALVAGE_IND_SIZES="16384" (space separated) restricts it on purpose.
set -uo pipefail

DEV="${1:?usage: scan-device.sh <device> [chunk_gib] [outdir]}"
case "$DEV" in /*) ;; *) DEV="/dev/mapper/$DEV" ;; esac
CHUNK_GIB="${2:-64}"
HOST="${ZFS_SALVAGE_HOST:-recovery-host}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${3:-$REPO/logs/raw/scan_$(basename "$DEV")}"
mkdir -p "$OUT"

# from the case: TXG limit, the top-level vdev of this device and the highest vdev id
read -r MAXTXG VDEV MAXVDEV <<< "$(python3 -I -c "
import sys
sys.path.insert(0, sys.argv[1] + '/scripts')
from common import CASE, newest_txg
t = newest_txg()
devs = CASE.get('pool', {}).get('devices', [])
ids = [d['vdev'] for d in devs if d.get('vdev') is not None]
mine = [d['vdev'] for d in devs if d.get('path') == sys.argv[2] and d.get('vdev') is not None]
print(t + 1000 if t else '-', mine[0] if mine else 0, max(ids) if ids else 0)" "$REPO" "$DEV" 2>/dev/null || echo "- 0 0")"
MAXOPT="--max-vdev ${MAXVDEV:-0}"; [ "${MAXTXG:--}" != - ] && MAXOPT="$MAXOPT --max-txg $MAXTXG"
echo "${VDEV:-0}" > "$OUT/vdev"                 # rank-trees.py prefixes this vdev's offsets
for s in ${ZFS_SALVAGE_IND_SIZES:-}; do MAXOPT="$MAXOPT --ind-size $s"; done
echo "max txg: ${MAXTXG:-none}, top-level vdev ${VDEV:-0} of 0..${MAXVDEV:-0}, indirect block sizes: ${ZFS_SALVAGE_IND_SIZES:-128K and 16K}"

[ "$(ssh "$HOST" "blockdev --getro $DEV")" = 1 ] || { echo "$DEV is not read-only on $HOST – refusing to scan"; exit 1; }
SIZE=$(ssh "$HOST" "blockdev --getsize64 $DEV")
STEP=$((CHUNK_GIB << 30))
N=$(( (SIZE + STEP - 1) / STEP ))
echo "$DEV: $SIZE bytes, $N chunks of $CHUNK_GIB GiB -> $OUT"

for ((i = 0; i < N; i++)); do
    f=$(printf '%s/chunk_%04d.tsv.gz' "$OUT" "$i")
    [ -f "$f.done" ] && continue
    start=$((i * STEP)); end=$(((i + 1) * STEP))
    for try in 1 2 3; do
        t0=$(date +%s)
        if ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "$HOST" \
               "zscan $MAXOPT $DEV $start $end 2>/dev/null" | gzip -1 > "$f" &&
           [ "${PIPESTATUS[0]}" = 0 ]; then
            touch "$f.done"
            echo "$(date +%H:%M) chunk $i/$N done: $(zcat "$f" | wc -l) finds, $(( $(date +%s) - t0 )) s"
            break
        fi
        echo "$(date +%H:%M) chunk $i attempt $try failed, retrying"
        sleep 20
    done
done
echo "ALL CHUNKS: $(ls "$OUT"/*.done 2>/dev/null | wc -l)/$N"
