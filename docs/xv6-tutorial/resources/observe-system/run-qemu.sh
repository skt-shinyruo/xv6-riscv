#!/bin/sh

set -eu

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "usage: $0 CPUS IMAGE [GDB_PORT]" >&2
  exit 2
fi

cpus=$1
image=$2
gdb_port=${3-}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(git -C "$script_dir" rev-parse --show-toplevel)

case $image in
  /*) ;;
  *) image=$PWD/$image ;;
esac

if [ ! -f "$image" ]; then
  echo "image does not exist: $image" >&2
  exit 2
fi

if [ -n "$gdb_port" ]; then
  set -- -S -gdb "tcp::$gdb_port"
else
  set --
fi

exec qemu-system-riscv64 \
  -machine virt \
  -bios none \
  -kernel "$repo_root/kernel/kernel" \
  -m 128M \
  -smp "$cpus" \
  -nographic \
  -global virtio-mmio.force-legacy=false \
  -drive "file=$image,if=none,format=raw,id=x0" \
  -device virtio-blk-device,drive=x0,bus=virtio-mmio-bus.0 \
  "$@"
