# QEMU/KVM host for throwaway test VMs (see run.sh). Nothing is installed on the developer's machine.
FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      qemu-system-x86 qemu-utils cloud-image-utils ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
