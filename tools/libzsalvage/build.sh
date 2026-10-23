#!/bin/bash
# Build libzsalvage.so in the Arch snapshot of the recovery system (glibc 2.41), loaded by zsread.py via ctypes.
set -euo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
docker run --rm -v "$D":/src -w /src archlinux:base-20250406.0.331908 bash -euo pipefail -c '
  echo "Server=https://archive.archlinux.org/repos/2025/04/08/\$repo/os/\$arch" > /etc/pacman.d/mirrorlist
  sed -i "s/^#\?SigLevel.*/SigLevel = Never/" /etc/pacman.conf
  pacman -Syyuu --noconfirm --needed gcc >/dev/null
  gcc -O2 -Wall -Wextra -shared -fPIC -o libzsalvage.so zsalvage.c
  chown "$(stat -c %u:%g /src)" libzsalvage.so'
ls -l "$D/libzsalvage.so"
