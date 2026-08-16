#!/usr/bin/env python3

"""Run the isolated virtual-memory, lazy-fault, and NX bounded change lab."""

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
PATCH = Path(__file__).with_name("lazy-nx.patch")
PATCH_PATHS = {
    "Makefile",
    "kernel/defs.h",
    "kernel/kalloc.c",
    "kernel/syscall.c",
    "kernel/syscall.h",
    "kernel/sysproc.c",
    "kernel/trap.c",
    "kernel/vm.c",
    "kernel/vmprobe.h",
    "user/user.h",
    "user/usys.pl",
    "user/vmtrace.c",
}
SOURCE_PATHS = PATCH_PATHS | {
    "kernel/exec.c",
    "kernel/kernel.ld",
    "kernel/memlayout.h",
    "kernel/proc.c",
    "kernel/proc.h",
    "kernel/riscv.h",
    "kernel/trampoline.S",
    "kernel/vm.h",
    "user/ulib.c",
    "user/usertests.c",
}
REGRESSIONS = (
    "lazy_alloc",
    "lazy_unmap",
    "lazy_copy",
    "lazy_sbrk",
    "sbrkfail",
    "sbrkbugs",
    "sbrklast",
    "execout",
)

PTE_V = 1 << 0
PTE_R = 1 << 1
PTE_W = 1 << 2
PTE_X = 1 << 3
PTE_U = 1 << 4
LEAF_MASK = PTE_V | PTE_R | PTE_W | PTE_X | PTE_U
DATA_FLAGS = PTE_V | PTE_R | PTE_W | PTE_U
TRAPFRAME_GPRS = (
    ("ra", 40), ("sp", 48), ("gp", 56), ("tp", 64),
    ("t0", 72), ("t1", 80), ("t2", 88), ("s0", 96), ("s1", 104),
    ("a1", 120), ("a2", 128), ("a3", 136), ("a4", 144),
    ("a5", 152), ("a6", 160), ("a7", 168), ("s2", 176),
    ("s3", 184), ("s4", 192), ("s5", 200), ("s6", 208),
    ("s7", 216), ("s8", 224), ("s9", 232), ("s10", 240),
    ("s11", 248), ("t3", 256), ("t4", 264), ("t5", 272),
    ("t6", 280),
)


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
    listing = git_output_bytes(["ls-files", "-c", "-o", "--exclude-standard", "-z"])
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
        path: (root / path).read_text(encoding="utf-8")
        for path in SOURCE_PATHS
        if (root / path).is_file()
    }


def analyze_baseline(root: Path) -> None:
    sources = read_sources(root)
    vm = sources["kernel/vm.c"]
    trap = sources["kernel/trap.c"]
    proc = sources["kernel/proc.c"]
    exec_source = sources["kernel/exec.c"]
    trampoline = sources["kernel/trampoline.S"]
    linker = sources["kernel/kernel.ld"]
    sysproc = sources["kernel/sysproc.c"]

    require_in_order(
        vm,
        ("walk(pagetable_t", "for (int level = 2", "kalloc()", "PA2PTE"),
        "Sv39 walk allocation",
    )
    require_in_order(
        vm,
        ("uvmalloc(", "PTE_R | PTE_U | xperm", "uvmdealloc"),
        "eager allocation rollback",
    )
    require_in_order(
        vm,
        ("uvmcopy(", "continue; // page table entry hasn't been allocated",
         "continue; // physical page hasn't been allocated", "PTE_FLAGS"),
        "fork sparse mapping",
    )
    require_in_order(
        vm,
        ("copyout(", "walkaddr", "vmfault", "PTE_W"),
        "copyout permission path",
    )
    require_in_order(vm, ("copyin(", "walkaddr", "vmfault"), "copyin fault path")
    require_in_order(
        vm,
        ("copyinstr(", "walkaddr", "if (pa0 == 0)", "return -1"),
        "copyinstr no-allocation path",
    )
    require_in_order(
        vm,
        ("vmfault(", "if (va >= p->sz)", "PGROUNDDOWN",
         "ismapped", "kalloc", "PTE_W | PTE_U | PTE_R"),
        "lazy fault policy",
    )
    fault_condition = re.search(
        r"else if \(\(r_scause\(\) == 15 \|\| r_scause\(\) == 13\).*?vmfault",
        trap,
        re.S,
    )
    require(fault_condition is not None, "usertrap no longer limits lazy repair to 13/15")
    require("r_scause() == 12" not in fault_condition.group(0),
            "instruction fault entered baseline lazy repair")
    require_in_order(
        proc,
        ("proc_pagetable(", "TRAMPOLINE", "PTE_R | PTE_X",
         "TRAPFRAME", "PTE_R | PTE_W"),
        "special user mappings",
    )
    require_in_order(
        proc,
        ("proc_freepagetable(", "uvmunmap(pagetable, TRAMPOLINE, 1, 0)",
         "uvmunmap(pagetable, TRAPFRAME, 1, 0)", "uvmfree"),
        "special mapping teardown",
    )
    require_in_order(
        exec_source,
        ("flags2perm", "PTE_X", "PTE_W", "kexec(",
         "pagetable = proc_pagetable", "oldpagetable = p->pagetable",
         "p->pagetable = pagetable", "proc_freepagetable(oldpagetable",
         "bad:", "proc_freepagetable(pagetable, sz)"),
        "exec commit and rollback",
    )
    require_in_order(
        sysproc,
        ("sys_sbrk", "SBRK_EAGER || n < 0", "addr + n < addr",
         "addr + n > TRAPFRAME", "myproc()->sz += n"),
        "lazy logical growth",
    )
    require_in_order(
        trap,
        ("prepare_return();", "MAKE_SATP(p->pagetable)", "return satp;"),
        "usertrap return handoff",
    )
    require_in_order(
        trap,
        ("prepare_return(void)", "intr_off()",
         "TRAMPOLINE + (uservec - trampoline)", "w_stvec(trampoline_uservec)",
         "kernel_satp = r_satp()", "kernel_sp = p->kstack + PGSIZE",
         "kernel_trap = (uint64)usertrap", "kernel_hartid = r_tp()",
         "r_sstatus()", "~SSTATUS_SPP", "SSTATUS_SPIE", "w_sstatus(x)",
         "w_sepc(p->trapframe->epc)"),
        "prepare_return user contract",
    )
    require_in_order(
        trampoline,
        ("uservec:", "csrw sscratch, a0", "li a0, TRAPFRAME",
         "csrr t0, sscratch", "sd t0, 112(a0)", "ld sp, 8(a0)",
         "ld tp, 32(a0)", "ld t0, 16(a0)", "ld t1, 0(a0)",
         "sfence.vma zero, zero", "csrw satp, t1",
         "sfence.vma zero, zero", "jalr t0", "userret:",
         "sfence.vma zero, zero", "csrw satp, a0", "sfence.vma zero, zero",
         "li a0, TRAPFRAME", "ld a0, 112(a0)", "sret"),
        "full trampoline contract",
    )
    uservec, userret = trampoline.split("userret:", 1)
    for register, offset in TRAPFRAME_GPRS:
        require(f"sd {register}, {offset}(a0)" in uservec,
                f"uservec does not save {register} at trapframe+{offset}")
        require(f"ld {register}, {offset}(a0)" in userret,
                f"userret does not restore {register} from trapframe+{offset}")
    require("ASSERT(. - _trampoline == 0x1000" in linker,
            "trampoline one-page linker assertion missing")


def analyze_patched(root: Path) -> None:
    sources = read_sources(root)
    vm = sources["kernel/vm.c"]
    trap = sources["kernel/trap.c"]
    kalloc = sources["kernel/kalloc.c"]
    guest = sources["user/vmtrace.c"]
    require("VMACCESS_COPYOUT" in vm and "VMACCESS_COPYIN" in vm,
            "copy helper access kinds missing")
    require("VMACCESS_LOAD : VMACCESS_STORE" in trap,
            "hardware access kinds missing")
    require_in_order(
        vm,
        ("vmprobe_begin(access", "if (va >= p->sz)", "ismapped",
         "kalloc()", "mappages", "PTE_W | PTE_U | PTE_R",
         "vmprobe_finish(1"),
        "patched fault trace",
    )
    require("vmprobe_kalloc_should_fail" in kalloc and "kfreepages" in kalloc,
            "scoped allocator seam missing")
    require("!vmprobe_state.active || !vmprobe_state.in_fault" in vm and
            "p->pid != vmprobe_state.pid" in vm,
            "failpoint is not scoped to active pid/fault")
    require_in_order(
        vm,
        ("if (op == VMPROBE_FETCH)", "result = copyout",
         "vmprobe_state.active = 0", "return result"),
        "record fetch must disarm the test seam",
    )
    require("uchar pattern[4] = {0x11, 0x22, 0x33, 0x44}" in guest and
            "p[0] != 0x11" in guest and "bytes[0] != 0" in guest,
            "copyin/copyout payload oracle missing")
    exec_case_start = guest.find("run_exec_rollback(void)")
    exec_case_end = guest.find("\nint\nmain(void)", exec_case_start)
    require(exec_case_start >= 0 and exec_case_end > exec_case_start,
            "late exec rollback fixture missing")
    require("VMPROBE_ARM" not in guest[exec_case_start:exec_case_end],
            "late exec rollback left the audit seam armed")
    require_in_order(
        guest,
        ("run_success(\"load\"", "run_success(\"store\"",
         "run_success(\"copyout\"", "run_success(\"copyin\"",
         "run_killed(\"invalid\"", "run_killed(\"permission\"",
         "run_killed(\"nx\"", "VMPROBE_SET_X", "run_oom(9, 1)",
         "run_oom(10, 2)", "run_oom(11, 3)", "run_exec_rollback()",
         "VM PASS cases=12"),
        "guest scenario order",
    )
    require("0x00008067" in guest and "fence.i" in guest,
            "valid ret instruction or fence.i missing")
    require("exec_arg[3000]" in guest and "exec_arg2[3000]" in guest and
            "exit(42)" in guest,
            "late exec rollback discriminator missing")


def build(root: Path) -> None:
    checked(
        ["make", "-j2", "CPUS=1", "kernel/kernel", "user/_vmtrace", "fs.img"],
        cwd=root,
        timeout=420,
    )


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
        raise LabError(f"driver watchdog expired: {' '.join(args)}\n{output[-10000:]}") from exc
    finally:
        if process.poll() is None or process_group_exists(process.pid):
            stop_process_group(process)
    require(process.returncode == 0,
            f"driver failed: {' '.join(args)}\n{output[-10000:]}")
    require(output.count("ALL TESTS PASSED") == 1,
            f"driver success marker count changed: {' '.join(args)}")
    require("SOME TESTS FAILED" not in output,
            f"driver reported failure: {' '.join(args)}")
    require("VM " not in output,
            f"vmprobe marker leaked into regression: {' '.join(args)}")
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


def validate_vmtrace(output: str):
    require("VM FAIL" not in output, "guest rejected a VM scenario")
    require(output.count("VM PASS cases=12") == 1, "VM PASS marker count changed")
    special_lines = marker_lines(output, "VM SPECIAL ")
    require(len(special_lines) == 1, "special mapping marker count changed")
    special = parse_fields(special_lines[0])
    require(special["user_root"] != special["kernel_root"],
            "kernel and user roots unexpectedly match")
    require(special["tramp_va"] == 0x3FFFFFF000 and
            special["trap_va"] == 0x3FFFFFE000,
            "special virtual addresses changed")
    require(special["user_tramp_pa"] == special["kernel_tramp_pa"] != 0,
            "trampoline is not same VA/PA in both roots")
    require(special["trap_pa"] == special["owner_pa"] != 0,
            "user TRAPFRAME does not map the process-owned page")
    require((special["user_tramp_flags"] & LEAF_MASK) == (PTE_V | PTE_R | PTE_X),
            "user trampoline flags changed")
    require((special["kernel_tramp_flags"] & LEAF_MASK) ==
            (PTE_V | PTE_R | PTE_X), "kernel trampoline flags changed")
    require((special["trap_flags"] & LEAF_MASK) == (PTE_V | PTE_R | PTE_W),
            "user trapframe flags changed")
    require(special["kernel_trap_flags"] == 0,
            "kernel table unexpectedly maps numeric TRAPFRAME VA")

    case_lines = marker_lines(output, "VM CASE ")
    require(len(case_lines) == 10, f"VM CASE count changed: {len(case_lines)}")
    cases = [parse_fields(line) for line in case_lines]
    by_generation = {case["gen"]: case for case in cases}
    require(set(by_generation) == {1, 2, 3, 4, 5, 6, 7, 9, 10, 11},
            f"case generations changed: {sorted(by_generation)}")
    for case in cases:
        require(case["base"] == case["after"],
                f"post-wait page ledger changed: generation {case['gen']}")

    for generation, name, access, scause in (
        (1, "load", 1, 13),
        (2, "store", 2, 15),
        (3, "copyout", 4, 0),
        (4, "copyin", 3, 0),
    ):
        case = by_generation[generation]
        require(case["name"] == name and case["access"] == access and
                case["status"] == 0 and case["result"] == 1 and
                case["scause"] == scause and case["before"] == 0 and
                (case["after_flags"] & LEAF_MASK) == DATA_FLAGS and
                case["allocations"] >= 1,
                f"normal oracle failed: {name}")

    invalid = by_generation[5]
    require(invalid["name"] == "invalid" and invalid["status"] == -1 and
            invalid["scause"] == 15 and invalid["result"] == 0 and
            invalid["before"] == invalid["after_flags"] == 0 and
            invalid["allocations"] == 0,
            "invalid-address oracle failed")
    permission = by_generation[6]
    require(permission["name"] == "permission" and permission["status"] == -1 and
            permission["scause"] == 15 and permission["result"] == 0 and
            (permission["before"] & (PTE_V | PTE_R | PTE_X | PTE_U)) ==
            (PTE_V | PTE_R | PTE_X | PTE_U) and
            (permission["before"] & PTE_W) == 0 and
            permission["after_flags"] == permission["before"] and
            permission["allocations"] == 0,
            "permission-fault oracle failed")
    nx = by_generation[7]
    require(nx["name"] == "nx" and nx["status"] == -1 and nx["access"] == 5 and
            nx["scause"] == 12 and nx["stval"] == nx["epc"] != 0 and
            (nx["after_flags"] & LEAF_MASK) == DATA_FLAGS and
            nx["allocations"] == 0,
            "NX instruction-fault oracle failed")

    for generation, fail_at in ((9, 1), (10, 2), (11, 3)):
        case = by_generation[generation]
        require(case["name"] == "oom" and case["status"] == -1 and
                case["scause"] == 15 and case["result"] == 0 and
                case["fail_at"] == fail_at and case["allocations"] == fail_at and
                case["before"] == case["after_flags"] == 0,
                f"OOM oracle failed at allocation {fail_at}")
        debt = 1 if fail_at == 3 else 0
        require(case["free_after"] == case["free_before"] - debt,
                f"OOM immediate rollback debt changed at allocation {fail_at}")

    mutation_lines = marker_lines(output, "VM MUTATION ")
    require(len(mutation_lines) == 1, "mutation marker count changed")
    mutation = parse_fields(mutation_lines[0])
    require(mutation["status"] == 0 and (mutation["flags"] & PTE_X) != 0,
            "PTE_X mutation did not execute the same ret instruction")
    exec_lines = marker_lines(output, "VM EXEC ")
    require(len(exec_lines) == 1, "exec marker count changed")
    exec_record = parse_fields(exec_lines[0])
    require(exec_record["status"] == 42 and
            exec_record["base"] == exec_record["after"],
            "late exec rollback did not preserve the old image and ledger")
    return {
        "special": special_lines[0],
        "cases": case_lines,
        "mutation": mutation_lines[0],
        "exec": exec_lines[0],
        "pass": "VM PASS cases=12",
    }


def validate_guest_regression(name: str, transcript: str) -> None:
    require(transcript.count("ALL TESTS PASSED") == 1,
            f"guest regression marker count changed: {name}")
    require(f"test {name}:" in transcript,
            f"guest regression did not report the selected test: {name}")
    require("SOME TESTS FAILED" not in transcript,
            f"guest regression failed: {name}")
    require("VM " not in transcript,
            f"vmprobe marker leaked into guest regression: {name}")


def run_dynamic(root: Path):
    records = {"regressions": {}}
    qemu = None
    try:
        qemu = Qemu(root, cpus=1)
        transcript = qemu.command("vmtrace", timeout=240)
        records["vmtrace"] = validate_vmtrace(transcript)
        records["vmtrace_transcript"] = transcript
        for name in REGRESSIONS:
            transcript = qemu.command(f"usertests {name}", timeout=360)
            validate_guest_regression(name, transcript)
            records["regressions"][name] = transcript
    finally:
        if qemu is not None:
            qemu.stop()
    records["quick"] = run_driver(root, ["-q", "usertests"], cpus=2, timeout=540)
    records["full"] = run_driver(root, ["usertests"], cpus=1, timeout=900)
    return records


def environment_record(root: Path) -> str:
    uname = checked(["uname", "-srmo"], timeout=30).strip()
    qemu = checked(["qemu-system-riscv64", "--version"], timeout=30).splitlines()[0]
    database = checked(["make", "-pn"], cwd=root, timeout=60)
    match = re.search(r"^TOOLPREFIX := (\S*)$", database, re.MULTILINE)
    require(match is not None, "cannot resolve Makefile TOOLPREFIX")
    compiler = checked(
        [f"{match.group(1)}gcc", "--version"], timeout=30
    ).splitlines()[0]
    return f"{uname}; {qemu}; {compiler}; Python {sys.version.split()[0]}"


def write_report(path: Path, *, baseline: str, tutorial_commit: str,
                 patch_digest: str, runner_digest: str, environment: str,
                 shared_state_digest: str, shared_image_digest: str, records) -> None:
    lines = [
        "# Virtual memory, lazy faults, and NX 0.1.0 证据报告包",
        "",
        f"- 源码基线：`{baseline}`",
        f"- 走查时教程提交：`{tutorial_commit}`（候选 diff 单独复核）",
        "- patch：`resources/virtual-memory/lazy-nx.patch`",
        f"- patch SHA-256：`{patch_digest}`",
        f"- runner SHA-256：`{runner_digest}`",
        f"- 主机与工具：`{environment}`",
        "- 动态配置：focused/related/full 使用 CPUS=1；quick 使用 CPUS=2；128 MiB、临时源码导出和私有 `fs.img`",
        "",
        "## S 静态契约",
        "",
        "pinned baseline 上已检查 Sv39 walk、kernel/user root、TRAMPOLINE 同 VA/PA、",
        "TRAPFRAME 进程所有权、uservec/userret 的两次 satp+sfence、lazy `p->sz`、",
        "13/15 repair、cause 12 拒绝、copyin/out 与 copyinstr 差异、fork holes、",
        "exec commit/bad 和 freewalk teardown。patch 只存在于临时导出。",
        "",
        "## F/B page-table 与 fault 轨迹",
        "",
    ]
    if records is None:
        lines.append("`--static-only` 未运行 QEMU；动态轨迹为 N/A。")
    else:
        lines.extend(["```text", records["vmtrace"]["special"]])
        lines.extend(records["vmtrace"]["cases"])
        lines.extend([
            records["vmtrace"]["mutation"],
            records["vmtrace"]["exec"],
            records["vmtrace"]["pass"],
            "```",
            "",
        "四条 normal 路径均从 absent leaf 形成 `V|R|W|U`、无 `X` 的页，且 pipe payload 保真；",
            "invalid 与已映射只读 text 均不分配。NX 使用有效 `ret 0x00008067`，",
            "cause 12 令 child 以 -1 回收；同一指令在临时 `PTE_X` + `sfence.vma`",
            "后返回 0，排除了坏 opcode。exec 用有效 ELF 和两个合法长参数在临时",
            "映像建立后失败，旧 pid/marker 继续执行并以状态 42 区分成功 exec。",
            "",
            "## OOM、资源和回归",
            "",
            "fail_at=1/2/3 分别命中 data/L1/L0 分配尝试。前两种当场无页债；",
            "第三种允许一个空中间页由当前进程暂存，但无有效 leaf，child 被 wait",
            "后总 free-page 账本均精确恢复。",
            "",
            f"focused/related：`{', '.join(REGRESSIONS)}` 均出现所选测试记录与唯一 `ALL TESTS PASSED`；",
            "quick `CPUS=2` 与完整 `CPUS=1` 均各有且仅有一个 `ALL TESTS PASSED`。",
        ])
    lines.extend([
        "",
        "## S/F/B/C/R 与局限",
        "",
        "- S：源码/patch scope、映射权限、commit/rollback 和清理顺序。",
        "- F：四种首次物化、ELF text、exec 与 lazy/VM 回归。",
        "- B：invalid、permission、NX、三点模拟 OOM 与 late exec failure。",
        "- C：N/A；CPUS=2 quick 只是回归，xv6 没有同地址空间用户线程，本报告不证明 remote TLB shootdown。",
        "- R：N/A；私有镜像只用于隔离，不产生 crash/persistence 结论。",
        "",
        "测试 syscall、全局记录和 failpoint 是 tutorial audit seam；只在 CPUS=1",
        "focused 运行时 arm，改变时序且不是生产 API。A/D 位由硬件设置，host 只",
        "比较 V/R/W/X/U mask。一次运行不能穷尽内存压力或替代形式化证明。",
        "",
        "## 清理证明",
        "",
        f"- 共享工作树/索引/内容指纹：`{shared_state_digest}`（前后相同）",
        f"- 共享 `fs.img`：`{shared_image_digest}`（前后相同）",
        "- 临时导出已 `make clean`、逆向 patch，并逐文件恢复；QEMU/driver 进程组均消失。",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = args.report.expanduser().resolve() if args.report else None
    if report is not None:
        require(not report.is_relative_to(REPO_ROOT),
                "--report must be outside repository")
    baseline = load_baseline()
    tutorial_commit = checked(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT).strip()
    before_state = repo_state_digest()
    before_image = digest_or_missing(REPO_ROOT / "fs.img")
    records = None
    environment = ""
    with tempfile.TemporaryDirectory(prefix="xv6-virtual-memory-") as temporary:
        root = Path(temporary) / "source"
        export_baseline(root, baseline)
        original = snapshot(root)
        analyze_baseline(root)
        scope = patch_paths(root)
        require(scope == PATCH_PATHS, f"patch scope changed: {sorted(scope)}")
        applied = False
        try:
            apply_patch(root)
            applied = True
            analyze_patched(root)
            build(root)
            if report is not None:
                environment = environment_record(root)
            if not args.static_only:
                records = run_dynamic(root)
        finally:
            if applied:
                checked(["make", "clean"], cwd=root, timeout=180)
                apply_patch(root, reverse=True)
                require(snapshot(root) == original,
                        "temporary source did not return to pinned baseline")
    after_state = repo_state_digest()
    after_image = digest_or_missing(REPO_ROOT / "fs.img")
    require(after_state == before_state, "shared repository content or index changed")
    require(after_image == before_image, "shared fs.img changed")
    if report is not None:
        write_report(
            report,
            baseline=baseline,
            tutorial_commit=tutorial_commit,
            patch_digest=sha256(PATCH),
            runner_digest=sha256(Path(__file__)),
            environment=environment,
            shared_state_digest=before_state,
            shared_image_digest=before_image,
            records=records,
        )
    label = "static, build, cleanup" if args.static_only else (
        "static, F/B, focused, related, quick, full, cleanup"
    )
    print(f"virtual memory and lazy NX passed: {label}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LabError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
