#!/bin/bash
# Build zscan statically in a pinned Arch snapshot (runs on SystemRescue, which has no gcc).
set -euo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
docker run --rm -v "$D":/src -w /src archlinux:base-20250406.0.331908 bash -euo pipefail -c '
  echo "Server=https://archive.archlinux.org/repos/2025/04/08/\$repo/os/\$arch" > /etc/pacman.d/mirrorlist
  sed -i "s/^#\?SigLevel.*/SigLevel = Never/" /etc/pacman.conf
  pacman -Syyuu --noconfirm --needed gcc >/dev/null
  gcc -O2 -Wall -Wextra -static -o zscan zscan.c
  chown "$(stat -c %u:%g /src)" zscan'
ls -l "$D/zscan"
