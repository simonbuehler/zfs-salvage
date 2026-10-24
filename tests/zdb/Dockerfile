# zdb 2.2.2 (as on the Ubuntu 24.04 live image) plus python3, to run make-case.py on fixture images
# without a ZFS kernel module (zdb works on image files in userspace).
FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends zfsutils-linux python3 util-linux \
 && rm -rf /var/lib/apt/lists/*
