#!/bin/bash
# Check that the binaries run on old systems: libzsalvage.so needs at most glibc 2.17, zscan is linked statically.
#
# Usage: tools/check-portable.sh [zscan|libzsalvage|<path to a binary>...]    (default: both in tools/)
set -uo pipefail
T="$(cd "$(dirname "$0")" && pwd)"
MAX="2.17"
[ $# -gt 0 ] || set -- "$T/zscan/zscan" "$T/libzsalvage/libzsalvage.so"
rc=0
for f in "$@"; do
    case "$f" in zscan) f="$T/zscan/zscan";; libzsalvage) f="$T/libzsalvage/libzsalvage.so";; esac
    [ -f "$f" ] || { echo "FAIL $f: not built"; rc=1; continue; }
    if file -L "$f" | grep -q 'statically linked'; then
        echo "OK   $f: statically linked"
        continue
    fi
    need="$(objdump -T "$f" 2>/dev/null | grep -o 'GLIBC_[0-9.]*' | sed 's/GLIBC_//' | sort -V | tail -1)"
    if [ -z "$need" ]; then
        echo "FAIL $f: neither static nor readable with objdump"; rc=1
    elif [ "$(printf '%s\n%s\n' "$need" "$MAX" | sort -V | tail -1)" != "$MAX" ]; then
        echo "FAIL $f: needs glibc $need (at most $MAX is allowed) – build it with tools/build-portable.sh"; rc=1
    else
        echo "OK   $f: needs glibc $need or newer (limit $MAX)"
    fi
done
exit $rc
