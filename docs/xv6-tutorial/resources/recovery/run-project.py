#!/usr/bin/env python3
"""Isolated crash/recovery publication runner for the pinned xv6 baseline."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path


sys.dont_write_bytecode = True
RESOURCE_DIR = Path(__file__).resolve().parent
REPO_ROOT = RESOURCE_DIR.parents[3]
MANIFEST = REPO_ROOT / "docs/xv6-tutorial/curriculum.json"
SCENARIOS = RESOURCE_DIR / "scenarios.json"
FIXTURE = RESOURCE_DIR / "recovery-audit.patch"
FSCK_PATH = RESOURCE_DIR / "fsck.py"
PERSISTENCE_RUNNER = RESOURCE_DIR.parent / "persistence/run-lab.py"
RUNNER = Path(__file__).resolve()

BSIZE = 1024
FSSIZE = 2000
LOGBLOCKS = 30
HOME_BLOCKS = (1990, 1991)
POINTS = (
    ("crash-10-log-data", 10, "LOG_DATA_COMPLETE", "before", 0, 0),
    ("crash-20-commit-header", 20, "COMMIT_HEADER_COMPLETE", "after", 2, 2),
    ("crash-30-home-install", 30, "HOME_INSTALL_COMPLETE", "after", 2, 2),
    ("crash-40-header-clear", 40, "HEADER_CLEAR_COMPLETE", "after", 0, 0),
)
TEAR_IDS = (
    "header-zero", "payload-half", "home-half", "header-restore",
    "invalid-count", "invalid-target",
)
FIXTURE_PATHS = {
    "Makefile", "kernel/defs.h", "kernel/log.c", "kernel/recoveryaudit.c",
    "kernel/recoveryaudit.h", "kernel/syscall.c", "kernel/syscall.h",
    "kernel/sysproc.c", "user/recoverycase.c", "user/user.h", "user/usys.pl",
}
FIXTURE_CREATED = {
    "kernel/recoveryaudit.c", "kernel/recoveryaudit.h", "user/recoverycase.c",
}
CANDIDATE_PATHS = {"kernel/log.c", "kernel/recoveryaudit.c"}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load publication helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HOST = load_module("xv6_persistence_publication", PERSISTENCE_RUNNER)
FSCK = load_module("xv6_recovery_fsck", FSCK_PATH)
LabError = HOST.LabError
require = HOST.require


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_contract() -> tuple[dict, dict, str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    baseline = manifest.get("release", {}).get("baseline_commit")
    require(isinstance(baseline, str) and re.fullmatch(r"[0-9a-f]{40}", baseline),
            "manifest baseline_commit must be a full lowercase commit ID")
    units = {unit.get("id"): unit for unit in manifest.get("units", [])
             if isinstance(unit, dict)}
    unit = units.get("project.crash-recovery")
    require(unit is not None, "manifest is missing project.crash-recovery")
    require(unit.get("stage") == "persistence" and unit.get("order") == 30 and
            unit.get("status") in {"draft", "verified"} and
            unit.get("requires") == ["core.persistence"],
            "crash-recovery manifest identity/status/requires changed")
    require(unit.get("resolves") == ["crash-recovery-and-offline-consistency"] and
            set(unit.get("evidence_dimensions", [])) == {"S", "F", "B", "R"},
            "crash-recovery ownership or evidence dimensions changed")
    required_resources = {
        "resources/recovery/recovery-audit.patch",
        "resources/recovery/run-project.py", "resources/recovery/fsck.py",
        "resources/recovery/scenarios.json", "resources/recovery/rubric.md",
        "resources/recovery/report-template.md", "questions/recovery.md",
        "questions/answers/recovery.md",
    }
    require(required_resources <= set(unit.get("resources", [])),
            "crash-recovery resources are not manifest-authoritative")
    anchors = unit.get("source_anchors")
    require(isinstance(anchors, list) and anchors,
            "crash-recovery source anchors are missing")
    for anchor in anchors:
        require(isinstance(anchor, dict) and set(anchor) == {"path", "symbol"},
                "crash-recovery source anchor schema changed")
    require(scenarios.get("schema_version") == 1 and scenarios.get("abi") == 1 and
            scenarios.get("home_blocks") == list(HOME_BLOCKS),
            "recovery scenario header changed")
    crash_points = scenarios.get("crash_points")
    require(isinstance(crash_points, list) and len(crash_points) == len(POINTS),
            "recovery crash-point set changed")
    expected_points = [
        {"id": "log-data", "logical_id": 10, "point": "LOG_DATA_COMPLETE",
         "phase": "precommit", "crash_header_n": 0,
         "crash_home": "before", "recovery_seen_n": 0,
         "recovery_home": "before"},
        {"id": "commit-header", "logical_id": 20,
         "point": "COMMIT_HEADER_COMPLETE", "phase": "postcommit",
         "crash_header_n": 2, "crash_home": "before",
         "recovery_seen_n": 2, "recovery_home": "after"},
        {"id": "home-install", "logical_id": 30,
         "point": "HOME_INSTALL_COMPLETE", "phase": "postcommit",
         "crash_header_n": 2, "crash_home": "after",
         "recovery_seen_n": 2, "recovery_home": "after"},
        {"id": "header-clear", "logical_id": 40,
         "point": "HEADER_CLEAR_COMPLETE", "phase": "postcommit",
         "crash_header_n": 0, "crash_home": "after",
         "recovery_seen_n": 0, "recovery_home": "after"},
    ]
    require(crash_points == expected_points,
            "recovery crash-point declarations changed")
    tears = scenarios.get("synthetic_tears")
    require(isinstance(tears, list) and
            [tear.get("id") for tear in tears if isinstance(tear, dict)] ==
            list(TEAR_IDS) and all(set(tear) == {"id", "source_point",
                                                "operation", "expected"}
                                   for tear in tears),
            "synthetic tear declarations changed")
    return manifest, scenarios, baseline


def ordered(text: str, tokens: tuple[str, ...], context: str) -> None:
    position = -1
    for token in tokens:
        position = text.find(token, position + 1)
        require(position >= 0, f"{context}: missing/out-of-order {token!r}")


def analyze_baseline(root: Path) -> None:
    log = (root / "kernel/log.c").read_text(encoding="utf-8")
    fs = (root / "kernel/fs.c").read_text(encoding="utf-8")
    param = (root / "kernel/param.h").read_text(encoding="utf-8")
    ordered(log, ("write_log();", "write_head();", "install_trans(0);",
                  "log.lh.n = 0;", "write_head();"), "baseline commit")
    for token in ("recover_from_log(", "read_head();", "install_trans(1);",
                  "struct logheader", "LOGBLOCKS"):
        require(token in log + param, f"baseline recovery anchor missing: {token}")
    for token in ("ialloc(", "itrunc(", "dirlink(", "dirlookup("):
        require(token in fs, f"baseline fsck anchor missing: {token}")
    require("RECOVERY CRASH" not in log and "recoveryaudit_hit" not in log,
            "pinned baseline unexpectedly contains recovery fixture")


def analyze_fixture(root: Path, *, candidate: bool) -> None:
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    audit = (root / "kernel/recoveryaudit.c").read_text(encoding="utf-8")
    header = (root / "kernel/recoveryaudit.h").read_text(encoding="utf-8")
    guest = (root / "user/recoverycase.c").read_text(encoding="utf-8")
    log = (root / "kernel/log.c").read_text(encoding="utf-8")
    for token in ("$K/recoveryaudit.o", "$U/_recoverycase"):
        require(token in makefile, f"fixture build token missing: {token}")
    for name, point_id in (("RC_LOG_DATA_COMPLETE", 10),
                           ("RC_COMMIT_HEADER_COMPLETE", 20),
                           ("RC_HOME_INSTALL_COMPLETE", 30),
                           ("RC_HEADER_CLEAR_COMPLETE", 40)):
        require(re.search(rf"^#define\s+{name}\s+{point_id}\b", header,
                          re.MULTILINE) is not None,
                f"fixture logical ID changed: {name}")
    for token in ("recoveryaudit_case(", "recoveryaudit_replay(",
                  "RECOVERY REPLAY abi=1", "RC_HOME0", "RC_HOME1"):
        require(token in audit + header, f"fixture oracle token missing: {token}")
    require("recoverycase(" in guest and "LOG_DATA_COMPLETE" in guest and
            "HEADER_CLEAR_COMPLETE" in guest,
            "fixture guest command contract changed")
    ordered(log, ("read_head();", "seen = log.lh.n;", "install_trans(1);",
                  "log.lh.n = 0;", "write_head();", "recoveryaudit_replay("),
            "recovery observation")
    if not candidate:
        require("RECOVERY CRASH abi=1" not in audit and
                "recoveryaudit_hit(RC_" not in log,
                "published fixture contains the learner crash-hook answer")
        return
    for token in ("RECOVERY CRASH abi=1", "intr_off();", 'asm volatile("wfi")'):
        require(token in audit, f"candidate crash hook missing: {token}")
    ordered(log, ("write_log();", "recoveryaudit_hit(RC_LOG_DATA_COMPLETE",
                  "write_head();", "recoveryaudit_hit(RC_COMMIT_HEADER_COMPLETE",
                  "install_trans(0);", "recoveryaudit_hit(RC_HOME_INSTALL_COMPLETE",
                  "log.lh.n = 0;", "write_head();",
                  "recoveryaudit_hit(RC_HEADER_CLEAR_COMPLETE"),
            "candidate crash points")


def expected_pattern(after: bool, index: int, epoch: int) -> bytes:
    data = bytearray([(0xa0 if after else 0x50) + index] * BSIZE)
    struct.pack_into("<III", data, 0, epoch, int(after), index)
    return bytes(data)


def geometry(data: bytes) -> tuple[int, int]:
    require(len(data) == FSSIZE * BSIZE, "private fs.img size changed")
    fields = struct.unpack_from("<8I", data, BSIZE)
    require(fields[0] == 0x10203040 and fields[1] == FSSIZE,
            "private fs.img superblock changed")
    return fields[5], fields[6]


def log_state(data: bytes) -> tuple[int, tuple[int, ...], tuple[bytes, ...]]:
    logstart, _ = geometry(data)
    count = struct.unpack_from("<i", data, logstart * BSIZE)[0]
    require(0 <= count <= LOGBLOCKS, f"invalid live log count: {count}")
    targets = struct.unpack_from(f"<{count}I", data, logstart * BSIZE + 4) \
        if count else ()
    payload = tuple(
        data[(logstart + 1 + index) * BSIZE:(logstart + 2 + index) * BSIZE]
        for index in range(min(2, count if count else 2))
    )
    return count, targets, payload


def classify_homes(data: bytes, epoch: int) -> str:
    homes = tuple(data[number * BSIZE:(number + 1) * BSIZE]
                  for number in HOME_BLOCKS)
    before = tuple(expected_pattern(False, index, epoch) for index in range(2))
    after = tuple(expected_pattern(True, index, epoch) for index in range(2))
    if homes == before:
        return "before"
    if homes == after:
        return "after"
    return "mixed"


def assert_fsck(data: bytes, *, pending: bool, context: str) -> dict:
    result = FSCK.check_bytes(data)
    require(result.get("clean") is True and result.get("recovered_clean") is True,
            f"{context}: offline recovered view is inconsistent: "
            f"{result.get('diagnostics')}")
    require(result.get("log_pending") is pending,
            f"{context}: offline log-pending state changed")
    if not pending:
        require(result.get("raw_clean") is True,
                f"{context}: clean-header raw image is inconsistent")
    return result


def fsck_summary(result: dict) -> dict:
    return {
        "input_sha256": result.get("input_sha256", result.get("raw_sha256")),
        "raw_clean": result.get("raw_clean"),
        "recovered_clean": result.get("recovered_clean"),
        "log_pending": result.get("log_pending"),
        "log": result.get("log"),
        "diagnostics": result.get("diagnostics", []),
    }


def verify_home_blocks_free(data: bytes) -> None:
    diagnostics = FSCK.Diagnostics()
    layout = FSCK.parse_geometry(data, diagnostics)
    require(layout is not None and not diagnostics.codes(),
            "cannot verify fixture home-block ownership")
    inodes = FSCK.parse_inodes(data, layout, diagnostics)
    owned = {
        address
        for inode in inodes.values()
        for _, _, address in FSCK.inode_blocks(data, layout, inode, diagnostics)
    }
    require(not diagnostics.codes() and
            all(block not in owned and
                FSCK.bitmap_bit(data, layout, block) == 0
                for block in HOME_BLOCKS),
            "fixture home blocks are not free and unreferenced")


CRASH_RE = re.compile(
    r"^RECOVERY CRASH abi=1 point=([A-Z_]+) hart=(\d+) epoch=(\d+) "
    r"count=(\d+) left=(\d+) right=(\d+) fired=1$", re.MULTILINE)
REPLAY_RE = re.compile(
    r"^RECOVERY REPLAY abi=1 seen_n=(\d+) installed=(\d+) cleared=1 "
    r"left=(\d+) right=(\d+)$", re.MULTILINE)


class Qemu:
    def __init__(self, root: Path, image: Path, timeout: int = 120):
        self.timeout = timeout
        self.output = bytearray()
        self.pidfd = -1
        self.process = subprocess.Popen(
            ["qemu-system-riscv64", "-machine", "virt", "-bios", "none",
             "-kernel", os.fspath(root / "kernel/kernel"), "-m", "128M",
             "-smp", "1", "-nographic",
             "-global", "virtio-mmio.force-legacy=false",
             "-drive", f"file={image},if=none,format=raw,cache=directsync,id=x0",
             "-device", "virtio-blk-device,drive=x0,bus=virtio-mmio-bus.0"],
            cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True)
        try:
            self.pidfd = os.pidfd_open(self.process.pid)
            self.wait_for(b"$ ", 0)
        except BaseException:
            self.kill()
            raise

    def read(self, wait: float = 1) -> bool:
        ready, _, _ = select.select([self.process.stdout], [], [], wait)
        if not ready:
            return False
        chunk = os.read(self.process.stdout.fileno(), 4096)
        require(chunk, f"QEMU output closed: {self.tail()}")
        self.output.extend(chunk.replace(b"\r", b""))
        return True

    def wait_for(self, marker: bytes, start: int) -> None:
        deadline = time.monotonic() + self.timeout
        while marker not in self.output[start:]:
            require(time.monotonic() < deadline,
                    f"QEMU watchdog waiting for {marker!r}: {self.tail()}")
            self.read()

    def write(self, data: bytes) -> None:
        require(self.process.stdin is not None, "QEMU stdin closed")
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def command_until(self, command: str, marker: str) -> str:
        start = len(self.output)
        self.write((command + "\n").encode())
        self.wait_for(marker.encode(), start)
        return bytes(self.output[start:]).decode("utf-8", "replace")

    def transcript(self) -> str:
        return bytes(self.output).decode("utf-8", "replace")

    def tail(self) -> str:
        return bytes(self.output[-8000:]).decode("utf-8", "replace")

    def kill(self) -> None:
        if self.process.poll() is None:
            if self.pidfd < 0:
                HOST.stop_group(self.process)
            else:
                try:
                    signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
                except OSError:
                    HOST.stop_group(self.process)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            HOST.stop_group(self.process)
        if self.pidfd >= 0:
            os.close(self.pidfd)
            self.pidfd = -1

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.write(b"\x01x")
                self.process.wait(timeout=10)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                HOST.stop_group(self.process)
        if self.pidfd >= 0:
            os.close(self.pidfd)
            self.pidfd = -1


def parse_replay(transcript: str, *, seen: int, context: str) -> dict:
    matches = REPLAY_RE.findall(transcript)
    require(len(matches) == 1, f"{context}: replay marker count changed: {matches}")
    observed, installed, left, right = map(int, matches[0])
    require((observed, installed, left, right) ==
            (seen, seen, HOME_BLOCKS[0], HOME_BLOCKS[1]),
            f"{context}: replay marker changed: {matches[0]}")
    return {"seen_n": observed, "installed": installed}


def boot(root: Path, image: Path, *, seen: int, context: str) -> tuple[str, str]:
    qemu = Qemu(root, image)
    try:
        transcript = qemu.transcript()
        parse_replay(transcript, seen=seen, context=context)
    finally:
        qemu.close()
    digest = HOST.sha256(image)
    return transcript, digest


def crash(root: Path, image: Path, *, point: tuple, epoch: int) -> tuple[str, bytes]:
    case, _, name, _, _, _ = point
    qemu = Qemu(root, image)
    try:
        parse_replay(qemu.transcript(), seen=0, context=f"{case} initial boot")
        marker = (
            f"RECOVERY CRASH abi=1 point={name} hart=0 epoch={epoch} count=2 "
            f"left={HOME_BLOCKS[0]} right={HOME_BLOCKS[1]} fired=1\n"
        )
        transcript = qemu.command_until(
            f"recoverycase {name} {epoch}", marker)
        matches = CRASH_RE.findall(transcript)
        require(len(matches) == 1, f"{case}: crash marker count changed")
        observed_name, hart, observed_epoch, count, left, right = matches[0]
        require((observed_name, int(hart), int(observed_epoch), int(count),
                 int(left), int(right)) ==
                (name, 0, epoch, 2, HOME_BLOCKS[0], HOME_BLOCKS[1]),
                f"{case}: crash marker changed: {matches[0]}")
    finally:
        qemu.kill()
    with image.open("rb") as stream:
        data = stream.read()
    return transcript, data


def validate_crash(data: bytes, *, point: tuple, epoch: int) -> dict:
    case, _, name, final, header_n, seen = point
    count, targets, payload = log_state(data)
    require(count == header_n, f"{case}: log header count changed: {count}")
    if count:
        require(targets == HOME_BLOCKS, f"{case}: log targets changed: {targets}")
    require(payload == tuple(expected_pattern(True, index, epoch)
                             for index in range(2)),
            f"{case}: completed log payload is not exact AFTER bytes")
    crash_home = "before" if name in {"LOG_DATA_COMPLETE",
                                      "COMMIT_HEADER_COMPLETE"} else "after"
    require(classify_homes(data, epoch) == crash_home,
            f"{case}: crash-image home state changed")
    check = assert_fsck(data, pending=count != 0, context=f"{case} crash image")
    homes = tuple(data[number * BSIZE:(number + 1) * BSIZE]
                  for number in HOME_BLOCKS)
    return {"case": case, "point": name, "epoch": epoch,
            "crash_sha256": sha256_bytes(data), "header_n": count,
            "targets": list(targets), "crash_home": crash_home,
            "final": final, "replay_seen_n": seen,
            "payload_sha256": [sha256_bytes(item) for item in payload],
            "home_sha256": [sha256_bytes(item) for item in homes],
            "crash_fsck": fsck_summary(check),
            "fsck_shadow_sha256": check["shadow_sha256"]}


def run_crash_points(root: Path, pristine: Path, scratch: Path) -> tuple[dict, dict]:
    records = {}
    images = {}
    for sequence, point in enumerate(POINTS, start=1):
        case, _, name, final, _, seen = point
        epoch = 0x1700 + sequence
        image = scratch / f"{case}.img"
        shutil.copyfile(pristine, image)
        transcript, crash_data = crash(root, image, point=point, epoch=epoch)
        record = validate_crash(crash_data, point=point, epoch=epoch)
        images[case] = crash_data
        first_transcript, first_hash = boot(
            root, image, seen=seen, context=f"{case} first recovery")
        recovered = image.read_bytes()
        require(log_state(recovered)[0] == 0 and
                classify_homes(recovered, epoch) == final,
                f"{case}: first recovery did not produce exact {final} state")
        first_fsck = assert_fsck(
            recovered, pending=False, context=f"{case} first recovery")
        second_transcript, second_hash = boot(
            root, image, seen=0, context=f"{case} idempotent restart")
        second = image.read_bytes()
        second_fsck = assert_fsck(
            second, pending=False, context=f"{case} idempotent restart")
        require(first_hash == second_hash and second == recovered,
                f"{case}: second restart changed the recovered image")
        record.update({
            "crash_marker": CRASH_RE.search(transcript).group(0),
            "first_replay": REPLAY_RE.search(first_transcript).group(0),
            "second_replay": REPLAY_RE.search(second_transcript).group(0),
            "first_stop_sha256": first_hash,
            "second_stop_sha256": second_hash,
            "first_stop_fsck": fsck_summary(first_fsck),
            "second_stop_fsck": fsck_summary(second_fsck),
        })
        records[case] = record
        print(f"recovery crash point passed: {case}", flush=True)
    return records, images


def set_log_count(data: bytearray, count: int) -> None:
    logstart, _ = geometry(data)
    struct.pack_into("<i", data, logstart * BSIZE, count)


def run_tears(root: Path, images: dict, scratch: Path) -> dict:
    result = {}
    header = images["crash-20-commit-header"]
    home = images["crash-30-home-install"]
    clear = images["crash-40-header-clear"]

    rollback = bytearray(header)
    header_source_sha = sha256_bytes(header)
    set_log_count(rollback, 0)
    path = scratch / "tear-header-rollback.img"
    path.write_bytes(rollback)
    rollback_sha = sha256_bytes(bytes(rollback))
    transcript, rollback_hash = boot(root, path, seen=0,
                                     context="header-count rollback")
    rollback_data = path.read_bytes()
    rollback_fsck = assert_fsck(
        rollback_data, pending=False, context="header-count rollback")
    require(log_state(rollback_data)[0] == 0 and
            classify_homes(rollback_data, 0x1702) == "before",
            "header-count rollback did not expose rejected BEFORE state")
    result["header-zero"] = {
        "accepted_as_postcommit": False,
        "source_sha256": header_source_sha,
        "mutated_sha256": rollback_sha,
        "first_stop_sha256": rollback_hash,
        "mutation": {"offset": geometry(header)[0] * BSIZE, "length": 4,
                      "value": 0},
        "observed": "before", "replay": REPLAY_RE.search(transcript).group(0),
        "fsck": fsck_summary(rollback_fsck)}

    payload_tear = bytearray(header)
    logstart, _ = geometry(payload_tear)
    offset = (logstart + 1) * BSIZE
    payload_tear[offset:offset + BSIZE // 2] = \
        expected_pattern(False, 0, 0x1702)[:BSIZE // 2]
    path = scratch / "tear-log-payload.img"
    path.write_bytes(payload_tear)
    payload_sha = sha256_bytes(bytes(payload_tear))
    payload_transcript, payload_hash = boot(
        root, path, seen=2, context="log-payload half-write")
    payload_data = path.read_bytes()
    payload_fsck = FSCK.check_bytes(payload_data)
    require(classify_homes(payload_data, 0x1702) == "mixed",
            "log-payload half-write was not rejected as MIXED")
    result["payload-half"] = {
        "accepted_as_postcommit": False,
        "source_sha256": header_source_sha,
        "mutated_sha256": payload_sha,
        "first_stop_sha256": payload_hash,
        "mutation": {"offset": offset, "length": BSIZE // 2,
                      "replacement": "BEFORE block 0 prefix"},
        "replay": REPLAY_RE.search(payload_transcript).group(0),
        "observed": "mixed", "fsck": fsck_summary(payload_fsck)}

    home_tear = bytearray(home)
    offset = HOME_BLOCKS[0] * BSIZE
    home_tear[offset:offset + BSIZE // 2] = \
        expected_pattern(False, 0, 0x1703)[:BSIZE // 2]
    path = scratch / "tear-home-block.img"
    path.write_bytes(home_tear)
    home_sha = sha256_bytes(bytes(home_tear))
    home_transcript, home_hash = boot(
        root, path, seen=2, context="home-block half-write")
    home_data = path.read_bytes()
    home_fsck = assert_fsck(home_data, pending=False,
                            context="home-block half-write")
    require(log_state(home_data)[0] == 0 and
            classify_homes(home_data, 0x1703) == "after",
            "redo did not repair the torn home block")
    result["home-half"] = {
        "accepted": True, "source_sha256": sha256_bytes(home),
        "mutated_sha256": home_sha, "first_stop_sha256": home_hash,
        "mutation": {"offset": HOME_BLOCKS[0] * BSIZE, "length": BSIZE // 2,
                      "replacement": "BEFORE block 0 prefix"},
        "replay": REPLAY_RE.search(home_transcript).group(0),
        "observed": "after", "fsck": fsck_summary(home_fsck)}

    reappeared = bytearray(clear)
    logstart, _ = geometry(reappeared)
    header_block = header[logstart * BSIZE:(logstart + 1) * BSIZE]
    reappeared[logstart * BSIZE:(logstart + 1) * BSIZE] = header_block
    path = scratch / "tear-cleared-header.img"
    path.write_bytes(reappeared)
    restore_source_sha = sha256_bytes(clear)
    restore_mutated_sha = sha256_bytes(bytes(reappeared))
    restore_first_transcript, first_hash = boot(
        root, path, seen=2, context="cleared header reappears")
    restore_first_data = path.read_bytes()
    restore_first_fsck = assert_fsck(
        restore_first_data, pending=False, context="cleared header reappears")
    restore_second_transcript, second_hash = boot(
        root, path, seen=0, context="cleared header idempotent restart")
    restore_second_data = path.read_bytes()
    restore_second_fsck = assert_fsck(
        restore_second_data, pending=False,
        context="cleared header idempotent restart")
    require(log_state(restore_first_data)[0] == 0 and
            log_state(restore_second_data)[0] == 0 and
            classify_homes(restore_second_data, 0x1704) == "after" and
            first_hash == second_hash,
            "repeated redo after restored header was not idempotent")
    result["header-restore"] = {
        "accepted": True, "source_sha256": restore_source_sha,
        "mutated_sha256": restore_mutated_sha,
        "first_stop_sha256": first_hash, "second_stop_sha256": second_hash,
        "mutation": {"offset": logstart * BSIZE, "length": BSIZE,
                      "replacement": "committed header block"},
        "first_replay": REPLAY_RE.search(restore_first_transcript).group(0),
        "second_replay": REPLAY_RE.search(restore_second_transcript).group(0),
        "observed": "after", "first_fsck": fsck_summary(restore_first_fsck),
        "second_fsck": fsck_summary(restore_second_fsck)}

    bad_count = bytearray(clear)
    set_log_count(bad_count, LOGBLOCKS + 1)
    checked = FSCK.check_bytes(bytes(bad_count))
    require(not checked["clean"] and any(item.startswith("E_LOG_COUNT")
                                         for item in checked["diagnostics"]),
            "offline checker accepted invalid log count")
    result["invalid-count"] = {
        "accepted": False, "mutated_sha256": sha256_bytes(bytes(bad_count)),
        "mutation": {"offset": geometry(bad_count)[0] * BSIZE,
                      "length": 4, "value": LOGBLOCKS + 1},
        "diagnostics": checked["diagnostics"]}

    bad_target = bytearray(header)
    logstart, _ = geometry(bad_target)
    struct.pack_into("<I", bad_target, logstart * BSIZE + 4, logstart)
    checked = FSCK.check_bytes(bytes(bad_target))
    require(not checked["clean"] and any(item.startswith("E_LOG_TARGET")
                                         for item in checked["diagnostics"]),
            "offline checker accepted a log-region target")
    result["invalid-target"] = {
        "accepted": False, "mutated_sha256": sha256_bytes(bytes(bad_target)),
        "mutation": {"offset": logstart * BSIZE + 4, "length": 4,
                      "value": logstart}, "diagnostics": checked["diagnostics"]}
    print("recovery synthetic tears passed: 4 booted, 2 offline-negative", flush=True)
    return result


def fsck_mutation_self_test(image: Path) -> dict:
    good = image.read_bytes()
    verify_home_blocks_free(good)
    before = HOST.sha256(image)
    pristine = FSCK.check_path(image)
    require(pristine["clean"] and pristine["raw_clean"] and
            pristine["recovered_clean"] and not pristine["log_pending"] and
            HOST.sha256(image) == before,
            "offline checker rejected or modified pristine fs.img")
    diagnostics = FSCK.Diagnostics()
    layout = FSCK.parse_geometry(good, diagnostics)
    require(layout is not None and not diagnostics.codes(),
            "self-test cannot parse pristine geometry")
    inodes = FSCK.parse_inodes(good, layout, diagnostics)
    files = [inode for inode in inodes.values()
             if inode.type == FSCK.T_FILE and inode.size and inode.addrs[0]]
    require(len(files) >= 2, "self-test needs two allocated regular files")

    def inode_offset(inum: int) -> int:
        return layout.inodestart * BSIZE + inum * FSCK.DINODE.size

    def set_bitmap(data: bytearray, number: int, value: bool) -> None:
        offset = (layout.bmapstart + number // FSCK.BPB) * BSIZE
        offset += (number % FSCK.BPB) // 8
        mask = 1 << (number % 8)
        data[offset] = ((data[offset] | mask) if value else
                        (data[offset] & ~mask))

    def dirent(wanted: bytes) -> tuple[int, int]:
        root = inodes[FSCK.ROOTINO]
        remaining = root.size
        for kind, _, address in FSCK.inode_blocks(
                good, layout, root, diagnostics):
            if kind != "data":
                continue
            for local in range(0, min(remaining, BSIZE), FSCK.DIRENT.size):
                offset = address * BSIZE + local
                target, raw_name = FSCK.DIRENT.unpack_from(good, offset)
                if target and FSCK.decode_name(raw_name) == wanted:
                    return offset, target
            remaining -= min(remaining, BSIZE)
        raise LabError(f"self-test directory entry missing: {wanted!r}")

    mutations = []

    broken = bytearray(good)
    struct.pack_into("<I", broken, BSIZE, 0)
    mutations.append(("super-magic", broken, "E_SUPER_MAGIC"))

    broken = bytearray(good)
    set_log_count(broken, LOGBLOCKS + 1)
    mutations.append(("log-count", broken, "E_LOG_COUNT"))

    broken = bytearray(good)
    logstart, _ = geometry(broken)
    set_log_count(broken, 1)
    struct.pack_into("<I", broken, logstart * BSIZE + 4, logstart)
    mutations.append(("log-target", broken, "E_LOG_TARGET"))

    broken = bytearray(good)
    dot_offset, _ = dirent(b".")
    struct.pack_into("<H", broken, dot_offset, 0)
    mutations.append(("directory-dot", broken, "E_DOT"))

    broken = bytearray(good)
    dotdot_offset, _ = dirent(b"..")
    struct.pack_into("<H", broken, dotdot_offset, files[0].inum)
    mutations.append(("directory-dotdot", broken, "E_DOTDOT"))

    broken = bytearray(good)
    set_bitmap(broken, 0, False)
    mutations.append(("bitmap-metadata", broken, "E_BITMAP_METADATA"))

    broken = bytearray(good)
    set_bitmap(broken, files[0].addrs[0], False)
    mutations.append(("bitmap-missing", broken, "E_BITMAP_UNMARKED"))

    broken = bytearray(good)
    set_bitmap(broken, HOME_BLOCKS[0], True)
    mutations.append(("bitmap-leak", broken, "E_BITMAP_LEAK"))

    broken = bytearray(good)
    file_inode = inode_offset(files[0].inum)
    struct.pack_into("<I", broken, file_inode + 12, 0)
    mutations.append(("file-logical-hole", broken, "E_INODE_HOLE"))

    broken = bytearray(good)
    file_size = struct.unpack_from("<I", broken, file_inode + 8)[0]
    trailing = (file_size + BSIZE - 1) // BSIZE
    require(trailing < 12, "self-test README no longer fits direct blocks")
    struct.pack_into("<I", broken, file_inode + 12 + trailing * 4, HOME_BLOCKS[0])
    mutations.append(("file-trailing-block", broken, "E_INODE_TRAILING"))

    broken = bytearray(good)
    victim = files[1]
    struct.pack_into("<I", broken, inode_offset(victim.inum) + 12,
                     files[0].addrs[0])
    set_bitmap(broken, victim.addrs[0], False)
    mutations.append(("duplicate-owner", broken, "E_BLOCK_DUP"))

    readme_offset, readme_inum = dirent(b"README")
    broken = bytearray(good)
    struct.pack_into("<H", broken, readme_offset, 0)
    mutations.append(("unreachable-nlink", broken, "E_UNREACHABLE"))

    broken = bytearray(good)
    current_nlink = struct.unpack_from("<h", broken,
                                       inode_offset(readme_inum) + 6)[0]
    struct.pack_into("<h", broken, inode_offset(readme_inum) + 6,
                     current_nlink + 1)
    mutations.append(("nlink-mismatch", broken, "E_NLINK"))

    broken = bytearray(good)
    struct.pack_into("<H", broken, readme_offset, 0)
    struct.pack_into("<h", broken, inode_offset(readme_inum) + 6, 0)
    mutations.append(("orphan", broken, "E_ORPHAN"))

    rejected = []
    for name, data, code in mutations:
        raw = bytes(data)
        result = FSCK.check_bytes(raw)
        require(not result.get("clean") and
                any(item.startswith(code) for item in result.get("diagnostics", [])),
                f"offline checker accepted mutation: {name}")
        rejected.append({"id": name, "input_sha256": sha256_bytes(raw),
                         "expected": code,
                         "diagnostics": result.get("diagnostics", [])})

    owned = files[0].addrs[0]
    bitmap = FSCK.block(good, layout.bmapstart)
    repaired = bytearray(good)
    repaired[(layout.logstart + 1) * BSIZE:(layout.logstart + 2) * BSIZE] = bitmap
    set_bitmap(repaired, owned, False)
    struct.pack_into("<iI", repaired, layout.logstart * BSIZE,
                     1, layout.bmapstart)
    repaired_bytes = bytes(repaired)
    result = FSCK.check_bytes(repaired_bytes)
    require(result["clean"] and not result["raw_clean"] and
            result["recovered_clean"] and result["log_pending"],
            "offline checker rejected recoverable pending log")

    corrupt = bytearray(good)
    bad_bitmap = bytearray(bitmap)
    local = (owned % FSCK.BPB) // 8
    bad_bitmap[local] &= ~(1 << (owned % 8))
    corrupt[(layout.logstart + 1) * BSIZE:(layout.logstart + 2) * BSIZE] = bad_bitmap
    struct.pack_into("<iI", corrupt, layout.logstart * BSIZE,
                     1, layout.bmapstart)
    corrupt_bytes = bytes(corrupt)
    result = FSCK.check_bytes(corrupt_bytes)
    require(not result["clean"] and result["raw_clean"] and
            not result["recovered_clean"] and result["log_pending"] and
            any(item.startswith("E_BITMAP_UNMARKED")
                for item in result["diagnostics"]),
            "offline checker accepted corrupt pending-log shadow")
    rejected.append({
        "id": "pending-corrupt-shadow",
        "input_sha256": sha256_bytes(corrupt_bytes),
        "expected": "E_BITMAP_UNMARKED",
        "diagnostics": result["diagnostics"],
    })
    return {
        "good": 2,
        "accepted": [
            {"id": "pristine", "input_sha256": before,
             "fsck": fsck_summary(pristine)},
            {"id": "pending-repairs-home",
             "input_sha256": sha256_bytes(repaired_bytes),
             "fsck": fsck_summary(FSCK.check_bytes(repaired_bytes))},
        ],
        "rejected": rejected,
    }


def environment_record(root: Path) -> str:
    database = HOST.checked(["make", "-pn"], cwd=root, timeout=60)
    match = re.search(r"^TOOLPREFIX := (\S*)$", database, re.MULTILINE)
    require(match is not None, "cannot resolve Makefile TOOLPREFIX")
    return "; ".join((
        HOST.checked(["uname", "-srmo"], cwd=root, timeout=30).strip(),
        HOST.checked(["qemu-system-riscv64", "--version"], cwd=root,
                     timeout=30).splitlines()[0],
        HOST.checked([f"{match.group(1)}gcc", "--version"], cwd=root,
                     timeout=30).splitlines()[0],
        f"Python {sys.version.split()[0]}", "QEMU cache=directsync; CPUS=1",
    ))


def run_regressions(root: Path) -> dict:
    focused = {}
    qemu = HOST.QemuSource(root, 1, 300)
    try:
        for name in ("writebig", "bigwrite", "bigfile", "manywrites"):
            command = f"usertests {name}"
            transcript = qemu.command(command)
            HOST.assert_usertest(command, transcript)
            require("RECOVERY CRASH" not in transcript,
                    f"unarmed candidate fired during {command}")
            focused[command] = HOST.transcript_sha(transcript)
            print(f"focused regression passed: {name}", flush=True)
        transcript = qemu.command("logstress f0")
        require(transcript.count("write failed -1") == 1 and
                "panic:" not in transcript and "RECOVERY CRASH" not in transcript,
                f"logstress boundary changed:\n{transcript[-5000:]}")
        cleanup = qemu.command("rm f0")
        require("failed" not in cleanup.lower(), "logstress file cleanup failed")
        focused["logstress f0"] = HOST.transcript_sha(transcript)
        print("focused regression passed: logstress f0", flush=True)
    finally:
        qemu.close()
    HOST.checked(["make", "clean"], cwd=root, timeout=180)
    quick = HOST.run_driver(root, ["-q", "usertests"], cpus=2, timeout=600)
    print("quick regression passed: CPUS=2", flush=True)
    HOST.checked(["make", "clean"], cwd=root, timeout=180)
    full = HOST.run_driver(root, ["usertests"], cpus=1, timeout=1200)
    print("full regression passed: CPUS=1", flush=True)
    return {"focused": focused, "quick": HOST.transcript_sha(quick),
            "full": HOST.transcript_sha(full)}


def write_report(path: Path, *, baseline: str, candidate: Path | None,
                 candidate_hash: str | None, environment: str, before: dict,
                 self_test: dict, records: dict | None, tears: dict | None,
                 regressions: dict | None) -> None:
    lines = [
        "# Crash recovery 与 offline fsck 机器证据附录", "",
        f"- 源码 baseline：`{baseline}`",
        f"- 教程 HEAD：`{HOST.checked(['git', 'rev-parse', 'HEAD'], cwd=REPO_ROOT).strip()}`",
        f"- 外部 candidate：`{candidate or 'N/A (--static-only)'}`",
        f"- candidate SHA-256：`{candidate_hash or 'N/A (--static-only)'}`",
        f"- fixture SHA-256：`{HOST.sha256(FIXTURE)}`",
        f"- scenarios SHA-256：`{HOST.sha256(SCENARIOS)}`",
        f"- fsck oracle SHA-256：`{HOST.sha256(FSCK_PATH)}`",
        f"- runner SHA-256：`{HOST.sha256(RUNNER)}`",
        f"- 环境：`{environment}`",
        f"- 运行时间：`{time.strftime('%Y-%m-%dT%H:%M:%S%z')}`",
        "- 边界：QEMU marker 证明 guest 逻辑顺序；进程死亡后的 image SHA/bytes 证明 host 文件状态；synthetic tears 仅证明列出的人工 byte mutations。",
        "- 本文件是机器附录；完整 walkthrough 仍须按 report-template.md 记录 owner 时间线、误解修正、局限和签名。",
        "", "## Static 与 offline mutation self-test", "",
        f"- accepted relations：`{self_test['good']}`",
        f"- rejected mutations：`{len(self_test['rejected'])}`",
        "", "```json", json.dumps(self_test, sort_keys=True, indent=2), "```",
    ]
    if records is None:
        lines.extend(["", "`--static-only` 未启动 crash/restart QEMU。"])
    else:
        lines.extend(["", "## Crash/restart records", ""])
        for case in (point[0] for point in POINTS):
            record = records[case]
            lines.extend([f"### {case}", "", "```json",
                          json.dumps(record, sort_keys=True, indent=2),
                          "```", ""])
        lines.extend(["## Synthetic tears", "", "```json",
                      json.dumps(tears, sort_keys=True, indent=2), "```"])
        lines.extend(["", "## Focused / quick / full regressions", "",
                      "```json", json.dumps(regressions, sort_keys=True,
                                             indent=2), "```"])
    lines.extend([
        "", "## Isolation", "",
        f"- 共享工作树/索引/内容指纹：`{before['digest']}`（运行前后相同）",
        f"- 共享 fs.img：`{HOST.digest_or_missing(REPO_ROOT / 'fs.img')}`（运行前后相同）",
        "- 每个 crash point 使用独立临时 image；SIGKILL 通过启动时取得的 pidfd 精确送达直接 QEMU PID。",
        "- fsck.py 是只读 publication acceptance oracle，不是 learner candidate 或 production repair tool。",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def run(*, candidate: Path | None, static_only: bool, self_test_only: bool,
        report: Path | None) -> None:
    _, _, baseline = load_contract()
    require(not (static_only and self_test_only),
            "--static-only and --self-test are mutually exclusive")
    require(static_only or self_test_only or candidate is not None,
            "--candidate is required for dynamic crash/recovery runs")
    if candidate is not None:
        candidate = candidate.expanduser().resolve(strict=True)
        require(not candidate.is_relative_to(REPO_ROOT),
                "--candidate must be outside the repository")
    if report is not None:
        require(not report.resolve().is_relative_to(REPO_ROOT),
                "--report must be outside the repository")
    before = HOST.repo_state()
    before_image = HOST.digest_or_missing(REPO_ROOT / "fs.img")
    records = tears = regressions = None
    candidate_hash = None
    environment = ""
    self_test = {}

    with tempfile.TemporaryDirectory(prefix="xv6-recovery-") as directory:
        scratch = Path(directory)
        root = scratch / "source"
        HOST.export_baseline(root, baseline)
        original = HOST.snapshot(root)
        analyze_baseline(root)
        HOST.inspect_patch(root, FIXTURE, FIXTURE_PATHS,
                           expected_created=FIXTURE_CREATED)
        with HOST.ExportSession(root, original) as session:
            HOST.checked(["make", "-j2", "kernel/kernel"], cwd=root, timeout=420)
            HOST.checked(["make", "clean"], cwd=root, timeout=180)
            require(HOST.snapshot(root) == original,
                    "native baseline build did not clean to its snapshot")
            session.apply(FIXTURE)
            analyze_fixture(root, candidate=False)
            if candidate is not None:
                staged = scratch / "candidate.patch"
                candidate_hash = HOST.stage_artifact(candidate, staged)
                HOST.inspect_patch(root, staged, CANDIDATE_PATHS)
                session.apply(staged)
                analyze_fixture(root, candidate=True)
            HOST.checked(["make", "-j2", "CPUS=1", "kernel/kernel",
                          "user/_recoverycase", "fs.img"], cwd=root, timeout=480)
            environment = environment_record(root)
            self_test = fsck_mutation_self_test(root / "fs.img")
            if not (static_only or self_test_only):
                pristine = scratch / "pristine.img"
                shutil.copyfile(root / "fs.img", pristine)
                records, crash_images = run_crash_points(root, pristine, scratch)
                tears = run_tears(root, crash_images, scratch)
                regressions = run_regressions(root)

    after = HOST.repo_state()
    require(after["digest"] == before["digest"],
            "shared repository content, mode, status, or index changed")
    require(HOST.digest_or_missing(REPO_ROOT / "fs.img") == before_image,
            "shared fs.img changed")
    if candidate is not None:
        require(HOST.sha256(candidate) == candidate_hash,
                "external candidate changed during validation")
    if report is not None:
        write_report(report, baseline=baseline, candidate=candidate,
                     candidate_hash=candidate_hash, environment=environment,
                     before=before, self_test=self_test, records=records,
                     tears=tears, regressions=regressions)
    if self_test_only:
        print(f"recovery self-test passed: {self_test['good']} good/"
              f"{len(self_test['rejected'])} rejected")
    elif static_only:
        print("recovery project passed: static, build, fsck mutations, cleanup")
    else:
        print("recovery project passed: static, crash/restart, fsck, tears, "
              "focused, quick, full, cleanup")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path,
                        help="external learner crash-hook patch")
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    require(not (args.self_test and (args.candidate or args.report)),
            "--self-test cannot be combined with --candidate or --report")
    run(candidate=args.candidate, static_only=args.static_only,
        self_test_only=args.self_test, report=args.report)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LabError, FSCK.ImageError, OSError, ValueError,
            subprocess.SubprocessError, struct.error) as exc:
        print(f"recovery project failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
