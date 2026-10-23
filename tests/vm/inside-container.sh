#!/bin/bash
# Started by run.sh inside the Docker container: boots the VM, runs the script via cloud-init, waits.
set -euo pipefail
SCRIPT="$1"; OWNER="$2"; shift 2
ARGS="$*"

qemu-img create -q -f qcow2 -b /cache/base.img -F qcow2 /tmp/disk.qcow2 16G
cat > /tmp/user-data <<EOF
#cloud-config
runcmd:
  - [sh, -c, "mkdir -p /mnt/work && mount -t 9p -o trans=virtio,version=9p2000.L,msize=1048576 work /mnt/work"]
  - [sh, -c, "DEBIAN_FRONTEND=noninteractive apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q zfsutils-linux xz-utils python3 > /mnt/work/tests/vm/apt.log 2>&1"]
  - [sh, -c, "cd /mnt/work && bash $SCRIPT $ARGS > /mnt/work/tests/vm/run.log 2>&1; echo \$? > /mnt/work/tests/vm/exit-code"]
  - [poweroff]
EOF
cloud-localds /tmp/seed.img /tmp/user-data
timeout 7200 qemu-system-x86_64 -enable-kvm -cpu host -m 6144 -smp 4 -nographic \
    -drive file=/tmp/disk.qcow2,if=virtio -drive file=/tmp/seed.img,if=virtio,format=raw \
    -virtfs local,path=/work,mount_tag=work,security_model=none,id=work \
    -nic user,model=virtio-net-pci > /work/tests/vm/console.log 2>&1 || true
# files written in the VM belong to root here; give them back to the developer
chown -R "$OWNER" /work/tests 2>/dev/null || true
