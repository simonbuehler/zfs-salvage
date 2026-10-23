#!/bin/bash
# Run zscan on the recovery host chunk by chunk and keep the finds on this machine
# (<outdir>/chunk_NNNN.tsv.gz). Resumable: finished chunks (file + .done) are skipped,
# failed ones are retried.
#
# Usage: scripts/scan-device.sh <device> [chunk_gib=64] [outdir=logs/raw/scan_<name>] >> logs/scan_<name>.log
#   device   read-only block device on the recovery host, e.g. /dev/mapper/ro_sdX4
# The recovery host is ZFS_SALVAGE_HOST (default recovery-host); install the tools first
# with scripts/deploy-tools.sh. The status page expects the paths given in the case file (scan.*).
set -uo pipefail

DEV="${1:?usage: scan-device.sh <device> [chunk_gib] [outdir]}"
case "$DEV" in /*) ;; *) DEV="/dev/mapper/$DEV" ;; esac
CHUNK_GIB="${2:-64}"
HOST="${ZFS_SALVAGE_HOST:-recovery-host}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${3:-$REPO/logs/raw/scan_$(basename "$DEV")}"
mkdir -p "$OUT"

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
               "zscan $DEV $start $end 2>/dev/null" | gzip -1 > "$f" &&
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
