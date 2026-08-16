#!/usr/bin/env python3

import argparse
import hashlib
import io
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
TUTORIAL_ROOT = SCRIPT_DIR.parents[1]
REPO_ROOT = SCRIPT_DIR.parents[3]
MANIFEST = TUTORIAL_ROOT / "curriculum.json"
GDB_TEMPLATE = SCRIPT_DIR / "trace.gdb.in"
MASK64 = (1 << 64) - 1
TRAMPOLINE = (1 << 38) - 4096
TRAPFRAME = TRAMPOLINE - 4096


class TraceError(RuntimeError):
    pass


def checked(command, *, cwd=None, timeout=180):
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr)[-8000:]
        raise TraceError(f"command failed ({result.returncode}): {' '.join(command)}\n{output}")
    return result.stdout


def first_line(command, *, cwd=None):
    return checked(command, cwd=cwd, timeout=30).splitlines()[0]


def toolchain_version(root):
    database = checked(["make", "-pn"], cwd=root, timeout=60)
    match = re.search(r"^TOOLPREFIX := (\S*)$", database, re.MULTILINE)
    if not match:
        raise TraceError("cannot resolve Makefile TOOLPREFIX")
    return first_line([f"{match.group(1)}gcc", "--version"])


def sha256(path):
    if not path.is_file():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_baseline():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return data["release"]["baseline_commit"]


def export_baseline(root, baseline):
    archive = subprocess.run(
        ["git", "archive", "--format=tar", baseline],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        stream.extractall(root, filter="data")


def build(root, full):
    targets = ["kernel/kernel", "user/_usertests"]
    if full:
        targets.append("fs.img")
    checked(["make", "-C", str(root), *targets], timeout=240)


def parse_instructions(text):
    instructions = []
    pattern = re.compile(
        r"^\s*([0-9a-f]+):\s+([0-9a-f]+)\s+([.a-z0-9]+)(?:\s+(.*?))?\s*$"
    )
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            instructions.append(
                {
                    "address": int(match.group(1), 16),
                    "encoding": match.group(2),
                    "mnemonic": match.group(3),
                    "operands": (match.group(4) or "").replace(" ", ""),
                }
            )
    return instructions


def symbol_address(path, symbol):
    pattern = re.compile(rf"^([0-9a-f]+)\s+{re.escape(symbol)}$", re.MULTILINE)
    match = pattern.search(path.read_text(encoding="utf-8"))
    if not match:
        raise TraceError(f"missing symbol in {path}: {symbol}")
    return int(match.group(1), 16)


def next_address(instructions, predicate, description):
    for index, instruction in enumerate(instructions[:-1]):
        if predicate(instruction):
            return instruction["address"], instructions[index + 1]["address"]
    raise TraceError(f"cannot locate instruction: {description}")


def analyze_static(root, baseline):
    syscall_header = (root / "kernel/syscall.h").read_text(encoding="utf-8")
    numbers = {
        name: int(value)
        for name, value in re.findall(r"^#define SYS_(\w+)\s+(\d+)$", syscall_header, re.MULTILINE)
    }
    if numbers.get("getpid") != 11:
        raise TraceError(f"expected SYS_getpid=11, got {numbers.get('getpid')}")
    unknown = max(numbers.values()) + 1

    syscall_source = (root / "kernel/syscall.c").read_text(encoding="utf-8")
    if not re.search(r"\[SYS_getpid\]\s+sys_getpid", syscall_source):
        raise TraceError("getpid dispatch entry is missing")
    if "num < NELEM(syscalls)" not in syscall_source:
        raise TraceError("syscall bounds guard is missing")
    if "unknown sys call %d" not in syscall_source:
        raise TraceError("unknown-system-call diagnostic is missing")

    riscv = (root / "kernel/riscv.h").read_text(encoding="utf-8")
    memlayout = (root / "kernel/memlayout.h").read_text(encoding="utf-8")
    if not re.search(r"#define MAXVA\s+\(1L << \(9 \+ 9 \+ 9 \+ 12 - 1\)\)", riscv):
        raise TraceError("MAXVA expression changed")
    if not re.search(r"#define PGSIZE\s+4096\b", riscv):
        raise TraceError("PGSIZE expression changed")
    if "#define TRAMPOLINE (MAXVA - PGSIZE)" not in memlayout:
        raise TraceError("TRAMPOLINE expression changed")
    if "#define TRAPFRAME (TRAMPOLINE - PGSIZE)" not in memlayout:
        raise TraceError("TRAPFRAME expression changed")

    user_asm = (root / "user/usertests.asm").read_text(encoding="utf-8")
    block_match = re.search(
        r"^[0-9a-f]+ <getpid>:\n(.*?)(?=^[0-9a-f]+ <[^>]+>:\n)",
        user_asm,
        re.MULTILINE | re.DOTALL,
    )
    if not block_match:
        raise TraceError("cannot find generated getpid stub")
    stub_instructions = parse_instructions(block_match.group(1))
    if len(stub_instructions) != 3:
        raise TraceError(f"expected three getpid instructions, got {stub_instructions}")
    li, ecall, ret = stub_instructions
    if (li["mnemonic"], li["operands"]) != ("li", "a7,11"):
        raise TraceError(f"unexpected getpid number load: {li}")
    if ecall["mnemonic"] != "ecall" or ecall["encoding"] != "00000073":
        raise TraceError(f"unexpected ecall instruction: {ecall}")
    if ret["mnemonic"] != "ret" or ret["address"] != ecall["address"] + 4:
        raise TraceError(f"ecall does not advance to E+4: {ecall}, {ret}")

    reparent_match = re.search(
        r"^[0-9a-f]+ <reparent>:\n(.*?)(?=^[0-9a-f]+ <[^>]+>:\n)",
        user_asm,
        re.MULTILINE | re.DOTALL,
    )
    if not reparent_match:
        raise TraceError("cannot find reparent in usertests disassembly")
    reparent_instructions = parse_instructions(reparent_match.group(1))
    caller_return = None
    for index, instruction in enumerate(reparent_instructions[:-1]):
        if instruction["mnemonic"] == "jal" and instruction["operands"].endswith("<getpid>"):
            caller_return = reparent_instructions[index + 1]["address"]
            break
    if caller_return is None:
        raise TraceError("cannot find reparent's first getpid call")

    kernel_sym = root / "kernel/kernel.sym"
    trampoline_link = symbol_address(kernel_sym, "trampoline")
    uservec_link = symbol_address(kernel_sym, "uservec")
    userret_link = symbol_address(kernel_sym, "userret")
    usertrap_link = symbol_address(kernel_sym, "usertrap")
    kerneltrap_link = symbol_address(kernel_sym, "kerneltrap")
    if uservec_link != trampoline_link:
        raise TraceError("uservec no longer aliases trampoline start")

    kernel_asm = (root / "kernel/kernel.asm").read_text(encoding="utf-8")
    kernel_instructions = parse_instructions(kernel_asm)
    uservec_instructions = [
        item
        for item in kernel_instructions
        if trampoline_link <= item["address"] < userret_link
    ]
    userret_instructions = [
        item for item in kernel_instructions if userret_link <= item["address"] < userret_link + 0x200
    ]
    usertrap_instructions = [
        item
        for item in kernel_instructions
        if usertrap_link <= item["address"] < kerneltrap_link
    ]
    saved_link, kstack_link = next_address(
        uservec_instructions,
        lambda item: item["mnemonic"] == "ld" and item["operands"] == "sp,8(a0)",
        "uservec ld sp,8(a0)",
    )
    _, kpagetable_link = next_address(
        uservec_instructions,
        lambda item: item["mnemonic"] == "csrw" and item["operands"] == "satp,t1",
        "uservec csrw satp,t1",
    )
    _, user_satp_link = next_address(
        userret_instructions,
        lambda item: item["mnemonic"] == "csrw" and item["operands"] == "satp,a0",
        "userret csrw satp,a0",
    )
    sret = next(
        (item for item in userret_instructions if item["mnemonic"] == "sret"), None
    )
    if sret is None:
        raise TraceError("cannot locate userret sret")
    syscall_calls = [
        item
        for item in usertrap_instructions
        if item["mnemonic"] == "jal" and item["operands"].endswith("<syscall>")
    ]
    if len(syscall_calls) != 1:
        raise TraceError(f"cannot identify usertrap syscall call: {syscall_calls}")
    advanced_candidates = [
        item
        for item in usertrap_instructions
        if item["address"] < syscall_calls[0]["address"]
        and item["mnemonic"] == "csrr"
        and item["operands"].endswith(",sstatus")
    ]
    if not advanced_candidates:
        raise TraceError("cannot locate the pre-intr_on checkpoint")
    advanced_link = advanced_candidates[-1]["address"]

    relocate = lambda address: TRAMPOLINE + address - trampoline_link
    return {
        "baseline": baseline,
        "sysno": numbers["getpid"],
        "unknown": unknown,
        "stub": li["address"],
        "caller_return": caller_return,
        "li_width": ecall["address"] - li["address"],
        "ecall": ecall["address"],
        "after": ret["address"],
        "ecall_encoding": ecall["encoding"],
        "trampoline": TRAMPOLINE,
        "trapframe": TRAPFRAME,
        "uservec": relocate(uservec_link),
        "saved": relocate(saved_link),
        "kstack": relocate(kstack_link),
        "kpagetable": relocate(kpagetable_link),
        "advanced": advanced_link,
        "userret": relocate(userret_link),
        "user_satp": relocate(user_satp_link),
        "restored": relocate(sret["address"]),
    }


def free_port():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def render_gdb(static, port, mutate, destination):
    replacements = {
        "PORT": str(port),
        "STUB": hex(static["stub"]),
        "ECALL": hex(static["ecall"]),
        "UNKNOWN": str(static["unknown"]),
        "MUTATE": "1" if mutate else "0",
        "USERVEC": hex(static["uservec"]),
        "SAVED": hex(static["saved"]),
        "KSTACK": hex(static["kstack"]),
        "KPAGETABLE": hex(static["kpagetable"]),
        "ADVANCED": hex(static["advanced"]),
        "TRAPFRAME": hex(static["trapframe"]),
        "USERRET": hex(static["userret"]),
        "USER_SATP": hex(static["user_satp"]),
        "RESTORED": hex(static["restored"]),
        "AFTER": hex(static["after"]),
    }
    text = GDB_TEMPLATE.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace(f"@{key}@", value)
    if re.search(r"@[A-Z_]+@", text):
        raise TraceError("unresolved GDB template token")
    destination.write_text(text, encoding="utf-8")


def stop_process(proc):
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def read_until(proc, output, marker, timeout):
    deadline = time.monotonic() + timeout
    while marker not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            tail = output[-4000:].decode("utf-8", "replace")
            raise TraceError(f"timed out waiting for {marker!r}\n{tail}")
        ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
        if not ready:
            if proc.poll() is not None:
                raise TraceError(f"QEMU exited early with {proc.returncode}")
            continue
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            raise TraceError("QEMU output closed")
        output.extend(chunk.replace(b"\r", b""))


def parse_markers(data):
    markers = []
    for line in data.replace(b"\r", b"").decode("utf-8", "replace").splitlines():
        if not line.startswith("TRACE "):
            continue
        parts = line.split()
        name = parts[1]
        values = {}
        for item in parts[2:]:
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            values[key] = int(value, 0)
        markers.append((name, values))
    return markers


def require(condition, message):
    if not condition:
        raise TraceError(message)


def validate_trace(static, mutate, gdb_output, qemu_output):
    markers = parse_markers(gdb_output)
    expected_names = [
        "ARMED", "STUB", "ECALL_BEFORE", "ECALL", "USERVEC", "SAVED",
        "KSTACK", "KPAGETABLE", "USERTRAP", "ADVANCED", "SYSCALL",
    ]
    if not mutate:
        expected_names.append("HANDLER")
    expected_names.extend(
        ["PREPARE", "USERRET", "USER_SATP", "RESTORED", "AFTER", "DONE"]
    )
    names = [name for name, _ in markers]
    require(names == expected_names, f"unexpected checkpoint order: {names}")
    points = {name: values for name, values in markers}
    for name in ("STUB", "ECALL_BEFORE", "ECALL", "AFTER"):
        require(points[name]["priv"] == 0, f"{name} is not in user mode")
    supervisor_points = [
        "USERVEC", "SAVED", "KSTACK", "KPAGETABLE", "USERTRAP",
        "ADVANCED", "SYSCALL", "PREPARE", "USERRET", "USER_SATP",
        "RESTORED",
    ]
    if not mutate:
        supervisor_points.append("HANDLER")
    for name in supervisor_points:
        require(points[name]["priv"] == 1, f"{name} is not in supervisor mode")
    expected_num = static["unknown"] if mutate else static["sysno"]
    expected_result = MASK64 if mutate else points["SYSCALL"]["pid"]

    require(points["STUB"]["pc"] == static["stub"], "stub PC mismatch")
    require(points["STUB"]["ra"] == static["caller_return"], "trace did not start at reparent's first getpid")
    before_ecall = points["ECALL_BEFORE"]
    require(points["ECALL"]["pc"] == static["ecall"], "ecall PC mismatch")
    require(before_ecall["pc"] == static["ecall"], "pre-mutation ecall PC mismatch")
    require(before_ecall["a7"] == static["sysno"], "generated stub did not load SYS_getpid")
    for register in ("priv", "pc", "sp", "ra", "a0", "satp"):
        require(
            before_ecall[register] == points["ECALL"][register],
            f"GDB mutation changed {register}",
        )
    require(before_ecall["ra"] == static["caller_return"], "ecall caller mismatch")
    require(points["ECALL"]["a7"] == expected_num, "ecall a7 mismatch")
    require(points["USERVEC"]["pc"] == static["uservec"], "uservec PC mismatch")
    require(points["USERVEC"]["sepc"] == static["ecall"], "uservec sepc mismatch")
    require(points["USERVEC"]["scause"] == 8, "uservec scause is not user ecall")
    require(points["USERVEC"]["sstatus"] & 0x100 == 0, "SPP does not identify user origin")
    require(points["USERVEC"]["stvec"] == static["uservec"], "stvec does not target runtime uservec")
    require(points["USERVEC"]["satp"] == points["ECALL"]["satp"], "hardware changed satp")
    require(points["USERVEC"]["sp"] == points["ECALL"]["sp"], "hardware changed sp")

    require(points["SAVED"]["pc"] == static["saved"], "saved checkpoint mismatch")
    require(points["SAVED"]["tf_sp"] == points["ECALL"]["sp"], "user sp was not saved")
    require(points["SAVED"]["tf_a0"] == points["ECALL"]["a0"], "user a0 was not saved")
    require(points["SAVED"]["tf_a7"] == expected_num, "user a7 was not saved")
    require(points["KSTACK"]["pc"] == static["kstack"], "kernel-stack checkpoint mismatch")
    require(points["KSTACK"]["sp"] != points["ECALL"]["sp"], "stack did not switch")
    require(points["KSTACK"]["satp"] == points["ECALL"]["satp"], "satp switched before checkpoint")
    require(points["KPAGETABLE"]["pc"] == static["kpagetable"], "kernel-satp checkpoint mismatch")
    require(points["KPAGETABLE"]["satp"] != points["ECALL"]["satp"], "satp did not switch")
    require(points["KPAGETABLE"]["sp"] == points["KSTACK"]["sp"], "kernel sp changed unexpectedly")

    require(points["USERTRAP"]["satp"] == points["KPAGETABLE"]["satp"], "usertrap page table mismatch")
    require(points["USERTRAP"]["sp"] == points["KSTACK"]["sp"], "usertrap stack mismatch")
    require(points["USERTRAP"]["scause"] == 8, "usertrap lost original scause before intr_on")
    require(points["USERTRAP"]["sepc"] == static["ecall"], "usertrap sepc mismatch")
    advanced = points["ADVANCED"]
    require(advanced["pc"] == static["advanced"], "pre-intr_on checkpoint mismatch")
    require(advanced["scause"] == 8, "pre-intr_on scause mismatch")
    require(advanced["sepc"] == static["ecall"], "pre-intr_on sepc mismatch")
    require(advanced["sstatus"] & 0x2 == 0, "interrupts enabled before intr_on")
    require(advanced["tf_epc"] == static["after"], "epc was not advanced before intr_on")
    require(advanced["tf_a7"] == expected_num, "pre-intr_on a7 mismatch")

    syscall = points["SYSCALL"]
    prepare = points["PREPARE"]
    require(syscall["pid"] > 0, "invalid dynamic pid")
    require(syscall["tf_epc"] == static["after"], "epc was not advanced by four")
    require(syscall["tf_a7"] == expected_num, "dispatch number mismatch")
    require(syscall["tf_a0"] == points["ECALL"]["a0"], "saved a0 changed before dispatch")
    require(prepare["pid"] == syscall["pid"], "process changed during trace")
    require(prepare["handler_hits"] == (0 if mutate else 1), "handler hit count mismatch")
    require(prepare["tf_epc"] == static["after"], "return epc mismatch")
    require(prepare["tf_a7"] == expected_num, "return a7 mismatch")
    require(prepare["tf_a0"] == expected_result, "trapframe result mismatch")
    if not mutate:
        require(points["HANDLER"]["pid"] == syscall["pid"], "handler pid mismatch")

    userret = points["USERRET"]
    require(userret["pc"] == static["userret"], "runtime userret PC mismatch")
    require(userret["satp"] == points["KPAGETABLE"]["satp"], "userret did not start on kernel satp")
    require(userret["sp"] == points["KSTACK"]["sp"], "userret did not start on kernel stack")
    require(userret["target_satp"] == points["ECALL"]["satp"], "wrong target user satp")
    require(userret["sepc"] == static["after"], "userret sepc mismatch")
    require(userret["sstatus"] & 0x100 == 0, "userret SPP is not user")
    require(points["USER_SATP"]["pc"] == static["user_satp"], "user-satp checkpoint mismatch")
    require(points["USER_SATP"]["satp"] == points["ECALL"]["satp"], "user satp not restored")
    require(points["USER_SATP"]["sp"] == points["KSTACK"]["sp"], "stack restored too early")

    restored = points["RESTORED"]
    after = points["AFTER"]
    require(restored["pc"] == static["restored"], "sret checkpoint mismatch")
    require(restored["sp"] == points["ECALL"]["sp"], "user sp not restored")
    require(restored["a0"] == expected_result, "user a0 not restored")
    require(restored["a7"] == expected_num, "user a7 not restored")
    require(restored["satp"] == points["ECALL"]["satp"], "sret uses wrong satp")
    require(restored["sepc"] == static["after"], "sret destination mismatch")
    require(after["pc"] == static["after"], "did not reach E+4")
    require(after["ra"] == static["caller_return"], "post-sret caller return mismatch")
    require(after["sp"] == points["ECALL"]["sp"], "post-sret sp mismatch")
    require(after["a0"] == expected_result, "post-sret result mismatch")
    require(after["a7"] == expected_num, "post-sret a7 mismatch")
    require(after["satp"] == points["ECALL"]["satp"], "post-sret satp mismatch")

    console = qemu_output.replace(b"\r", b"").decode("utf-8", "replace")
    diagnostics = re.findall(r"(\d+) usertests: unknown sys call (\d+)\n", console)
    if mutate:
        require(
            len(diagnostics) == 1,
            f"expected one unknown diagnostic, got {diagnostics}; console tail={console[-1000:]!r}",
        )
        require(int(diagnostics[0][0]) == syscall["pid"], "diagnostic pid mismatch")
        require(int(diagnostics[0][1]) == static["unknown"], "diagnostic number mismatch")
    else:
        require(not diagnostics, f"normal trace printed unknown diagnostic: {diagnostics}")
    return points


def run_scenario(root, static, mutate):
    port = free_port()
    script = root / ("trace-unknown.gdb" if mutate else "trace-normal.gdb")
    render_gdb(static, port, mutate, script)
    qemu_command = [
        "qemu-system-riscv64", "-machine", "virt", "-bios", "none",
        "-kernel", str(root / "kernel/kernel"), "-m", "128M", "-smp", "1",
        "-nographic", "-global", "virtio-mmio.force-legacy=false",
        "-drive", f"file={root / 'fs.img'},if=none,format=raw,id=x0",
        "-device", "virtio-blk-device,drive=x0,bus=virtio-mmio-bus.0",
        "-gdb", f"tcp:127.0.0.1:{port}",
    ]
    qemu = None
    gdb = None
    qemu_output = bytearray()
    gdb_output = bytearray()
    try:
        qemu = subprocess.Popen(
            qemu_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        read_until(qemu, qemu_output, b"$ ", 60)
        gdb = subprocess.Popen(
            ["gdb-multiarch", "-q", "-nx", "-batch", str(root / "kernel/kernel"), "-x", str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 120
        sent = False
        open_streams = {qemu.stdout: qemu_output, gdb.stdout: gdb_output}
        while gdb.poll() is None or gdb.stdout in open_streams:
            if time.monotonic() >= deadline:
                raise TraceError("timed out during GDB trace")
            streams = list(open_streams)
            if not streams:
                break
            ready, _, _ = select.select(streams, [], [], 1.0)
            for stream in ready:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    open_streams.pop(stream, None)
                    continue
                open_streams[stream].extend(chunk.replace(b"\r", b""))
            if not sent and b"TRACE ARMED" in gdb_output:
                qemu.stdin.write(b"usertests reparent\n")
                qemu.stdin.flush()
                sent = True
            if gdb.poll() is not None and gdb.stdout not in open_streams:
                break
        require(sent, "GDB never armed the user breakpoint")
        require(gdb.returncode == 0, f"GDB exited with {gdb.returncode}\n{gdb_output[-8000:].decode('utf-8', 'replace')}")
        if mutate:
            read_until(
                qemu,
                qemu_output,
                f"unknown sys call {static['unknown']}\n".encode("ascii"),
                5,
            )
        points = validate_trace(static, mutate, bytes(gdb_output), bytes(qemu_output))
        qemu.stdin.write(b"\x01x")
        qemu.stdin.flush()
        qemu.wait(timeout=10)
        require(qemu.returncode == 0, f"QEMU exited with {qemu.returncode}")
        return {
            "name": "unknown" if mutate else "normal",
            "points": points,
            "diagnostic": f"{points['SYSCALL']['pid']} usertests: unknown sys call {static['unknown']}" if mutate else "none",
        }
    finally:
        stop_process(gdb)
        stop_process(qemu)


def format_values(values):
    rendered = []
    for key, value in values.items():
        rendered.append(f"{key}=0x{value:x}")
    return ", ".join(rendered)


def write_report(path, tutorial_commit, static, environment, scenarios, before, after):
    lines = [
        "# `getpid` 往返报告包",
        "",
        f"- 教程提交：`{tutorial_commit}`",
        f"- pinned source baseline：`{static['baseline']}`",
        f"- 环境：`{environment['qemu']}`；`{environment['gdb']}`；`{environment['toolchain']}`",
        "- 配置：`CPUS=1`，128 MiB，三个临时导出（一次静态、两次动态）、两个私有 `fs.img`、loopback GDB 端口",
        "",
        "## 静态身份",
        "",
        f"- `SYS_getpid={static['sysno']}`；未知编号 `{static['unknown']}`。",
        f"- stub=`0x{static['stub']:x}`；`li` 长度={static['li_width']}；`E=0x{static['ecall']:x}`；`E+4=0x{static['after']:x}`；encoding=`{static['ecall_encoding']}`。",
        f"- `reparent` 首次 `jal getpid` 的返回地址=`0x{static['caller_return']:x}`；开中断前 checkpoint=`0x{static['advanced']:x}`。",
        f"- runtime `uservec=0x{static['uservec']:x}`；`userret=0x{static['userret']:x}`；`TRAPFRAME=0x{static['trapframe']:x}`。",
        "",
    ]
    for scenario in scenarios:
        title = "正常编号" if scenario["name"] == "normal" else "未知编号"
        lines.extend([
            f"## {title} checkpoint",
            "",
            "| checkpoint | 记录 |",
            "|---|---|",
        ])
        for name, values in scenario["points"].items():
            if not values:
                continue
            lines.append(f"| `{name}` | `{format_values(values)}` |")
        lines.extend(["", f"诊断：`{scenario['diagnostic']}`。", ""])
    lines.extend([
        "## Oracle、资源与限制",
        "",
        "- `S`：编号、生成 stub、`ecall` 编码、`E+4` 与 runtime trampoline 地址关系通过。",
        "- `F`：动态 pid 经 handler、`trapframe->a0` 和用户 `a0` 一致返回；user/kernel 页表与栈按 checkpoint 切换。",
        "- `B`：编号 22 不命中 handler，精确诊断一次，`a0=UINT64_MAX`，仍只到达一次 `E+4`。",
        f"- 原工作树状态前后：`{(before['status'] or '(clean)').replace(chr(10), '; ')}` / `{(after['status'] or '(clean)').replace(chr(10), '; ')}`。",
        f"- 共享 `fs.img` SHA-256 前后：`{before['image']}` / `{after['image']}`。",
        "- 两组 QEMU/GDB 进程均已退出；私有镜像、临时导出、命令文件与端口随临时目录清理。",
        "- 原始 `scause/sepc/sstatus` 在 `uservec`/`usertrap` 开中断前采集；断点与单步改变时序。",
        "- `full-trampoline-page-table-contract` 仍保留：本报告不证明 PTE、映射建立、TLB 或多 hart 迁移安全性。",
        "- `C/R` 不适用：单 hart 调试不建立并发或恢复结论。",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def print_static(static):
    print(
        "static trace passed: "
        f"SYS_getpid={static['sysno']} unknown={static['unknown']} "
        f"stub=0x{static['stub']:x} E=0x{static['ecall']:x} "
        f"E+4=0x{static['after']:x} caller=0x{static['caller_return']:x} "
        f"uservec=0x{static['uservec']:x} "
        f"userret=0x{static['userret']:x}"
    )


def main():
    parser = argparse.ArgumentParser(description="Trace one xv6 getpid round trip")
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    for command in ("git", "make", "qemu-system-riscv64", "gdb-multiarch"):
        if shutil.which(command) is None:
            raise TraceError(f"required command not found: {command}")
    if args.report is not None:
        report = args.report.expanduser().resolve()
        if report.is_relative_to(REPO_ROOT):
            raise TraceError("--report must be outside the repository")
    else:
        report = None

    baseline = load_baseline()
    tutorial_commit = checked(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT).strip()
    before = {
        "status": checked(["git", "status", "--short"], cwd=REPO_ROOT).strip(),
        "image": sha256(REPO_ROOT / "fs.img"),
    }
    environment = {
        "qemu": first_line(["qemu-system-riscv64", "--version"]),
        "gdb": first_line(["gdb-multiarch", "--version"]),
        "toolchain": "",
    }

    with tempfile.TemporaryDirectory(prefix="xv6-syscall-static.") as temp:
        static_root = Path(temp) / "repo"
        export_baseline(static_root, baseline)
        build(static_root, full=False)
        static = analyze_static(static_root, baseline)
        environment["toolchain"] = toolchain_version(static_root)
    print_static(static)
    if args.static_only:
        return

    scenarios = []
    for mutate in (False, True):
        with tempfile.TemporaryDirectory(prefix="xv6-syscall-trace.") as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            export_baseline(root, baseline)
            build(root, full=True)
            scenario_static = analyze_static(root, baseline)
            require(scenario_static == static, "scenario build changed static identity")
            scenario = run_scenario(root, static, mutate)
            scenarios.append(scenario)
            print(f"{scenario['name']} dynamic trace passed")
        require(not temp_path.exists(), f"temporary directory remains: {temp_path}")

    after = {
        "status": checked(["git", "status", "--short"], cwd=REPO_ROOT).strip(),
        "image": sha256(REPO_ROOT / "fs.img"),
    }
    require(before == after, f"shared repository changed: before={before}, after={after}")
    if report is not None:
        write_report(report, tutorial_commit, static, environment, scenarios, before, after)
        print(f"report written: {report}")
    print("getpid round-trip passed: static, normal, unknown, cleanup")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.SubprocessError, TraceError, json.JSONDecodeError) as exc:
        print(f"trace failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
