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
import stat
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
PATCH = SCRIPT_DIR / "irqtrace.patch"
PATCH_PATHS = {
    "Makefile",
    "kernel/defs.h",
    "kernel/sysproc.c",
    "kernel/trap.c",
    "user/irqtrace.c",
}
TIMER_CAUSE = 0x8000000000000005


class LabError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise LabError(message)


def checked(command, *, cwd=None, timeout=180, env=None):
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr)[-8000:]
        raise LabError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{output}"
        )
    return result.stdout


def first_line(command, *, cwd=None):
    return checked(command, cwd=cwd, timeout=30).splitlines()[0]


def sha256(path):
    if not path.is_file():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = (
                stat.S_IMODE(path.stat().st_mode),
                sha256(path),
            )
    return result


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


def apply_patch(root, *, reverse=False):
    command = ["git", "apply", "--whitespace=error-all", "--unidiff-zero"]
    if reverse:
        command.append("-R")
    command.append(str(PATCH))
    checked(command, cwd=root)


def patch_paths(root):
    output = checked(
        ["git", "apply", "--numstat", "--unidiff-zero", str(PATCH)], cwd=root
    )
    paths = set()
    for line in output.splitlines():
        fields = line.split("\t", 2)
        require(len(fields) == 3, f"unexpected patch numstat: {line!r}")
        paths.add(fields[2])
    return paths


def ordered(text, tokens, label):
    positions = [text.find(token) for token in tokens]
    require(all(position >= 0 for position in positions), f"missing {label}: {tokens}")
    require(positions == sorted(positions), f"wrong {label} order: {tokens}")


def count_once(text, token, label):
    require(text.count(token) == 1, f"{label} is not unique: {token!r}")


def register_slots(text, opcode, base):
    matches = re.findall(
        rf"^[ \t]*{opcode}[ \t]+([a-z][a-z0-9]*),[ \t]*(\d+)\({base}\)[ \t]*$",
        text,
        re.MULTILINE,
    )
    return {(register, int(offset)) for register, offset in matches}


def check_machine_entry(entry, start, riscv):
    ordered(
        entry,
        [
            "_entry:",
            "la sp, stack0",
            "li a0, 1024*4",
            "csrr a1, mhartid",
            "addi a1, a1, 1",
            "mul a0, a0, a1",
            "add sp, sp, a0",
            "call start",
        ],
        "machine entry",
    )
    ordered(
        start,
        [
            "r_mstatus()",
            "MSTATUS_MPP_S",
            "w_mepc((uint64)main)",
            "w_satp(0)",
            "w_medeleg(0xffff)",
            "w_mideleg(0xffff)",
            "SIE_SEIE | SIE_STIE",
            "w_pmpaddr0",
            "  timerinit();",
            "r_mhartid()",
            "w_tp(id)",
            'asm volatile("mret")',
        ],
        "M-to-S transition",
    )
    require(
        re.search(r"^#define MSTATUS_MPP_S\s+\(1L << 11\)$", riscv, re.MULTILINE),
        "MPP_S definition changed",
    )
    require("w_stimecmp(r_time() + 1000000)" in start, "first timer is not armed")


def check_hart_initialization(main):
    ordered(
        main,
        [
            "if (cpuid() == 0)",
            "trapinit()",
            "trapinithart()",
            "plicinit()",
            "plicinithart()",
            "__atomic_thread_fence(__ATOMIC_SEQ_CST)",
            "started = 1",
        ],
        "hart-zero publication",
    )
    require(main.count("__atomic_thread_fence(__ATOMIC_SEQ_CST)") == 2, "hart fence count changed")
    else_start = main.find("} else {")
    require(else_start >= 0, "secondary-hart branch is missing")
    ordered(
        main[else_start:],
        [
            "while (started == 0)",
            "__atomic_thread_fence(__ATOMIC_SEQ_CST)",
            "kvminithart()",
            "trapinithart()",
            "plicinithart()",
        ],
        "secondary-hart acquire and initialization",
    )


def check_trap_vectors_and_devices(trap, plic):
    require("w_stvec((uint64)kernelvec)" in trap, "kernel vector is not installed")
    require("w_stvec(trampoline_uservec)" in trap, "user vector is not prepared")
    return_start = trap.find("prepare_return(void)")
    require(return_start >= 0, "prepare_return definition is missing")
    return_block = trap[return_start:]
    ordered(
        return_block,
        ["prepare_return(void)", "intr_off();", "w_stvec(trampoline_uservec)", "w_sepc(p->trapframe->epc)"],
        "return preparation",
    )
    require("r_scause() == 8" in trap, "user syscall cause is missing")
    require("0x8000000000000005L" in trap, "timer cause is missing")
    require("0x8000000000000009L" in trap, "external cause is missing")
    require("int irq = plic_claim()" in trap, "PLIC claim is missing")
    require("irq == UART0_IRQ" in trap and "uartintr()" in trap, "UART route is missing")
    require("irq == VIRTIO0_IRQ" in trap and "virtio_disk_intr()" in trap, "VirtIO route is missing")
    require("plic_complete(irq)" in trap, "PLIC completion is missing")
    require("*(uint32 *)PLIC_SCLAIM(hart)" in plic, "PLIC claim/complete register changed")


def check_trampoline(trampoline):
    uservec_start = trampoline.find("uservec:")
    userret_start = trampoline.find("userret:")
    require(
        uservec_start >= 0 and userret_start > uservec_start,
        "trampoline entry labels are missing or reordered",
    )
    uservec = trampoline[uservec_start:userret_start]
    userret = trampoline[userret_start:]
    ordered(
        uservec,
        ["uservec:", "csrw sscratch, a0", "csrw satp, t1", "jalr t0"],
        "uservec contract",
    )
    ordered(
        userret,
        ["userret:", "csrw satp, a0", "sret"],
        "userret contract",
    )
    user_slots = {
        ("ra", 40), ("sp", 48), ("gp", 56), ("tp", 64),
        ("t0", 72), ("t1", 80), ("t2", 88), ("s0", 96), ("s1", 104),
        ("a1", 120), ("a2", 128), ("a3", 136), ("a4", 144),
        ("a5", 152), ("a6", 160), ("a7", 168),
        ("s2", 176), ("s3", 184), ("s4", 192), ("s5", 200),
        ("s6", 208), ("s7", 216), ("s8", 224), ("s9", 232),
        ("s10", 240), ("s11", 248),
        ("t3", 256), ("t4", 264), ("t5", 272), ("t6", 280),
    }
    require(
        register_slots(uservec, "sd", "a0") == user_slots | {("t0", 112)},
        "uservec save slots changed",
    )
    require(
        register_slots(userret, "ld", "a0") == user_slots | {("a0", 112)},
        "userret restore slots changed",
    )
    ordered(uservec, ["csrr t0, sscratch", "sd t0, 112(a0)"], "saved user a0")


def check_kernelvec(kernelvec):
    require("addi sp, sp, -256" in kernelvec, "kernelvec frame size changed")
    require("sd ra, 0(sp)" in kernelvec and "call kerneltrap" in kernelvec, "kernelvec save/call missing")
    require(
        "# sd sp, 8(sp)" in kernelvec
        and "# not tp" in kernelvec
        and "in case we moved CPUs" in kernelvec,
        "kernelvec sp/tp exception missing",
    )
    kernel_slots = {
        ("ra", 0), ("gp", 16), ("t0", 32), ("t1", 40), ("t2", 48),
        ("a0", 72), ("a1", 80), ("a2", 88), ("a3", 96),
        ("a4", 104), ("a5", 112), ("a6", 120), ("a7", 128),
        ("t3", 216), ("t4", 224), ("t5", 232), ("t6", 240),
    }
    require(register_slots(kernelvec, "sd", "sp") == kernel_slots, "kernelvec save slots changed")
    require(register_slots(kernelvec, "ld", "sp") == kernel_slots, "kernelvec restore slots changed")
    require(kernelvec.rstrip().endswith("sret"), "kernelvec does not return with sret")


def check_swtch(swtch, proc_h):
    for register in ("ra", "sp", *[f"s{i}" for i in range(12)]):
        require(f"sd {register}," in swtch, f"swtch does not save {register}")
        require(f"ld {register}," in swtch, f"swtch does not restore {register}")
    require(swtch.rstrip().endswith("ret"), "swtch does not return through restored ra")
    require("struct context" in proc_h and "uint64 s11;" in proc_h, "context shape changed")


def check_timer_routing(trap):
    clock_start = trap.find("clockintr()")
    devintr_start = trap.find("devintr()", clock_start)
    require(clock_start >= 0 and devintr_start > clock_start, "clock/devintr blocks are missing")
    ordered(
        trap[clock_start:devintr_start],
        ["if (cpuid() == 0)", "acquire(&tickslock)", "ticks++", "wakeup(&ticks)", "release(&tickslock)", "w_stimecmp"],
        "clock interrupt contract",
    )
    devintr_block = trap[devintr_start:]
    ordered(
        devintr_block,
        ["scause == 0x8000000000000005L", "clockintr();", "return 2;"],
        "timer dispatch contract",
    )
    usertrap_base = trap.find("usertrap(void)")
    prepare_base = trap.find("prepare_return(void)", usertrap_base)
    ordered(
        trap[usertrap_base:prepare_base],
        ["which_dev = devintr()", "if (which_dev == 2)", "yield();", "prepare_return();"],
        "user timer yield contract",
    )
    kerneltrap_base = trap.find("kerneltrap()", prepare_base)
    clock_base = trap.find("clockintr()", kerneltrap_base)
    ordered(
        trap[kerneltrap_base:clock_base],
        ["which_dev = devintr()", "if (which_dev == 2 && myproc() != 0)", "yield();", "w_sepc(sepc)", "w_sstatus(sstatus)"],
        "kernel timer yield contract",
    )


def check_instrumentation(root):
    paths = patch_paths(root)
    require(paths == PATCH_PATHS, f"patch paths differ: {sorted(paths)}")
    trap = (root / "kernel/trap.c").read_text(encoding="utf-8")
    sysproc = (root / "kernel/sysproc.c").read_text(encoding="utf-8")
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    user = (root / "user/irqtrace.c").read_text(encoding="utf-8")

    require("$U/_irqtrace\\" in makefile, "irqtrace is not in UPROGS")
    require("strncmp(myproc()->name, \"irqtrace\"" in sysproc, "uptime arm gate is missing")
    ordered(trap, ["irqtrace_arm(void)", "intr_off()", "irqtrace_stage = 1"], "trace arm")
    for marker in (
        "IRQTRACE 1 USERTRAP",
        "IRQTRACE 2 CLOCK",
        "IRQTRACE 3 YIELD",
        "IRQTRACE 4 RESUME",
    ):
        count_once(trap, marker, "trace marker")
    usertrap_start = trap.find("usertrap(void)")
    prepare_return_start = trap.find("prepare_return(void)", usertrap_start)
    require(
        usertrap_start >= 0 and prepare_return_start > usertrap_start,
        "usertrap block is missing",
    )
    ordered(
        trap[usertrap_start:prepare_return_start],
        [
            "IRQTRACE 1 USERTRAP",
            "which_dev = devintr()",
            "if (which_dev != 2 || ticks != irqtrace_before + 1)",
            "IRQTRACE 2 CLOCK",
            "if (which_dev == 2)",
            'panic("irqtrace before yield state")',
            "IRQTRACE 3 YIELD",
            "yield();",
            'panic("irqtrace after yield state")',
            "IRQTRACE 4 RESUME",
            "irqtrace_stage = 2",
            "prepare_return();",
        ],
        "instrumented timer path",
    )
    require(
        trap.count("if (p->state != RUNNING)") == 2,
        "trace must check RUNNING state on both sides of yield",
    )
    require("ticks != irqtrace_before + 1" in trap, "single-tick oracle is missing")
    require("for (int i = 0; i < 10000000; i++)" in user, "user-mode window changed")
    require(user.count("uptime()") == 2, "irqtrace must sample uptime twice in source")
    require("while (after == before)" in user, "irqtrace completion loop is missing")

    checked(["make", "-j2", "kernel/kernel", "user/_irqtrace", "fs.img"], cwd=root, timeout=300)
    require((root / "user/_irqtrace").is_file(), "irqtrace executable was not built")
    require((root / "user/irqtrace.asm").is_file(), "irqtrace disassembly was not generated")
    return {
        "paths": sorted(paths),
        "patch_sha256": sha256(PATCH),
    }


def analyze_baseline(root, baseline):
    check_machine_entry(
        (root / "kernel/entry.S").read_text(encoding="utf-8"),
        (root / "kernel/start.c").read_text(encoding="utf-8"),
        (root / "kernel/riscv.h").read_text(encoding="utf-8"),
    )
    check_hart_initialization((root / "kernel/main.c").read_text(encoding="utf-8"))
    trap = (root / "kernel/trap.c").read_text(encoding="utf-8")
    check_trap_vectors_and_devices(
        trap,
        (root / "kernel/plic.c").read_text(encoding="utf-8"),
    )
    check_timer_routing(trap)
    check_trampoline((root / "kernel/trampoline.S").read_text(encoding="utf-8"))
    check_kernelvec((root / "kernel/kernelvec.S").read_text(encoding="utf-8"))
    check_swtch(
        (root / "kernel/swtch.S").read_text(encoding="utf-8"),
        (root / "kernel/proc.h").read_text(encoding="utf-8"),
    )
    checked(["make", "-j2", "kernel/kernel"], cwd=root, timeout=300)
    return {
        "baseline": baseline,
        "timer_cause": TIMER_CAUSE,
        "contracts": ["uservec", "userret", "kernelvec", "swtch"],
    }


def analyze_instrumentation(root, baseline_static):
    instrumentation = check_instrumentation(root)
    return {**baseline_static, **instrumentation}


def process_group_exists(group_id):
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if proc.poll() is None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    deadline = time.monotonic() + 5
    while process_group_exists(proc.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if process_group_exists(proc.pid):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while process_group_exists(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
    require(not process_group_exists(proc.pid), f"process group remains: {proc.pid}")


def read_until(proc, output, marker, timeout, *, start=0):
    deadline = time.monotonic() + timeout
    while marker not in output[start:]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            tail = output[-4000:].decode("utf-8", "replace")
            raise LabError(f"timed out waiting for {marker!r}\n{tail}")
        ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
        if not ready:
            if proc.poll() is not None:
                raise LabError(f"QEMU exited early with {proc.returncode}")
            continue
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            raise LabError("QEMU output closed")
        output.extend(chunk.replace(b"\r", b""))


def command_transcript(proc, output, command, timeout):
    start = len(output)
    proc.stdin.write(command.encode("ascii") + b"\n")
    proc.stdin.flush()
    read_until(proc, output, b"$ ", timeout, start=start)
    return bytes(output[start:]).decode("utf-8", "replace")


def validate_trace(transcript):
    pattern = re.compile(
        r"irqtrace\nIRQTRACE ARM\n"
        r"IRQTRACE 1 USERTRAP pid=(\d+) scause=0x([0-9a-f]+) sepc=0x([0-9a-f]+)\n"
        r"IRQTRACE 2 CLOCK pid=(\d+) ticks=(\d+)->(\d+)\n"
        r"IRQTRACE 3 YIELD pid=(\d+) state=RUNNING\n"
        r"IRQTRACE 4 RESUME pid=(\d+) state=RUNNING\n"
        r"IRQTRACE USER ticks=(\d+)->(\d+) spin=1\n\$ "
    )
    match = pattern.fullmatch(transcript)
    require(match is not None, f"unexpected irqtrace transcript:\n{transcript}")
    values = [int(value, 16 if index in (1, 2) else 10) for index, value in enumerate(match.groups())]
    pid1, cause, sepc, pid2, tick0, tick1, pid3, pid4, user0, user1 = values
    require(pid1 > 0 and {pid1, pid2, pid3, pid4} == {pid1}, "trace pid changed")
    require(cause == TIMER_CAUSE, f"wrong timer cause: 0x{cause:x}")
    require(sepc > 0, "timer sepc is not a user address")
    require(tick1 == tick0 + 1, "clock trace did not advance exactly one tick")
    require(user0 <= tick0 and user1 >= tick1 and user1 > user0, "user tick observation mismatch")
    return {"pid": pid1, "sepc": sepc, "tick_before": tick0, "tick_after": tick1, "transcript": transcript}


def run_focused(root):
    proc = subprocess.Popen(
        ["make", "CPUS=1", "qemu"],
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output = bytearray()
    try:
        read_until(proc, output, b"$ ", 60)
        control = command_transcript(proc, output, "usertests reparent", 120)
        expected_control = (
            "usertests reparent\nusertests starting\n"
            "test reparent: OK\nALL TESTS PASSED\n$ "
        )
        require(control == expected_control, f"unexpected control transcript:\n{control}")
        require("IRQTRACE" not in control, "trace marker fired without the named trigger")
        trace = validate_trace(command_transcript(proc, output, "irqtrace", 120))
        proc.stdin.write(b"\x01x")
        proc.stdin.flush()
        proc.wait(timeout=10)
        require(proc.returncode == 0, f"QEMU exited with {proc.returncode}")
        return {"control": control, "trace": trace}
    finally:
        stop_process_group(proc)


def run_driver(root, command, timeout):
    env = os.environ.copy()
    env["CPUS"] = "1"
    proc = subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LabError(f"driver timed out: {' '.join(command)}")
    finally:
        stop_process_group(proc)
    require(proc.returncode == 0, f"driver failed: {' '.join(command)}\n{output[-8000:]}")
    require(
        len(re.findall(r"^ALL TESTS PASSED$", output, re.MULTILINE)) == 1,
        f"driver marker mismatch: {' '.join(command)}\n{output[-8000:]}",
    )
    require("SOME TESTS FAILED" not in output, f"driver reported failure: {command}")
    require("IRQTRACE" not in output, f"trace gate fired during regression: {command}")
    return {"command": " ".join(command), "marker": "ALL TESTS PASSED"}


def write_report(path, tutorial_commit, environment, static, focused, tests, before):
    trace = focused["trace"]
    lines = [
        "# 启动、陷阱、中断与汇编边界报告包",
        "",
        f"- 教程提交：`{tutorial_commit}`",
        f"- pinned source baseline：`{static['baseline']}`",
        f"- patch：`resources/boot-traps-and-interrupts/irqtrace.patch`；SHA-256 `{static['patch_sha256']}`",
        f"- 环境：`{environment['qemu']}`；`{environment['toolchain']}`；`CPUS=1`、128 MiB",
        "- 隔离：一个临时源码导出、一份私有 `fs.img`、每次 QEMU/driver 使用独立进程组",
        "",
        "## S：启动与汇编契约",
        "",
        "```text",
        "kernel/entry.S:_entry -> kernel/start.c:start -> mret -> kernel/main.c:main",
        "kernel/trampoline.S:uservec -> kernel/trap.c:usertrap -> kernel/trap.c:devintr",
        "kernel/trap.c:prepare_return -> kernel/trampoline.S:userret -> sret -> U mode",
        "kernel/kernelvec.S:kernelvec -> kernel/trap.c:kerneltrap -> sret -> S mode",
        "kernel/swtch.S:swtch -> restored ra -> ret",
        "```",
        "",
        "- `_entry` 以 `mhartid` 选择每 hart 栈；`start()` 设置 MPP/mepc/delegation/PMP/timer/tp 后执行 `mret`。",
        "- 每个 hart 在 M mode 的 `start()` 中先设置自己的 timer；hart 0 发布 `started=1` 前完成全局初始化，两个 `__ATOMIC_SEQ_CST` fence 分别位于发布和等待边界。",
        "- `uservec/userret` 保存完整用户状态并切页表；`kernelvec` 用 256 字节内核栈帧保存 `ra/gp/t0-t6/a0-a7`，由帧增减恢复 sp，保留 hart-local tp，s0-s11 由 C ABI 保护；`swtch` 交换 `ra/sp/s0-s11` 的 kernel context。",
        "- external interrupt 经 PLIC claim 路由 UART/VirtIO，处理后 complete；timer cause 经 `devintr()` 进入 `clockintr()`，仅 hart 0 更新全局 ticks，返回 2 后由 `usertrap()` 或有进程上下文的 `kerneltrap()` 调用 `yield()`。",
        "",
        "## B：未触发 gate",
        "",
        "```text",
        focused["control"].rstrip(),
        "```",
        "control 完整结束且 `IRQTRACE` marker 数为 0；watchdog 不作为该否定结论。",
        "",
        "## F：受控 timer preemption",
        "",
        "```text",
        trace["transcript"].rstrip(),
        "```",
        f"同一 pid `{trace['pid']}` 在 user `sepc=0x{trace['sepc']:x}` 进入；tick `{trace['tick_before']}->{trace['tick_after']}` 精确增加 1。",
        "",
        "## 回归、资源与局限",
        "",
        "- focused：上面的 exact transcript 与事件顺序通过。",
    ]
    for label, test in zip(("related quick", "full"), tests):
        lines.append(f"- {label}：`{test['command']}` -> `{test['marker']}`。")
    lines.extend(
        [
            "- 允许副作用：仅临时树构建产物、私有镜像、guest 进程和短寿命进程组。",
            "- 资源结果：trace 进程退出；QEMU/driver 进程组消失；`make clean` 后逆向 patch，临时树快照恢复。",
            f"- 原工作树：`{(before['status'] or '(clean)').replace(chr(10), '; ')}`（前后相同）。",
            f"- 共享 `fs.img` SHA-256：`{before['image']}`（前后相同）。",
            "- `C/R` 不适用：`CPUS=1` 的 tutorial-only gate 不证明多 hart 顺序、设备 DMA、持久化或恢复。",
            "- instrumentation、串口输出和 10,000,000 次用户循环会改变时序；证据只支持 marker 间的 happens-before，不支持真实延迟或公平性。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Verify boot/trap/interrupt boundaries")
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    required = ["git", "make"]
    if not args.static_only:
        required.append("qemu-system-riscv64")
    for command in required:
        require(shutil.which(command) is not None, f"required command not found: {command}")
    require(PATCH.is_file(), f"missing patch: {PATCH}")
    report = args.report.expanduser().resolve() if args.report else None
    if report is not None:
        require(not report.is_relative_to(REPO_ROOT), "--report must be outside repository")

    baseline = load_baseline()
    tutorial_commit = checked(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT).strip()
    before = {
        "status": checked(["git", "status", "--short"], cwd=REPO_ROOT).strip(),
        "image": sha256(REPO_ROOT / "fs.img"),
    }
    environment = {"qemu": "not run", "toolchain": ""}
    with tempfile.TemporaryDirectory(prefix="xv6-irqtrace-lab.") as temp:
        temp_path = Path(temp)
        root = temp_path / "repo"
        export_baseline(root, baseline)
        baseline_snapshot = snapshot(root)
        baseline_static = analyze_baseline(root, baseline)
        print(
            "baseline boundaries passed: boot, hart, vectors, uservec, "
            "userret, kernelvec, swtch, plic",
            flush=True,
        )
        checked(["git", "apply", "--check", "--unidiff-zero", str(PATCH)], cwd=root)
        apply_patch(root)
        static = analyze_instrumentation(root, baseline_static)
        database = checked(["make", "-pn"], cwd=root, timeout=60)
        match = re.search(r"^TOOLPREFIX := (\S*)$", database, re.MULTILINE)
        require(match is not None, "cannot resolve Makefile TOOLPREFIX")
        environment["toolchain"] = first_line([f"{match.group(1)}gcc", "--version"])
        print("instrumentation passed: patch scope, event bindings, build", flush=True)

        if args.static_only:
            focused = None
            tests = []
        else:
            environment["qemu"] = first_line(["qemu-system-riscv64", "--version"])
            focused = run_focused(root)
            print("focused interrupt passed: control=0-markers usertrap->clock->yield->resume", flush=True)
            tests = []
            for command, timeout in (
                (["./test-xv6.py", "-q", "usertests"], 420),
                (["./test-xv6.py", "usertests"], 720),
            ):
                print(f"running regression: {' '.join(command)}", flush=True)
                tests.append(run_driver(root, command, timeout))
                print(f"regression passed: {' '.join(command)}", flush=True)

        checked(["make", "clean"], cwd=root, timeout=120)
        apply_patch(root, reverse=True)
        require(snapshot(root) == baseline_snapshot, "temporary source did not cleanly restore")

    require(not temp_path.exists(), f"temporary directory remains: {temp_path}")
    after = {
        "status": checked(["git", "status", "--short"], cwd=REPO_ROOT).strip(),
        "image": sha256(REPO_ROOT / "fs.img"),
    }
    require(before == after, f"shared repository changed: before={before}, after={after}")
    if args.static_only:
        print("interrupt lab passed: static, patch cleanup")
        return
    if report is not None:
        write_report(report, tutorial_commit, environment, static, focused, tests, before)
        print(f"report written: {report}")
    print("interrupt lab passed: static, control, focused, related, full, cleanup")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.SubprocessError, LabError, json.JSONDecodeError) as exc:
        print(f"interrupt lab failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
