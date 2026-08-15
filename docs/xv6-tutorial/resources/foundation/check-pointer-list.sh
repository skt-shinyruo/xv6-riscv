#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM

cc -std=c99 -Wall -Wextra -Werror \
  "$script_dir/pointer-list.c" -o "$work_dir/pointer-list"

actual=$($work_dir/pointer-list)
expected='count=2 sum=12 active=2 pinned=1'

if [ "$actual" != "$expected" ]; then
  echo "expected: $expected" >&2
  echo "actual:   $actual" >&2
  exit 1
fi

echo "pointer-list check passed"
