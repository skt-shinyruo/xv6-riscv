#!/usr/bin/env python3
"""Isolated oracle and publication runner for the device-I/O learning unit."""

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


RESOURCE_DIR = Path(__file__).resolve().parent
REPO_ROOT = RESOURCE_DIR.parents[3]
MANIFEST = REPO_ROOT / "docs/xv6-tutorial/curriculum.json"
PATCH = RESOURCE_DIR / "device-audit.patch"
PATCH_PATHS = {
    "Makefile", "kernel/bio.c", "kernel/console.c", "kernel/devaudit.c",
    "kernel/devaudit.h", "kernel/plic.c", "kernel/proc.c", "kernel/syscall.c",
    "kernel/syscall.h", "kernel/sysproc.c", "kernel/uart.c",
    "kernel/virtio_disk.c",
    "user/devtrace.c", "user/user.h", "user/usys.pl",
}


class LabError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise LabError(message)


SCHEMAS = {
    "CONSOLE_WAIT": {"generation", "pid", "state", "chan", "seq"},
    "CONSOLE": {
        "generation", "pid", "irq", "wait_seq", "rx_seq", "wake_seq",
        "done_seq", "claims", "completes", "claim_harts", "complete_harts",
        "wait_chan", "wake_chan", "rx", "first", "last", "read",
        "ring_before", "ring_after", "publish_lock", "condition_before",
        "proc_after", "condition_after", "wake_proc_lock", "lock_errors",
        "status", "cleanup",
    },
    "DISK": {
        "generation", "pid", "block", "sector", "submit_hart", "head",
        "d0", "d1", "d2", "d0_addr", "d0_len", "d0_flags", "d0_next",
        "d1_addr", "d1_len", "d1_flags", "d1_next", "d2_addr", "d2_len",
        "d2_flags", "queue_desc", "queue_avail", "queue_used", "queue_ready",
        "free_before", "free_programmed", "free_after", "avail_before",
        "avail_after", "used_before", "used_after", "notify", "b_owner",
        "device_status", "wait_chan", "wake_chan", "program_seq", "publish_seq",
        "notify_seq", "sleep_seq", "irq_seq", "complete_seq", "wake_seq",
        "reclaim_seq", "irq", "claims", "completes", "claim_harts",
        "complete_harts", "info_after", "read", "status", "cleanup",
        "program_lock", "publish_lock", "notify_lock", "complete_lock",
        "reclaim_lock", "condition_before", "proc_after", "condition_after",
        "wake_proc_lock", "lock_errors",
    },
    "QUEUE": {
        "generation", "p0", "p1", "p2", "block0", "block1", "block2",
        "free_before", "free_held", "free_after", "deferred_held",
        "deferred_after", "third_pid", "third_state", "third_chan", "desc_chan",
        "avail_before", "avail_after", "used_before", "used_after",
        "submit0_seq", "submit1_seq", "defer0_seq", "defer1_seq", "wait_seq",
        "release_seq", "reclaim_seq", "irq", "claims", "completes",
        "claim_harts", "complete_harts", "controller_hart", "status0", "status1",
        "status2", "info_after", "cleanup",
        "program_lock", "publish_lock", "notify_lock", "complete_lock",
        "reclaim_lock", "condition_before", "proc_after", "condition_after",
        "wake_proc_lock", "lock_errors",
    },
    "AFTER": {
        "phase", "active", "waiters", "deferred", "free_desc", "info",
        "buf_refs",
    },
    "PASS": {"cases", "cleanup"},
}


def parse_fields(tokens, phase):
    values = {}
    for token in tokens:
        require(token.count("=") == 1, f"malformed {phase} field: {token}")
        key, raw = token.split("=", 1)
        require(key not in values, f"duplicate {phase} field: {key}")
        if key == "phase":
            values[key] = raw
        else:
            require(re.fullmatch(r"-?[0-9]+", raw) is not None,
                    f"non-integer {phase} field: {token}")
            values[key] = int(raw)
    require(set(values) == SCHEMAS[phase],
            f"{phase} fields changed: {sorted(values)}")
    return values


def one_marker(text, phase):
    matches = []
    for line in text.splitlines():
        prefix = f"DEV {phase} "
        if line.startswith(prefix):
            matches.append(parse_fields(line[len(prefix):].split(), phase))
    require(len(matches) == 1, f"expected one {phase} marker, got {len(matches)}")
    return matches[0]


def marker_order(text):
    names = []
    for line in text.splitlines():
        if line.startswith("DEV "):
            fields = line.split()
            require(len(fields) >= 2, f"malformed DEV marker: {line!r}")
            names.append(fields[1])
    return names


def clean_after(text, phase):
    afters = []
    for line in text.splitlines():
        if line.startswith("DEV AFTER "):
            afters.append(parse_fields(line[len("DEV AFTER "):].split(), "AFTER"))
    require(len(afters) == 1 and afters[0]["phase"] == phase,
            f"missing {phase} cleanup marker: {afters}")
    require({key: afters[0][key] for key in
             ("active", "waiters", "deferred", "free_desc", "info", "buf_refs")} ==
            {"active": 0, "waiters": 0, "deferred": 0, "free_desc": 8,
             "info": 0, "buf_refs": 0},
            f"{phase} resources not clean: {afters[0]}")


def validate_devtrace(console_text, disk_text, queue_text):
    console_order = marker_order(console_text)
    disk_order = marker_order(disk_text)
    queue_order = marker_order(queue_text)
    require(console_order == ["CONSOLE_WAIT", "CONSOLE", "AFTER"],
            f"console DEV marker stream is not closed or ordered: {console_order}")
    require(disk_order == ["DISK", "AFTER"],
            f"disk DEV marker stream is not closed or ordered: {disk_order}")
    require(queue_order == ["QUEUE", "AFTER", "PASS"],
            f"queue DEV marker stream is not closed or ordered: {queue_order}\n"
            f"{queue_text[-3000:]}")
    wait = one_marker(console_text, "CONSOLE_WAIT")
    console = one_marker(console_text, "CONSOLE")
    require(wait["generation"] == console["generation"] == 1,
            "console generation changed")
    require(wait["pid"] == console["pid"] > 0 and wait["state"] == 2,
            "console reader was not the observed sleeping process")
    require(wait["chan"] == console["wait_chan"] == console["wake_chan"] != 0,
            "console wait/wakeup channel identity changed")
    require(wait["seq"] == console["wait_seq"] and
            0 < console["wait_seq"] < console["rx_seq"] < console["wake_seq"] <
            console["done_seq"], "console event order changed")
    require(console["irq"] == 10 and console["claims"] >= 1 and
            console["claims"] == console["completes"] and
            console["claim_harts"] == console["complete_harts"],
            "UART PLIC claim/complete ledger changed")
    require(0 < console["claim_harts"] < 4 and
            0 < console["complete_harts"] < 4,
            "console hart mask is outside CPUS=2")
    require(console["rx"] == console["read"] == 10 and
            console["first"] == 68 and console["last"] == 10 and
            console["ring_after"] - console["ring_before"] == 10,
            "console input bytes/ring ledger changed")
    require({key: console[key] for key in
             ("publish_lock", "condition_before", "proc_after",
              "wake_proc_lock")} ==
            {"publish_lock": 1, "condition_before": 1, "proc_after": 1,
             "wake_proc_lock": 1} and
            console["condition_after"] == 0 and console["lock_errors"] == 0,
            "console lock handoff context changed")
    require(console["status"] == 0 and console["cleanup"] == 1,
            "console caller did not complete cleanly")
    clean_after(console_text, "CONSOLE")

    disk = one_marker(disk_text, "DISK")
    require(disk["generation"] == 2 and disk["pid"] > 0 and
            disk["block"] == 1999 and disk["sector"] == 3998,
            "disk trigger identity changed")
    require(disk["submit_hart"] in (0, 1), "disk submit hart outside CPUS=2")
    descriptors = (disk["d0"], disk["d1"], disk["d2"])
    require(disk["head"] == disk["d0"] and len(set(descriptors)) == 3 and
            all(0 <= index < 8 for index in descriptors),
            "disk descriptor chain identities changed")
    require(disk["d0_next"] == disk["d1"] and disk["d1_next"] == disk["d2"] and
            disk["d0_len"] == 16 and disk["d1_len"] == 1024 and
            disk["d2_len"] == 1 and disk["d0_flags"] == 1 and
            disk["d1_flags"] == 3 and disk["d2_flags"] == 2,
            "disk descriptor shape changed")
    require(all(disk[key] > 0 for key in
                ("d0_addr", "d1_addr", "d2_addr", "queue_desc", "queue_avail",
                 "queue_used")) and
            all(disk[key] % 4096 == 0 for key in
                ("queue_desc", "queue_avail", "queue_used")),
            "DMA-visible queue/address ledger changed")
    require(disk["queue_ready"] == 1 and disk["free_before"] == 8 and
            disk["free_programmed"] == 5 and disk["free_after"] == 8,
            "single-request descriptor ledger changed")
    require(disk["avail_after"] == disk["avail_before"] + 1 and
            disk["used_after"] == disk["used_before"] + 1 and
            disk["notify"] == 1 and disk["b_owner"] == 1,
            "disk publication/completion index changed")
    require(disk["device_status"] == 0 and disk["wait_chan"] ==
            disk["wake_chan"] != 0, "disk completion/channel changed")
    require({key: disk[key] for key in
             ("program_lock", "publish_lock", "notify_lock", "complete_lock",
              "reclaim_lock", "condition_before", "proc_after",
              "wake_proc_lock")} ==
            {"program_lock": 1, "publish_lock": 1, "notify_lock": 1,
             "complete_lock": 1, "reclaim_lock": 1, "condition_before": 1,
             "proc_after": 1, "wake_proc_lock": 1} and
            disk["condition_after"] == 0 and disk["lock_errors"] == 0,
            "disk lock handoff context changed")
    program = disk["program_seq"]
    publish = disk["publish_seq"]
    notify = disk["notify_seq"]
    sleep = disk["sleep_seq"]
    irq = disk["irq_seq"]
    complete = disk["complete_seq"]
    wake = disk["wake_seq"]
    reclaim = disk["reclaim_seq"]
    require(program < publish < notify and notify < sleep < wake < reclaim and
            notify < irq < complete < wake,
            "disk ownership partial order changed: "
            f"{[program, publish, notify, sleep, irq, complete, wake, reclaim]}")
    disk_seq = [program, publish, notify, sleep, irq, complete, wake, reclaim]
    require(len(set(disk_seq)) == len(disk_seq),
            "disk event sequence is not unique")
    require(disk["irq"] == 1 and disk["claims"] >= 1 and
            disk["claims"] == disk["completes"] and
            disk["claim_harts"] == disk["complete_harts"] and
            0 < disk["claim_harts"] < 4 and 0 < disk["complete_harts"] < 4,
            "VirtIO PLIC ledger changed")
    require(disk["info_after"] == 0 and 0 <= disk["read"] <= 255 and
            disk["status"] == 0 and disk["cleanup"] == 1,
            "disk caller/reclaim result changed")
    clean_after(disk_text, "DISK")

    queue = one_marker(queue_text, "QUEUE")
    pids = (queue["p0"], queue["p1"], queue["p2"])
    require(queue["generation"] == 3 and all(pid > 0 for pid in pids) and
            len(set(pids)) == 3, "queue worker identities changed")
    require((queue["block0"], queue["block1"], queue["block2"]) ==
            (1996, 1997, 1998), "queue block trigger changed")
    require(queue["free_before"] == 8 and queue["free_held"] == 2 and
            queue["free_after"] == 8 and queue["deferred_held"] == 2 and
            queue["deferred_after"] == 0, "queue capacity ledger changed")
    require(queue["third_pid"] in pids and queue["third_state"] == 2 and
            queue["third_chan"] == queue["desc_chan"] != 0,
            "third request did not sleep on descriptor capacity")
    require(queue["avail_after"] == queue["avail_before"] + 3 and
            queue["used_after"] == queue["used_before"] + 3,
            "queue ring delta changed")
    submit_seq = sorted((queue["submit0_seq"], queue["submit1_seq"]))
    deferred_seq = sorted((queue["defer0_seq"], queue["defer1_seq"]))
    wait_seq = queue["wait_seq"]
    before_release = submit_seq + deferred_seq + [wait_seq]
    require(all(submit < deferred for submit, deferred in
                zip(submit_seq, deferred_seq)) and
            max(deferred_seq) < wait_seq < queue["release_seq"] <
            queue["reclaim_seq"] and all(seq > 0 for seq in before_release),
            "queue submit/defer/wait/release order changed")
    all_queue_seq = before_release + [queue["release_seq"], queue["reclaim_seq"]]
    require(len(set(all_queue_seq)) == len(all_queue_seq),
            "queue event sequence is not unique")
    require({key: queue[key] for key in
             ("program_lock", "publish_lock", "notify_lock", "complete_lock",
              "reclaim_lock", "condition_before", "proc_after",
              "wake_proc_lock")} ==
            {"program_lock": 1, "publish_lock": 1, "notify_lock": 1,
             "complete_lock": 1, "reclaim_lock": 1, "condition_before": 1,
             "proc_after": 1, "wake_proc_lock": 1} and
            queue["condition_after"] == 0 and queue["lock_errors"] == 0,
            "queue lock handoff context changed")
    require(queue["irq"] == 1 and queue["claims"] >= 1 and
            queue["claims"] == queue["completes"] and
            queue["claim_harts"] == queue["complete_harts"] and
            0 < queue["claim_harts"] < 4 and 0 < queue["complete_harts"] < 4 and
            queue["controller_hart"] in (0, 1), "queue hart/PLIC ledger changed")
    require((queue["status0"], queue["status1"], queue["status2"]) == (0, 0, 0) and
            queue["info_after"] == 0 and queue["cleanup"] == 1,
            "queue workers or info slots did not clean up")
    clean_after(queue_text, "QUEUE")
    passed = one_marker(queue_text, "PASS")
    require(passed == {"cases": 3, "cleanup": 1}, "device PASS marker changed")
    return {"console": console, "disk": disk, "queue": queue}


def self_test():
    console = (
        "DEV CONSOLE_WAIT generation=1 pid=4 state=2 chan=4096 seq=1\n"
        "DEV CONSOLE generation=1 pid=4 irq=10 wait_seq=1 rx_seq=2 "
        "wake_seq=3 done_seq=4 claims=1 completes=1 claim_harts=1 "
        "complete_harts=1 wait_chan=4096 wake_chan=4096 rx=10 first=68 "
        "last=10 read=10 ring_before=0 ring_after=10 publish_lock=1 "
        "condition_before=1 proc_after=1 condition_after=0 wake_proc_lock=1 "
        "lock_errors=0 status=0 cleanup=1\n"
        "DEV AFTER phase=CONSOLE active=0 waiters=0 deferred=0 free_desc=8 "
        "info=0 buf_refs=0\n"
    )
    disk = (
        "DEV DISK generation=2 pid=5 block=1999 sector=3998 submit_hart=0 "
        "head=0 d0=0 d1=1 d2=2 d0_addr=8192 d0_len=16 d0_flags=1 "
        "d0_next=1 d1_addr=12288 d1_len=1024 d1_flags=3 d1_next=2 "
        "d2_addr=16384 d2_len=1 d2_flags=2 queue_desc=20480 "
        "queue_avail=24576 queue_used=28672 queue_ready=1 free_before=8 "
        "free_programmed=5 free_after=8 avail_before=7 avail_after=8 "
        "used_before=7 used_after=8 notify=1 b_owner=1 device_status=0 "
        "wait_chan=32768 wake_chan=32768 program_seq=10 publish_seq=11 "
        "notify_seq=12 sleep_seq=13 irq_seq=14 complete_seq=15 wake_seq=16 "
        "reclaim_seq=17 irq=1 claims=1 completes=1 claim_harts=2 "
        "complete_harts=2 info_after=0 read=0 program_lock=1 publish_lock=1 "
        "notify_lock=1 complete_lock=1 reclaim_lock=1 condition_before=1 "
        "proc_after=1 condition_after=0 wake_proc_lock=1 lock_errors=0 "
        "status=0 cleanup=1\n"
        "DEV AFTER phase=DISK active=0 waiters=0 deferred=0 free_desc=8 "
        "info=0 buf_refs=0\n"
    )
    queue = (
        "DEV QUEUE generation=3 p0=6 p1=7 p2=8 block0=1996 block1=1997 "
        "block2=1998 free_before=8 free_held=2 free_after=8 deferred_held=2 "
        "deferred_after=0 third_pid=8 third_state=2 third_chan=40960 "
        "desc_chan=40960 avail_before=8 avail_after=11 used_before=8 "
        "used_after=11 submit0_seq=20 submit1_seq=21 defer0_seq=22 "
        "defer1_seq=23 wait_seq=24 release_seq=25 reclaim_seq=30 irq=1 "
        "claims=2 completes=2 claim_harts=3 complete_harts=3 "
        "controller_hart=0 status0=0 status1=0 status2=0 info_after=0 "
        "program_lock=1 publish_lock=1 notify_lock=1 complete_lock=1 "
        "reclaim_lock=1 condition_before=1 proc_after=1 condition_after=0 "
        "wake_proc_lock=1 lock_errors=0 cleanup=1\n"
        "DEV AFTER phase=QUEUE active=0 waiters=0 deferred=0 free_desc=8 "
        "info=0 buf_refs=0\n"
        "DEV PASS cases=3 cleanup=1\n"
    )
    validate_devtrace(console, disk, queue)
    mutations = (
        (console.replace(" cleanup=1", " forged=1 cleanup=1", 1), disk, queue),
        (console.replace("wake_chan=4096", "wake_chan=4097"), disk, queue),
        (console, disk.replace("free_programmed=5", "free_programmed=6"), queue),
        (console, disk.replace("wake_seq=16", "wake_seq=14"), queue),
        (console, disk, queue.replace("free_held=2", "free_held=3")),
        (console, disk, queue.replace("release_seq=25", "release_seq=22")),
        (console, disk, queue.replace("cases=3", "cases=2")),
        (console + "DEV FAIL appended\n", disk, queue),
        (console + "DEV PASS cases=3 cleanup=1\n", disk, queue),
        (console, disk.replace("complete_harts=2", "complete_harts=1"), queue),
        (console, disk.replace("sleep_seq=13", "sleep_seq=14"), queue),
        (console, disk, queue.replace("buf_refs=0", "buf_refs=1", 1)),
        (console.replace("publish_lock=1", "publish_lock=0"), disk, queue),
        (console.replace("pid=4 state=2", "pid=4 pid=4 state=2", 1), disk, queue),
        (console.replace("ring_before=0 ", "", 1), disk, queue),
        (console.replace("state=2", "state=two", 1), disk, queue),
        (console, disk,
         queue.replace("submit0_seq=20", "submit0_seq=22").replace(
             "defer0_seq=22", "defer0_seq=20")),
        (console, disk,
         queue.replace("submit1_seq=21", "submit1_seq=23").replace(
             "defer1_seq=23", "defer1_seq=21")),
        (console, disk, queue.replace("wait_seq=24", "wait_seq=19")),
    )
    for index, candidate in enumerate(mutations, 1):
        try:
            validate_devtrace(*candidate)
        except LabError:
            continue
        raise LabError(f"oracle mutation {index} was accepted")
    sampled = set(repo_file_names())
    for resource in (PATCH, RESOURCE_DIR / "run-lab.py"):
        relative = resource.relative_to(REPO_ROOT).as_posix()
        require(relative in sampled,
                f"repository state sampling omitted {relative}")
    print(f"device oracle self-test passed: good trace accepted, "
          f"{len(mutations)} mutations rejected")


def process_group_exists(group_id):
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_group(process):
    if process is None:
        return
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
    require(not process_group_exists(process.pid),
            f"process group remains after cleanup: {process.pid}")


def checked(command, *, cwd, timeout=300, env=None):
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
        if process.returncode != 0:
            raise LabError(
                f"command failed ({process.returncode}): {' '.join(command)}\n"
                f"{output[-8000:]}")
        return output
    except subprocess.TimeoutExpired as exc:
        raise LabError(f"command watchdog expired: {' '.join(command)}") from exc
    finally:
        stop_group(process)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_or_missing(path):
    return sha256(path) if path.is_file() else "missing"


def transcript_sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def tool_environment():
    compilers = ("riscv64-unknown-elf-gcc", "riscv64-linux-gnu-gcc")
    compiler = next((name for name in compilers if shutil.which(name)), None)
    require(compiler is not None, "RISC-V compiler is required")
    uname = os.uname()
    return {
        "host": f"{uname.sysname} {uname.release} {uname.machine}",
        "python": sys.version.split()[0],
        "make": checked(["make", "--version"], cwd=REPO_ROOT).splitlines()[0],
        "compiler": checked([compiler, "--version"], cwd=REPO_ROOT).splitlines()[0],
        "qemu": checked(["qemu-system-riscv64", "--version"],
                        cwd=REPO_ROOT).splitlines()[0],
    }


def repo_file_names():
    visible = checked(
        ["git", "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"], cwd=REPO_ROOT).split("\0")
    ignored = checked(
        ["git", "ls-files", "-z", "--others", "--ignored",
         "--exclude-standard"], cwd=REPO_ROOT).split("\0")
    return sorted(set(item for item in visible + ignored if item))


def repo_state():
    digest = hashlib.sha256()
    index = checked(["git", "ls-files", "--stage", "-z"], cwd=REPO_ROOT)
    digest.update(b"INDEX\0")
    digest.update(index.encode())
    digest.update(b"\0FILES\0")
    for name in repo_file_names():
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(str(stat.S_IMODE(path.stat().st_mode)).encode())
        digest.update(b"\0")
        digest.update(sha256(path).encode())
        digest.update(b"\0")
    status = checked(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--ignored"],
        cwd=REPO_ROOT)
    digest.update(status.encode())
    return digest.hexdigest(), status.strip() or "clean"


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = (
                stat.S_IMODE(path.stat().st_mode), sha256(path))
    return result


def export_baseline(root, commit):
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit], cwd=REPO_ROOT,
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


def apply_fixture(root, reverse=False):
    command = ["git", "apply", "--whitespace=error-all"]
    if reverse:
        command.append("--reverse")
    command.append(str(PATCH))
    checked(command, cwd=root)


def static_check(root, manifest, baseline):
    self_test()
    require(patch_paths(root) == PATCH_PATHS,
            "device fixture scope differs from the fifteen declared paths")
    units = {unit["id"]: unit for unit in manifest["units"]}
    require("core.device-io" in units, "manifest is missing core.device-io")
    unit = units["core.device-io"]
    require(unit["requires"] == ["core.communication-and-io"],
            "device unit requires edge changed")
    require(set(unit["evidence_dimensions"]) == {"S", "F", "B", "C"},
            "device unit evidence dimensions changed")
    for anchor in unit["source_anchors"]:
        path = root / anchor["path"]
        require(path.is_file(), f"source anchor path missing: {anchor['path']}")
        require(anchor["symbol"] in path.read_text(encoding="utf-8"),
                f"source anchor missing: {anchor['path']}:{anchor['symbol']}")
    source = (root / "user/devtrace.c").read_text(encoding="utf-8")
    for token in ("console_case(", "disk_case(", "queue_case(",
                  "DEV CONSOLE_WAIT", "DEV DISK", "DEV QUEUE", "DEV PASS",
                  "condition_before", "lock_errors"):
        require(token in source, f"guest oracle token missing: {token}")
    driver = (root / "kernel/virtio_disk.c").read_text(encoding="utf-8")
    for token in ("devaudit_disk_program", "devaudit_disk_publish",
                  "devaudit_disk_notify", "devaudit_disk_complete",
                  "virtio_disk_audit_release", "virtio_disk_audit_ledger"):
        require(token in driver, f"driver audit seam missing: {token}")
    require("bioauditrefs" in (root / "kernel/bio.c").read_text(encoding="utf-8"),
            "buffer reference ledger seam missing")
    checked(["make", "-j2", "kernel/kernel", "user/_devtrace"], cwd=root,
            timeout=300)
    return {"baseline": baseline, "paths": sorted(PATCH_PATHS)}


class Qemu:
    def __init__(self, root, timeout=240):
        self.timeout = timeout
        self.output = bytearray()
        self.process = subprocess.Popen(
            ["make", "CPUS=2", "qemu"], cwd=root, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            self.wait_for(b"$ ", 0)
        except Exception:
            stop_group(self.process)
            raise

    def wait_for(self, marker, start):
        deadline = time.monotonic() + self.timeout
        while marker not in self.output[start:]:
            require(time.monotonic() < deadline,
                    f"QEMU watchdog expired waiting for {marker!r}")
            ready, _, _ = select.select([self.process.stdout], [], [], 1)
            if ready:
                chunk = os.read(self.process.stdout.fileno(), 4096)
                require(chunk, "QEMU output closed")
                self.output.extend(chunk.replace(b"\r", b""))

    def write(self, data):
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def console_trace(self):
        start = len(self.output)
        self.write(b"devtrace console\n")
        self.wait_for(b" seq=1\n", start)
        self.write(b"D14-input\n")
        self.wait_for(b"$ ", start)
        return bytes(self.output[start:]).decode("utf-8", "replace")

    def command(self, command):
        start = len(self.output)
        self.write((command + "\n").encode())
        self.wait_for(b"$ ", start)
        return bytes(self.output[start:]).decode("utf-8", "replace")

    def close(self):
        if self.process.poll() is None:
            try:
                self.write(b"\x01x")
                self.process.wait(timeout=10)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                pass
        stop_group(self.process)


def assert_regression(command, transcript):
    require(transcript.count("ALL TESTS PASSED") == 1 and
            "SOME TESTS FAILED" not in transcript and
            "DEV " not in transcript,
            f"focused regression failed: {command}\n{transcript[-5000:]}")


def run_driver(root, arguments, *, cpus, timeout):
    env = os.environ.copy()
    env["CPUS"] = str(cpus)
    output = checked(["python3", "test-xv6.py", *arguments], cwd=root,
                     env=env, timeout=timeout)
    require(output.count("ALL TESTS PASSED") == 1 and
            "SOME TESTS FAILED" not in output and "DEV " not in output,
            f"driver regression failed: {' '.join(arguments)}")
    return output


def write_report(path, *, tutorial_commit, static, traces, focused, quick, full,
                 before_digest, before_status, before_image, fixture_hash,
                 runner_hash, environment):
    relations = validate_devtrace(*traces)
    lines = [
        "# 设备中断与 VirtIO 队列机器证据附录", "",
        f"- 教程提交：`{tutorial_commit}`",
        f"- pinned baseline：`{static['baseline']}`",
        f"- fixture SHA-256：`{fixture_hash}`",
        f"- runner SHA-256：`{runner_hash}`",
        "- 本附录 SHA-256：由 tracked review record 在生成后外部记录",
        f"- host：`{environment['host']}`；Python：`{environment['python']}`",
        f"- make：`{environment['make']}`",
        f"- compiler：`{environment['compiler']}`",
        f"- QEMU：`{environment['qemu']}`",
        "- 隔离：临时源码导出、私有 fs.img、CPUS=2 设备 trace、独立进程组", "",
        "## S/F/B/C/R", "",
        "- S：manifest anchors 与 15-path fixture scope 由 runner 从 pinned export 复核。",
        "- F：console 外部输入和单个只读磁盘请求均从 caller 追到 PLIC/driver/wakeup/reclaim。",
        "- B：两笔完成被确定性延后，第三笔请求在 2 个空闲 descriptor 时睡眠；释放后 3 笔完成。",
        "- C：CPUS=2；记录 submit/IRQ/controller hart mask 与 gate event order。",
        "- R：N/A；本单元不声称 host persistence、DMA formal ordering 或 crash recovery。", "",
        "## Host-recomputed relations", "",
        f"- console：`{relations['console']}`",
        f"- disk：`{relations['disk']}`",
        f"- queue：`{relations['queue']}`", "",
        "## Embedded worksheet", "",
        "| slice | trigger | owner / lock / channel | completion | cleanup |",
        "| --- | --- | --- | --- | --- |",
        "| console | host 注入 `D14-input\\n` | `cons.lock -> p->lock`；`&cons.r` | IRQ10 claim/RX/wake/read/complete | AFTER ledger |",
        "| disk | block 1999 cache miss | `vdisk_lock -> p->lock`；`b` | program/publish/notify/IRQ1/reclaim | AFTER ledger |",
        "| queue | blocks 1996..1998 | `vdisk_lock -> p->lock`；`&disk.free[0]` | two deferred + release + three reclaim | AFTER ledger |", "",
        "## Raw transcripts", "",
    ]
    for title, transcript in zip(("console", "disk", "queue"), traces):
        lines.extend([f"### {title}", "", "```text", transcript.rstrip(), "```", ""])
    lines.extend(["## Focused, related, quick, full", ""])
    for command, transcript in focused.items():
        lines.append(f"- `{command}`：exit=0；SHA-256 `{transcript_sha(transcript)}`。")
    lines.extend([
        f"- quick CPUS=2：exit=0；SHA-256 `{transcript_sha(quick)}`。",
        f"- full CPUS=1：exit=0；SHA-256 `{transcript_sha(full)}`。", "",
        "## Cleanup and limitations", "",
        "- 三个 audit 场景、4 个 focused、quick、full 均 exit=0；fixture reverse 与 make clean 后源码 snapshot 精确回到 baseline。",
        "- 每个 `DEV AFTER` 都是 active=0、waiters=0、deferred=0、free_desc=8、info=0、buf_refs=0。",
        f"- 共享工作树 digest：`{before_digest}`；状态：`{before_status}`；共享 fs.img：`{before_image}`。",
        "- 私有 buffer cache 可保留 valid data，但所有 buffer refcnt 已归零；event gate 与 audit hook 会改变时序；地址只在本次 boot 内可比较。",
        "- 测试不证明所有交错、设备固件实现、跨设备 DMA 内存模型、写回或持久化。", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    require(PATCH.is_file(), "missing device-audit.patch")
    if not args.static_only:
        require(shutil.which("qemu-system-riscv64"),
                "qemu-system-riscv64 is required")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    baseline = manifest["release"]["baseline_commit"]
    tutorial_commit = checked(["git", "rev-parse", "HEAD"],
                              cwd=REPO_ROOT).strip()
    before_digest, before_status = repo_state()
    before_image = digest_or_missing(REPO_ROOT / "fs.img")
    environment = None if args.static_only else tool_environment()
    traces = None
    focused = None
    quick = None
    full = None
    with tempfile.TemporaryDirectory(prefix="xv6-device-io.") as temp:
        root = Path(temp) / "repo"
        export_baseline(root, baseline)
        original = snapshot(root)
        checked(["git", "apply", "--check", "--whitespace=error-all", str(PATCH)],
                cwd=root)
        apply_fixture(root)
        static = static_check(root, manifest, baseline)
        print("static device I/O passed: manifest, fixture scope, anchors, build, oracle",
              flush=True)
        if args.static_only:
            checked(["make", "clean"], cwd=root, timeout=120)
            apply_fixture(root, reverse=True)
            require(snapshot(root) == original,
                    "static-only fixture did not reverse to baseline")
            after_digest, _ = repo_state()
            require(after_digest == before_digest,
                    "shared repository changed during static-only run")
            require(digest_or_missing(REPO_ROOT / "fs.img") == before_image,
                    "shared fs.img changed during static-only run")
            return
        checked(["make", "-j2", "fs.img"], cwd=root, timeout=300)
        qemu = None
        try:
            qemu = Qemu(root)
            console = qemu.console_trace()
            disk = qemu.command("devtrace disk")
            queue = qemu.command("devtrace queue")
            traces = (console, disk, queue)
            validate_devtrace(*traces)
            print("dynamic device trace passed: console, disk, queue boundary, cleanup",
                  flush=True)
            commands = (
                "usertests pipe1", "usertests writebig",
                "usertests bigfile", "usertests manywrites",
            )
            focused = {}
            for command in commands:
                transcript = qemu.command(command)
                assert_regression(command, transcript)
                focused[command] = transcript
                print(f"focused regression passed: {command}", flush=True)
        finally:
            if qemu is not None:
                qemu.close()
        quick = run_driver(root, ["-q", "usertests"], cpus=2, timeout=420)
        print("quick regression passed: CPUS=2", flush=True)
        full = run_driver(root, ["usertests"], cpus=1, timeout=780)
        print("full regression passed: CPUS=1", flush=True)
        checked(["make", "clean"], cwd=root, timeout=120)
        apply_fixture(root, reverse=True)
        require(snapshot(root) == original,
                "temporary source did not restore exactly")
    after_digest, _ = repo_state()
    require(after_digest == before_digest,
            "shared repository content, mode, or index changed during run")
    require(digest_or_missing(REPO_ROOT / "fs.img") == before_image,
            "shared fs.img changed during isolated run")
    if args.report:
        report = args.report.expanduser().resolve()
        require(not report.is_relative_to(REPO_ROOT),
                "report must be outside the repository")
        write_report(
            report, tutorial_commit=tutorial_commit, static=static, traces=traces,
            focused=focused, quick=quick, full=full,
            before_digest=before_digest, before_status=before_status,
            before_image=before_image,
            fixture_hash=sha256(PATCH), runner_hash=sha256(Path(__file__)),
            environment=environment)
    print("device I/O passed: static, F/B/C, focused, quick, full, cleanup")


if __name__ == "__main__":
    try:
        main()
    except (LabError, OSError, subprocess.SubprocessError) as exc:
        print(f"device I/O lab failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
