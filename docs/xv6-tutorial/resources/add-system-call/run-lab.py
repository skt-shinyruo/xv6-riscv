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
PATCH = SCRIPT_DIR / "sysprobe.patch"
HANDWRITTEN = {
    "kernel/syscall.c",
    "kernel/syscall.h",
    "kernel/sysproc.c",
    "user/user.h",
    "user/usertests.c",
    "user/usys.pl",
}
DISPATCH_PATHS = ("kernel/syscall.c", "kernel/sysproc.c")


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


def patch_paths(root):
    output = checked(["git", "apply", "--numstat", str(PATCH)], cwd=root)
    paths = set()
    for line in output.splitlines():
        fields = line.split("\t", 2)
        require(len(fields) == 3, f"unexpected patch numstat line: {line!r}")
        paths.add(fields[2])
    return paths


def apply_patch(root, *, reverse=False, paths=()):
    command = ["git", "apply", "--whitespace=error-all"]
    if reverse:
        command.append("-R")
    command.extend(f"--include={path}" for path in paths)
    command.append(str(PATCH))
    checked(command, cwd=root)


def build(root, *, image):
    targets = ["kernel/kernel", "user/_usertests"]
    if image:
        targets.append("fs.img")
    checked(["make", "-j2", *targets], cwd=root, timeout=300)


def make_clean(root):
    checked(["make", "clean"], cwd=root, timeout=120)


def toolchain_version(root):
    database = checked(["make", "-pn"], cwd=root, timeout=60)
    match = re.search(r"^TOOLPREFIX := (\S*)$", database, re.MULTILINE)
    require(match is not None, "cannot resolve Makefile TOOLPREFIX")
    return first_line([f"{match.group(1)}gcc", "--version"])


def extract_symbol_block(text, symbol):
    match = re.search(
        rf"^[0-9a-f]+ <{re.escape(symbol)}>:\n(.*?)(?=^[0-9a-f]+ <[^>]+>:\n|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    require(match is not None, f"missing disassembly symbol: {symbol}")
    return match.group(1)


def analyze_final(root, baseline):
    paths = patch_paths(root)
    require(paths == HANDWRITTEN, f"patch paths differ: {sorted(paths)}")
    require("user/usys.S" not in paths, "generated user/usys.S entered the patch")

    sources = {
        path: (root / path).read_text(encoding="utf-8") for path in HANDWRITTEN
    }
    require(
        sources["user/user.h"].count("int sysprobe(int);") == 1,
        "sysprobe user declaration is not unique",
    )
    require(
        sources["user/usys.pl"].count('entry("sysprobe");') == 1,
        "sysprobe generator entry is not unique",
    )

    numbers = [
        (name, int(value))
        for name, value in re.findall(
            r"^#define SYS_(\w+)\s+(\d+)$",
            sources["kernel/syscall.h"],
            re.MULTILINE,
        )
    ]
    require(("sysprobe", 22) in numbers, "SYS_sysprobe must be 22")
    require(len({value for _, value in numbers}) == len(numbers), "syscall number reused")
    require(
        max(value for name, value in numbers if name != "sysprobe") == 21,
        "22 is not next",
    )

    syscall_source = sources["kernel/syscall.c"]
    require(
        syscall_source.count("extern uint64 sys_sysprobe(void);") == 1,
        "sys_sysprobe extern is not unique",
    )
    require(
        len(re.findall(r"\[SYS_sysprobe\]\s+sys_sysprobe", syscall_source)) == 1,
        "sysprobe dispatch entry is not unique",
    )
    require(
        "num > 0 && num < NELEM(syscalls) && syscalls[num]" in syscall_source,
        "safe dispatch guard changed",
    )

    handler = sources["kernel/sysproc.c"]
    require(handler.count("sys_sysprobe(void)") == 1, "sys_sysprobe handler is not unique")
    require(
        re.search(
            r"sys_sysprobe\(void\).*?int value;.*?argint\(0, &value\);.*?return value;",
            handler,
            re.DOTALL,
        ),
        "sys_sysprobe does not return argint(0) unchanged",
    )

    tests = sources["user/usertests.c"]
    require(tests.count("sysprobetest(char *s)") == 1, "sysprobe test is not unique")
    require(
        tests.count('{sysprobetest, "sysprobe"}') == 1,
        "sysprobe quicktests registration is not unique",
    )
    require(
        re.search(
            r"sysprobetest\(char \*s\)\s*\{\s*"
            r"int values\[\] = \{0, 12345, -7\};\s*"
            r"for \(int i = 0; i < sizeof\(values\) / sizeof\(values\[0\]\); i\+\+\) \{\s*"
            r"int got = sysprobe\(values\[i\]\);\s*"
            r"if \(got != values\[i\]\) \{\s*"
            r'printf\("%s: sysprobe\(%d\) returned %d, expected %d\\n",\s*'
            r"s, values\[i\], got,\s*values\[i\]\);\s*"
            r"exit\(1\);\s*\}\s*\}\s*\}",
            tests,
        ),
        "sysprobe test must call and compare all three int probes",
    )

    generated = checked(["perl", "user/usys.pl"], cwd=root).encode("utf-8")
    checked(["make", "user/usys.S"], cwd=root, timeout=120)
    require(generated == (root / "user/usys.S").read_bytes(), "usys replay differs")
    usys = generated.decode("utf-8")
    require(
        re.search(
            r"\.global sysprobe\nsysprobe:\n li a7, SYS_sysprobe\n ecall\n ret\n",
            usys,
        ),
        "generated sysprobe stub contract is incomplete",
    )

    build(root, image=False)
    block = extract_symbol_block(
        (root / "user/usertests.asm").read_text(encoding="utf-8"), "sysprobe"
    )
    require(re.search(r"\bli\s+a7,22\b", block), "stub does not load syscall 22")
    require(re.search(r"\becall\b", block), "stub lacks ecall")
    require(re.search(r"\bret\b", block), "stub lacks ret")
    return {
        "baseline": baseline,
        "paths": sorted(paths),
        "number": 22,
        "values": [0, 12345, -7],
    }


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


def run_exact_qemu(root, *, mode):
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
        start = len(output)
        proc.stdin.write(b"usertests sysprobe\n")
        proc.stdin.flush()
        read_until(proc, output, b"$ ", 90, start=start)
        transcript = bytes(output[start:]).decode("utf-8", "replace")
        if mode == "boundary":
            match = re.fullmatch(
                r"usertests sysprobe\nusertests starting\n"
                r"test sysprobe: ([1-9][0-9]*) usertests: unknown sys call 22\n"
                r"sysprobe: sysprobe\(0\) returned -1, expected 0\n"
                r"FAILED\nSOME TESTS FAILED\n\$ ",
                transcript,
            )
            require(match is not None, f"unexpected boundary transcript:\n{transcript}")
            result = {"pid": int(match.group(1)), "transcript": transcript}
        else:
            expected = (
                "usertests sysprobe\nusertests starting\n"
                "test sysprobe: OK\nALL TESTS PASSED\n$ "
            )
            require(transcript == expected, f"unexpected final transcript:\n{transcript}")
            result = {"transcript": transcript}
        proc.stdin.write(b"\x01x")
        proc.stdin.flush()
        proc.wait(timeout=10)
        require(proc.returncode == 0, f"QEMU exited with {proc.returncode}")
        return result
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
        f"driver completion marker mismatch: {' '.join(command)}\n{output[-8000:]}",
    )
    require("SOME TESTS FAILED" not in output, f"driver reported failure: {command}")
    require("unknown sys call 22" not in output, f"driver hit unknown sysprobe: {command}")
    return {
        "command": " ".join(command),
        "marker": "ALL TESTS PASSED",
    }


def require_boundary_source(root):
    for path in DISPATCH_PATHS:
        require(
            "sys_sysprobe" not in (root / path).read_text(encoding="utf-8"),
            f"boundary still has dispatch code in {path}",
        )
    require(
        "SYS_sysprobe 22" in (root / "kernel/syscall.h").read_text(encoding="utf-8"),
        "boundary lost syscall number",
    )
    require(
        'entry("sysprobe");' in (root / "user/usys.pl").read_text(encoding="utf-8"),
        "boundary lost generated stub input",
    )


def write_report(path, tutorial_commit, environment, static, boundary, final, tests, before):
    lines = [
        "# `sysprobe` 有界修改报告包",
        "",
        f"- 教程提交：`{tutorial_commit}`",
        f"- pinned source baseline：`{static['baseline']}`",
        f"- patch：`resources/add-system-call/sysprobe.patch`；SHA-256 `{sha256(PATCH)}`",
        f"- 环境：`{environment['qemu']}`；`{environment['toolchain']}`；`CPUS=1`、128 MiB",
        "- 隔离：一个临时源码导出；私有 `fs.img`；每次 driver 使用独立进程组",
        "",
        "## 范围与 S oracle",
        "",
        f"- 唯一编号：`SYS_sysprobe={static['number']}`，原最大编号为 `21`。",
        f"- 六个手写文件：`{'`, `'.join(static['paths'])}`。",
        "- `user/usys.S` 由 `user/usys.pl` 重放且逐字节一致；反汇编 stub 为 `li a7,22`、`ecall`、`ret`。",
        "- handler 只执行 `argint(0, &value)` 并返回 `value`；dispatch guard 保持不变。",
        "",
        "## F/B oracle 契约",
        "",
        "| 维度 | 输入或触发器 | 预期结果 | 允许副作用 | 资源结果 | cleanup |",
        "|---|---|---|---|---|---|",
        "| B | 保留 number/stub/test，只逆向 dispatch/handler 后运行 `usertests sysprobe` | unknown 22 恰好一次、返回 -1、测试失败、shell 恢复 | 仅私有树构建产物、私有 `fs.img` 和短寿命 QEMU 进程 | 无宿主共享资源变化；guest 进程由 xv6 回收 | 终止 QEMU，恢复 dispatch/handler，再全量清理 |",
        "| F | 完整 patch 下运行三个 int 值和 focused/quick/full driver | 0/12345/-7 精确往返；三个 driver 各有唯一成功 marker | 仅私有树构建产物、私有 `fs.img` 和各自进程组 | handler 不分配资源；每个 driver 进程组最终不存在 | `make clean`、逆向 patch、快照与共享仓库复核 |",
        "",
        "## B oracle：缺少 dispatch",
        "",
        f"- 动态 pid：`{boundary['pid']}`；unknown 诊断恰好一次；返回 `-1`；shell 恢复。",
        "```text",
        boundary["transcript"].rstrip(),
        "```",
        "",
        "## F oracle 与回归",
        "",
        f"- 测试值：`{static['values']}`，逐项按 C `int` 精确比较。",
        "```text",
        final["transcript"].rstrip(),
        "```",
        "",
    ]
    labels = ("定向", "相关 quick", "完整")
    for label, test in zip(labels, tests):
        lines.append(f"- {label}：`{test['command']}` -> `{test['marker']}`。")
    lines.extend(
        [
            "- `C/R`：`N/A`。handler 不共享可变状态、不阻塞、不分配资源，也不写持久化数据。",
            "",
            "## 清理与局限",
            "",
            "- patch 已逆向，`make clean` 后临时源码的逐文件哈希和 mode 与导出时完全一致。",
            f"- 原工作树状态：`{(before['status'] or '(clean)').replace(chr(10), '; ')}`（前后相同）。",
            f"- 共享 `fs.img` SHA-256：`{before['image']}`（前后相同）。",
            "- quick 与完整回归不能替代 ABI 链路静态核对；该实验不证明并发、恢复、复杂参数或用户指针语义。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Verify the bounded sysprobe lab")
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    required = ["git", "make", "perl"]
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
    environment = {
        "qemu": "not run",
        "toolchain": "",
    }
    boundary = final = None
    tests = []

    with tempfile.TemporaryDirectory(prefix="xv6-sysprobe-lab.") as temp:
        temp_path = Path(temp)
        root = temp_path / "repo"
        export_baseline(root, baseline)
        baseline_snapshot = snapshot(root)
        checked(["git", "apply", "--check", str(PATCH)], cwd=root)
        apply_patch(root)
        static = analyze_final(root, baseline)
        environment["toolchain"] = toolchain_version(root)
        print(
            "static sysprobe passed: paths=6 number=22 values=0,12345,-7 "
            "stub=li-a7-22/ecall/ret",
            flush=True,
        )

        if not args.static_only:
            environment["qemu"] = first_line(["qemu-system-riscv64", "--version"])
            apply_patch(root, reverse=True, paths=DISPATCH_PATHS)
            require_boundary_source(root)
            make_clean(root)
            build(root, image=True)
            boundary = run_exact_qemu(root, mode="boundary")
            print("boundary QEMU passed: unknown-once, return=-1, shell-recovered", flush=True)

            apply_patch(root, paths=DISPATCH_PATHS)
            make_clean(root)
            build(root, image=True)
            final = run_exact_qemu(root, mode="final")
            print("final QEMU passed: 0,12345,-7 exact", flush=True)

            driver_specs = [
                (["./test-xv6.py", "sysprobe"], 240),
                (["./test-xv6.py", "-q", "usertests"], 420),
                (["./test-xv6.py", "usertests"], 720),
            ]
            for command, timeout in driver_specs:
                print(f"running regression: {' '.join(command)}", flush=True)
                tests.append(run_driver(root, command, timeout))
                print(f"regression passed: {' '.join(command)}", flush=True)

        make_clean(root)
        apply_patch(root, reverse=True)
        require(snapshot(root) == baseline_snapshot, "temporary source did not cleanly restore")

    require(not temp_path.exists(), f"temporary directory remains: {temp_path}")
    after = {
        "status": checked(["git", "status", "--short"], cwd=REPO_ROOT).strip(),
        "image": sha256(REPO_ROOT / "fs.img"),
    }
    require(before == after, f"shared repository changed: before={before}, after={after}")
    if args.static_only:
        print("sysprobe lab passed: static, patch cleanup")
        return
    if report is not None:
        write_report(report, tutorial_commit, environment, static, boundary, final, tests, before)
        print(f"report written: {report}")
    print("sysprobe lab passed: static, boundary, final, focused, related, full, cleanup")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.SubprocessError, LabError, json.JSONDecodeError) as exc:
        print(f"sysprobe lab failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
