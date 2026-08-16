#!/usr/bin/env python3

"""Run the isolated scheduling, synchronization, and lost-wakeup project."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import selectors
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
PATCH = Path(__file__).with_name("syncproject.patch")
PATCH_PATHS = {
    "Makefile",
    "kernel/defs.h",
    "kernel/proc.c",
    "kernel/syncproject.c",
    "kernel/syncproject.h",
    "kernel/syscall.c",
    "kernel/syscall.h",
    "kernel/sysproc.c",
    "user/schedtrace.c",
    "user/user.h",
    "user/usys.pl",
}
SOURCE_PATHS = PATCH_PATHS | {
    "kernel/pipe.c",
    "kernel/proc.h",
    "kernel/riscv.h",
    "kernel/sleeplock.c",
    "kernel/spinlock.c",
    "kernel/spinlock.h",
    "kernel/swtch.S",
    "kernel/trap.c",
    "user/usertests.c",
}

FIXED_ORDER = [
    "PRODUCER_ARMED",
    "WAIT_CHECK",
    "WAIT_PLOCK",
    "PRODUCER_ATTEMPT",
    "WAIT_RELEASE_BOUNDARY",
    "PRODUCER_ACQUIRE",
    "WAIT_PUBLISH",
    "PRODUCER_READY",
    "WAKE_MATCH",
    "WAKE_DONE",
    "ORIGIN_SKIP",
    "MIGRATE_RELEASE",
    "MIGRATE_SELECT",
    "WAIT_RESUME",
    "WAIT_RECHECK",
]
BROKEN_ORDER = [
    "PRODUCER_ARMED",
    "WAIT_CHECK",
    "WAIT_RELEASED",
    "PRODUCER_ATTEMPT",
    "PRODUCER_ACQUIRE",
    "PRODUCER_READY",
    "WAKE_MISS",
    "WAKE_DONE",
    "WAIT_PUBLISH",
    "BAD_ORACLE",
    "RESCUE_BEGIN",
    "RESCUE_MATCH",
    "RESCUE_DONE",
    "ORIGIN_SKIP",
    "MIGRATE_RELEASE",
    "MIGRATE_SELECT",
    "WAIT_RESUME",
    "WAIT_RECHECK",
]


class LabError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise LabError(message)


def require_in_order(source: str, tokens, context: str) -> None:
    offset = 0
    for token in tokens:
        position = source.find(token, offset)
        require(position >= 0, f"{context} token missing or reordered: {token}")
        offset = position + len(token)


def checked(command, *, cwd=None, timeout=180, env=None) -> str:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stdout = ""
    stderr = ""
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stop_process_group(process)
        stdout, stderr = process.communicate()
        partial_stdout = exc.stdout or ""
        partial_stderr = exc.stderr or ""
        if isinstance(partial_stdout, bytes):
            partial_stdout = partial_stdout.decode("utf-8", "replace")
        if isinstance(partial_stderr, bytes):
            partial_stderr = partial_stderr.decode("utf-8", "replace")
        output = (partial_stdout + partial_stderr + stdout + stderr)[-12000:]
        raise LabError(
            f"command watchdog expired: {' '.join(command)}\n{output}"
        ) from exc
    finally:
        if process.poll() is None or process_group_exists(process.pid):
            stop_process_group(process)
    if process.returncode != 0:
        output = (stdout + stderr)[-12000:]
        raise LabError(
            f"command failed ({process.returncode}): {' '.join(command)}\n{output}"
        )
    return stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_or_missing(path: Path) -> str:
    return sha256(path) if path.is_file() else "missing"


def git_output_bytes(arguments) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise LabError(
            f"git {' '.join(arguments)} failed: "
            f"{result.stderr.decode('utf-8', 'replace')[-4000:]}"
        )
    return result.stdout


def repo_state_digest() -> str:
    digest = hashlib.sha256()
    for label, arguments in (
        (b"index", ["ls-files", "-s", "-z"]),
        (b"status", ["status", "--porcelain=v1", "-z", "--untracked-files=all"]),
    ):
        digest.update(label + b"\0" + git_output_bytes(arguments))

    listing = git_output_bytes(
        ["ls-files", "-c", "-o", "--exclude-standard", "-z"]
    )
    for raw_path in sorted(set(listing.split(b"\0")) - {b""}):
        path = REPO_ROOT / os.fsdecode(raw_path)
        digest.update(b"path\0" + raw_path + b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(f"file:{stat.S_IMODE(path.stat().st_mode):o}\0".encode())
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"missing\0")
    return digest.hexdigest()


def load_baseline() -> str:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return data["release"]["baseline_commit"]


def export_baseline(root: Path, baseline: str) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", baseline],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        stream.extractall(root, filter="data")


def snapshot(root: Path):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = (
                stat.S_IMODE(path.stat().st_mode),
                sha256(path),
            )
    return result


def patch_paths(root: Path):
    output = checked(
        ["git", "apply", "--numstat", "--unidiff-zero", str(PATCH)], cwd=root
    )
    paths = set()
    for line in output.splitlines():
        fields = line.split("\t", 2)
        require(len(fields) == 3, f"unexpected patch numstat: {line!r}")
        paths.add(fields[2])
    return paths


def apply_patch(root: Path, *, reverse=False) -> None:
    command = ["git", "apply", "--whitespace=error-all", "--unidiff-zero"]
    if reverse:
        command.append("-R")
    command.append(str(PATCH))
    checked(command, cwd=root, timeout=60)


def read_sources(root: Path):
    return {
        path: (root / path).read_text(encoding="utf-8") for path in SOURCE_PATHS
    }


def function_body(source: str, signature: str, end_marker: str) -> str:
    start = source.index(signature)
    end = source.index(end_marker, start)
    return source[start:end]


def analyze_sources(root: Path) -> None:
    require(patch_paths(root) == PATCH_PATHS, "syncproject patch path scope changed")
    sources = read_sources(root)

    makefile = sources["Makefile"]
    require("$K/syncproject.o" in makefile and "$U/_schedtrace" in makefile,
            "syncproject build registrations are missing")
    require("$U/usys.S :" in makefile, "generated syscall stub rule is missing")

    proc_header = sources["kernel/proc.h"]
    require_in_order(
        proc_header,
        ("struct context", "uint64 ra", "uint64 sp", "uint64 s0", "uint64 s11",
         "struct cpu", "struct proc *proc", "int noff", "int intena"),
        "context/cpu declarations",
    )
    require_in_order(
        proc_header,
        ("UNUSED", "USED", "SLEEPING", "RUNNABLE", "RUNNING", "ZOMBIE"),
        "process states",
    )
    swtch = sources["kernel/swtch.S"]
    expected_registers = {"ra", "sp", *[f"s{i}" for i in range(12)]}
    for register in expected_registers:
        require(re.search(rf"\bsd\s+{register},", swtch),
                f"swtch save missing: {register}")
        require(re.search(rf"\bld\s+{register},", swtch),
                f"swtch restore missing: {register}")
    saved_registers = set(re.findall(r"^\s*sd\s+(\w+),", swtch, re.MULTILINE))
    loaded_registers = set(re.findall(r"^\s*ld\s+(\w+),", swtch, re.MULTILINE))
    require(saved_registers == loaded_registers == expected_registers,
            "swtch register save/restore set changed")
    require("sret" not in swtch and "satp" not in swtch,
            "swtch unexpectedly crosses privilege/page-table boundary")

    spinlock = sources["kernel/spinlock.c"]
    acquire_body = function_body(spinlock, "\nacquire(", "\n// Release")
    require_in_order(
        acquire_body,
        ("push_off()", "holding(lk)", "__atomic_exchange_n", "__ATOMIC_ACQUIRE",
         "lk->cpu = mycpu()"),
        "spinlock acquire",
    )
    release_body = function_body(spinlock, "\nrelease(", "\n// Check")
    require_in_order(
        release_body,
        ("holding(lk)", "lk->cpu = 0", "__atomic_store_n", "__ATOMIC_RELEASE",
         "pop_off()"),
        "spinlock release",
    )
    require_in_order(
        spinlock,
        ("push_off(void)", "int old = intr_get()", "intr_off()",
         "mycpu()->intena = old", "mycpu()->noff += 1", "pop_off(void)",
         "c->noff", "intr_on()"),
        "nested interrupt state",
    )

    proc = sources["kernel/proc.c"]
    scheduler = function_body(proc, "\nscheduler(void)", "\n// Switch to scheduler")
    require_in_order(
        scheduler,
        ("intr_on()", "intr_off()", "acquire(&p->lock)",
         "p->state == RUNNABLE", "syncproject_scheduler_skip(p)",
         "syncproject_scheduler_select(p)", "p->state = RUNNING", "c->proc = p",
         "swtch(&c->context, &p->context)", "c->proc = 0", "release(&p->lock)"),
        "scheduler lock transfer",
    )
    sched = function_body(proc, "\nsched(void)", "\n// Give up")
    for token in (
        "holding(&p->lock)", "mycpu()->noff != 1", "p->state == RUNNING",
        "intr_get()", "intena = mycpu()->intena",
        "swtch(&p->context, &mycpu()->context)", "mycpu()->intena = intena",
    ):
        require(token in sched, f"sched precondition/transfer missing: {token}")
    yield_body = function_body(proc, "\nyield(void)", "\n// A fork")
    require_in_order(
        yield_body,
        ("acquire(&p->lock)", "p->state = RUNNABLE", "sched()",
         "release(&p->lock)"),
        "yield transition",
    )
    sleep_body = function_body(proc, "\nsleep(void *chan", "\n// Wake up")
    require_in_order(
        sleep_body,
        ("acquire(&p->lock)", "syncproject_sleep_before_release", "release(lk)",
         "syncproject_sleep_after_release", "p->chan = chan",
         "p->state = SLEEPING", "syncproject_sleep_published", "sched()",
         "syncproject_sleep_resumed", "p->chan = 0", "release(&p->lock)",
         "acquire(lk)"),
        "sleep ownership handoff",
    )
    wakeup_body = function_body(proc, "\nwakeup(void *chan)", "\n// Kill")
    require_in_order(
        wakeup_body,
        ("acquire(&p->lock)", "p->state == SLEEPING && p->chan == chan",
         "syncproject_wakeup_scan", "p->state = RUNNABLE", "release(&p->lock)"),
        "wakeup publication",
    )

    trap = sources["kernel/trap.c"]
    require(trap.count("if (which_dev == 2)") == 1 and
            "if (which_dev == 2 && myproc() != 0)" in trap,
            "user/kernel timer preemption branches changed")
    require(trap.count("yield();") == 2, "timer yield call count changed")
    prepare_return = function_body(trap, "\nprepare_return(void)", "\n// interrupts")
    require("p->trapframe->kernel_hartid = r_tp()" in prepare_return,
            "prepare_return no longer publishes the current hart id")
    pipe = sources["kernel/pipe.c"]
    require_in_order(
        pipe,
        ("acquire(&pi->lock)", "pi->nread == pi->nwrite && pi->writeopen",
         "sleep(&pi->nread, &pi->lock)", "wakeup(&pi->nwrite)",
         "release(&pi->lock)"),
        "pipe condition protocol",
    )
    sleeplock = sources["kernel/sleeplock.c"]
    require_in_order(
        sleeplock,
        ("while (lk->locked)", "sleep(lk, &lk->lk)", "lk->locked = 1",
         "wakeup(lk)"),
        "sleep-lock condition loop",
    )

    header = sources["kernel/syncproject.h"]
    for field in (
        "generation", "target_state", "target_chan_match", "condition_locked",
        "gate_pending", "wake_misses", "wake_matches", "origin_hart",
        "resume_hart", "migrated", "condition_owner_hart", "proc_owner_hart",
    ):
        require(re.search(rf"\b{field}\b", header), f"trace field missing: {field}")
    require("#define SYNC_MAX_EVENTS 32" in header, "trace bound changed")

    project = sources["kernel/syncproject.c"]
    require_in_order(
        project,
        ("target_event(struct proc", "aload(&project.active)",
         "p->pid == aload(&project.target_pid)", "chan == &project.channel"),
        "targeted hook predicate",
    )
    require("__atomic_fetch_add(&project.next_event" in project and
            "__atomic_store_n(&event->valid, 1, __ATOMIC_RELEASE)" in project and
            "__atomic_load_n(&project.events[i].valid, __ATOMIC_ACQUIRE)" in project,
            "bounded event publication changed")
    before_release = function_body(
        project, "\nsyncproject_sleep_before_release", "\nvoid\nsyncproject_sleep_after_release"
    )
    require_in_order(
        before_release,
        ("SYNC_EVENT_WAIT_PLOCK", "project.wait_plock_ready, 1",
         "while (!aload(&project.producer_attempt))",
         "SYNC_EVENT_WAIT_RELEASE_BOUNDARY"),
        "fixed pre-release checkpoint",
    )
    after_release = function_body(
        project, "\nsyncproject_sleep_after_release", "\nvoid\nsyncproject_sleep_published"
    )
    require_in_order(
        after_release,
        ("project.condition_released, 1",
         "while (!aload(&project.producer_acquired))"),
        "fixed post-release handoff",
    )
    broken = function_body(project, "\nbroken_sleep(void)", "\nint\nsyncprojectwait")
    require_in_order(
        broken,
        ("push_off()", "release(&project.condition)", "SYNC_EVENT_WAIT_RELEASED",
         "while (!aload(&project.wake_done))", "acquire(&p->lock)",
         "pop_off()", "p->chan = &project.channel", "p->state = SLEEPING",
         "sched()"),
        "broken lost-wakeup mutation",
    )
    producer = function_body(project, "\nsyncprojectproduce(void)",
                             "\nstatic int\nbad_state_recorded")
    require_in_order(
        producer,
        ("push_off()", "SYNC_EVENT_PRODUCER_ARMED", "project.producer_started",
         "SYNC_EVENT_PRODUCER_ATTEMPT", "acquire(&project.condition)",
         "SYNC_EVENT_PRODUCER_ACQUIRE", "project.ready, 1",
         "SYNC_EVENT_PRODUCER_READY", "wakeup(&project.channel)",
         "SYNC_EVENT_WAKE_DONE", "project.origin_skip_seen",
         "SYNC_EVENT_MIGRATE_RELEASE", "project.resume_allowed, 1",
         "release(&project.condition)", "pop_off()"),
        "two-hart producer control",
    )
    bad = function_body(project, "\nbad_state_recorded(void)",
                        "\nint\nsyncprojectrescue")
    for token in (
        "p->state == SLEEPING", "p->chan == &project.channel",
        "aload(&project.ready) == 1", "aload(&project.wake_misses) == 1",
        "p->killed == 0", "SYNC_EVENT_BAD_ORACLE",
    ):
        require(token in bad, f"bad-state oracle missing: {token}")
    rescue = function_body(project, "\nsyncprojectrescue(void)",
                           "\nvoid\nsyncprojectsnapshot")
    require_in_order(
        rescue,
        ("bad_state_recorded()", "project.rescuing, 1", "SYNC_EVENT_RESCUE_BEGIN",
         "wakeup(&project.channel)", "SYNC_EVENT_RESCUE_DONE",
         "project.origin_skip_seen", "SYNC_EVENT_MIGRATE_RELEASE",
         "project.resume_allowed, 1"),
        "post-oracle rescue",
    )
    scheduler_gate = function_body(project, "\nsyncproject_scheduler_skip",
                                   "\nvoid\nsyncproject_scheduler_select")
    require("cpuid() == aload(&project.origin_hart)" in scheduler_gate and
            "!aload(&project.origin_skip_seen) || !aload(&project.resume_allowed)" in
            scheduler_gate,
            "migration gate no longer waits for origin skip and release")

    syscall_h = sources["kernel/syscall.h"]
    syscall_c = sources["kernel/syscall.c"]
    sysproc = sources["kernel/sysproc.c"]
    user_h = sources["user/user.h"]
    usys = sources["user/usys.pl"]
    require("#define SYS_syncproject 22" in syscall_h,
            "syncproject syscall number changed")
    require("[SYS_syncproject] sys_syncproject" in syscall_c,
            "syncproject dispatch slot missing")
    for operation in ("RESET", "WAIT", "PRODUCE", "SNAPSHOT", "RESCUE", "CLEAR"):
        require(f"SYNC_OP_{operation}" in sysproc,
                f"syncproject syscall operation missing: {operation}")
    require("int syncproject(int, int, struct syncsnapshot *);" in user_h and
            'entry("syncproject");' in usys,
            "syncproject user ABI generation chain missing")

    guest = sources["user/schedtrace.c"]
    require_in_order(
        guest,
        ("SYNC_OP_RESET", "baseline_used = snapshot.used", "fork()",
         "SYNC_OP_PRODUCE", "snapshot.producer_started", "fork()",
         "SYNC_OP_WAIT", "snapshot.target_state == 2",
         "snapshot.target_chan_match", "snapshot.wake_misses == 1",
         "SYNC BADSTATE", "SYNC_OP_RESCUE", "wait_child()", "dump_events",
         "SYNC FINAL", "SYNC_OP_CLEAR", "SYNC CLEAN", "SYNC PASS"),
        "guest trigger/oracle/cleanup flow",
    )
    require("uptime() + 50" in guest and "fail(\"bad-state watchdog\")" in guest,
            "guest watchdog boundary missing")
    require("snapshot.target_killed == 0" in guest,
            "guest rescue gate no longer requires an un-killed target")


def build(root: Path) -> None:
    checked(
        ["make", "-j2", "CPUS=2", "kernel/kernel", "user/_schedtrace", "fs.img"],
        cwd=root,
        timeout=360,
    )


def process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_group(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
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
            f"process group remains: {process.pid}")


class Qemu:
    def __init__(self, root: Path, *, cpus: int):
        env = os.environ.copy()
        env["CPUS"] = str(cpus)
        self.proc = subprocess.Popen(
            ["make", "qemu"],
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.output = bytearray()
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        try:
            self.read_until(b"$ ", 90)
        except BaseException:
            self.stop()
            raise

    def read_until(self, marker: bytes, timeout: float, *, start=0) -> None:
        deadline = time.monotonic() + timeout
        while marker not in self.output[start:]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LabError(f"QEMU watchdog waiting for {marker!r}: {self.tail()}")
            events = self.selector.select(remaining)
            if not events:
                raise LabError(f"QEMU watchdog waiting for {marker!r}: {self.tail()}")
            chunk = os.read(self.proc.stdout.fileno(), 4096)
            if not chunk:
                raise LabError(f"QEMU closed output: {self.tail()}")
            self.output.extend(chunk.replace(b"\r", b""))

    def command(self, command: str, *, timeout=180) -> str:
        start = len(self.output)
        self.proc.stdin.write((command + "\n").encode())
        self.proc.stdin.flush()
        self.read_until(b"$ ", timeout, start=start)
        return bytes(self.output[start:]).decode("utf-8", "replace")

    def tail(self) -> str:
        return bytes(self.output[-8000:]).decode("utf-8", "replace")

    def stop(self) -> None:
        self.selector.close()
        stop_process_group(self.proc)


def run_driver(root: Path, args, *, cpus: int, timeout: int) -> str:
    env = os.environ.copy()
    env["CPUS"] = str(cpus)
    process = subprocess.Popen(
        ["python3", "test-xv6.py", *args],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    output = ""
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stop_process_group(process)
        raise LabError(
            f"driver watchdog expired: {' '.join(args)}\n{output[-10000:]}"
        ) from exc
    finally:
        if process.poll() is None or process_group_exists(process.pid):
            stop_process_group(process)
    require(process.returncode == 0,
            f"driver failed: {' '.join(args)}\n{output[-10000:]}")
    require(output.count("ALL TESTS PASSED") == 1,
            f"driver success marker count changed: {' '.join(args)}")
    require("SOME TESTS FAILED" not in output,
            f"driver reported failure: {' '.join(args)}")
    require("SYNC " not in output,
            f"syncproject marker leaked into regression: {' '.join(args)}")
    return output


def parse_fields(line: str):
    values = {}
    for key, value in re.findall(r"([a-z_]+)=([^\s]+)", line):
        try:
            values[key] = int(value, 0)
        except ValueError:
            values[key] = value
    return values


def marker_lines(output: str, prefix: str):
    return [line.strip() for line in output.splitlines() if line.startswith(prefix)]


def event_map(events):
    return {event["kind"]: event for event in events}


def validate_common(mode: str, output: str, expected_order):
    require("SYNC FAIL" not in output, f"guest rejected {mode} scenario")
    event_lines = marker_lines(output, "SYNC EVENT ")
    events = [parse_fields(line) for line in event_lines]
    require([event.get("kind") for event in events] == expected_order,
            f"{mode} event order changed: {[event.get('kind') for event in events]}")
    require([event.get("seq") for event in events] == list(range(len(events))),
            f"{mode} event sequence is not contiguous")
    generations = {event.get("gen") for event in events}
    require(len(generations) == 1, f"{mode} mixed generations: {generations}")
    require(all(event.get("mode") == mode for event in events),
            f"{mode} event label mismatch")

    final_lines = marker_lines(output, "SYNC FINAL ")
    clean_lines = marker_lines(output, "SYNC CLEAN ")
    pass_lines = marker_lines(output, "SYNC PASS ")
    require(len(final_lines) == len(clean_lines) == len(pass_lines) == 1,
            f"{mode} final/clean/pass marker count changed")
    final = parse_fields(final_lines[0])
    clean = parse_fields(clean_lines[0])
    require(final.get("mode") == mode and clean.get("mode") == mode,
            f"{mode} summary label changed")
    require(final.get("gen") in generations and clean.get("gen") in generations,
            f"{mode} summary generation changed")
    require(final.get("used_before") == final.get("used_after") and
            final.get("used_before", 0) >= 3,
            f"{mode} process ledger did not recover")
    require(final.get("target") == 0 and final.get("ready") == 1 and
            final.get("cond") == 0 and final.get("migrated") == 1,
            f"{mode} final state/lock/migration relation failed")
    require(final.get("origin") in (0, 1) and final.get("resume") in (0, 1) and
            final.get("origin") != final.get("resume"),
            f"{mode} did not resume on the other hart")
    require(final.get("events") == len(events), f"{mode} trace count changed")
    for key in ("active", "target", "ready", "cond", "trace", "gates"):
        require(clean.get(key) == 0, f"{mode} cleanup retained {key}")
    require(clean.get("used") == clean.get("baseline") == final.get("used_before"),
            f"{mode} cleanup process ledger changed")
    return (
        event_lines,
        events,
        final,
        final_lines[0],
        clean_lines[0],
        pass_lines[0],
    )


def validate_fixed(output: str):
    lines, events, final, final_line, clean_line, pass_line = validate_common(
        "fixed", output, FIXED_ORDER
    )
    by_kind = event_map(events)
    check = by_kind["WAIT_CHECK"]
    plock = by_kind["WAIT_PLOCK"]
    attempt = by_kind["PRODUCER_ATTEMPT"]
    released = by_kind["WAIT_RELEASE_BOUNDARY"]
    acquired = by_kind["PRODUCER_ACQUIRE"]
    published = by_kind["WAIT_PUBLISH"]
    ready = by_kind["PRODUCER_READY"]
    match = by_kind["WAKE_MATCH"]
    skipped = by_kind["ORIGIN_SKIP"]
    selected = by_kind["MIGRATE_SELECT"]
    resumed = by_kind["WAIT_RESUME"]
    recheck = by_kind["WAIT_RECHECK"]

    require(check["state"] == 4 and check["ready"] == 0 and
            check["cond"] == 1 and check["cond_owner"] == check["hart"] and
            check["noff"] == 1,
            "fixed waiter did not check under the condition lock")
    require(plock["cond_owner"] == plock["hart"] == plock["proc_owner"] and
            plock["plock"] == 1 and plock["noff"] == 2,
            "fixed waiter did not hold lk and p->lock together")
    require(attempt["hart"] != plock["hart"] and
            attempt["cond_owner"] == plock["hart"],
            "fixed producer was not blocked behind waiter condition ownership")
    require(released["proc_owner"] == plock["hart"] and
            released["cond_owner"] == plock["hart"] and released["cond"] == 1,
            "fixed release boundary lost waiter ownership before release")
    require(acquired["hart"] == acquired["cond_owner"] == attempt["hart"] and
            acquired["noff"] == 2,
            "fixed producer condition-lock acquisition is inconsistent")
    require(published["state"] == 2 and published["chan"] == 1 and
            published["ready"] == 0 and published["proc_owner"] == plock["hart"],
            "fixed waiter did not publish SLEEPING before predicate update")
    require(ready["ready"] == 1 and ready["cond_owner"] == attempt["hart"],
            "fixed producer did not update predicate under lk")
    require(match["state"] == 2 and match["chan"] == 1 and
            match["proc_owner"] == match["hart"] == attempt["hart"],
            "fixed wakeup did not match under target p->lock")
    require(skipped["state"] == 3 and skipped["hart"] == final["origin"],
            "fixed origin scheduler did not skip RUNNABLE target")
    require(selected["state"] == 3 and selected["hart"] == final["resume"],
            "fixed other hart did not select RUNNABLE target")
    require(resumed["state"] == 4 and resumed["chan"] == 1 and
            resumed["hart"] == resumed["proc_owner"] == final["resume"],
            "fixed waiter resume ownership changed")
    require(recheck["ready"] == 1 and recheck["chan"] == 0 and
            recheck["cond_owner"] == recheck["hart"],
            "fixed waiter did not clear channel and recheck under lk")
    require(final["miss"] == 0 and final["match"] == 1 and final["rescue"] == 0,
            "fixed wake accounting changed")
    require(not marker_lines(output, "SYNC BADSTATE "),
            "fixed scenario unexpectedly reported a bad state")
    return lines, final, final_line, clean_line, pass_line


def validate_broken(output: str):
    lines, events, final, final_line, clean_line, pass_line = validate_common(
        "broken", output, BROKEN_ORDER
    )
    by_kind = event_map(events)
    released = by_kind["WAIT_RELEASED"]
    miss = by_kind["WAKE_MISS"]
    wake_done = by_kind["WAKE_DONE"]
    published = by_kind["WAIT_PUBLISH"]
    oracle = by_kind["BAD_ORACLE"]
    rescue = by_kind["RESCUE_MATCH"]
    selected = by_kind["MIGRATE_SELECT"]
    resumed = by_kind["WAIT_RESUME"]

    require(released["cond"] == 0 and released["plock"] == 0 and
            released["intr"] == 0 and released["noff"] == 1,
            "broken window did not release lk while pinning its hart")
    require(miss["state"] == 4 and miss["ready"] == 1 and miss["chan"] == 0 and
            miss["proc_owner"] == miss["hart"],
            "broken wake did not scan the still-RUNNING target")
    require(wake_done["seq"] < published["seq"] and
            published["state"] == 2 and published["ready"] == 1 and
            published["chan"] == 1,
            "broken waiter did not publish only after the wake completed")
    require(oracle["state"] == 2 and oracle["ready"] == 1 and oracle["chan"] == 1,
            "broken active bad-state oracle changed")
    bad_lines = marker_lines(output, "SYNC BADSTATE ")
    require(len(bad_lines) == 1, "broken bad-state marker count changed")
    bad = parse_fields(bad_lines[0])
    require(bad.get("ready") == 1 and bad.get("state") == 2 and
            bad.get("chan") == 1 and bad.get("miss") == 1 and
            bad.get("killed") == 0,
            "broken snapshot is not ready=1/SLEEPING/expected-chan/wake-miss")
    require(rescue["state"] == 2 and rescue["chan"] == 1,
            "rescue did not target the recorded sleeper")
    require(selected["hart"] == final["resume"] != final["origin"] and
            resumed["hart"] == final["resume"],
            "broken cleanup did not restore on the other hart")
    require(final["miss"] == 1 and final["match"] == 0 and final["rescue"] == 1,
            "broken wake/rescue accounting changed")
    return lines, final, final_line, clean_line, pass_line, bad_lines[0]


def validate_regression(command: str, transcript: str) -> None:
    require(transcript.count("ALL TESTS PASSED") == 1,
            f"regression marker count changed: {command}")
    require("SOME TESTS FAILED" not in transcript,
            f"regression reported failure: {command}")
    require("SYNC " not in transcript,
            f"syncproject marker leaked into regression: {command}")


def run_dynamic(root: Path):
    records = {"cases": []}
    qemu = None
    try:
        qemu = Qemu(root, cpus=2)
        for command in ("usertests preempt", "usertests pipe1", "usertests killstatus"):
            transcript = qemu.command(command, timeout=300)
            validate_regression(command, transcript)
            records[command] = transcript
        generations = []
        for mode in ("fixed", "broken", "fixed", "broken"):
            transcript = qemu.command(f"schedtrace {mode}", timeout=180)
            if mode == "fixed":
                trace, final, final_line, clean_line, pass_line = validate_fixed(
                    transcript
                )
                bad_state = None
            else:
                (
                    trace,
                    final,
                    final_line,
                    clean_line,
                    pass_line,
                    bad_state,
                ) = validate_broken(transcript)
            generations.append(final["gen"])
            records["cases"].append({
                "mode": mode,
                "trace": trace,
                "final": final,
                "final_line": final_line,
                "clean_line": clean_line,
                "pass_line": pass_line,
                "bad_state": bad_state,
            })
        require(generations == list(range(generations[0], generations[0] + 4)),
                f"generation/reset sequence changed: {generations}")
    finally:
        if qemu is not None:
            qemu.stop()
    records["quick"] = run_driver(root, ["-q", "usertests"], cpus=2, timeout=420)
    records["full"] = run_driver(root, ["usertests"], cpus=1, timeout=780)
    return records


def write_report(
    path: Path,
    *,
    baseline: str,
    tutorial_commit: str,
    patch_digest: str,
    runner_digest: str,
    environment: str,
    shared_state_digest: str,
    shared_image_digest: str,
    records,
) -> None:
    event_meanings = {
        "PRODUCER_ARMED": "producer 已固定 hart 并关中断，随后才发布启动 gate",
        "WAIT_CHECK": "waiter 持 lk 检查 ready=0",
        "WAIT_PLOCK": "fixed waiter 同时持 lk 与 p->lock",
        "PRODUCER_ATTEMPT": "另一 hart 的 producer 开始竞争 lk",
        "WAIT_RELEASE_BOUNDARY": "fixed waiter 在实际 release(lk) 前采样",
        "WAIT_RELEASED": "broken waiter 已释放 lk，尚未取得 p->lock",
        "PRODUCER_ACQUIRE": "producer 已取得 lk",
        "WAIT_PUBLISH": "waiter 在 p->lock 下发布 SLEEPING/channel",
        "PRODUCER_READY": "producer 在 lk 下发布 ready=1",
        "WAKE_MATCH": "wakeup 在 p->lock 下命中已发布 sleeper",
        "WAKE_MISS": "wakeup 扫到 RUNNING/channel absent，确定 miss",
        "WAKE_DONE": "本轮生产者 wake scan 已完成",
        "BAD_ORACLE": "主动读到稳定坏状态，尚未 rescue",
        "RESCUE_BEGIN": "只在 BAD_ORACLE 后启动清理 wake",
        "RESCUE_MATCH": "清理 wake 命中 sleeper",
        "RESCUE_DONE": "清理 wake scan 完成",
        "ORIGIN_SKIP": "origin scheduler 明确跳过 RUNNABLE target",
        "MIGRATE_RELEASE": "另一 hart 的选择 gate 现在才开放",
        "MIGRATE_SELECT": "非 origin hart 在 p->lock 下选择 target",
        "WAIT_RESUME": "waiter 在新 hart 持 p->lock 恢复",
        "WAIT_RECHECK": "waiter 清 channel、重取 lk 并复查 ready",
    }
    lines = [
        "# Scheduling and synchronization 0.1.0 证据报告包",
        "",
        f"- 源码基线：`{baseline}`",
        f"- 走查时教程提交：`{tutorial_commit}`（候选 diff 单独复核）",
        "- patch：`resources/scheduling-and-synchronization/syncproject.patch`",
        f"- patch SHA-256：`{patch_digest}`",
        f"- runner SHA-256：`{runner_digest}`",
        f"- 主机与工具：`{environment}`",
        "- 动态配置：lost-wakeup/focused/related/quick 使用 CPUS=2；full 使用 CPUS=1；128 MiB、临时源码导出和私有 `fs.img`",
        "",
        "## S 静态契约",
        "",
        "pinned baseline 上已检查 scheduler/sched/yield、swtch 保存集合、spinlock",
        "acquire/release、push_off/pop_off、sleep/wakeup、pipe/sleeplock condition loop、",
        "timer preemption 和 11-path tutorial patch scope。hook 只有在 active、target pid",
        "与专用 channel 同时匹配时生效；未应用 patch 的 pinned baseline 不含 hook",
        "或 broken path，patched build 也必须先显式 arm 才会触发专用路径。",
        "",
        "```text",
        "scheduler: acquire(p.lock) -> RUNNING -> swtch -> process releases p.lock",
        "process: acquire(p.lock) -> non-RUNNING -> swtch -> scheduler releases p.lock",
        "waiter: lk -> p.lock -> release(lk) -> SLEEPING -> sched",
        "producer: lk -> ready=1 -> target p.lock in wakeup -> RUNNABLE",
        "resume: scheduler p.lock -> sched returns -> release(p.lock) -> reacquire(lk)",
        "```",
        "",
        "每条事件的 `cond_owner`/`proc_owner` 是对应锁的 hart owner，`state` 为 target",
        "进程状态（2=SLEEPING、3=RUNNABLE、4=RUNNING），`chan=1` 表示专用 channel，",
        "`intr`/`noff` 分别记录本 hart 中断使能和嵌套关闭层数。`pid` 与绝对时刻不作",
        "跨运行契约；事件 `seq` 和 owner/state/chan/ready 关系才是 oracle。",
    ]
    if records is not None:
        lines.extend([
            "",
            "## F/B/C 四轮受控轨迹",
            "",
        ])
        for ordinal, case in enumerate(records["cases"], 1):
            final = case["final"]
            lines.extend([
                f"### {ordinal}. {case['mode']} generation {final['gen']}",
                "",
                "```text",
            ])
            for trace_line in case["trace"]:
                lines.append(trace_line)
                if case["bad_state"] is not None and "kind=BAD_ORACLE " in trace_line:
                    lines.append(case["bad_state"])
            lines.extend([
                case["final_line"],
                case["clean_line"],
                case["pass_line"],
                "```",
                "",
                "| seq / event | hart | lk / p->lock owner | state / chan / ready / noff | 解释 |",
                "| --- | --- | --- | --- | --- |",
            ])
            for trace_line in case["trace"]:
                event = parse_fields(trace_line)
                kind = event["kind"]
                lines.append(
                    f"| {event['seq']} / `{kind}` | {event['hart']} | "
                    f"{event['cond_owner']} / {event['proc_owner']} | "
                    f"{event['state']} / {event['chan']} / {event['ready']} / "
                    f"{event['noff']} | {event_meanings[kind]} |"
                )
            lines.append("")
        lines.extend([
            "固定路径建立 `lk + p->lock -> release(lk) -> publish SLEEPING -> wakeup",
            "取得 p->lock -> RUNNABLE -> scheduler ownership -> release gate ->",
            "other-hart resume -> reacquire(lk)`。错误包装器只命中专用 waiter，并在",
            "关中断固定 hart 的窗口中先释放 lk、后取得 p->lock。runner 主动读取",
            "`ready=1 && state=SLEEPING && chan=expected && wake_miss=1 && killed=0`",
            "后才允许 rescue；timeout 只是 watchdog，kill 从未作为 oracle 或清理手段。",
            "pid、地址和绝对时刻不作长期契约。",
            "",
            "## 重复性、回归和资源账本",
            "",
            "同一 QEMU 中依次执行 fixed/broken/fixed/broken，四个 generation 连续，",
            "每轮事件序列、关系断言与 process/condition-lock/channel/trace/gate 清理都通过。",
            "",
            "| 命令 | CPUS | 精确结果 | transcript SHA-256 |",
            "| --- | --- | --- | --- |",
        ])
        regression_rows = (
            ("usertests preempt", 2, records["usertests preempt"]),
            ("usertests pipe1", 2, records["usertests pipe1"]),
            ("usertests killstatus", 2, records["usertests killstatus"]),
            ("test-xv6.py -q usertests", 2, records["quick"]),
            ("test-xv6.py usertests", 1, records["full"]),
        )
        for command, cpus, transcript in regression_rows:
            transcript_digest = hashlib.sha256(transcript.encode()).hexdigest()
            lines.append(
                f"| `{command}` | {cpus} | 一次 `ALL TESTS PASSED`；无失败/`SYNC` marker | "
                f"`{transcript_digest}` |"
            )
        lines.extend([
            "",
            "允许副作用仅为临时源码/build、私有镜像和短寿命 QEMU/driver 进程组。",
            "runner 最后 `make clean`、逆向 patch、比较完整源码快照并删除临时目录；",
            f"共享工作树内容指纹：`{shared_state_digest}`；共享 `fs.img`：`{shared_image_digest}`。",
            "trace buffer 固定 32 槽并在 clear 后为 0；条件锁已解锁、target 已回收、",
            "channel 无 sleeper、全部 gate 归零。",
            "",
            "## 证据局限",
            "",
            "S/F/B/C 支持本报告；R 为 N/A，因为没有持久状态或 crash/restart 主张。",
            "关闭中断的 spin gate 只在 CPUS=2 实验中可推进，不能移植为单 hart 协议。",
            "强制 migration hook 证明一次锁/状态所有权可跨 hart 交接，不证明公平性、",
            "所有可能交错、设备 DMA memory order 或形式化正确性。串口输出不用于实时结论。",
        ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(static_only: bool, report: Path | None) -> None:
    baseline = load_baseline()
    tutorial_commit = checked(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, timeout=30
    ).strip()
    patch_digest = sha256(PATCH)
    runner_digest = sha256(Path(__file__))
    environment = "; ".join([
        checked(["uname", "-srmo"], timeout=30).strip(),
        checked(["qemu-system-riscv64", "--version"], timeout=30).splitlines()[0],
        checked(["make", "--version"], timeout=30).splitlines()[0],
        f"Python {sys.version.split()[0]}",
    ])
    original_state_digest = repo_state_digest()
    original_image = digest_or_missing(REPO_ROOT / "fs.img")
    records = None

    with tempfile.TemporaryDirectory(prefix="xv6-scheduling-sync-") as directory:
        root = Path(directory) / "src"
        export_baseline(root, baseline)
        clean_snapshot = snapshot(root)
        applied = False
        try:
            checked(
                ["git", "apply", "--check", "--whitespace=error-all",
                 "--unidiff-zero", str(PATCH)],
                cwd=root,
                timeout=60,
            )
            apply_patch(root)
            applied = True
            analyze_sources(root)
            build(root)
            if not static_only:
                records = run_dynamic(root)
        finally:
            checked(["make", "clean"], cwd=root, timeout=180)
            if applied:
                apply_patch(root, reverse=True)
            checked(["make", "clean"], cwd=root, timeout=180)
            require(snapshot(root) == clean_snapshot,
                    "temporary source did not cleanly restore")

    require(repo_state_digest() == original_state_digest,
            "shared repository content or index changed")
    require(digest_or_missing(REPO_ROOT / "fs.img") == original_image,
            "shared fs.img changed")
    if report is not None:
        write_report(
            report,
            baseline=baseline,
            tutorial_commit=tutorial_commit,
            patch_digest=patch_digest,
            runner_digest=runner_digest,
            environment=environment,
            shared_state_digest=original_state_digest,
            shared_image_digest=original_image,
            records=records,
        )
    if static_only:
        print("scheduling and synchronization passed: static, build, cleanup")
    else:
        print("scheduling and synchronization passed: static, F/B/C, focused, related, quick, full, cleanup")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        run(args.static_only, args.report)
    except (LabError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
