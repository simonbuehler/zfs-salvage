#!/bin/bash
# Build zscan (static) and libzsalvage.so in a manylinux2014 container (glibc 2.17, gcc 10): the result runs
# on every Linux x86-64 from CentOS 7 / Ubuntu 18.04 on, including SystemRescue and the Ubuntu live system.
# The image is pinned by digest so that a rebuild gives the same toolchain. Needs Docker and network access
# (glibc-static is installed into the throw-away container).
#
# Usage: tools/build-portable.sh [zscan|libzsalvage]      (default: both)
set -euo pipefail
T="$(cd "$(dirname "$0")" && pwd)"
IMAGE="quay.io/pypa/manylinux2014_x86_64@sha256:ac38650bc13796f4a5858c1c46202bc163b7060f38629a50a0385522f193ce32"
WHAT="${1:-both}"

build() {      # build <directory> <output> <gcc arguments...>
    local dir="$1" out="$2"; shift 2
    docker run --rm -v "$T/$dir":/src -w /src "$IMAGE" bash -euo pipefail -c "
        yum -q -y install glibc-static >/dev/null 2>&1 || true
        gcc -O2 -Wall -Wextra $* -o $out
        chown \"\$(stat -c %u:%g /src)\" $out"
    ls -l "$T/$dir/$out"
}

case "$WHAT" in
    zscan|both)       build zscan zscan -static zscan.c ;;
esac
case "$WHAT" in
    libzsalvage|both) build libzsalvage libzsalvage.so -shared -fPIC zsalvage.c ;;
esac
"$T/check-portable.sh" ${WHAT/both/}
