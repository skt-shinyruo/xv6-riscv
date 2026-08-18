#!/usr/bin/env python3
"""Isolated oracle and publication runner for the filesystem learning unit."""

import argparse
import hashlib
import io
import json
import os
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
PATCH = RESOURCE_DIR / "filesystem-audit.patch"
PATCH_PATHS = {
    "Makefile", "kernel/bio.c", "kernel/defs.h", "kernel/file.c",
    "kernel/fs.c", "kernel/fsaudit.c", "kernel/fsaudit.h", "kernel/main.c",
    "kernel/syscall.c", "kernel/syscall.h", "kernel/sysfile.c",
    "kernel/sysproc.c", "user/fstrace.c", "user/user.h", "user/usys.pl",
}


class LabError(RuntimeError):
    pass


LEDGER = {
    "blocks", "inodes", "itable_active", "itable_refs", "file_objects",
    "file_refs", "buf_refs",
}
SCHEMAS = {
    "BASE": LEDGER,
    "RW": {
        "generation", "path_steps", "inum", "type", "nlink", "size",
        "write", "read", "checksum", "direct0", "direct11", "indirect",
        "indirect0", "data_blocks", "open_seq", "file_write_seq",
        "inode_write_seq", "direct_seq", "buffer_seq", "indirect_seq",
        "file_read_seq", "inode_read_seq", "buffer_block", "buffer_valid",
        "buffer_ref", "buffer_locked", "status", "cleanup",
    },
    "LINK": {
        "generation", "inum", "nlink0", "nlink1", "nlink2", "nlink3",
        "path_old", "path_alias", "read", "first", "last", "open_fd",
        "blocks_held", "inodes_held", "status", "cleanup",
    },
    "TRUNC": {
        "generation", "inum_fd0", "inum_fd1", "before_size", "before_blocks",
        "after_size", "after_blocks", "peer_size", "rewrite", "final_size",
        "status", "cleanup",
    },
    "FDFAIL": {
        "generation", "filled", "create", "check_fd", "nlink", "size",
        "blocks_held", "inodes_held", "status", "cleanup",
    },
    "LINKFAIL": {
        "generation", "fail_at", "eligible", "fired", "link", "target",
        "nlink_before", "nlink_after", "blocks_before", "blocks_after",
        "inodes_before", "inodes_after", "status", "cleanup",
    },
    "ALLOCFAIL": {
        "generation", "fail_at", "eligible", "fired", "write",
        "size_before", "size_after", "indirect_before", "indirect_after",
        "indirect_data", "blocks_before", "blocks_after", "inodes_before",
        "inodes_after", "status", "cleanup",
    },
    "AFTER": LEDGER | {"phase"},
    "PASS": {"cases", "cleanup"},
}
ORDER = (
    "BASE", "RW", "AFTER", "LINK", "AFTER", "TRUNC", "AFTER",
    "FDFAIL", "AFTER", "LINKFAIL", "AFTER", "ALLOCFAIL", "AFTER", "PASS",
)


def require(condition, message):
    if not condition:
        raise LabError(message)


def parse_fields(phase, tokens):
    fields = {}
    for token in tokens:
        require(token.count("=") == 1, f"malformed {phase} field: {token}")
        key, value = token.split("=", 1)
        require(key not in fields, f"duplicate {phase} field: {key}")
        fields[key] = value
    require(set(fields) == SCHEMAS[phase],
            f"{phase} fields changed: {sorted(set(fields) ^ SCHEMAS[phase])}")
    parsed = {}
    for key, value in fields.items():
        if phase == "AFTER" and key == "phase":
            parsed[key] = value
            continue
        require(value.lstrip("-").isdigit(),
                f"non-integer {phase} field: {key}={value}")
        parsed[key] = int(value)
    return parsed


def parse_trace(text):
    markers = []
    for line in text.replace("\r", "").splitlines():
        if not line.startswith("FS "):
            continue
        parts = line.split()
        require(len(parts) >= 3, f"malformed FS marker: {line}")
        phase = parts[1]
        require(phase in SCHEMAS, f"unexpected FS marker: {phase}")
        markers.append((phase, parse_fields(phase, parts[2:])))
    require(tuple(phase for phase, _ in markers) == ORDER,
            "filesystem marker order/count changed")
    return markers


def validate_fstrace(text):
    markers = parse_trace(text)
    values = [fields for _, fields in markers]
    base = values[0]
    require(base["blocks"] > 0 and base["inodes"] > 0 and
            base["itable_active"] > 0 and base["itable_refs"] >=
            base["itable_active"] and base["file_objects"] > 0 and
            base["file_refs"] >= base["file_objects"] and
            base["buf_refs"] == 0, "invalid filesystem BASE ledger")

    after_indices = (2, 4, 6, 8, 10, 12)
    phases = ("RW", "LINK", "TRUNC", "FDFAIL", "LINKFAIL", "ALLOCFAIL")
    for index, phase in zip(after_indices, phases):
        after = values[index]
        require(after["phase"] == phase, f"wrong AFTER phase for {phase}")
        require({key: after[key] for key in LEDGER} == base,
                f"{phase} resource ledger did not return to BASE")

    rw = values[1]
    require((rw["generation"], rw["path_steps"], rw["type"], rw["nlink"]) ==
            (1, 2, 2, 1) and rw["inum"] > 0, "RW path/inode identity changed")
    require(rw["size"] == 12305 and rw["write"] == rw["read"] == 12305 and
            rw["checksum"] == 867834 and rw["data_blocks"] == 13,
            "RW byte or direct/indirect count changed")
    addresses = (rw["direct0"], rw["direct11"], rw["indirect"], rw["indirect0"])
    require(all(address > 0 for address in addresses) and
            len(set(addresses)) == len(addresses), "RW block ownership changed")
    sequence = [rw[key] for key in (
        "open_seq", "file_write_seq", "inode_write_seq", "direct_seq",
        "buffer_seq", "indirect_seq", "file_read_seq", "inode_read_seq")]
    require(all(seq > 0 for seq in sequence) and len(set(sequence)) == len(sequence) and
            sequence == sorted(sequence), "RW layer/event order changed")
    require(rw["buffer_block"] == rw["direct0"] and rw["buffer_valid"] == 1 and
            rw["buffer_ref"] >= 1 and rw["buffer_locked"] == 1 and
            rw["status"] == 0 and rw["cleanup"] == 1,
            "RW buffer state or result changed")

    link = values[3]
    require(link["generation"] == 2 and link["inum"] > 0 and
            (link["nlink0"], link["nlink1"], link["nlink2"], link["nlink3"]) ==
            (1, 2, 1, 0), "link/unlink nlink state changed")
    require(link["path_old"] == link["path_alias"] == -1 and
            link["read"] == 5 and link["first"] == 104 and link["last"] == 111 and
            link["open_fd"] >= 0 and link["blocks_held"] == base["blocks"] + 1 and
            link["inodes_held"] == base["inodes"] + 1 and
            link["status"] == 0 and link["cleanup"] == 1,
            "open-unlinked inode ownership changed")

    trunc = values[5]
    require(trunc["generation"] == 3 and trunc["inum_fd0"] > 0 and
            trunc["inum_fd0"] == trunc["inum_fd1"] and
            trunc["before_size"] == 2048 and trunc["before_blocks"] == 2 and
            trunc["after_size"] == trunc["after_blocks"] == trunc["peer_size"] == 0 and
            trunc["rewrite"] == trunc["final_size"] == 3 and
            trunc["status"] == 0 and trunc["cleanup"] == 1,
            "O_TRUNC ownership/result changed")

    fdfail = values[7]
    require(fdfail["generation"] == 4 and fdfail["filled"] == 12 and
            fdfail["create"] == -1 and fdfail["check_fd"] >= 0 and
            fdfail["nlink"] == 1 and fdfail["size"] == 0 and
            fdfail["blocks_held"] == base["blocks"] and
            fdfail["inodes_held"] == base["inodes"] + 1 and
            fdfail["status"] == 0 and fdfail["cleanup"] == 1,
            "fd-exhaustion create side effect changed")

    linkfail = values[9]
    require(linkfail["generation"] == 5 and linkfail["fail_at"] == 1 and
            linkfail["eligible"] == linkfail["fired"] == 1 and
            linkfail["link"] == linkfail["target"] == -1 and
            linkfail["nlink_before"] == linkfail["nlink_after"] == 1 and
            linkfail["blocks_before"] == linkfail["blocks_after"] and
            linkfail["inodes_before"] == linkfail["inodes_after"] and
            linkfail["status"] == 0 and linkfail["cleanup"] == 1,
            "link rollback changed")

    alloc = values[11]
    require(alloc["generation"] == 6 and alloc["fail_at"] == 2 and
            alloc["eligible"] == 2 and alloc["fired"] == 1 and
            alloc["write"] == -1 and alloc["size_before"] ==
            alloc["size_after"] == 12288 and alloc["indirect_before"] == 0 and
            alloc["indirect_after"] > 0 and alloc["indirect_data"] == 0 and
            alloc["blocks_after"] == alloc["blocks_before"] + 1 and
            alloc["inodes_after"] == alloc["inodes_before"] and
            alloc["status"] == 0 and alloc["cleanup"] == 1,
            "late block-allocation failure contract changed")
    require(values[13] == {"cases": 6, "cleanup": 1},
            "filesystem PASS marker changed")
    return {phase: fields for phase, fields in markers if phase not in ("AFTER",)}


def self_test():
    ledger = "blocks=100 inodes=10 itable_active=3 itable_refs=3 " \
             "file_objects=4 file_refs=7 buf_refs=0"
    good = "\n".join((
        f"FS BASE {ledger}",
        "FS RW generation=1 path_steps=2 inum=20 type=2 nlink=1 size=12305 "
        "write=12305 read=12305 checksum=867834 direct0=1000 direct11=1011 "
        "indirect=1012 indirect0=1013 data_blocks=13 open_seq=1 "
        "file_write_seq=2 inode_write_seq=3 direct_seq=4 buffer_seq=5 "
        "indirect_seq=6 file_read_seq=7 inode_read_seq=8 buffer_block=1000 "
        "buffer_valid=1 buffer_ref=1 buffer_locked=1 status=0 cleanup=1",
        f"FS AFTER phase=RW {ledger}",
        "FS LINK generation=2 inum=21 nlink0=1 nlink1=2 nlink2=1 nlink3=0 "
        "path_old=-1 path_alias=-1 read=5 first=104 last=111 open_fd=3 "
        "blocks_held=101 inodes_held=11 status=0 cleanup=1",
        f"FS AFTER phase=LINK {ledger}",
        "FS TRUNC generation=3 inum_fd0=22 inum_fd1=22 "
        "before_size=2048 before_blocks=2 "
        "after_size=0 after_blocks=0 peer_size=0 rewrite=3 final_size=3 "
        "status=0 cleanup=1",
        f"FS AFTER phase=TRUNC {ledger}",
        "FS FDFAIL generation=4 filled=12 create=-1 check_fd=3 nlink=1 size=0 "
        "blocks_held=100 inodes_held=11 status=0 cleanup=1",
        f"FS AFTER phase=FDFAIL {ledger}",
        "FS LINKFAIL generation=5 fail_at=1 eligible=1 fired=1 link=-1 "
        "target=-1 nlink_before=1 nlink_after=1 blocks_before=100 "
        "blocks_after=100 inodes_before=10 inodes_after=10 "
        "status=0 cleanup=1",
        f"FS AFTER phase=LINKFAIL {ledger}",
        "FS ALLOCFAIL generation=6 fail_at=2 eligible=2 fired=1 write=-1 "
        "size_before=12288 size_after=12288 indirect_before=0 "
        "indirect_after=1100 indirect_data=0 blocks_before=112 "
        "blocks_after=113 inodes_before=11 inodes_after=11 "
        "status=0 cleanup=1",
        f"FS AFTER phase=ALLOCFAIL {ledger}",
        "FS PASS cases=6 cleanup=1",
    )) + "\n"
    validate_fstrace(good)
    mutations = (
        good.replace(" cleanup=1", " forged=1 cleanup=1", 1),
        good.replace("inum=20 type=2", "inum=20 inum=20 type=2", 1),
        good.replace("path_steps=2 ", "", 1),
        good.replace("path_steps=2", "path_steps=two", 1),
        good.replace("checksum=867834", "checksum=867833", 1),
        good.replace("indirect0=1013", "indirect0=1000", 1),
        good.replace("buffer_seq=5", "buffer_seq=4", 1),
        good.replace("buffer_locked=1", "buffer_locked=0", 1),
        good.replace("nlink3=0", "nlink3=1", 1),
        good.replace("blocks_held=101", "blocks_held=100", 1),
        good.replace("after_blocks=0", "after_blocks=1", 1),
        good.replace("inum_fd1=22", "inum_fd1=23", 1),
        good.replace("create=-1", "create=0", 1),
        good.replace("nlink_after=1", "nlink_after=2", 1),
        good.replace("eligible=1 fired=1 link=-1", "eligible=0 fired=1 link=-1", 1),
        good.replace("blocks_after=100 inodes_before", "blocks_after=101 inodes_before", 1),
        good.replace("eligible=2", "eligible=1", 1),
        good.replace("indirect_data=0", "indirect_data=1101", 1),
        good.replace("blocks_after=113", "blocks_after=112", 1),
        good.replace("FS AFTER phase=RW", "FS AFTER phase=LINK", 1),
        good.replace("FS PASS cases=6 cleanup=1", "FS FAIL reason=1"),
        good.replace("FS PASS cases=6 cleanup=1", "FS PASS cases=5 cleanup=1"),
        good + "FS PASS cases=6 cleanup=1\n",
    )
    for index, mutation in enumerate(mutations, 1):
        try:
            validate_fstrace(mutation)
        except LabError:
            continue
        raise LabError(f"oracle mutation {index} was accepted")
    sampled = set(repo_file_names())
    for resource in (PATCH, Path(__file__).resolve()):
        relative = resource.relative_to(REPO_ROOT).as_posix()
        require(relative in sampled,
                f"repository state sampling omitted {relative}")
    print(f"filesystem oracle self-test passed: good trace accepted, "
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
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True)
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
    compiler = next((name for name in (
        "riscv64-unknown-elf-gcc", "riscv64-linux-gnu-gcc")
        if shutil.which(name)), None)
    require(compiler is not None, "RISC-V compiler is required")
    uname = os.uname()
    return {
        "host": f"{uname.sysname} {uname.release} {uname.machine}",
        "python": sys.version.split()[0],
        "make": checked(["make", "--version"], cwd=REPO_ROOT).splitlines()[0],
        "compiler": checked([compiler, "--version"],
                            cwd=REPO_ROOT).splitlines()[0],
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
    digest.update(b"INDEX\0")
    digest.update(checked(
        ["git", "ls-files", "--stage", "-z"], cwd=REPO_ROOT).encode())
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
        ["git", "status", "--porcelain=v1", "--untracked-files=all",
         "--ignored"], cwd=REPO_ROOT)
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


def check_manifest_anchors(root, manifest):
    units = {unit["id"]: unit for unit in manifest["units"]}
    require("core.filesystem" in units, "manifest is missing core.filesystem")
    unit = units["core.filesystem"]
    require(unit["requires"] == ["core.device-io"],
            "filesystem unit requires edge changed")
    require(set(unit["evidence_dimensions"]) == {"S", "F", "B"},
            "filesystem evidence dimensions changed")
    require("resources/filesystem/filesystem-audit.patch" in unit["resources"] and
            "resources/filesystem/run-lab.py" in unit["resources"],
            "filesystem resources are not manifest-authoritative")
    for anchor in unit["source_anchors"]:
        path = root / anchor["path"]
        require(path.is_file(), f"source anchor path missing: {anchor['path']}")
        require(anchor["symbol"] in path.read_text(encoding="utf-8"),
                f"source anchor missing: {anchor['path']}:{anchor['symbol']}")
    return unit


def static_check(root, manifest, baseline):
    self_test()
    require(patch_paths(root) == PATCH_PATHS,
            "filesystem fixture scope differs from the fifteen declared paths")
    check_manifest_anchors(root, manifest)
    guest = (root / "user/fstrace.c").read_text(encoding="utf-8")
    for token in ("case_rw(", "case_link(", "case_trunc(", "case_fdfail(",
                  "case_linkfail(", "case_allocfail(", "FS BASE", "FS PASS"):
        require(token in guest, f"guest oracle token missing: {token}")
    hooks = {
        "kernel/fs.c": ("fsaudit_balloc_fail", "fsaudit_dirlink_fail",
                        "fsaudit_path_step", "fs_audit_inode",
                        "fs_audit_counts"),
        "kernel/fsaudit.c": ("fsaudit_command", "inspect_fd", "audit.lock"),
        "kernel/file.c": ("fsaudit_event", "file_audit_counts"),
        "kernel/bio.c": ("bio_audit_counts",),
    }
    for relative, tokens in hooks.items():
        source = (root / relative).read_text(encoding="utf-8")
        for token in tokens:
            require(token in source,
                    f"filesystem audit seam missing: {relative}:{token}")
    checked(["make", "-j2", "kernel/kernel", "user/_fstrace"], cwd=root,
            timeout=300)
    return {"baseline": baseline, "paths": sorted(PATCH_PATHS)}


class Qemu:
    def __init__(self, root, timeout=240):
        self.timeout = timeout
        self.output = bytearray()
        self.process = subprocess.Popen(
            ["make", "CPUS=1", "qemu"], cwd=root, stdin=subprocess.PIPE,
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
            "SOME TESTS FAILED" not in transcript and "FS " not in transcript,
            f"focused regression failed: {command}\n{transcript[-5000:]}")


def run_driver(root, arguments, *, cpus, timeout):
    env = os.environ.copy()
    env["CPUS"] = str(cpus)
    output = checked(["python3", "test-xv6.py", *arguments], cwd=root,
                     env=env, timeout=timeout)
    require(output.count("ALL TESTS PASSED") == 1 and
            "SOME TESTS FAILED" not in output and "FS " not in output,
            f"driver regression failed: {' '.join(arguments)}")
    return output


def write_report(path, *, tutorial_commit, static, trace, focused, quick, full,
                 before_digest, before_status, before_image, fixture_hash,
                 runner_hash, environment):
    relations = validate_fstrace(trace)
    lines = [
        "# 文件系统 namespace、inode 与数据路径机器证据附录", "",
        f"- 教程提交：`{tutorial_commit}`",
        f"- pinned baseline：`{static['baseline']}`",
        f"- fixture SHA-256：`{fixture_hash}`",
        f"- runner SHA-256：`{runner_hash}`",
        "- 本附录 SHA-256：由 tracked review record 在生成后外部记录",
        f"- host：`{environment['host']}`；Python：`{environment['python']}`",
        f"- make：`{environment['make']}`",
        f"- compiler：`{environment['compiler']}`",
        f"- QEMU：`{environment['qemu']}`",
        "- 隔离：临时源码导出、私有 fs.img、CPUS=1 trace、独立进程组", "",
        "## S/F/B/C/R", "",
        "- S：manifest anchors 与 15-path fixture scope 从 pinned export 复核。",
        "- F：RW、link/unlink-open-ref、O_TRUNC 三条正常路径逐字段复算。",
        "- B：fd exhaustion、dirlink rollback 与第二次 balloc 失败均由主动 marker 判定。",
        "- C：N/A；fixture 以 CPUS=1 固定事件顺序，本实验不声称并发 pathname/inode 覆盖。",
        "- R：N/A；commit/recovery 属于后续 persistence 单元。", "",
        "## Host-recomputed relations", "",
    ]
    for phase, fields in relations.items():
        lines.append(f"- {phase}：`{fields}`")
    lines.extend(["", "## Embedded worksheet", "",
        "| slice | trigger | ownership / mapping | failure or completion | cleanup |",
        "| --- | --- | --- | --- | --- |",
        "| RW | `fsd/data` 12 blocks + 17 bytes | fd/file/inode/direct/indirect/buffer | exact bytes/checksum | AFTER=BASE |",
        "| LINK | two names then both unlink | nlink + open file ref | paths absent, fd readable | close/reclaim |",
        "| TRUNC | peer `O_TRUNC` | same inode, blocks released | peer observes size 0 | rewrite/unlink |",
        "| FDFAIL | fill NOFILE slots | create precedes fd allocation | open=-1 but inode exists | unlink |",
        "| LINKFAIL | fail `dirlink` | nlink increment rolls back | target absent | unlink source |",
        "| ALLOCFAIL | fail second `balloc` | empty indirect block allowed | size unchanged/write=-1 | itrunc/unlink |", "",
        "## Raw transcript", "", "```text", trace.rstrip(), "```", "",
        "## Focused, quick, full", ""])
    for command, transcript in focused.items():
        lines.append(
            f"- `{command}`：exit=0；SHA-256 `{transcript_sha(transcript)}`。")
    lines.extend([
        f"- quick CPUS=2：exit=0；SHA-256 `{transcript_sha(quick)}`。",
        f"- full CPUS=1：exit=0；SHA-256 `{transcript_sha(full)}`。", "",
        "## Cleanup and limitations", "",
        "- 六个场景、8 个 focused、quick、full 均 exit=0；make clean、fixture reverse 与源码 snapshot 精确回到 baseline。",
        "- 每个 `FS AFTER` 的 block/inode/itable/file/buffer ledger 都精确等于 BASE。",
        f"- 共享工作树 digest：`{before_digest}`；状态：`{before_status}`；共享 fs.img：`{before_image}`。",
        "- failpoint 与计数 syscall 仅存在于临时 fixture；buffer cache 可保留 valid data，但 refcnt 归零。",
        "- 本证据不证明任意并发交错、log commit 原子性、断电持久化或 crash recovery。", "",
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
    require(PATCH.is_file(), "missing filesystem-audit.patch")
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
    trace = focused = quick = full = None
    with tempfile.TemporaryDirectory(prefix="xv6-filesystem.") as temp:
        root = Path(temp) / "repo"
        export_baseline(root, baseline)
        original = snapshot(root)
        check_manifest_anchors(root, manifest)
        checked(["git", "apply", "--check", "--whitespace=error-all",
                 str(PATCH)], cwd=root)
        apply_fixture(root)
        static = static_check(root, manifest, baseline)
        print("static filesystem passed: manifest, fixture scope, anchors, build, oracle",
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
            print("filesystem static-only passed: build, reverse, snapshot, cleanup")
            return
        checked(["make", "-j2", "fs.img"], cwd=root, timeout=300)
        qemu = None
        try:
            qemu = Qemu(root)
            trace = qemu.command("fstrace")
            validate_fstrace(trace)
            print("dynamic filesystem trace passed: six cases and cleanup", flush=True)
            commands = (
                "usertests unlinkread", "usertests linktest",
                "usertests createdelete", "usertests subdir",
                "usertests dirfile", "usertests iref",
                "usertests writebig", "usertests bigfile",
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
            report, tutorial_commit=tutorial_commit, static=static, trace=trace,
            focused=focused, quick=quick, full=full,
            before_digest=before_digest, before_status=before_status,
            before_image=before_image, fixture_hash=sha256(PATCH),
            runner_hash=sha256(Path(__file__)), environment=environment)
    print("filesystem passed: static, F/B, focused, quick, full, cleanup")


if __name__ == "__main__":
    try:
        main()
    except (LabError, OSError, ValueError) as exc:
        print(f"filesystem lab failed: {exc}")
        raise SystemExit(1)
