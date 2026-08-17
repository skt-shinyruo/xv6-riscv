#!/usr/bin/env python3

"""Run the isolated copy-on-write evidence project.

The learner implementation is intentionally supplied as an external patch (or
an external git worktree).  The repository only publishes the audit fixture;
this runner exports the pinned baseline into a temporary directory, applies the
private candidate and then the fixture, and removes both before returning.
"""

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
AUDIT_PATCH = Path(__file__).with_name("audit-fixture.patch")

# The fixture is deliberately narrow: it adds observation syscalls and a guest
# program, but never supplies the COW implementation being assessed.
AUDIT_PATHS = {
    "Makefile",
    "kernel/cowaudit.c",
    "kernel/cowaudit.h",
    "kernel/kalloc.c",
    "kernel/main.c",
    "kernel/syscall.c",
    "kernel/syscall.h",
    "kernel/sysproc.c",
    "user/cowtrace.c",
    "user/user.h",
    "user/usys.pl",
}

RELATED_TESTS = ("forktest", "copyout", "lazy_copy", "sbrkfail")
CASE_NAMES = (
    "SHARE",
    "MULTI",
    "LAZY",
    "COPYOUT",
    "PARTIAL",
    "OOM",
    "ROLLBACK",
    "PAIR",
    "BOUNDARY",
    "TEARDOWN",
)

MARKER_FIELDS = {
    "SHARE": frozenset({
        "pa", "parent_new", "child_old", "before_flags", "before_refs",
        "before_cows", "after_flags", "after_refs", "after_cows",
        "fast_flags", "fast_ref", "fast_cow", "parent_alloc", "child_alloc",
        "values",
    }),
    "MULTI": frozenset({
        "root", "child", "grandchild", "shared_pa", "before_flags",
        "before_refs", "before_cows", "after_pa", "after_flags", "after_refs",
        "after_cows", "before_values", "after_values",
    }),
    "LAZY": frozenset({
        "before", "before_flags", "parent_pa", "child_pa", "after_flags",
        "after_refs", "after_cows", "values",
    }),
    "COPYOUT": frozenset({
        "parent_pa", "child_pa", "flags", "refs", "cows", "parent", "child",
    }),
    "PARTIAL": frozenset({
        "generation", "eligible", "first", "retry", "fail_at", "fired",
        "parent", "child", "parent_flags", "child_flags", "parent_refs",
        "child_refs", "parent_cows", "child_cows", "parent_values",
        "child_values",
    }),
    "OOM": frozenset({
        "generation", "eligible", "status", "fail_at", "fired", "old_pa",
        "old_flags", "old_ref", "old_cow", "fast_alloc", "value",
    }),
    "ROLLBACK": frozenset({
        "generation", "fork", "fail_at", "fired", "eligible", "preexisting_pa",
        "preexisting_flags", "preexisting_ref", "preexisting_cow",
        "new_parent_flags", "new_parent_ref", "new_parent_cow", "fast_flags",
        "fast_ref", "fast_cow", "fast_alloc", "setup_free", "after_fork_free",
        "values",
    }),
    "PAIR": frozenset({
        "generation", "pids", "harts", "arrived", "gate_open", "eligible", "old",
        "new", "flags", "refs", "cows", "values",
    }),
    "BOUNDARY": frozenset({
        "text_pa", "text_flags", "text_ref", "text_cow", "guard_parent_pa",
        "guard_child_pa", "guard_flags", "guard_cows", "guard_status",
        "text_status", "tail_status",
    }),
    "TEARDOWN": frozenset({
        "shrink_leaf", "child_pa", "parent_pa", "parent_flags", "parent_ref",
        "parent_cow", "exec_bad", "bad_before_pa", "bad_after_pa", "bad_flags",
        "bad_refs", "bad_cows", "bad_value", "exec_status", "errors",
    }),
    "PASS": frozenset({"cases"}),
    "CLEAN": frozenset({
        "base", "after", "errors", "active_before", "active_after", "refs_before",
        "refs_after",
    }),
}


class LabError(RuntimeError):
    """A deterministic publication or experiment contract failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise LabError(message)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_or_missing(path: Path) -> str:
    return sha256(path) if path.is_file() else "missing"


def process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_group(process: subprocess.Popen) -> None:
    """Stop a process and all children, including a QEMU child process."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
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


def checked(command, *, cwd: Path | None = None, timeout: int = 180,
            env=None) -> str:
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


def git_bytes(arguments, *, cwd: Path = REPO_ROOT) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
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
    """Hash index, status, and every tracked/untracked/ignored shared file."""

    digest = hashlib.sha256()
    for label, arguments in (
        (b"index", ["ls-files", "-s", "-z"]),
        (b"status", ["status", "--porcelain=v1", "-z",
                     "--untracked-files=all", "--ignored"]),
    ):
        digest.update(label + b"\0" + git_bytes(arguments))
    listing = git_bytes([
        "ls-files", "-c", "-o", "-i", "--exclude-standard", "-z",
    ])
    for raw_path in sorted(set(listing.split(b"\0")) - {b""}):
        path = REPO_ROOT / os.fsdecode(raw_path)
        digest.update(b"path\0" + raw_path + b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(
                f"file:{stat.S_IMODE(path.stat().st_mode):o}\0".encode()
            )
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"missing\0")
    return digest.hexdigest()


def load_baseline() -> str:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    baseline = data["release"]["baseline_commit"]
    require(re.fullmatch(r"[0-9a-f]{40}", baseline) is not None,
            f"manifest baseline is not a full commit id: {baseline}")
    return baseline


def export_baseline(root: Path, baseline: str) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", baseline],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    root.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        try:
            stream.extractall(root, filter="data")
        except TypeError:  # Python 3.11 compatibility.
            stream.extractall(root)


def snapshot(root: Path):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = (
                stat.S_IMODE(path.stat().st_mode),
                sha256(path),
            )
    return result


def patch_paths(patch: bytes) -> set[str]:
    """Read paths from a unified patch without applying it."""

    text = patch.decode("utf-8", "replace")
    paths = set()
    for match in re.finditer(r"^diff --git a/(.+?) b/(.+?)$", text,
                             re.MULTILINE):
        old, new = match.groups()
        require(old == new, f"rename patch is not allowed: {old} -> {new}")
        require(not Path(new).is_absolute() and ".." not in Path(new).parts,
                f"unsafe patch path: {new}")
        paths.add(new)
    require(paths, "patch contains no git diff paths")
    return paths


def apply_patch(root: Path, patch_path: Path, *, reverse: bool = False) -> None:
    command = [
        "git", "apply", "--whitespace=error-all", "--recount",
        "--unidiff-zero",
    ]
    if reverse:
        command.append("-R")
    command.append(str(patch_path))
    checked(command, cwd=root, timeout=120)


def candidate_patch(path: Path, baseline: str, scratch: Path) -> tuple[bytes, str]:
    """Return an external patch and its digest.

    A git worktree is preferred because it lets a learner keep private notes or
    fixture experiments uncommitted: only ``baseline..HEAD`` is exported.  A
    plain patch file is accepted as well.
    """

    resolved = path.expanduser().resolve()
    require(not resolved.is_relative_to(REPO_ROOT),
            "--candidate must point outside the repository")
    require(resolved.exists(), f"candidate does not exist: {resolved}")
    if resolved.is_file():
        patch = resolved.read_bytes()
    else:
        require(resolved.is_dir(), f"candidate is not a file or directory: {resolved}")
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=resolved,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        require(probe.returncode == 0 and probe.stdout.strip() == "true",
                "candidate directory must be a git worktree or provide a patch file")
        patch = git_bytes(["diff", "--binary", f"{baseline}..HEAD"], cwd=resolved)
    require(patch.strip(), "candidate patch is empty")
    patch_path = scratch / "candidate.patch"
    patch_path.write_bytes(patch)
    patch_paths(patch)
    return patch, sha256_bytes(patch)


def validate_candidate_patch(patch: bytes) -> set[str]:
    paths = patch_paths(patch)
    forbidden = {
        "kernel/cowaudit.c",
        "kernel/cowaudit.h",
        "user/cowtrace.c",
    }
    require(not paths & forbidden,
            f"candidate must not include audit fixture files: {sorted(paths & forbidden)}")
    for path in paths:
        require(path != "fs.img" and not path.startswith("docs/"),
                f"candidate patch changes publication/shared artifacts: {path}")
        require(path.startswith("kernel/") or path.startswith("user/") or
                path == "Makefile",
                f"candidate patch leaves xv6 source scope: {path}")
    text = patch.decode("utf-8", "replace")
    for token in ("COWAUDIT", "cowaudit", "cowtrace", "COW CLEAN", "COW PASS"):
        require(token not in text,
                f"candidate patch embeds the publication fixture: {token}")
    return paths


def read_sources(root: Path, paths) -> dict[str, str]:
    result = {}
    for path in paths:
        candidate = root / path
        if candidate.is_file():
            result[path] = candidate.read_text(encoding="utf-8")
    return result


def require_tokens(text: str, tokens, context: str) -> None:
    missing = [token for token in tokens if token not in text]
    require(not missing, f"{context} missing anchors: {', '.join(missing)}")


def analyze_baseline(root: Path) -> None:
    paths = (
        "kernel/riscv.h", "kernel/kalloc.c", "kernel/vm.c", "kernel/proc.c",
        "kernel/trap.c", "kernel/exec.c", "kernel/pipe.c", "user/usertests.c",
    )
    sources = read_sources(root, paths)
    require(len(sources) == len(paths), "pinned baseline source scope is incomplete")
    require_tokens(
        sources["kernel/riscv.h"],
        ("PTE_V", "PTE_R", "PTE_W", "PTE_X", "PTE_U", "PTE_FLAGS",
         "sfence_vma"),
        "Sv39/PTE baseline",
    )
    require_tokens(
        sources["kernel/kalloc.c"], ("kinit(", "freerange(", "kalloc(", "kfree("),
        "allocator baseline",
    )
    require_tokens(
        sources["kernel/vm.c"],
        ("walk(", "mappages(", "uvmcopy(", "uvmunmap(", "uvmfree(",
         "copyout(", "copyin("),
        "address-space baseline",
    )
    require_tokens(
        sources["kernel/proc.c"],
        ("freeproc(", "kfork(", "proc_freepagetable("),
        "process teardown baseline",
    )
    require_tokens(sources["kernel/trap.c"], ("usertrap(",), "trap baseline")
    require_tokens(sources["kernel/exec.c"], ("kexec(",), "exec baseline")
    require_tokens(sources["kernel/pipe.c"], ("piperead(",), "pipe baseline")
    require_tokens(
        sources["user/usertests.c"],
        ("forktest(", "copyout(", "lazy_copy(", "sbrkfail("),
        "regression baseline",
    )
    all_source = "\n".join(sources.values())
    for token in ("PTE_COW", "cow_resolve", "kref_get", "COWAUDIT"):
        require(token not in all_source,
                f"pinned baseline unexpectedly contains candidate token: {token}")


def analyze_candidate(root: Path, candidate_paths: set[str]) -> None:
    files = [root / path for path in candidate_paths if (root / path).is_file()]
    changed_text = "\n".join(path.read_text(encoding="utf-8") for path in files)
    all_source = "\n".join(
        path.read_text(encoding="utf-8")
        for directory in (root / "kernel", root / "user")
        for path in directory.rglob("*")
        if path.is_file() and path.suffix in {".c", ".h", ".S"}
    )
    require("uvmcopy(" in all_source and "copyout(" in all_source and
            "usertrap(" in all_source,
            "candidate does not cover fork, kernel copyout, and store-fault paths")
    require(re.search(r"kalloc\s*\(", all_source) is not None and
            re.search(r"kfree\s*\(", all_source) is not None,
            "candidate does not expose allocation and release paths")
    adapter_signatures = {
        "cowproject_ref":
            r"\buint\s+cowproject_ref\s*\(\s*uint64\s+[A-Za-z_]\w*\s*\)\s*\{",
        "cowproject_active":
            r"\buint\s+cowproject_active\s*\(\s*void\s*\)\s*\{",
        "cowproject_total":
            r"\buint\s+cowproject_total\s*\(\s*void\s*\)\s*\{",
        "cowproject_errors":
            r"\buint\s+cowproject_errors\s*\(\s*void\s*\)\s*\{",
        "cowproject_free_pages":
            r"\bint\s+cowproject_free_pages\s*\(\s*void\s*\)\s*\{",
        "cowproject_is_cow":
            r"\bint\s+cowproject_is_cow\s*\(\s*pte_t\s+[A-Za-z_]\w*\s*\)\s*\{",
    }
    missing = [
        name for name, pattern in adapter_signatures.items()
        if re.search(pattern, changed_text) is None
    ]
    require(not missing,
            f"candidate audit adapter signatures missing: {', '.join(missing)}")


def analyze_audit_patch(patch: bytes) -> set[str]:
    paths = patch_paths(patch)
    require(paths == AUDIT_PATHS,
            f"audit fixture scope changed: {sorted(paths)}")
    text = patch.decode("utf-8", "replace")
    require_tokens(
        text,
        ("cowaudit", "COWAUDIT_", "cowaudit_kalloc_gate", "_cowtrace",
         "cowproject_", "COW SHARE", "COW PAIR", "COW CLEAN"),
        "audit fixture",
    )
    require("PTE_COW" not in text and "kref_" not in text,
            "audit fixture leaks candidate-specific COW symbols")
    require("cow_resolve" not in text and "uvmcopy(" not in text,
            "audit fixture must not publish a complete COW implementation")
    return paths


def analyze_fixture(root: Path) -> None:
    sources = read_sources(root, AUDIT_PATHS)
    require("kernel/cowaudit.c" in sources and "user/cowtrace.c" in sources,
            "audit fixture files did not apply")
    require_tokens(
        sources["Makefile"], ("$K/cowaudit.o", "$U/_cowtrace"),
        "audit build registration",
    )
    require_tokens(
        sources["kernel/cowaudit.c"],
        ("cowaudit_command", "cowaudit_kalloc_gate", "cowproject_ref",
         "cowproject_is_cow", "COWAUDIT_MODE_PAIR"),
        "audit kernel seam",
    )
    require_tokens(
        sources["user/cowtrace.c"],
        tuple(f"COW {name}" for name in CASE_NAMES) +
        ("COW PASS cases=%d", "COW CLEAN", "active_before=%d",
         "refs_before=%d"),
        "audit guest oracle",
    )


def build(root: Path) -> None:
    checked(
        ["make", "-j2", "CPUS=2", "kernel/kernel", "user/_cowtrace", "fs.img"],
        cwd=root,
        timeout=480,
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
            self.read_until(b"$ ", 120)
        except BaseException:
            self.stop()
            raise

    def read_until(self, marker: bytes, timeout: float, *, start: int = 0) -> None:
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

    def command(self, command: str, *, timeout: int = 240) -> str:
        require(self.proc.stdin is not None, "QEMU stdin is closed")
        start = len(self.output)
        self.proc.stdin.write((command + "\n").encode())
        self.proc.stdin.flush()
        self.read_until(b"$ ", timeout, start=start)
        return bytes(self.output[start:]).decode("utf-8", "replace")

    def tail(self) -> str:
        return bytes(self.output[-10000:]).decode("utf-8", "replace")

    def stop(self) -> None:
        try:
            self.selector.close()
        finally:
            stop_process_group(self.proc)


def parse_fields(line: str) -> dict[str, object]:
    values: dict[str, object] = {}
    tokens = line.split()
    require(len(tokens) >= 3, f"marker has no fields: {line}")
    for token in tokens[2:]:
        match = re.fullmatch(r"([a-z_]+)=([^\s]+)", token)
        require(match is not None, f"malformed marker field: {token}")
        key, value = match.groups()
        require(key not in values, f"duplicate marker field: {key}")
        try:
            values[key] = int(value, 0)
        except ValueError:
            values[key] = value
    return values


def require_exact_fields(fields: dict[str, object], expected: frozenset[str],
                         context: str) -> None:
    observed = frozenset(fields)
    require(observed == expected,
            f"{context} field set changed: expected {sorted(expected)}, "
            f"got {sorted(observed)}")


def parse_int_list(value: object, *, count: int, context: str) -> list[int]:
    if isinstance(value, int):
        require(count == 1, f"{context} expected {count} fields, got one")
        return [value]
    require(isinstance(value, str), f"{context} is not a slash-separated field")
    parts = value.split("/")
    require(len(parts) == count, f"{context} field count changed: {value}")
    try:
        return [int(part, 0) for part in parts]
    except ValueError as exc:
        raise LabError(f"{context} contains a non-integer value: {value}") from exc


def marker_lines(output: str, prefix: str) -> list[str]:
    return [line.strip() for line in output.splitlines()
            if line.strip().startswith(prefix)]


def validate_leaf_fields(fields: dict[str, object], *, flags: str,
                         refs: str, cows: str, count: int,
                         expected_flags: int, expected_ref: int,
                         expected_cow: int, context: str) -> None:
    flag_values = parse_int_list(fields.get(flags), count=count,
                                 context=f"{context} flags")
    ref_values = parse_int_list(fields.get(refs), count=count,
                                context=f"{context} refs")
    cow_values = parse_int_list(fields.get(cows), count=count,
                                context=f"{context} cows")
    require(all((value & 0x1f) == expected_flags for value in flag_values),
            f"{context} PTE flag mask changed: {flag_values}")
    require(all(value == expected_ref for value in ref_values),
            f"{context} refcount changed: {ref_values}")
    require(all(value == expected_cow for value in cow_values),
            f"{context} normalized COW bit changed: {cow_values}")


def validate_single_leaf(fields: dict[str, object], *, flags: str,
                        ref: str, cow: str, expected_flags: int,
                        expected_ref: int, expected_cow: int,
                        context: str) -> None:
    validate_leaf_fields(
        fields, flags=flags, refs=ref, cows=cow, count=1,
        expected_flags=expected_flags, expected_ref=expected_ref,
        expected_cow=expected_cow, context=context,
    )


def validate_cowtrace(output: str) -> dict[str, object]:
    failures = marker_lines(output, "COW FAIL ")
    require(not failures,
            f"cowtrace reported failed oracle(s): {failures}; "
            f"transcript tail: {output[-4000:]}")
    cases = []
    case_fields = []
    for name in CASE_NAMES:
        lines = marker_lines(output, f"COW {name} ")
        require(len(lines) == 1, f"COW {name} marker count changed")
        cases.append(lines[0])
        fields = parse_fields(lines[0])
        require_exact_fields(fields, MARKER_FIELDS[name], f"COW {name}")
        case_fields.append(fields)
    pass_lines = marker_lines(output, "COW PASS ")
    require(len(pass_lines) == 1, "COW PASS marker count changed")
    passed = parse_fields(pass_lines[0])
    require_exact_fields(passed, MARKER_FIELDS["PASS"], "COW PASS")
    require(passed.get("cases") == len(CASE_NAMES),
            "COW PASS case count does not match observed cases")
    clean_lines = marker_lines(output, "COW CLEAN ")
    require(len(clean_lines) == 1, "COW CLEAN marker count changed")
    clean = parse_fields(clean_lines[0])
    require_exact_fields(clean, MARKER_FIELDS["CLEAN"], "COW CLEAN")
    require(clean.get("base") == clean.get("after") and
            clean.get("errors") == 0 and
            clean.get("active_before") == clean.get("active_after") and
            clean.get("refs_before") == clean.get("refs_after") and
            isinstance(clean.get("active_before"), int) and
            clean["active_before"] > 0 and
            isinstance(clean.get("refs_before"), int) and
            clean["refs_before"] > 0,
            "whole-worker free-page/refcount ledger did not recover")

    share = case_fields[0]
    require(share.get("pa") == share.get("child_old") and
            share.get("pa") != share.get("parent_new") and
            isinstance(share.get("pa"), int) and share["pa"] != 0,
            "share PA ownership relation failed")
    require(isinstance(share.get("parent_new"), int) and
            share["parent_new"] != 0,
            "share private PA is zero")
    validate_leaf_fields(
        share, flags="before_flags", refs="before_refs", cows="before_cows",
        count=2, expected_flags=0x13, expected_ref=2, expected_cow=1,
        context="share before",
    )
    share_after_flags = parse_int_list(share.get("after_flags"), count=2,
                                       context="share after flags")
    share_after_refs = parse_int_list(share.get("after_refs"), count=2,
                                      context="share after refs")
    share_after_cows = parse_int_list(share.get("after_cows"), count=2,
                                      context="share after cows")
    require([(value & 0x1f) for value in share_after_flags] == [0x17, 0x13] and
            share_after_refs == [1, 1] and share_after_cows == [0, 1],
            "share after PTE ownership relation failed")
    validate_single_leaf(
        share, flags="fast_flags", ref="fast_ref", cow="fast_cow",
        expected_flags=0x17, expected_ref=1, expected_cow=0,
        context="share fast path",
    )
    share_values = parse_int_list(share.get("values"), count=2,
                                  context="share values")
    require(share.get("parent_alloc") == 1 and
            share.get("child_alloc") == 0 and share_values == [34, 51],
            "share/fast-path oracle failed")
    multi = case_fields[1]
    multi_pids = [multi.get("root"), multi.get("child"), multi.get("grandchild")]
    multi_before_flags = parse_int_list(multi.get("before_flags"), count=3,
                                        context="multi before flags")
    multi_before_refs = parse_int_list(multi.get("before_refs"), count=3,
                                       context="multi before refs")
    multi_before_cows = parse_int_list(multi.get("before_cows"), count=3,
                                       context="multi before cows")
    multi_after_pa = parse_int_list(multi.get("after_pa"), count=3,
                                    context="multi after PA")
    multi_after_flags = parse_int_list(multi.get("after_flags"), count=3,
                                       context="multi after flags")
    multi_after_refs = parse_int_list(multi.get("after_refs"), count=3,
                                      context="multi after refs")
    multi_after_cows = parse_int_list(multi.get("after_cows"), count=3,
                                      context="multi after cows")
    multi_before_values = parse_int_list(multi.get("before_values"), count=3,
                                         context="multi before values")
    multi_after_values = parse_int_list(multi.get("after_values"), count=3,
                                        context="multi after values")
    require(all(isinstance(pid, int) and pid > 0 for pid in multi_pids) and
            len(set(multi_pids)) == 3 and
            isinstance(multi.get("shared_pa"), int) and multi["shared_pa"] != 0 and
            [(value & 0x1f) for value in multi_before_flags] ==
                [0x13, 0x13, 0x13] and
            multi_before_refs == [3, 3, 3] and multi_before_cows == [1, 1, 1] and
            multi_after_pa[0] == multi_after_pa[1] == multi["shared_pa"] and
            multi_after_pa[2] != 0 and multi_after_pa[2] != multi["shared_pa"] and
            [(value & 0x1f) for value in multi_after_flags] == [0x13, 0x13, 0x17] and
            multi_after_refs == [2, 2, 1] and multi_after_cows == [1, 1, 0] and
            multi_before_values == [33, 33, 33] and
            multi_after_values == [33, 33, 42],
            "multi-generation write/refcount oracle failed")
    lazy = case_fields[2]
    before_flags = parse_int_list(lazy.get("before_flags"), count=2,
                                  context="lazy before flags")
    lazy_values = parse_int_list(lazy.get("values"), count=2,
                                 context="lazy values")
    require(lazy.get("before") == "0/0" and before_flags == [0, 0] and
            lazy_values == [102, 85] and
            isinstance(lazy.get("parent_pa"), int) and
            isinstance(lazy.get("child_pa"), int) and
            lazy["parent_pa"] != 0 and lazy["child_pa"] != 0 and
            lazy["parent_pa"] != lazy["child_pa"],
            "lazy-hole oracle failed")
    validate_leaf_fields(
        lazy, flags="after_flags", refs="after_refs", cows="after_cows",
        count=2, expected_flags=0x17, expected_ref=1, expected_cow=0,
        context="lazy after",
    )
    copyout = case_fields[3]
    # The parent remains COW while the child is private after kernel copyout;
    # both raw pairs are emitted and checked independently.
    copyout_flags = parse_int_list(copyout.get("flags"), count=2,
                                   context="copyout flags")
    copyout_refs = parse_int_list(copyout.get("refs"), count=2,
                                  context="copyout refs")
    copyout_cows = parse_int_list(copyout.get("cows"), count=2,
                                  context="copyout cows")
    require((copyout_flags[0] & 0x1f) == 0x13 and
            (copyout_flags[1] & 0x1f) == 0x17 and
            copyout_refs == [1, 1] and copyout_cows == [1, 0],
            "copyout PTE ownership relation failed")
    copyout_values = parse_int_list(copyout.get("child"), count=4,
                                    context="copyout child bytes")
    require(copyout.get("parent") == 68 and
            copyout_values == [97, 98, 99, 100] and
            isinstance(copyout.get("parent_pa"), int) and
            isinstance(copyout.get("child_pa"), int) and
            copyout["parent_pa"] != 0 and copyout["child_pa"] != 0 and
            copyout["parent_pa"] != copyout["child_pa"],
            "kernel copyout isolation oracle failed")
    partial = case_fields[4]
    partial_parent = parse_int_list(partial.get("parent"), count=2,
                                    context="partial parent PA")
    partial_child = parse_int_list(partial.get("child"), count=2,
                                   context="partial child PA")
    validate_leaf_fields(
        partial, flags="parent_flags", refs="parent_refs", cows="parent_cows",
        count=2, expected_flags=0x13, expected_ref=1, expected_cow=1,
        context="partial parent",
    )
    validate_leaf_fields(
        partial, flags="child_flags", refs="child_refs", cows="child_cows",
        count=2, expected_flags=0x17, expected_ref=1, expected_cow=0,
        context="partial child",
    )
    parent_values = parse_int_list(partial.get("parent_values"), count=2,
                                   context="partial parent values")
    child_values = parse_int_list(partial.get("child_values"), count=2,
                                  context="partial child values")
    require(partial.get("generation") == 5 and partial.get("eligible") == 2 and
            partial.get("first") == 16 and partial.get("retry") == 16 and
            partial.get("fail_at") == 2 and partial.get("fired") == 1 and
            parent_values == [65, 65] and child_values == [32, 48] and
            all(pa != 0 for pa in partial_parent + partial_child) and
            partial_parent[0] != partial_child[0] and
            partial_parent[1] != partial_child[1] and
            len(set(partial_parent)) == 2 and len(set(partial_child)) == 2 and
            len(set(partial_parent + partial_child)) == 4,
            "partial copyout/OOM oracle failed")
    oom = case_fields[5]
    validate_single_leaf(
        oom, flags="old_flags", ref="old_ref", cow="old_cow",
        expected_flags=0x13, expected_ref=1, expected_cow=1,
        context="store OOM old mapping",
    )
    require(oom.get("generation") == 6 and oom.get("eligible") == 1 and
            oom.get("status") == -1 and oom.get("fail_at") == 1 and
            oom.get("fired") == 1 and isinstance(oom.get("old_pa"), int) and
            oom["old_pa"] != 0 and
            oom.get("fast_alloc") == 0 and oom.get("value") == 51,
            "store-fault OOM oracle failed")
    rollback = case_fields[6]
    validate_single_leaf(
        rollback, flags="preexisting_flags", ref="preexisting_ref",
        cow="preexisting_cow", expected_flags=0x13, expected_ref=2,
        expected_cow=1, context="rollback preexisting",
    )
    validate_single_leaf(
        rollback, flags="new_parent_flags", ref="new_parent_ref",
        cow="new_parent_cow", expected_flags=0x17, expected_ref=1,
        expected_cow=0, context="rollback new parent",
    )
    validate_single_leaf(
        rollback, flags="fast_flags", ref="fast_ref", cow="fast_cow",
        expected_flags=0x17, expected_ref=1, expected_cow=0,
        context="rollback fast path",
    )
    rollback_values = parse_int_list(rollback.get("values"), count=3,
                                     context="rollback values")
    require(rollback.get("generation") == 7 and rollback.get("fork") == -1 and
            rollback.get("fail_at") == 8 and
            rollback.get("fired") == 1 and rollback.get("eligible") == 8 and
            rollback.get("fast_alloc") == 0 and
            rollback.get("setup_free") == rollback.get("after_fork_free") and
            rollback_values == [65, 82, 67] and
            isinstance(rollback.get("preexisting_pa"), int) and
            rollback["preexisting_pa"] != 0 and
            isinstance(rollback.get("setup_free"), int) and
            rollback["setup_free"] > 0,
            "partial fork rollback oracle failed")
    pair = case_fields[7]
    pair_pids = parse_int_list(pair.get("pids"), count=2, context="pair pids")
    pair_harts = parse_int_list(pair.get("harts"), count=2, context="pair harts")
    pair_new = parse_int_list(pair.get("new"), count=2, context="pair new PA")
    pair_refs = parse_int_list(pair.get("refs"), count=3, context="pair refs")
    pair_flags = parse_int_list(pair.get("flags"), count=3,
                                context="pair flags")
    pair_cows = parse_int_list(pair.get("cows"), count=3,
                               context="pair cows")
    require([(value & 0x1f) for value in pair_flags] == [0x13, 0x17, 0x17] and
            pair_cows == [1, 0, 0],
            "two-hart PTE ownership relation failed")
    pair_values = parse_int_list(pair.get("values"), count=2,
                                 context="pair values")
    require(pair.get("generation") == 8 and pair.get("arrived") == 3 and
            pair.get("gate_open") == 1 and
            pair.get("eligible") == 2 and sorted(pair_values) == [114, 115] and
            pair_pids[0] != pair_pids[1] and pair_harts[0] != pair_harts[1] and
            all(pid > 0 for pid in pair_pids) and
            all(hart >= 0 for hart in pair_harts) and
            isinstance(pair.get("old"), int) and pair["old"] != 0 and
            all(pa != 0 for pa in pair_new) and
            len({pair["old"], *pair_new}) == 3 and pair_refs == [1, 1, 1],
            "two-hart simultaneous split oracle failed")
    boundary = case_fields[8]
    text_flags = int(boundary.get("text_flags", -1))
    text_ref = int(boundary.get("text_ref", -1))
    text_cow = int(boundary.get("text_cow", -1))
    require((text_flags & 0x1f) == 0x1b and text_ref >= 2 and text_cow == 0,
            "text boundary PTE ownership relation failed")
    guard_flags = parse_int_list(boundary.get("guard_flags"), count=2,
                                 context="guard flags")
    guard_cows = parse_int_list(boundary.get("guard_cows"), count=2,
                                context="guard cows")
    require(isinstance(boundary.get("text_pa"), int) and
            boundary["text_pa"] != 0 and
            isinstance(boundary.get("guard_parent_pa"), int) and
            isinstance(boundary.get("guard_child_pa"), int) and
            boundary["guard_parent_pa"] != 0 and
            boundary["guard_child_pa"] != 0 and
            boundary["guard_parent_pa"] != boundary["guard_child_pa"] and
            all((value & 0x10) == 0 for value in guard_flags) and
            guard_cows == [0, 0] and
            boundary.get("guard_status") == -1 and
            boundary.get("text_status") == -1 and
            boundary.get("tail_status") == 0,
            "text/guard/tail boundary oracle failed")
    teardown = case_fields[9]
    validate_single_leaf(
        teardown, flags="parent_flags", ref="parent_ref", cow="parent_cow",
        expected_flags=0x13, expected_ref=1, expected_cow=1,
        context="teardown parent",
    )
    bad_flags = parse_int_list(teardown.get("bad_flags"), count=2,
                               context="teardown bad flags")
    bad_refs = parse_int_list(teardown.get("bad_refs"), count=2,
                              context="teardown bad refs")
    bad_cows = parse_int_list(teardown.get("bad_cows"), count=2,
                              context="teardown bad cows")
    require(teardown.get("shrink_leaf") == 0 and
            teardown.get("child_pa") == 0 and
            teardown.get("parent_pa") != 0 and
            teardown.get("exec_bad") == -1 and
            teardown.get("bad_before_pa") == teardown.get("bad_after_pa") and
            teardown.get("bad_before_pa") != 0 and
            [(value & 0x1f) for value in bad_flags] == [0x13, 0x13] and
            bad_refs == [2, 2] and bad_cows == [1, 1] and
            teardown.get("bad_value") == 25 and
            teardown.get("exec_status") == 0 and
            teardown.get("errors") == 0,
            "teardown/exec oracle failed")
    allowed = {f"COW {name}" for name in CASE_NAMES} | {
        "COW PASS", "COW CLEAN"
    }
    observed = {line.split(" ", 2)[0] + " " + line.split(" ", 2)[1]
                for line in output.splitlines() if line.startswith("COW ")}
    require(observed <= allowed, f"unexpected COW markers: {sorted(observed - allowed)}")
    return {"markers": cases + [pass_lines[0], clean_lines[0]],
            "transcript": output}


def validate_regression(name: str, output: str) -> None:
    require("SOME TESTS FAILED" not in output,
            f"related regression failed: {name}")
    require(output.count("ALL TESTS PASSED") == 1,
            f"related marker count changed: {name}")
    require(f"test {name}:" in output,
            f"related test did not execute: {name}")
    require("COW " not in output,
            f"audit marker leaked into related regression: {name}")


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
            f"test-xv6 watchdog expired: {' '.join(args)}\n{output[-10000:]}"
        ) from exc
    finally:
        if process.poll() is None or process_group_exists(process.pid):
            stop_process_group(process)
    require(process.returncode == 0,
            f"test-xv6 failed: {' '.join(args)}\n{output[-10000:]}")
    require(output.count("ALL TESTS PASSED") == 1,
            f"driver success marker count changed: {' '.join(args)}")
    require("SOME TESTS FAILED" not in output,
            f"driver reported failure: {' '.join(args)}")
    require("COW " not in output,
            f"COW marker leaked into driver: {' '.join(args)}")
    return output


def run_dynamic(root: Path) -> dict[str, object]:
    records: dict[str, object] = {"related": {}}
    qemu = None
    try:
        qemu = Qemu(root, cpus=2)
        records["cowtrace"] = validate_cowtrace(
            qemu.command("cowtrace", timeout=300)
        )
        for name in RELATED_TESTS:
            transcript = qemu.command(f"usertests {name}", timeout=360)
            validate_regression(name, transcript)
            records["related"][name] = transcript
    finally:
        if qemu is not None:
            qemu.stop()
    records["quick"] = run_driver(root, ["-q", "usertests"], cpus=2,
                                   timeout=600)
    records["full"] = run_driver(root, ["usertests"], cpus=1,
                                  timeout=1000)
    return records


def environment_record(root: Path) -> str:
    database = checked(["make", "-pn"], cwd=root, timeout=60)
    match = re.search(r"^TOOLPREFIX := (\S*)$", database, re.MULTILINE)
    require(match is not None, "cannot resolve Makefile TOOLPREFIX")
    compiler = checked([f"{match.group(1)}gcc", "--version"],
                       timeout=30).splitlines()[0]
    return "; ".join((
        checked(["uname", "-srmo"], timeout=30).strip(),
        checked(["qemu-system-riscv64", "--version"], timeout=30).splitlines()[0],
        checked(["make", "--version"], timeout=30).splitlines()[0],
        compiler,
        f"Python {sys.version.split()[0]}",
    ))


def compact_transcript(output: str, limit: int = 12000) -> str:
    return output if len(output) <= limit else output[-limit:]


def write_report(path: Path, *, baseline: str, tutorial_commit: str,
                 candidate_path: str | None,
                 candidate_digest: str | None, audit_digest: str,
                 runner_digest: str, environment: str,
                 shared_state_digest: str, shared_image_digest: str,
                 records: dict[str, object] | None) -> None:
    lines = [
        "# Copy-on-Write fork 证据报告包",
        "",
        f"- 源码基线：`{baseline}`",
        f"- 走查时教程提交：`{tutorial_commit}`",
        f"- 私有 candidate：`{candidate_path or 'N/A (--static-only)'}`",
        f"- 私有 candidate patch SHA-256：`{candidate_digest or 'N/A (--static-only)'}`",
        f"- audit fixture SHA-256：`{audit_digest}`",
        f"- runner SHA-256：`{runner_digest}`",
        f"- 主机与工具：`{environment}`",
        f"- 运行时间：`{time.strftime('%Y-%m-%dT%H:%M:%S%z')}`",
        "- 动态配置：cowtrace/related 使用 CPUS=2；quick 使用 CPUS=2；full 使用 CPUS=1；QEMU guest RAM 128 MiB；临时 fs.img",
        "- 本文件是机器原始记录附录；完整出口仍须按 report-template.md 补充 owner 时间线、逐场景 source/副作用/资源解释、局限和签名。",
        "",
        "## S 静态契约",
        "",
        "固定 baseline 的 Sv39/PTE、allocator、uvmcopy、copyout、trap、exec、",
        "freewalk 和 usertests anchors 已检查；candidate 只能来自仓库外，并须",
        "提供六个 cowproject_* 只读 adapter；",
        "audit fixture 不包含 `cow_resolve`/`uvmcopy` 的完整实现。candidate 与",
        "fixture 都只在临时导出中应用，不能改变共享工作树。",
        "",
        "## F/B/C 动态原始 marker",
        "",
    ]
    if records is None:
        lines.append("`--static-only` 未启动 QEMU；F/B/C 动态轨迹为 N/A。")
    else:
        cow = records["cowtrace"]
        lines.extend(["```text", *cow["markers"], "```", ""])
        lines.extend([
            f"focused transcript SHA-256：`{sha256_bytes(cow['transcript'].encode())}`",
            "focused 命令为 `cowtrace`，退出到 shell prompt；host 已逐字段复算 raw flags/cow/ref/PA/bytes/status。",
            "",
            "share/fast path、multi-generation、lazy hole、kernel copyout、partial",
            "copyout、store-fault OOM、partial fork rollback、two-hart pair、",
            "text/guard/tail boundary 和 shrink/exec teardown 均有精确 marker；",
            "`COW CLEAN base=after errors=0` 是 worker 结束后的资源账本 oracle。",
            "",
            "### related",
            "",
        ])
        for name, transcript in records["related"].items():
            lines.append(f"#### {name}")
            lines.append(f"transcript SHA-256: `{sha256_bytes(transcript.encode())}`")
            lines.extend(["```text", compact_transcript(transcript), "```", ""])
        lines.extend([
            "### quick/full",
            "",
            "quick `CPUS=2` 与 full `CPUS=1` 各有且仅有一个 `ALL TESTS PASSED`，",
            "且无 COW fixture marker 泄漏。",
            f"quick transcript SHA-256：`{sha256_bytes(records['quick'].encode())}`",
            f"full transcript SHA-256：`{sha256_bytes(records['full'].encode())}`",
        ])
    lines.extend([
        "",
        "## 证据分类与局限",
        "",
        "- S：固定源码 anchors、candidate/fixture scope、PTE/refcount/teardown 契约。",
        "- F：共享读、私有写、lazy 物化、copyout、正常 shrink/exec 与回归。",
        "- B：权限/guard/text/tail、确定性 allocator fail_at、partial rollback 与 cleanup。",
        "- C：pair fixture 以两个进程的 kalloc gate 和 hart 字段证明受控同时拆页；不推出公平性、所有交错或 remote TLB shootdown。",
        "- R：N/A；本项目不声称磁盘 crash/recovery 或持久化保证。",
        "- timeout 只是 watchdog；固定场景和有限 OOM 序列不能替代形式化证明。",
        "",
        "## 清理证明",
        "",
        f"- 共享工作树/索引/内容指纹：`{shared_state_digest}`（前后相同）",
        f"- 共享 `fs.img`：`{shared_image_digest}`（前后相同）",
        "- 临时导出执行 `make clean`，按 audit → candidate 逆序撤销；QEMU/driver 进程组均消失。",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(*, static_only: bool, candidate: Path | None,
        report: Path | None) -> None:
    baseline = load_baseline()
    tutorial_commit = checked(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                              timeout=30).strip()
    require(static_only or candidate is not None,
            "--candidate is required unless --static-only is selected")
    if candidate is not None:
        candidate_resolved = candidate.expanduser().resolve()
        require(not candidate_resolved.is_relative_to(REPO_ROOT),
                "--candidate must point outside the repository")
        require(candidate_resolved.exists(),
                f"candidate does not exist: {candidate_resolved}")
    if report is not None:
        require(not report.resolve().is_relative_to(REPO_ROOT),
                "--report must be outside repository")
    before_state = repo_state_digest()
    before_image = digest_or_missing(REPO_ROOT / "fs.img")
    environment = ""
    audit_bytes = AUDIT_PATCH.read_bytes()
    audit_digest = sha256_bytes(audit_bytes)
    audit_paths = analyze_audit_patch(audit_bytes)
    records = None
    candidate_bytes = None
    candidate_digest = None

    with tempfile.TemporaryDirectory(prefix="xv6-copy-on-write-") as directory:
        scratch = Path(directory)
        root = scratch / "source"
        export_baseline(root, baseline)
        original = snapshot(root)
        analyze_baseline(root)
        environment = environment_record(root)
        candidate_path = None
        candidate_applied = False
        audit_applied = False
        try:
            if not static_only:
                candidate_bytes, candidate_digest = candidate_patch(
                    candidate, baseline, scratch
                )
                candidate_paths = validate_candidate_patch(candidate_bytes)
                candidate_path = scratch / "candidate.patch"
                apply_patch(root, candidate_path)
                candidate_applied = True
                analyze_candidate(root, candidate_paths)
                analyze_audit_patch(audit_bytes)
                audit_path = scratch / "audit-fixture.patch"
                audit_path.write_bytes(audit_bytes)
                apply_patch(root, audit_path)
                audit_applied = True
                analyze_fixture(root)
                build(root)
                records = run_dynamic(root)
            else:
                # Static-only still proves that the pinned executable baseline
                # builds, while fixture syntax/scope is checked above without
                # applying COW-dependent context to the baseline.
                checked(["make", "-j2", "CPUS=2", "kernel/kernel"],
                        cwd=root, timeout=420)
        finally:
            # QEMU is stopped by run_dynamic before this point.  Always remove
            # generated artifacts before reversing source patches.
            try:
                checked(["make", "clean"], cwd=root, timeout=180)
            finally:
                if audit_applied:
                    apply_patch(root, scratch / "audit-fixture.patch", reverse=True)
                if candidate_applied:
                    apply_patch(root, candidate_path, reverse=True)
                checked(["make", "clean"], cwd=root, timeout=180)
                require(snapshot(root) == original,
                        "temporary source did not return to pinned baseline")

    after_state = repo_state_digest()
    after_image = digest_or_missing(REPO_ROOT / "fs.img")
    require(after_state == before_state,
            "shared repository content, index, or generated navigation changed")
    require(after_image == before_image, "shared fs.img changed")
    if report is not None:
        write_report(
            report,
            baseline=baseline,
            tutorial_commit=tutorial_commit,
            candidate_path=(str(candidate.expanduser().resolve())
                            if candidate is not None else None),
            candidate_digest=candidate_digest,
            audit_digest=audit_digest,
            runner_digest=sha256(Path(__file__)),
            environment=environment,
            shared_state_digest=before_state,
            shared_image_digest=before_image,
            records=records,
        )
    label = "static, build, cleanup" if static_only else (
        "static, F/B/C, focused, related, quick, full, cleanup"
    )
    print(f"copy-on-write project passed: {label}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path,
                        help="external candidate patch or git worktree")
    parser.add_argument("--static-only", action="store_true",
                        help="validate baseline and published fixture without a candidate")
    parser.add_argument("--report", type=Path,
                        help="write the evidence report outside the repository")
    args = parser.parse_args()
    try:
        run(static_only=args.static_only, candidate=args.candidate,
            report=args.report)
    except (LabError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
