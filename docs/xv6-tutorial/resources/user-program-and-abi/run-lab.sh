#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)
LAB_TMP=$(mktemp -d "${TMPDIR:-/tmp}/xv6-user-program.XXXXXX")
LAB_ROOT=$LAB_TMP/repo

cleanup()
{
  chmod -R u+w "$LAB_TMP" 2>/dev/null || true
  rm -rf -- "$LAB_TMP"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$LAB_ROOT"
BEFORE_STATUS=$(git -C "$REPO_ROOT" status --short)
if [ -f "$REPO_ROOT/fs.img" ]; then
  BEFORE_IMAGE=$(sha256sum "$REPO_ROOT/fs.img")
else
  BEFORE_IMAGE=missing
fi

git -C "$REPO_ROOT" archive --format=tar HEAD | tar -xf - -C "$LAB_ROOT"
cp "$SCRIPT_DIR/hello.c" "$LAB_ROOT/user/hello.c"

make -C "$LAB_ROOT" user/_hello
make -C "$LAB_ROOT" -B fs.img
python3 "$SCRIPT_DIR/check-qemu.py" --cwd "$LAB_ROOT" --mode missing

git -C "$LAB_ROOT" apply --unidiff-zero \
  "$SCRIPT_DIR/add-hello-to-uprogs.patch"
make -C "$LAB_ROOT" -B fs.img
python3 "$SCRIPT_DIR/check-qemu.py" \
  --cwd "$LAB_ROOT" --mode present --usertests

git -C "$LAB_ROOT" apply --unidiff-zero -R \
  "$SCRIPT_DIR/add-hello-to-uprogs.patch"
rm -f \
  "$LAB_ROOT/user/hello.c" \
  "$LAB_ROOT/user/hello.o" \
  "$LAB_ROOT/user/hello.d" \
  "$LAB_ROOT/user/_hello" \
  "$LAB_ROOT/user/hello.asm" \
  "$LAB_ROOT/user/hello.sym"
make -C "$LAB_ROOT" -B fs.img
python3 "$SCRIPT_DIR/check-qemu.py" --cwd "$LAB_ROOT" --mode missing

AFTER_STATUS=$(git -C "$REPO_ROOT" status --short)
if [ -f "$REPO_ROOT/fs.img" ]; then
  AFTER_IMAGE=$(sha256sum "$REPO_ROOT/fs.img")
else
  AFTER_IMAGE=missing
fi

if [ "$BEFORE_STATUS" != "$AFTER_STATUS" ]; then
  echo "original worktree status changed" >&2
  exit 1
fi
if [ "$BEFORE_IMAGE" != "$AFTER_IMAGE" ]; then
  echo "shared fs.img changed" >&2
  exit 1
fi

echo "hello lab passed: boundary, success, regression, cleanup"
