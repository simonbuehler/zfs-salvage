# Development environment: everything for `make test` and `make test-full`, plus zdb (userland only: make-case
# works on fixture images without a ZFS kernel module). Building the fixture pools needs a kernel module or the
# test VM, which is not available inside this container.
FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-yaml gcc libc6-dev make git curl ca-certificates xz-utils \
      zfsutils-linux util-linux smartmontools \
 && rm -rf /var/lib/apt/lists/*
