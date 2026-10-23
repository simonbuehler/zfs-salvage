#!/bin/bash
# Smoke test for the test VM: ZFS works, and what does the metadata of a new pool look like?
set -euxo pipefail
uname -r
modprobe zfs
zfs version
W=/var/tmp/smoke; rm -rf $W; mkdir -p $W
for v in modern legacy; do
    truncate -s 256M $W/$v.img
    if [ $v = legacy ]; then opt="-d -o ashift=9"; else opt="-o ashift=12"; fi
    zpool create -f $opt -O mountpoint=/mnt/$v sm$v $W/$v.img
    zfs create sm$v/home
    for i in $(seq 1 300); do head -c 3000 /dev/urandom | base64 > /mnt/$v/home/f$i.txt; done
    zpool sync sm$v
    zpool get -H -o property,value all sm$v | grep -E 'feature@(lz4_compress|embedded_data|large_dnode|spacemap_v2)' || true
    zpool export sm$v
    zdb -e -p $W -d sm$v | grep -E '^Dataset sm[a-z]+/home'
    echo "--- meta-dnode (object 0) of $v"
    zdb -e -p $W -dddd sm$v/home 0 | sed -n '1,12p'
    zdb -lu $W/$v.img | grep -E '^\s+txg' | sort -k3 -n | tail -1
done
