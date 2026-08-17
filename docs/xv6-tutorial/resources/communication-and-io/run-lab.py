#!/usr/bin/env python3

"""Verify the isolated descriptors/pipes/console communication lab."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
TUTORIAL_ROOT = REPO_ROOT / "docs" / "xv6-tutorial"
MANIFEST = TUTORIAL_ROOT / "curriculum.json"
PATCH = Path(__file__).with_name("communication.patch")
PATCH_PATHS = {
    "Makefile", "kernel/defs.h", "kernel/file.c", "kernel/ioaudit.h",
    "kernel/kalloc.c", "kernel/pipe.c", "kernel/proc.c", "kernel/syscall.c",
    "kernel/syscall.h", "kernel/sysproc.c", "user/ioflow.c", "user/user.h",
    "user/usys.pl",
}


class LabError(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise LabError(message)


def checked(command, *, cwd=None, timeout=180, env=None):
    process = subprocess.Popen(command, cwd=cwd, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stop_group(process)
        stdout, stderr = process.communicate()
        partial = (exc.stdout or "") + (exc.stderr or "") + stdout + stderr
        raise LabError(f"command watchdog expired: {' '.join(command)}\n{partial[-8000:]}") from exc
    finally:
        if process.poll() is None or process_group_exists(process.pid):
            stop_group(process)
    if process.returncode:
        raise LabError(f"command failed ({process.returncode}): {' '.join(command)}\n"
                       f"{(stdout + stderr)[-8000:]}")
    return stdout


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_or_missing(path):
    return sha256(path) if path.is_file() else "missing"


def repo_digest():
    digest = hashlib.sha256()
    for arguments in (["ls-files", "-s", "-z"],
                      ["status", "--porcelain=v1", "-z", "--untracked-files=all"]):
        result = subprocess.run(["git", *arguments], cwd=REPO_ROOT, check=True,
                                stdout=subprocess.PIPE)
        digest.update(result.stdout)
    listing = subprocess.run(
        ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"],
        cwd=REPO_ROOT, check=True, stdout=subprocess.PIPE,
    ).stdout
    for raw in sorted(set(listing.split(b"\0")) - {b""}):
        path = REPO_ROOT / os.fsdecode(raw)
        digest.update(raw + b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = (stat.S_IMODE(path.stat().st_mode), sha256(path))
    return result


def export_baseline(root, commit):
    archive = subprocess.run(["git", "archive", "--format=tar", commit], cwd=REPO_ROOT,
                             check=True, stdout=subprocess.PIPE).stdout
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        stream.extractall(root, filter="data")


def patch_paths(root):
    output = checked(["git", "apply", "--numstat", str(PATCH)], cwd=root)
    paths = set()
    for line in output.splitlines():
        fields = line.split("\t", 2)
        require(len(fields) == 3, f"bad patch numstat: {line!r}")
        paths.add(fields[2])
    return paths


def apply(root, reverse=False):
    command = ["git", "apply", "--whitespace=error-all"]
    if reverse:
        command.append("-R")
    command.append(str(PATCH))
    checked(command, cwd=root)


def static_check(root, baseline):
    require(patch_paths(root) == PATCH_PATHS,
            "fixture scope differs from the thirteen audit paths")
    source = (root / "user/ioflow.c").read_text(encoding="utf-8")
    for token in ("pipeline(base)", "empty_wakeup(base)", "full_wakeup(base)",
                  "eof_case(base)", "broken_case(base)", "killed_reader(base)",
                  "killed_writer(base)", "fd_rollback(base)", "device_case(base)",
                  "wait_for_pipe", "wait_status", "same_ledger", "print_after", "IO PASS"):
        require(token in source, f"fixture token missing: {token}")
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    require("$U/_ioflow" in makefile, "ioflow is not registered in UPROGS")
    anchors = {
        "kernel/file.c": ("filealloc(", "filedup(", "fileclose(", "fileread(", "filewrite("),
        "kernel/param.h": ("#define NOFILE", "#define NFILE"),
        "kernel/pipe.c": ("pipealloc(", "pipeclose(", "pipewrite(", "piperead("),
        "kernel/proc.c": ("sleep(", "wakeup(", "kkill(", "kwait("),
        "kernel/sysfile.c": ("argfd(", "fdalloc(", "sys_read(", "sys_write(", "sys_dup(", "sys_close(", "sys_pipe("),
        "user/sh.c": ("runcmd(", "case PIPE", "dup(p[1])", "dup(p[0])"),
        "kernel/console.c": ("consolewrite(", "consoleread(", "consoleintr("),
        "kernel/uart.c": ("uartwrite(", "uartintr("),
        "kernel/plic.c": ("plic_claim(", "plic_complete("),
        "kernel/bio.c": ("bread(", "brelse("),
        "kernel/fs.c": ("readi(",),
        "kernel/log.c": ("begin_op(", "end_op("),
        "kernel/virtio_disk.c": ("virtio_disk_rw(", "virtio_disk_intr("),
    }
    for path, tokens in anchors.items():
        text = (root / path).read_text(encoding="utf-8")
        for token in tokens:
            require(token in text, f"missing source anchor {path}:{token}")
    header = (root / "kernel/ioaudit.h").read_text(encoding="utf-8")
    for field in ("fd_slots", "active_files", "file_refs", "active_pipes",
                  "used_procs", "free_pages", "target_state", "target_chan",
                  "occupancy", "readopen", "writeopen"):
        require(re.search(rf"\b{field}\b", header),
                f"snapshot field missing: {field}")
    for path, symbol in (("kernel/file.c", "fileaudit("),
                         ("kernel/kalloc.c", "freepagecount("),
                         ("kernel/pipe.c", "pipeaudit("),
                         ("kernel/proc.c", "procaudit("),
                         ("kernel/sysproc.c", "sys_iosnapshot(")):
        require(symbol in (root / path).read_text(encoding="utf-8"),
                f"audit helper missing: {path}:{symbol}")
    checked(["make", "-j2", "kernel/kernel", "user/_ioflow"], cwd=root, timeout=300)
    return {"baseline": baseline, "paths": sorted(PATCH_PATHS), "capacity": 512}


def process_group_exists(group_id):
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    deadline = time.monotonic() + 5
    while process_group_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while process_group_exists(process.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
    require(not process_group_exists(process.pid),
            f"process group remains after cleanup: {process.pid}")


def qemu_commands(root, commands, timeout=240):
    process = subprocess.Popen(["make", "CPUS=1", "qemu"], cwd=root,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    output = bytearray()
    try:
        def wait_for(marker, start):
            deadline = time.monotonic() + timeout
            while marker not in output[start:]:
                require(time.monotonic() < deadline, "QEMU watchdog expired")
                ready, _, _ = select.select([process.stdout], [], [], 1)
                if ready:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    require(chunk, "QEMU output closed")
                    output.extend(chunk.replace(b"\r", b""))
        wait_for(b"$ ", 0)
        transcripts = []
        for command in commands:
            start = len(output)
            process.stdin.write((command + "\n").encode())
            process.stdin.flush()
            wait_for(b"$ ", start)
            transcripts.append(bytes(output[start:]).decode("utf-8", "replace"))
        process.stdin.write(b"\x01x")
        process.stdin.flush()
        process.wait(timeout=10)
        return transcripts
    finally:
        stop_group(process)


def run_driver(root, arguments, *, cpus, timeout):
    env = os.environ.copy()
    env["CPUS"] = str(cpus)
    output = checked(["python3", "test-xv6.py", *arguments], cwd=root,
                     env=env, timeout=timeout)
    require(output.count("ALL TESTS PASSED") == 1,
            f"driver success marker count changed: {' '.join(arguments)}")
    require("SOME TESTS FAILED" not in output,
            f"driver reported failure: {' '.join(arguments)}")
    require("IO " not in output,
            f"communication fixture leaked into regression: {' '.join(arguments)}")
    return output


SCHEMAS = {
    "BASE": {"fd", "files", "refs", "pipes", "procs", "free"},
    "PIPELINE": {"left", "right", "left_fd", "right_fd", "bytes", "eof", "cleanup"},
    "EMPTY": {"child", "state", "chan", "occupancy", "wake", "cleanup"},
    "FULL": {"child", "state", "chan", "occupancy", "wake", "cleanup"},
    "EOF": {"occupancy", "read", "cleanup"},
    "BROKEN": {"occupancy", "write", "cleanup"},
    "KILL_READ": {"child", "state", "chan", "result", "cleanup"},
    "KILL_WRITE": {"child", "state", "chan", "committed", "result", "cleanup"},
    "FD_ROLLBACK": {"filled", "pipe", "cleanup"},
    "DEVICE": {"read", "cleanup"},
    "PASS": {"cleanup"},
}

LEDGER_FIELDS = {"fd", "files", "refs", "pipes", "procs", "free"}


def parse_fields(tokens, phase):
    values = {}
    for token in tokens:
        require(token.count("=") == 1, f"malformed {phase} field: {token}")
        key, raw = token.split("=", 1)
        require(key not in values, f"duplicate {phase} field: {key}")
        require(re.fullmatch(r"-?[0-9]+", raw) is not None,
                f"non-integer {phase} field: {token}")
        values[key] = int(raw)
    require(set(values) == SCHEMAS[phase],
            f"{phase} fields changed: {sorted(values)}")
    return values


def parse_after(line):
    fields = line.split()
    require(len(fields) >= 4 and fields[:2] == ["IO", "AFTER"],
            f"malformed after marker: {line}")
    values = {}
    for token in fields[2:]:
        require(token.count("=") == 1, f"malformed after field: {token}")
        key, raw = token.split("=", 1)
        require(key not in values, f"duplicate after field: {key}")
        if key == "phase":
            require(raw in ("BASE", "PIPELINE", "EMPTY", "FULL", "EOF", "BROKEN",
                            "KILL_READ", "KILL_WRITE", "FD_ROLLBACK", "DEVICE", "PASS"),
                    f"unknown after phase: {raw}")
            values[key] = raw
        else:
            require(re.fullmatch(r"-?[0-9]+", raw) is not None,
                    f"non-integer after field: {token}")
            values[key] = int(raw)
    require(set(values) == {"phase", *LEDGER_FIELDS},
            f"after fields changed: {sorted(values)}")
    return values


def validate_ioflow(transcript):
    require(transcript.startswith("ioflow\n") and transcript.endswith("$ "),
            f"ioflow transcript framing changed: {transcript!r}")
    marker_lines = []
    after_lines = []
    pass_lines = []
    for line in transcript.splitlines():
        if not line.startswith("IO "):
            continue
        fields = line.split()
        if len(fields) > 1 and fields[1] == "AFTER":
            after_lines.append(line)
        elif len(fields) > 1 and fields[1] == "PASS":
            pass_lines.append(line)
        else:
            marker_lines.append(line)
    expected_order = [
        "BASE", "PIPELINE", "EMPTY", "FULL", "EOF", "BROKEN",
        "KILL_READ", "KILL_WRITE", "FD_ROLLBACK", "DEVICE",
    ]
    require(len(marker_lines) == len(expected_order),
            f"ioflow marker count changed: {marker_lines}")
    phases = {}
    for line, expected in zip(marker_lines, expected_order):
        fields = line.split()
        require(len(fields) >= 3 and fields[0] == "IO" and fields[1] == expected,
                f"ioflow phase order changed: {line}")
        phases[expected] = parse_fields(fields[2:], expected)
    require(len(pass_lines) == 1 and
            parse_fields(pass_lines[0].split()[2:], "PASS") == {"cleanup": 1},
            f"PASS marker changed: {pass_lines}")

    base = phases["BASE"]
    require(base["fd"] == 3 and base["files"] == 1 and base["refs"] == 9 and
            base["pipes"] == 0 and base["procs"] == 3 and base["free"] > 32000,
            f"unexpected baseline ledger: {base}")
    pipeline = phases["PIPELINE"]
    require(pipeline["left"] > 0 and pipeline["right"] > 0 and
            pipeline["left"] != pipeline["right"] and
            pipeline["left_fd"] == 1 and pipeline["right_fd"] == 0 and
            pipeline["bytes"] == 641 and pipeline["eof"] == 0,
            f"pipeline oracle failed: {pipeline}")
    empty = phases["EMPTY"]
    full = phases["FULL"]
    require(empty["child"] > 0 and empty["state"] == 2 and empty["chan"] == 1 and
            empty["occupancy"] == 0 and empty["wake"] == 1,
            f"empty-pipe oracle failed: {empty}")
    require(full["child"] > 0 and full["state"] == 2 and full["chan"] == 2 and
            full["occupancy"] == 512 and full["wake"] == 1,
            f"full-pipe oracle failed: {full}")
    require(phases["EOF"] == {"occupancy": 0, "read": 0, "cleanup": 1},
            f"EOF oracle failed: {phases['EOF']}")
    require(phases["BROKEN"] == {"occupancy": 0, "write": -1, "cleanup": 1},
            f"broken-end oracle failed: {phases['BROKEN']}")
    killed_read = phases["KILL_READ"]
    killed_write = phases["KILL_WRITE"]
    require(killed_read["child"] > 0 and killed_read["state"] == 2 and
            killed_read["chan"] == 1 and killed_read["result"] == -1,
            f"killed-reader oracle failed: {killed_read}")
    require(killed_write["child"] > 0 and killed_write["state"] == 2 and
            killed_write["chan"] == 2 and killed_write["committed"] == 512 and
            killed_write["result"] == -1,
            f"killed-writer oracle failed: {killed_write}")
    require(phases["FD_ROLLBACK"] == {"filled": 13, "pipe": -1, "cleanup": 1},
            f"fd rollback oracle failed: {phases['FD_ROLLBACK']}")
    require(phases["DEVICE"] == {"read": 16, "cleanup": 1},
            f"device oracle failed: {phases['DEVICE']}")
    require(all(values.get("cleanup") == 1 for phase, values in phases.items()
                if phase != "BASE"), "one scenario did not report cleanup")
    child_pids = [pipeline["left"], pipeline["right"], empty["child"], full["child"],
                  killed_read["child"], killed_write["child"]]
    require(len(set(child_pids)) == len(child_pids),
            f"scenario child identities overlap: {child_pids}")
    require(len(after_lines) == len(expected_order) + 1,
            f"after-ledger marker count changed: {after_lines}")
    after = {}
    for line in after_lines:
        value = parse_after(line)
        require(value["phase"] not in after, f"duplicate after phase: {value['phase']}")
        after[value["phase"]] = value
    require(set(after) == {*expected_order, "PASS"},
            f"after-ledger phases changed: {sorted(after)}")
    base_ledger = {key: base[key] for key in LEDGER_FIELDS}
    for phase, value in after.items():
        observed = {key: value[key] for key in LEDGER_FIELDS}
        require(observed == base_ledger,
                f"{phase} after-ledger did not return to BASE: {observed} != {base_ledger}")
    return phases


def transcript_sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def run_report(path, tutorial_commit, static, dynamic_runs, focused, quick, full,
               before, before_image, fixture_hash, runner_hash):
    lines = [
        "# 通信与设备 I/O 证据报告包", "",
        f"- 教程提交：`{tutorial_commit}`",
        f"- pinned baseline：`{static['baseline']}`",
        f"- fixture：`{fixture_hash}`",
        f"- runner：`{runner_hash}`",
        "- 隔离：临时源码导出、私有 fs.img、ioflow/focused CPUS=1、quick CPUS=2、full CPUS=1、独立 QEMU 进程组", "",
        "## S/F/B/C oracle", "",
        "- S：patch 只包含十三个 audit paths；descriptor/file/pipe/console/UART/PLIC/VirtIO anchors 均存在。",
        "- F/B/C：host 解析并复算 BASE、各阶段原始 after-ledger、pipeline、empty/full waiter、EOF、broken-end、两种 killed waiter、fd rollback、inode read 和 PASS 的完整字段集合；第 513 字节 writer 与 empty reader 先被观察为 SLEEPING。",
        f"- ioflow run 1：`{dynamic_runs[0].strip()}`",
        f"- ioflow run 2：`{dynamic_runs[1].strip()}`",
        "", "## Focused/related/quick/full", "",
    ]
    for item, transcript in focused.items():
        lines.append(f"- `{item}`：SHA-256 `{transcript_sha(transcript)}`，一次 `ALL TESTS PASSED`。")
    lines.extend([
        f"- quick CPUS=2：SHA-256 `{transcript_sha(quick)}`。",
        f"- full CPUS=1：SHA-256 `{transcript_sha(full)}`。",
    ])
    lines.extend([
        "", "## Cleanup and limits", "",
        "- 命令结果：两次 `ioflow`、6 个 focused、quick、full 均 exit=0 且各自只出现一次 `ALL TESTS PASSED`；patch reverse 与 make clean 后临时导出 snapshot 与 baseline 一致。",
        "- 资源账本：每个 `IO AFTER` 的 fd/files/refs/pipes/procs/free 与 BASE 完全相等；所有 child wait、endpoint close 和 process groups 均清理。",
        f"- 工作树状态：`{before}`；共享 fs.img：`{before_image}`。",
        "- R=N/A；不宣称调度公平性、所有交错、DMA ordering 或 crash recovery。", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    require(PATCH.is_file(), "missing communication.patch")
    if not args.static_only:
        require(shutil.which("qemu-system-riscv64"), "qemu-system-riscv64 is required")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    baseline = manifest["release"]["baseline_commit"]
    tutorial_commit = checked(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT).strip()
    before = checked(["git", "status", "--short"], cwd=REPO_ROOT).strip() or "clean"
    before_digest = repo_digest()
    before_image = digest_or_missing(REPO_ROOT / "fs.img")
    with tempfile.TemporaryDirectory(prefix="xv6-communication-lab.") as temp:
        root = Path(temp) / "repo"
        export_baseline(root, baseline)
        original = snapshot(root)
        checked(["git", "apply", "--check", str(PATCH)], cwd=root)
        apply(root)
        static = static_check(root, baseline)
        print("static communication passed: fixture scope, anchors, build, capacity=512", flush=True)
        if args.static_only:
            checked(["make", "clean"], cwd=root, timeout=120)
            apply(root, reverse=True)
            require(snapshot(root) == original,
                    "static-only fixture did not reverse to the pinned baseline")
            require(repo_digest() == before_digest,
                    "shared repository changed during static-only run")
            require(digest_or_missing(REPO_ROOT / "fs.img") == before_image,
                    "shared fs.img changed during static-only run")
            return
        checked(["make", "-j2", "fs.img"], cwd=root, timeout=300)
        focused_commands = (
            "usertests pipe1", "usertests preempt", "usertests killstatus",
            "usertests sharedfd", "usertests copyout", "usertests exectest",
        )
        transcripts = qemu_commands(root, ("ioflow", "ioflow", *focused_commands),
                                    timeout=300)
        dynamic_runs = transcripts[:2]
        for run in dynamic_runs:
            validate_ioflow(run)
        print("dynamic communication passed twice: normal/full/EOF/broken/killed/cleanup", flush=True)
        focused = dict(zip(focused_commands, transcripts[2:]))
        for command, transcript in focused.items():
            require(transcript.count("ALL TESTS PASSED") == 1 and
                    "SOME TESTS FAILED" not in transcript and "IO " not in transcript,
                    f"focused regression failed: {command}: {transcript[-4000:]}")
            print(f"focused regression passed: {command}", flush=True)
        quick = run_driver(root, ["-q", "usertests"], cpus=2, timeout=360)
        print("quick regression passed: CPUS=2", flush=True)
        full = run_driver(root, ["usertests"], cpus=1, timeout=720)
        print("full regression passed: CPUS=1", flush=True)
        checked(["make", "clean"], cwd=root, timeout=120)
        apply(root, reverse=True)
        require(snapshot(root) == original, "temporary source did not restore exactly")
    require(repo_digest() == before_digest, "shared repository changed during isolated run")
    require(digest_or_missing(REPO_ROOT / "fs.img") == before_image,
            "shared fs.img changed during isolated run")
    report = args.report.expanduser().resolve() if args.report else None
    if report:
        require(not report.is_relative_to(REPO_ROOT), "report must be outside repository")
        run_report(report, tutorial_commit, static, dynamic_runs, focused, quick, full,
                   before, before_image, sha256(PATCH), sha256(Path(__file__)))


if __name__ == "__main__":
    try:
        main()
    except (LabError, subprocess.SubprocessError, OSError) as exc:
        print(f"communication lab failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
