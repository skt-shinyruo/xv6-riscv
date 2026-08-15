#!/usr/bin/env python3

import argparse
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


def normalized(data):
    return data.replace(b"\r", b"")


def wait_for(proc, output, marker, start, timeout, count=1):
    deadline = time.monotonic() + timeout
    while normalized(output[start:]).count(marker) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            tail = normalized(output)[-4000:].decode("utf-8", "replace")
            raise RuntimeError(f"timed out waiting for {marker!r}\n--- QEMU tail ---\n{tail}")
        ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
        if not ready:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"QEMU exited with {proc.returncode} before {marker!r}"
                )
            continue
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            raise RuntimeError(f"QEMU output closed before {marker!r}")
        output.extend(chunk)
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()


def send_and_wait(proc, output, command, marker, timeout=60, count=1):
    start = len(output)
    proc.stdin.write(command.encode("ascii") + b"\n")
    proc.stdin.flush()
    wait_for(proc, output, marker, start, timeout, count)
    wait_for(proc, output, b"$ ", start, timeout)
    return bytes(normalized(output[start:]))


def require_exact(actual, expected):
    if actual != expected:
        raise RuntimeError(
            "unexpected command transcript\n"
            f"expected: {expected!r}\n"
            f"actual:   {actual!r}"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--mode", choices=("missing", "present"), required=True)
    parser.add_argument("--usertests", action="store_true")
    args = parser.parse_args()

    proc = subprocess.Popen(
        ["make", "CPUS=1", "qemu"],
        cwd=args.cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output = bytearray()
    try:
        wait_for(proc, output, b"$ ", 0, 60)
        if args.mode == "missing":
            marker = b"\nexec hello failed\n"
            expected = b"hello\nexec hello failed\n$ "
        else:
            marker = b"\nhello-from-xv6\n"
            expected = b"hello\nhello-from-xv6\n$ "
        hello_output = send_and_wait(proc, output, "hello", marker)
        require_exact(hello_output, expected)
        echo_output = send_and_wait(
            proc, output, "echo abi-regression", b"\nabi-regression\n"
        )
        require_exact(
            echo_output,
            b"echo abi-regression\nabi-regression\n$ ",
        )
        if args.usertests:
            send_and_wait(
                proc,
                output,
                "usertests",
                b"ALL TESTS PASSED",
                timeout=600,
            )
        proc.stdin.write(b"\x01x")
        proc.stdin.flush()
        proc.wait(timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(f"QEMU exited with {proc.returncode}")
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

    print(f"qemu {args.mode} check passed")


if __name__ == "__main__":
    main()
