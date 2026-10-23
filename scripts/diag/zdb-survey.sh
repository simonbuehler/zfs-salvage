#!/bin/bash
# Survey without import: which datasets and snapshots of a pool can zdb open at each TXG,
# from each single device and from all of them? Runs ON the recovery host (with a current
# zdb) and reads only from read-only devices.
#
# Usage: POOL=<pool> DEVS="<dev> <dev>" [TARGET=<pool/dataset>] zdb-survey.sh [TXG ...]
#   DEVS   names below /dev/mapper or full paths, each must be read-only (blockdev --getro)
#   TXG    default: the newest uberblocks plus every 10th older one
# Output: /run/survey/<time>/{summary.tsv,<combo>_<txg>.txt}
set -uo pipefail

POOL="${POOL:?set POOL}"
DEVS="${DEVS:?set DEVS}"
TARGET="${TARGET:-}"
ZDB="${ZDB:-zdb-m}"
OUT="/run/survey/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"

paths=()
for d in $DEVS; do
    case "$d" in /*) p="$d" ;; *) p="/dev/mapper/$d" ;; esac
    [ -e "$p" ] || { echo "missing: $p"; exit 1; }
    [ "$(blockdev --getro "$p")" = 1 ] || { echo "$p is NOT read-only – aborting"; exit 1; }
    paths+=("$p")
done

# one directory per combination with exactly its devices (zdb -p searches there)
combos=()
for p in "${paths[@]}"; do
    c="$(basename "$p")"; combos+=("$c")
    rm -rf "/run/vdevs/$c"; mkdir -p "/run/vdevs/$c"; ln -s "$p" "/run/vdevs/$c/"
done
if [ ${#paths[@]} -gt 1 ]; then
    combos+=(all); rm -rf /run/vdevs/all; mkdir -p /run/vdevs/all
    for p in "${paths[@]}"; do ln -s "$p" /run/vdevs/all/; done
fi

if [ $# -gt 0 ]; then
    TXGS="$*"
else
    TXGS="$($ZDB -lu "${paths[@]}" 2>/dev/null | awk '/txg = /{print $3}' | sort -rnu | awk 'NR<=14 || NR%10==0')"
fi

printf 'combo\ttxg\texit\tdatasets\ttarget_readable\tunreadable\tother\n' > "$OUT/summary.tsv"
for txg in $TXGS; do
    for c in "${combos[@]}"; do
        f="$OUT/${c}_${txg}.txt"
        timeout 900 "$ZDB" -e -p "/run/vdevs/$c" -t "$txg" -d "$POOL" > "$f" 2>&1
        rc=$?
        ds=$(grep -c '^Dataset ' "$f")
        tgt=-; [ -n "$TARGET" ] && tgt=$(grep -cE "^Dataset ${TARGET}[ @]" "$f")
        bad=$(grep -oE "failed to hold dataset '[^']+'" "$f" | cut -d"'" -f2 | paste -sd, -)
        misc=$(grep -vE "^Dataset |failed to hold dataset" "$f" | head -3 | tr '\t\n' '  ' | cut -c1-120)
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$c" "$txg" "$rc" "$ds" "$tgt" "${bad:--}" "${misc:--}" | tee -a "$OUT/summary.tsv"
    done
done
echo "results: $OUT"
