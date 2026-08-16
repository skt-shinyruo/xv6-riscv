#!/usr/bin/env python3

"""Run the isolated process-lifecycle and resource-ledger exercise."""

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
PATCH = TUTORIAL_ROOT / "resources" / "process-lifecycle" / "lifecycle.patch"

PATCH_PATHS = {
    "Makefile",
    "kernel/defs.h",
    "kernel/file.c",
    "kernel/kalloc.c",
    "kernel/lifecycle.h",
    "kernel/proc.c",
    "kernel/syscall.c",
    "kernel/syscall.h",
    "kernel/sysproc.c",
    "user/lifecycle.c",
    "user/lifeexec.c",
    "user/user.h",
    "user/usys.pl",
}
SOURCE_PATHS = PATCH_PATHS | {
    "kernel/exec.c",
    "kernel/file.c",
    "kernel/fs.c",
    "kernel/param.h",
    "kernel/pipe.c",
    "kernel/proc.h",
    "kernel/riscv.h",
    "kernel/sysfile.c",
    "kernel/sysproc.c",
    "kernel/trap.c",
    "kernel/vm.c",
    "user/init.c",
    "user/sh.c",
    "user/usertests.c",
}


class LabError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise LabError(message)


def require_in_order(source: str, tokens, context: str) -> None:
    positions = []
    offset = 0
    for token in tokens:
        position = source.find(token, offset)
        require(position >= 0, f"{context} token missing: {token}")
        positions.append(position)
        offset = position + len(token)
    require(positions == sorted(positions), f"{context} order changed")


def checked(command, *, cwd=None, timeout=180, env=None):
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr)[-12000:]
        raise LabError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{output}"
        )
    return result.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_or_missing(path: Path) -> str:
    return sha256(path) if path.is_file() else "missing"


def repo_status() -> str:
    return checked(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=REPO_ROOT,
        timeout=30,
    )


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
    output = checked(["git", "apply", "--numstat", str(PATCH)], cwd=root)
    paths = set()
    for line in output.splitlines():
        fields = line.split("\t", 2)
        require(len(fields) == 3, f"unexpected patch numstat line: {line!r}")
        paths.add(fields[2])
    return paths


def apply_patch(root: Path, *, reverse=False):
    command = ["git", "apply", "--whitespace=error-all", "--unidiff-zero"]
    if reverse:
        command.append("-R")
    command.append(str(PATCH))
    checked(command, cwd=root, timeout=60)


def load_baseline() -> str:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return data["release"]["baseline_commit"]


def read_sources(root: Path):
    return {
        path: (root / path).read_text(encoding="utf-8") for path in SOURCE_PATHS
    }


def analyze_sources(root: Path, *, patched: bool) -> None:
    paths = patch_paths(root)
    require(paths == PATCH_PATHS, f"patch paths differ: {sorted(paths)}")
    sources = read_sources(root)
    makefile = sources["Makefile"]
    require("$U/_lifecycle" in makefile and "$U/_lifeexec" in makefile,
            "lifecycle programs are not registered in UPROGS")
    require("$U/usys.S :" in makefile, "generated stub rule is missing")

    header = sources["kernel/lifecycle.h"]
    for field in (
        "used", "sleeping", "runnable", "running", "zombie", "free_pages",
        "active_files", "file_refs", "parent_links", "target_present",
        "target_sleeping", "target_zombie", "target_parent_is_init",
        "target_sz", "target_name", "target_ofile_slots",
    ):
        require(re.search(rf"\b{field}\b", header), f"snapshot field missing: {field}")

    syscall_h = sources["kernel/syscall.h"]
    require(re.search(r"^#define SYS_lifesnapshot 22$", syscall_h, re.MULTILINE),
            "lifesnapshot must use the next syscall number 22")
    syscall_c = sources["kernel/syscall.c"]
    require("extern uint64 sys_lifesnapshot(void);" in syscall_c,
            "lifesnapshot syscall declaration missing")
    require(re.search(r"\[SYS_lifesnapshot\]\s+sys_lifesnapshot", syscall_c),
            "lifesnapshot dispatch slot missing")
    sysproc = sources["kernel/sysproc.c"]
    require("sys_lifesnapshot(void)" in sysproc and "procsnapshot" in sysproc,
            "lifesnapshot handler does not call the process snapshot")
    user_h = sources["user/user.h"]
    require("int lifesnapshot(int, void *);" in user_h,
            "lifesnapshot user declaration missing")
    usys = sources["user/usys.pl"]
    require('entry("lifesnapshot");' in usys,
            "lifesnapshot generator entry missing")

    proc = sources["kernel/proc.c"]
    proc_header = sources["kernel/proc.h"]
    require_in_order(
        proc_header,
        ("UNUSED", "USED", "SLEEPING", "RUNNABLE", "RUNNING", "ZOMBIE"),
        "process state declaration",
    )
    for symbol in (
        "allocproc", "freeproc", "proc_pagetable", "proc_freepagetable",
        "userinit", "kfork", "reparent", "kexit", "kwait", "forkret",
        "kkill", "killed", "procsnapshot",
    ):
        require(re.search(rf"\b{re.escape(symbol)}\s*\(", proc),
                f"process lifecycle anchor missing: {symbol}")
    for transition in ("UNUSED", "USED", "RUNNABLE", "RUNNING", "SLEEPING", "ZOMBIE"):
        require(transition in proc, f"process state token missing: {transition}")
    require("acquire(&wait_lock)" in proc and "acquire(&p->lock)" in proc,
            "snapshot/wait locking contract missing")
    require("p->state = ZOMBIE" in proc and "freeproc(pp)" in proc,
            "zombie and wait reclamation paths missing")
    require("pp->parent = initproc" in proc and "wakeup(initproc)" in proc,
            "orphan reparent path missing")
    require("p->state = RUNNABLE" in proc and "p->killed = 1" in proc,
            "blocked-kill wakeup path missing")

    userinit_start = proc.index("\nuserinit(")
    userinit = proc[userinit_start:proc.index("\n// Grow", userinit_start)]
    require_in_order(userinit, ("allocproc()", "initproc = p", 'namei("/")',
                                "p->state = RUNNABLE"),
                     "first-process publication")
    forkret_start = proc.index("\nforkret(")
    forkret = proc[forkret_start:proc.index("\n// Sleep", forkret_start)]
    require_in_order(forkret, ("release(&p->lock)", "fsinit(ROOTDEV)",
                               "first = 0", 'kexec("/init"'),
                     "first-process exec")
    fork_start = proc.index("\nkfork(")
    fork_body = proc[fork_start:proc.index("\n// Pass", fork_start)]
    require_in_order(fork_body, ("allocproc()", "uvmcopy(",
                                  "np->trapframe->a0 = 0", "filedup(",
                                  "idup(", "acquire(&wait_lock)",
                                  "np->parent = p", "np->state = RUNNABLE"),
                     "fork ownership publication")
    exit_start = proc.index("\nkexit(")
    exit_body = proc[exit_start:proc.index("\n// Wait", exit_start)]
    require_in_order(exit_body, ("fileclose(", "iput(p->cwd)",
                                  "acquire(&wait_lock)", "reparent(p)",
                                  "p->xstate = status", "p->state = ZOMBIE"),
                     "exit ownership transfer")
    wait_start = proc.index("\nkwait(")
    wait_body = proc[wait_start:proc.index("\n// Per-CPU", wait_start)]
    require_in_order(wait_body, ("if (pp->state == ZOMBIE)", "copyout(",
                                  "freeproc(pp)"),
                     "wait status delivery and reclamation")

    exec_source = sources["kernel/exec.c"]
    require("kexec(" in exec_source and "oldpagetable = p->pagetable" in exec_source,
            "exec replacement commit anchor missing")
    require("if (pagetable)" in exec_source and "proc_freepagetable(pagetable, sz)" in exec_source,
            "exec failure cleanup missing")
    require("p->pagetable = pagetable" in exec_source and "p->trapframe->epc = elf.entry" in exec_source,
            "exec commit fields missing")
    require_in_order(exec_source, ("proc_pagetable(p)", "uvmalloc(",
                                   "oldpagetable = p->pagetable",
                                   "p->pagetable = pagetable",
                                   "proc_freepagetable(oldpagetable, oldsz)",
                                   "bad:", "proc_freepagetable(pagetable, sz)"),
                     "exec prepare/commit/rollback")

    file_source = sources["kernel/file.c"]
    require("filealloc(" in file_source and "filedup(" in file_source and "fileclose(" in file_source,
            "file ownership anchors missing")
    require("filerefcount(" in file_source and "f->ref" in file_source,
            "file reference ledger missing")
    kalloc = sources["kernel/kalloc.c"]
    require("kalloc(" in kalloc and "kfree(" in kalloc and "freepagecount(" in kalloc,
            "page ownership ledger missing")

    pipe_source = sources["kernel/pipe.c"]
    pipe_read = pipe_source[pipe_source.index("\npiperead("):]
    require_in_order(pipe_read, ("if (killed(pr))", "sleep(&pi->nread",
                                 "return i"),
                     "blocked pipe cancellation")
    init_source = sources["user/init.c"]
    require_in_order(init_source, ('exec("sh"', "for (;;) {", "wait((int *)0)"),
                     "init shell/orphan reap loop")
    shell_source = sources["user/sh.c"]
    require("runcmd(" in shell_source and "exec(ecmd->argv[0]" in shell_source and
            "if (fork1() == 0)" in shell_source and "wait(0)" in shell_source,
            "shell fork/exec/wait path missing")

    lifecycle = sources["user/lifecycle.c"]
    for token in (
        "LIFE BASE", "LIFE EXEC_READY", "LIFE EXEC_AFTER", "LIFE WAIT_BAD",
        "LIFE EXEC_REAP",
        "LIFE EXECFAIL", "LIFE KILL_SLEEP", "LIFE KILL_REAP",
        "LIFE ORPHAN_REPARENT", "LIFE ORPHAN_REAP", "LIFE CAPACITY",
        "LIFE CAPACITY_REAP", "LIFE FINAL", "LIFE PASS",
    ):
        require(token in lifecycle, f"lifecycle phase missing: {token}")
    for token in ("fork()", "exec(", "wait(", "kill(", "lifesnapshot(", "O_RDONLY"):
        require(token in lifecycle, f"lifecycle operation missing: {token}")
    require("fork_fail" in lifecycle and "target_parent_is_init" in lifecycle,
            "capacity/orphan oracle fields missing")
    param = sources["kernel/param.h"]
    riscv = sources["kernel/riscv.h"]
    maxarg_match = re.search(r"^#define MAXARG\s+(\d+)", param, re.MULTILINE)
    userstack_match = re.search(r"^#define USERSTACK\s+(\d+)", param, re.MULTILINE)
    pgsize_match = re.search(r"^#define PGSIZE\s+(\d+)", riscv, re.MULTILINE)
    require(maxarg_match and userstack_match and pgsize_match,
            "exec argument/stack capacity constants are missing")
    maxarg = int(maxarg_match.group(1))
    user_stack_bytes = int(userstack_match.group(1)) * int(pgsize_match.group(1))
    require(maxarg == 32 and user_stack_bytes == 4096,
            "exec rollback fixture capacity assumptions changed")
    require("static char oversized_args[MAXARG - 1][300]" in lifecycle and
            "for (int i = 0; i < MAXARG - 1; i++)" in lifecycle and
            "for (int j = 0; j < 299; j++)" in lifecycle and
            "oversized_args[i][299] = 0" in lifecycle and
            'arguments[0] = "lifeexec"' in lifecycle and
            "arguments[MAXARG - 1] = 0" in lifecycle and
            'exec("lifeexec", arguments)' in lifecycle,
            "exec rollback fixture argument construction changed")
    oversized_argv_bytes = 9 + (maxarg - 2) * 300
    require(oversized_argv_bytes > user_stack_bytes,
            "exec rollback argv no longer exceeds the new user stack")
    sysfile = sources["kernel/sysfile.c"]
    sys_exec_start = sysfile.index("\nsys_exec(")
    sys_exec = sysfile[sys_exec_start:sysfile.index("\nuint64\nsys_pipe", sys_exec_start)]
    require_in_order(sys_exec, ("fetchaddr(", "kalloc()", "fetchstr(",
                                "kexec(path, argv)", "kfree(argv[i])"),
                     "exec argv pre-copy and release")
    helper = sources["user/lifeexec.c"]
    require("exec" not in helper and "getpid()" in helper and "read(" in helper,
            "exec helper must prove inherited pid and descriptors")

    defs = sources["kernel/defs.h"]
    require("freepagecount" in defs and "filerefcount" in defs and "procsnapshot" in defs,
            "snapshot helper declarations missing")
    if patched:
        require("struct lifesnapshot" in sources["kernel/proc.c"],
                "patched proc.c does not include snapshot type")


def build(root: Path, *, image=True):
    targets = ["kernel/kernel", "user/_lifecycle", "user/_lifeexec"]
    if image:
        targets.append("fs.img")
    checked(["make", "-j2", *targets], cwd=root, timeout=360)


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
    def __init__(self, root: Path):
        env = os.environ.copy()
        env["CPUS"] = "1"
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

    def read_until(self, marker: bytes, timeout: float, *, start=0):
        deadline = time.monotonic() + timeout
        while marker not in self.output[start:]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LabError(f"QEMU timeout waiting for {marker!r}: {self.tail()}")
            events = self.selector.select(remaining)
            if not events:
                raise LabError(f"QEMU timeout waiting for {marker!r}: {self.tail()}")
            chunk = os.read(self.proc.stdout.fileno(), 4096)
            if not chunk:
                raise LabError(f"QEMU closed output: {self.tail()}")
            self.output.extend(chunk.replace(b"\r", b""))
        return bytes(self.output)

    def command(self, command: str, timeout=180) -> str:
        start = len(self.output)
        self.proc.stdin.write((command + "\n").encode())
        self.proc.stdin.flush()
        self.read_until(b"$ ", timeout, start=start)
        return bytes(self.output[start:]).decode("utf-8", "replace")

    def tail(self):
        return bytes(self.output[-6000:]).decode("utf-8", "replace")

    def stop(self):
        self.selector.close()
        stop_process_group(self.proc)


def run_driver(root: Path, args, *, timeout: int):
    env = os.environ.copy()
    env["CPUS"] = "1"
    process = subprocess.Popen(
        ["python3", "test-xv6.py", *args],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = process.communicate(timeout=10)
        raise LabError(f"driver watchdog expired: {' '.join(args)}\n{output[-10000:]}") from exc
    finally:
        stop_process_group(process)
    require(process.returncode == 0, f"driver failed: {' '.join(args)}\n{output[-10000:]}")
    require(output.count("ALL TESTS PASSED") == 1,
            f"driver success marker count changed: {' '.join(args)}")
    require("SOME TESTS FAILED" not in output, f"driver reported failure: {' '.join(args)}")
    require("LIFE " not in output, f"lifecycle marker leaked into regression: {' '.join(args)}")
    return output


def parse_snapshot(line: str):
    values = {}
    for key, value in re.findall(r"([a-z_]+)=([^\s]+)", line):
        if key in {"name"}:
            values[key] = value
        else:
            try:
                values[key] = int(value, 0)
            except ValueError:
                values[key] = value
    return values


def lifecycle_lines(output: str):
    lines = [line.strip() for line in output.splitlines() if line.startswith("LIFE ")]
    require(lines, f"lifecycle produced no markers: {output[-8000:]}")
    phases = {}
    order = []
    for line in lines:
        match = re.match(r"LIFE ([A-Z_]+)", line)
        require(match is not None, f"malformed lifecycle marker: {line}")
        phase = match.group(1)
        order.append(phase)
        phases.setdefault(phase, []).append(parse_snapshot(line))
    return lines, phases, order


def validate_lifecycle(output: str):
    lines, phases, order = lifecycle_lines(output)
    expected = [
        "BASE", "EXEC_READY", "EXEC_AFTER", "WAIT_BAD", "EXEC_REAP", "EXECFAIL",
        "KILL_SLEEP", "KILL_REAP", "ORPHAN_REPARENT", "ORPHAN_REAP",
        "CAPACITY", "CAPACITY_REAP", "FINAL", "PASS",
    ]
    require(order == expected, f"lifecycle phase sequence changed: {order}")
    for phase in expected:
        require(len(phases.get(phase, [])) == 1, f"phase count mismatch: {phase}")

    base = phases["BASE"][0]
    for key in ("used", "free", "active_files", "refs", "parents"):
        require(key in base, f"BASE missing {key}")
    require(base["used"] >= 3 and base["free"] > 0, "invalid baseline resource ledger")

    ready = phases["EXEC_READY"][0]
    after = phases["EXEC_AFTER"][0]
    reap = phases["EXEC_REAP"][0]
    require(ready.get("pid") == after.get("pid"), "exec changed pid")
    require(after.get("pid") == reap.get("pid"), "wait reported another pid")
    require(ready.get("pid_same") == 1 and ready.get("fd_ok") == 1,
            "exec helper did not prove pid/fd inheritance")
    require(ready.get("sleeping") == 1 and ready.get("name") == "lifecycle",
            "pre-exec checkpoint is not a blocked lifecycle child")
    require(after.get("sleeping") == 1 and after.get("name") == "lifeexec",
            "post-exec checkpoint is not a blocked lifeexec child")
    require(after.get("sz_changed") == 1, "exec did not replace the address-space size")
    require(after.get("pagetable_changed") == 1 and after.get("trapframe_same") == 1,
            "exec changed the wrong process-owned objects")
    require(after.get("cwd_same") == 1, "exec did not preserve cwd ownership")
    require(ready["used"] > base["used"] and ready["parents"] > base["parents"],
            "fork did not publish a child slot/parent link")
    require(ready["free"] < base["free"] and ready["refs"] > base["refs"],
            "fork did not account copied pages/file references")
    bad = phases["WAIT_BAD"][0]
    require(bad.get("ret") == -1 and bad.get("target_present") == 1 and
            bad.get("zombie") == 1 and bad.get("ofile") == 0,
            "invalid wait status pointer did not retain a cleaned zombie")
    require(reap.get("status") == 37, "fork-exec child exit status was not preserved")

    def require_baseline_ledger(phase: str, values) -> None:
        for key in ("used", "free", "active_files", "refs", "parents"):
            require(values.get(key) == base[key],
                    f"{phase} ledger did not recover: {key}")

    require_baseline_ledger("EXEC_REAP", reap)

    failed = phases["EXECFAIL"][0]
    require(failed.get("ret") == -1 and failed.get("pid_same") == 1,
            "failed exec did not return while retaining the process")
    require(failed.get("sz_same") == 1 and failed.get("fd_ok") == 1,
            "failed exec did not preserve image/resources")
    require(base["free"] > 64,
            "baseline lacks enough free pages to rule out argv pre-copy exhaustion")

    kill_sleep = phases["KILL_SLEEP"][0]
    kill_reap = phases["KILL_REAP"][0]
    require(kill_sleep.get("sleeping") == 1 and kill_sleep.get("target_present") == 1,
            "kill fixture was not observed blocked")
    require(kill_reap.get("status") == -1 and kill_reap.get("target_present") == 0,
            "blocked kill did not produce -1 status and reclaim the target")
    require_baseline_ledger("KILL_REAP", kill_reap)

    orphan = phases["ORPHAN_REPARENT"][0]
    orphan_reap = phases["ORPHAN_REAP"][0]
    require(orphan.get("target_present") == 1 and orphan.get("sleeping") == 1,
            "orphan was not retained as a live blocked child")
    require(orphan.get("target_parent_init") == 1,
            "orphan parent was not init after intermediate exit")
    require(orphan_reap.get("target_present") == 0 and orphan_reap.get("absent") == 1,
            "init did not reclaim the orphan")
    require_baseline_ledger("ORPHAN_REAP", orphan_reap)

    capacity = phases["CAPACITY"][0]
    cap_reap = phases["CAPACITY_REAP"][0]
    require(capacity.get("fork_fail") == -1, "capacity fixture did not hit fork failure")
    require(capacity.get("used") == 64, "capacity fixture did not fill NPROC slots")
    require(capacity.get("free") > 0, "capacity fixture exhausted pages before NPROC")
    require(capacity.get("children") == capacity["used"] - base["used"],
            "capacity child count does not match used slots")
    require(capacity.get("parents") == base["parents"] + capacity["children"],
            "capacity parent-link ledger mismatch")
    require(capacity.get("active_files") == base["active_files"] + 2,
            "capacity created an unexpected number of file objects")
    expected_refs = base["refs"] + 2 + capacity["children"] * 4
    require(capacity.get("refs") == expected_refs and
            capacity.get("inherited_refs") == expected_refs - base["refs"],
            "capacity file reference ledger mismatch")
    require_baseline_ledger("CAPACITY_REAP", cap_reap)

    final = phases["FINAL"][0]
    require_baseline_ledger("FINAL", final)
    return lines


def run_dynamic(root: Path):
    qemu = None
    records = {}
    try:
        qemu = Qemu(root)
        for command in ("usertests exitwait", "usertests reparent", "usertests killstatus"):
            transcript = qemu.command(command, timeout=240)
            require(transcript.count("ALL TESTS PASSED") == 1,
                    f"focused regression marker count changed: {command}")
            require("LIFE " not in transcript, f"control emitted lifecycle marker: {command}")
            records[command] = transcript
        lifecycle = qemu.command("lifecycle", timeout=240)
        records["lifecycle"] = lifecycle
        trace = validate_lifecycle(lifecycle)
        for command in ("usertests exectest", "usertests reparent2", "usertests forktest"):
            transcript = qemu.command(command, timeout=240)
            require(transcript.count("ALL TESTS PASSED") == 1,
                    f"related regression marker count changed: {command}")
            require("LIFE " not in transcript,
                    f"lifecycle marker leaked into related regression: {command}")
            records[command] = transcript
    finally:
        if qemu is not None:
            qemu.stop()
    records["trace"] = trace
    records["quick"] = run_driver(root, ["-q", "usertests"], timeout=360)
    records["full"] = run_driver(root, ["usertests"], timeout=720)
    return records


def write_report(
    path: Path,
    *,
    baseline,
    tutorial_commit,
    patch_digest,
    runner_digest,
    environment,
    shared_status_digest,
    shared_image_digest,
    static_text,
    records=None,
):
    lines = [
        "# Process lifecycle 0.1.0 隔离报告包",
        "",
        f"- 源码基线：`{baseline}`",
        f"- 走查时教程提交：`{tutorial_commit}`（候选 diff 单独复核）",
        f"- patch：`resources/process-lifecycle/lifecycle.patch`",
        f"- patch SHA-256：`{patch_digest}`",
        f"- runner SHA-256：`{runner_digest}`",
        f"- 主机与工具：`{environment}`",
        "- 实验配置：CPUS=1、128 MiB、临时源码导出和私有 `fs.img`",
        "",
        "## S 静态契约",
        "",
        static_text.strip(),
    ]
    if records is not None:
        lines.extend([
            "",
            "## F/B 生命周期与资源账本",
            "",
            "触发顺序为 `fork -> pre-exec block -> exec -> post-exec block -> wait`，",
            "随后是有效 `lifeexec` 加超出 4 KiB 新用户栈的 argv 所触发的后期失败",
            "`exec`、blocked `kill`、orphan reparent/reclaim 和 NPROC capacity。",
            "`lifecycle` 输出中的每一行 `LIFE` 都是 guest snapshot；runner 对状态、PID、",
            "name、退出状态、父链接、进程槽、页、file object/ref 和清理回基线做关系断言。",
            "",
            "```text",
            *records["trace"],
            "```",
            "",
            "回归结果：`usertests exitwait`、`reparent`、`killstatus`、`exectest`、",
            "`reparent2`、`forktest` 均得到 `ALL TESTS PASSED`；",
            "`python3 test-xv6.py -q usertests` 与 `python3 test-xv6.py usertests`",
            "也都得到 `ALL TESTS PASSED`，且所有控制/回归输出均无 `LIFE` marker。",
            "",
            "## 资源、清理与局限",
            "",
            "临时 patch 只增加只读快照 syscall、两个 guest fixtures 和资源计数 helper；",
            "允许副作用是临时源码/build 产物、私有镜像和 QEMU 进程。focused/related",
            "命令共用一组 QEMU；其后 quick/full 各自启动并回收进程组。最后 `make clean`、",
            "逆向 patch、比较源码快照并删除临时目录；共享工作树状态与 `fs.img` hash 不变。",
            f"共享工作树状态摘要：`{shared_status_digest}`；共享 `fs.img`：`{shared_image_digest}`。",
            "free-page 账本不包含启动时永久 kernel stack；file refs 与 active file objects",
            "分别记录继承引用和全局对象。CPUS=1 不声称跨 hart happens-before、调度公平、",
            "页表权限或持久化恢复；timeout 仅是 watchdog。`C`、`R` 均为 `N/A`。",
        ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(static_only: bool, report: Path | None):
    baseline = load_baseline()
    tutorial_commit = checked(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                              timeout=30).strip()
    patch_digest = sha256(PATCH)
    runner_digest = sha256(Path(__file__))
    environment = "; ".join([
        checked(["uname", "-srmo"], timeout=30).strip(),
        checked(["qemu-system-riscv64", "--version"], timeout=30).splitlines()[0],
        checked(["make", "--version"], timeout=30).splitlines()[0],
        f"Python {sys.version.split()[0]}",
    ])
    original_status = repo_status()
    original_status_digest = hashlib.sha256(
        original_status.encode("utf-8")
    ).hexdigest()
    original_image = digest_or_missing(REPO_ROOT / "fs.img")
    static_text = ""
    records = None
    with tempfile.TemporaryDirectory(prefix="xv6-process-lifecycle-") as directory:
        root = Path(directory) / "src"
        export_baseline(root, baseline)
        clean_snapshot = snapshot(root)
        applied = False
        try:
            checked(["git", "apply", "--check", "--whitespace=error-all", "--unidiff-zero", str(PATCH)], cwd=root)
            apply_patch(root)
            applied = True
            analyze_sources(root, patched=True)
            build(root)
            static_text = (
                "pinned baseline、applied-patch 源码契约与 13-path scope 均通过；"
                "临时 kernel、lifecycle 与 lifeexec guest 构建成功，逆向清理后源码快照一致。"
            )
            if not static_only:
                records = run_dynamic(root)
        finally:
            checked(["make", "clean"], cwd=root, timeout=180)
            if applied:
                apply_patch(root, reverse=True)
            checked(["make", "clean"], cwd=root, timeout=180)
            require(snapshot(root) == clean_snapshot, "temporary source did not cleanly restore")
    require(repo_status() == original_status, "shared repository status changed")
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
            shared_status_digest=original_status_digest,
            shared_image_digest=original_image,
            static_text=static_text,
            records=records,
        )
    if static_only:
        print("process lifecycle passed: static, patch cleanup")
    else:
        print("process lifecycle passed: static, focused, related, quick, full, cleanup")


def main():
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
