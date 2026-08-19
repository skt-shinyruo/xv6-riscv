#!/usr/bin/env python3
"""Isolated publication runner for the persistence evidence project."""

import argparse
import copy
import hashlib
import io
import json
import os
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


RESOURCE_DIR = Path(__file__).resolve().parent
REPO_ROOT = RESOURCE_DIR.parents[3]
MANIFEST = REPO_ROOT / "docs/xv6-tutorial/curriculum.json"
SCENARIO_PATH = RESOURCE_DIR / "scenarios.json"
FIXTURE = RESOURCE_DIR / "persistence-audit.patch"
BASELINE_ADAPTER = RESOURCE_DIR / "baseline-cache-adapter.patch"
RUNNER = Path(__file__).resolve()

NBUF = 30
MAXOPBLOCKS = 10
LOGBLOCKS = 30
BSIZE = 1024
FSSIZE = 2000
ROOTDEV = 1
PA_ABI = 1
EVENT_BUDGET = 512
PANIC_TEXT = "panic: bget: no buffers"

FIXTURE_PATHS = {
    "Makefile",
    "kernel/defs.h",
    "kernel/log.c",
    "kernel/main.c",
    "kernel/persistenceaudit.c",
    "kernel/persistenceaudit.h",
    "kernel/syscall.c",
    "kernel/syscall.h",
    "kernel/sysproc.c",
    "kernel/virtio_disk.c",
    "user/persisttrace.c",
    "user/user.h",
    "user/usys.pl",
}
FIXTURE_CREATED = {
    "kernel/persistenceaudit.c",
    "kernel/persistenceaudit.h",
    "user/persisttrace.c",
}
CACHE_PATCH_PATHS = {"kernel/bio.c"}

CASE_RUNTIME = {
    "tx": ("transaction-flow", 1, 1),
    "admission-empty": ("log-capacity-empty", 2, 2),
    "admission-used": ("log-capacity-used", 3, 2),
    "same": ("cache-same-block", 4, 2),
    "collision": ("cache-collision", 5, 2),
    "parallel": ("cache-parallel", 6, 2),
    "cache-full": ("cache-full", 7, 1),
}
NORMAL_CASES = tuple(name for name in CASE_RUNTIME if name != "cache-full")
RESULT_CASES = {"same", "collision", "parallel"}

POINT_CAPABILITIES = {
    "CACHE_GET_ENTER": "sleep-ok",
    "CACHE_SELECTED": "observe-only",
    "CACHE_LOCKED": "sleep-ok-with-buffer-sleeplock",
    "CACHE_PARTITION_HELD": "spin-2cpu-ok",
    "CACHE_RELEASED": "observe-only",
    "LOG_APPENDED": "sleep-ok",
    "LOG_ABSORBED": "observe-only",
    "BEGIN_ADMITTED": "sleep-ok",
    "BEGIN_WAIT_SPACE": "observe-only",
    "IO_SUBMIT": "observe-only",
    "IO_COMPLETE": "observe-only",
    "HEADER_COMMIT_COMPLETE": "observe-only",
    "HOME_INSTALL_COMPLETE": "observe-only",
    "HEADER_CLEAR_COMPLETE": "observe-only",
}

CASE_CONTRACTS = {
    "transaction-flow": {
        "cpus": 1,
        "actors": {"writer": (
            "select-stable-home-block", "begin-op",
            "write-same-home-block-twice", "end-op", "orderly-stop",
        )},
        "oracle": "transaction-state-machine",
        "evidence": {"F", "R"},
    },
    "log-capacity-empty": {
        "cpus": 2,
        "actors": {
            "a": ("begin-op", "hold", "end-op"),
            "b": ("begin-op", "hold", "end-op"),
            "c": ("begin-op", "hold", "end-op"),
            "d": ("begin-op", "end-op"),
        },
        "oracle": "empty-log-admission-bound",
        "evidence": {"B", "C"},
    },
    "log-capacity-used": {
        "cpus": 2,
        "actors": {
            "a": ("begin-op", "append-one-block", "hold", "end-op"),
            "b": ("begin-op", "hold", "end-op"),
            "c": ("begin-op", "end-op"),
        },
        "oracle": "used-log-admission-bound",
        "evidence": {"B", "C"},
    },
    "cache-same-block": {
        "cpus": 2,
        "actors": {
            "a": ("read-uncached-target", "hold", "release"),
            "b": ("read-uncached-target", "release"),
        },
        "oracle": "one-buffer-per-block",
        "evidence": {"C"},
    },
    "cache-collision": {
        "cpus": 2,
        "actors": {
            "a": ("read-discovered-left", "hold", "release"),
            "b": ("read-discovered-right", "hold", "release"),
        },
        "oracle": "collision-keeps-distinct-identities",
        "evidence": {"C"},
        "discovery": "two-distinct-blocks-same-partition",
    },
    "cache-parallel": {
        "cpus": 2,
        "actors": {
            "a": ("read-discovered-left", "release"),
            "b": ("read-discovered-right", "release"),
        },
        "oracle": "different-partitions-overlap",
        "evidence": {"C"},
        "discovery": "two-blocks-different-partitions",
    },
    "cache-full": {
        "cpus": 1,
        "actors": {"holder": (
            "hold-nbuf-distinct-read-only-buffers",
            "request-one-more-buffer",
        )},
        "oracle": "exact-no-buffer-panic",
        "evidence": {"B"},
        "isolated_panic_run": True,
    },
}

POINT = {
    "CACHE_LOOKUP": 1,
    "CACHE_GUARD": 2,
    "CACHE_HIT": 3,
    "CACHE_PUBLISH": 4,
    "CACHE_SLEEP": 5,
    "CACHE_RELEASE": 6,
    "CACHE_PIN": 7,
    "CACHE_UNPIN": 8,
    "LOG_WAIT_COMMIT": 20,
    "LOG_WAIT_SPACE": 21,
    "LOG_ADMIT": 22,
    "LOG_END": 23,
    "LOG_APPEND": 24,
    "LOG_ABSORB": 25,
    "LOG_PAYLOAD": 26,
    "LOG_HEADER": 27,
    "LOG_HOME": 28,
    "LOG_CLEAR": 29,
    "LOG_COMMIT_DONE": 30,
    "IO_SUBMIT": 40,
    "IO_COMPLETE": 41,
}
CACHE_POINTS = set(range(1, 9))
LOG_POINTS = set(range(20, 31))
IO_POINTS = {POINT["IO_SUBMIT"], POINT["IO_COMPLETE"]}
KNOWN_POINTS = CACHE_POINTS | LOG_POINTS | IO_POINTS

MARKER_SCHEMAS = {
    "META": {
        "case", "abi", "generation", "count", "error", "arrived",
        "gate", "holders",
    },
    "EVENT": {
        "generation", "seq", "point", "pid", "hart", "domain", "slot",
        "ref", "dev", "block", "buffer_generation", "log_n",
        "outstanding", "committing", "aux",
    },
    "BUFFER": {
        "generation", "slot", "domain", "named", "ref", "valid", "disk",
        "locked", "dev", "block", "buffer_generation",
    },
    "LEDGER": {
        "case", "generation", "refs", "locks", "disk", "log_n",
        "outstanding", "committing", "error",
    },
    "RESULT": {
        "case", "block1", "block2", "pid1", "pid2", "domain1", "domain2",
        "expected1", "expected2", "value1", "value2",
    },
    "FULL": {"generation", "holders", "next"},
    "PASS": {"case"},
}
STRING_FIELDS = {("META", "case"), ("LEDGER", "case"),
                 ("RESULT", "case"), ("PASS", "case")}


class LabError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise LabError(message)


def require_plain_int(value, message):
    require(type(value) is int, message)
    return value


def validate_scenarios(data):
    require(type(data) is dict, "scenario root must be an object")
    require(set(data) == {
        "schema_version", "event_schema_version", "defaults",
        "point_capabilities", "cases",
    }, "scenario root fields changed")
    require_plain_int(data["schema_version"], "scenario schema_version must be int")
    require_plain_int(data["event_schema_version"],
                      "event_schema_version must be int")
    require(data["schema_version"] == data["event_schema_version"] == 1,
            "scenario/event schema version changed")
    defaults = data["defaults"]
    require(type(defaults) is dict and set(defaults) == {
        "image", "event_budget", "watchdog_seconds", "cleanup",
    }, "scenario defaults fields changed")
    require(defaults["image"] == "private-copy" and
            defaults["cleanup"] == "required",
            "scenario isolation/cleanup default changed")
    require_plain_int(defaults["event_budget"], "event_budget must be int")
    require_plain_int(defaults["watchdog_seconds"],
                      "watchdog_seconds must be int")
    require(defaults["event_budget"] == EVENT_BUDGET and
            1 <= defaults["watchdog_seconds"] <= 300,
            "scenario budget/watchdog changed")
    require(data["point_capabilities"] == POINT_CAPABILITIES,
            "point capability table changed")
    cases = data["cases"]
    require(type(cases) is list and len(cases) == len(CASE_CONTRACTS),
            "scenario suite must contain exactly seven cases")
    require([item.get("id") for item in cases] == list(CASE_CONTRACTS),
            "scenario order or IDs changed")

    for case in cases:
        require(type(case) is dict, "scenario case must be an object")
        case_id = case["id"]
        expected = CASE_CONTRACTS[case_id]
        keys = {"id", "cpus", "actors", "gates", "oracle", "evidence"}
        if "discovery" in expected:
            keys.add("discovery")
        if "isolated_panic_run" in expected:
            keys.add("isolated_panic_run")
        require(set(case) == keys, f"{case_id} fields changed")
        require_plain_int(case["cpus"], f"{case_id} cpus must be int")
        require(case["cpus"] == expected["cpus"],
                f"{case_id} CPU contract changed")
        require(case["oracle"] == expected["oracle"],
                f"{case_id} oracle changed")
        require(type(case["evidence"]) is list and
                set(case["evidence"]) == expected["evidence"] and
                len(case["evidence"]) == len(expected["evidence"]),
                f"{case_id} evidence dimensions changed")
        if "discovery" in expected:
            require(case["discovery"] == expected["discovery"],
                    f"{case_id} discovery changed")
        if "isolated_panic_run" in expected:
            require(case["isolated_panic_run"] is True,
                    "cache-full must remain an isolated panic run")

        actors = case["actors"]
        require(type(actors) is list and len(actors) == len(expected["actors"]),
                f"{case_id} actors changed")
        actual_actors = {}
        for actor in actors:
            require(type(actor) is dict and set(actor) == {"id", "actions"},
                    f"{case_id} actor fields changed")
            require(type(actor["id"]) is str and actor["id"] not in actual_actors,
                    f"{case_id} actor ID is missing or duplicated")
            require(type(actor["actions"]) is list and
                    all(type(action) is str for action in actor["actions"]),
                    f"{case_id} actor actions changed")
            actual_actors[actor["id"]] = tuple(actor["actions"])
        require(actual_actors == expected["actors"],
                f"{case_id} actor/action contract changed")
        _validate_gates(case_id, case, set(actual_actors))
    return data


def _validate_gates(case_id, case, actor_ids):
    gates = case["gates"]
    require(type(gates) is list, f"{case_id} gates must be a list")
    gate_ids = set()
    for gate in gates:
        require(type(gate) is dict, f"{case_id} gate must be an object")
        mode = gate.get("mode")
        expected_keys = {"id", "mode", "point", "actors"}
        if mode == "hold-until-event":
            expected_keys.add("release_after")
        elif mode == "rendezvous-spin":
            expected_keys.add("iteration_budget")
        else:
            require(mode == "rendezvous-sleep",
                    f"{case_id} has unknown gate mode {mode!r}")
        require(set(gate) == expected_keys, f"{case_id} gate fields changed")
        gate_id = gate["id"]
        require(type(gate_id) is str and gate_id and gate_id not in gate_ids,
                f"{case_id} gate ID is missing or duplicated")
        gate_ids.add(gate_id)
        point = gate["point"]
        require(point in POINT_CAPABILITIES,
                f"{case_id} gate uses unknown point {point!r}")
        actors = gate["actors"]
        require(type(actors) is list and actors and len(actors) == len(set(actors)) and
                set(actors) <= actor_ids,
                f"{case_id} gate actors changed")
        capability = POINT_CAPABILITIES[point]
        require(capability != "observe-only",
                f"{case_id} attempts to block at observe-only point {point}")
        if mode == "rendezvous-spin":
            require(capability == "spin-2cpu-ok" and case["cpus"] == 2 and
                    len(actors) == 2,
                    f"{case_id} spin gate capability/CPU/actor contract changed")
            budget = require_plain_int(
                gate["iteration_budget"], f"{case_id} spin budget must be int")
            require(1 <= budget <= 100_000_000,
                    f"{case_id} spin budget is not finite")
        else:
            require(capability in {"sleep-ok", "sleep-ok-with-buffer-sleeplock"},
                    f"{case_id} sleep gate capability changed")
        if mode == "hold-until-event":
            release = gate["release_after"]
            require(type(release) is dict and set(release) == {"point", "actor"} and
                    release["point"] in POINT_CAPABILITIES and
                    release["actor"] in actor_ids,
                    f"{case_id} gate release target changed")
            if capability == "sleep-ok-with-buffer-sleeplock":
                require(not (release["point"] == point and
                             release["actor"] in actors),
                        f"{case_id} buffer-lock gate has a self-dependent release")

    expected_counts = {
        "transaction-flow": 0,
        "log-capacity-empty": 1,
        "log-capacity-used": 2,
        "cache-same-block": 1,
        "cache-collision": 1,
        "cache-parallel": 1,
        "cache-full": 0,
    }
    require(len(gates) == expected_counts[case_id],
            f"{case_id} gate count changed")


@dataclass(frozen=True)
class Marker:
    kind: str
    fields: dict
    line: str


@dataclass
class EvidenceBundle:
    case: str
    generation: int
    cpus: int
    raw: str
    markers: list
    meta: dict
    events: list
    buffers: list
    ledger: dict
    result: dict | None
    full: dict | None
    summary: dict


class ReplaySource:
    """A captured trace source used by the same parser/reducer as QEMU."""

    def __init__(self, transcript):
        self.transcript = transcript

    def run(self, case, scenarios, *, panic=False):
        return validate_case_trace(self.transcript, case, scenarios, panic=panic)


def parse_marker_fields(kind, tokens):
    values = {}
    for token in tokens:
        require(token.count("=") == 1, f"malformed {kind} field: {token!r}")
        key, raw = token.split("=", 1)
        require(key and key not in values, f"duplicate {kind} field: {key!r}")
        require(not any(character.isspace() for character in raw),
                f"whitespace in {kind} field: {key}")
        if (kind, key) in STRING_FIELDS:
            require(re.fullmatch(r"[a-z][a-z0-9-]*", raw) is not None,
                    f"invalid string {kind} field: {key}={raw!r}")
            values[key] = raw
        else:
            require(re.fullmatch(r"-?[0-9]+", raw) is not None,
                    f"non-integer {kind} field: {key}={raw!r}")
            values[key] = int(raw)
    require(set(values) == MARKER_SCHEMAS[kind],
            f"{kind} fields changed: {sorted(set(values) ^ MARKER_SCHEMAS[kind])}")
    return values


def parse_markers(text):
    markers = []
    for raw_line in text.replace("\r", "").splitlines():
        if not raw_line.startswith("PERSIST"):
            continue
        require(raw_line.startswith("PERSIST "),
                f"malformed PERSIST marker prefix: {raw_line!r}")
        parts = raw_line.split()
        require(len(parts) >= 3, f"malformed PERSIST marker: {raw_line!r}")
        kind = parts[1]
        require(kind != "FAIL", f"guest reported failure: {raw_line}")
        require(kind in MARKER_SCHEMAS, f"unknown PERSIST marker: {kind!r}")
        markers.append(Marker(kind, parse_marker_fields(kind, parts[2:]),
                              raw_line))
    require(markers, "no PERSIST evidence markers found")
    return markers


def _validate_marker_stream(markers, case, panic):
    result_expected = case in RESULT_CASES
    index = 0
    result = None
    if result_expected:
        require(markers[index].kind == "RESULT",
                f"{case} must start with one RESULT marker")
        result = markers[index].fields
        index += 1
    require(index < len(markers) and markers[index].kind == "META",
            f"{case} is missing its META marker")
    meta = markers[index].fields
    index += 1
    events = []
    while index < len(markers) and markers[index].kind == "EVENT":
        events.append(markers[index].fields)
        index += 1
    buffers = []
    while index < len(markers) and markers[index].kind == "BUFFER":
        buffers.append(markers[index].fields)
        index += 1
    require(index < len(markers) and markers[index].kind == "LEDGER",
            f"{case} is missing its LEDGER marker")
    ledger = markers[index].fields
    index += 1
    full = None
    if panic:
        require(index < len(markers) and markers[index].kind == "FULL",
                "cache-full is missing its panic precondition marker")
        full = markers[index].fields
        index += 1
    else:
        require(index < len(markers) and markers[index].kind == "PASS",
                f"{case} is missing its PASS marker")
        index += 1
    require(index == len(markers),
            f"{case} marker stream has extra, repeated, or misordered markers")
    require((result is not None) == result_expected,
            f"{case} RESULT cardinality changed")
    require(len(events) == meta["count"],
            f"{case} META event count differs from raw EVENT count")
    require(len(buffers) == NBUF,
            f"{case} expected {NBUF} BUFFER markers, got {len(buffers)}")
    return meta, events, buffers, ledger, result, full


def _scenario_for_runtime(scenarios, case):
    scenario_id, generation, cpus = CASE_RUNTIME[case]
    by_id = {item["id"]: item for item in scenarios["cases"]}
    require(scenario_id in by_id, f"scenario missing for runtime case {case}")
    require(by_id[scenario_id]["cpus"] == cpus,
            f"runtime/scenario CPU mismatch for {case}")
    return by_id[scenario_id], generation, cpus


def _validate_common(case, cpus, generation, meta, events, buffers, ledger,
                     result, full, panic, scenarios):
    require(meta["case"] == case and ledger["case"] == case,
            f"{case} marker case labels changed")
    require(meta["abi"] == scenarios["event_schema_version"] == PA_ABI,
            f"{case} ABI/schema mismatch")
    require(meta["generation"] == ledger["generation"] == generation,
            f"{case} generation changed")
    require(meta["error"] == ledger["error"] == 0,
            f"{case} guest audit reported an error")
    require(0 <= meta["count"] <= scenarios["defaults"]["event_budget"],
            f"{case} event ring overflow or invalid count")
    if case in ("collision", "parallel"):
        require(meta["arrived"] == 3 and meta["gate"] == 1,
                f"{case} rendezvous did not observe and release both actors")
    else:
        require(meta["arrived"] == meta["gate"] == 0,
                f"{case} reported an unexpected gate state")
    require(meta["holders"] == (NBUF if panic else 0),
            f"{case} holder ledger changed")

    require([event["seq"] for event in events] == list(range(1, len(events) + 1)),
            f"{case} EVENT seq is not unique and contiguous")
    for event in events:
        require(event["generation"] == generation,
                f"{case} contains an event from another run")
        require(event["point"] in KNOWN_POINTS,
                f"{case} contains unknown point {event['point']}")
        require((event["pid"] >= 0 if event["point"] in IO_POINTS else
                 event["pid"] > 0) and 0 <= event["hart"] < cpus,
                f"{case} event actor/hart is outside the run")
        require(0 <= event["log_n"] <= LOGBLOCKS and
                0 <= event["outstanding"] <= 3 and
                event["committing"] in (0, 1),
                f"{case} event log state is outside its bounds")
        if event["point"] in CACHE_POINTS | IO_POINTS:
            require(event["domain"] >= 0 and event["dev"] == ROOTDEV and
                    0 <= event["block"] < FSSIZE and
                    (event["aux"] in (0, 1) if event["point"] in IO_POINTS
                     else event["aux"] == 0),
                    f"{case} cache subject changed at seq {event['seq']}")
            if event["point"] in (POINT["CACHE_LOOKUP"], POINT["CACHE_GUARD"]):
                require(event["slot"] == -1 and event["ref"] == 0 and
                        event["buffer_generation"] == 0,
                        f"{case} unselected cache point exposes a ready slot")
            else:
                require(0 <= event["slot"] < NBUF and event["ref"] >= 0 and
                        event["buffer_generation"] > 0,
                        f"{case} cache event references an unready slot")
        else:
            require(event["domain"] == event["slot"] == event["ref"] == -1 and
                    event["dev"] == ROOTDEV and
                    event["buffer_generation"] == 0 and
                    0 <= event["block"] < FSSIZE,
                    f"{case} log subject schema changed at seq {event['seq']}")

    slots = {}
    named_keys = {}
    for view in buffers:
        require(view["generation"] == generation and
                0 <= view["slot"] < NBUF and view["slot"] not in slots,
                f"{case} BUFFER slot/generation changed")
        require(view["domain"] >= 0 and view["named"] in (0, 1) and
                view["ref"] >= 0 and view["valid"] in (0, 1) and
                view["disk"] in (0, 1) and view["locked"] in (0, 1) and
                view["buffer_generation"] >= 0,
                f"{case} BUFFER state is outside its closed domain")
        if view["named"]:
            require(view["dev"] == ROOTDEV and 0 <= view["block"] < FSSIZE and
                    view["buffer_generation"] > 0,
                    f"{case} named BUFFER identity is not ready")
            key = (view["dev"], view["block"])
            require(key not in named_keys,
                    f"{case} has duplicate live buffer identity {key}")
            named_keys[key] = (view["slot"], view["buffer_generation"])
        slots[view["slot"]] = view
    require(set(slots) == set(range(NBUF)),
            f"{case} BUFFER snapshot does not cover all slots")
    require(ledger["refs"] == sum(view["ref"] for view in buffers) and
            ledger["locks"] == sum(view["locked"] for view in buffers) and
            ledger["disk"] == sum(view["disk"] for view in buffers),
            f"{case} LEDGER does not match BUFFER snapshot")
    require((ledger["log_n"], ledger["outstanding"], ledger["committing"]) ==
            (0, 0, 0), f"{case} final transaction ledger is not quiescent")
    if panic:
        require((ledger["refs"], ledger["locks"], ledger["disk"]) ==
                (NBUF, NBUF, 0), "cache-full pre-panic resource ledger changed")
        require(full == {"generation": generation, "holders": NBUF,
                         "next": 1000 + NBUF},
                "cache-full panic precondition marker changed")
    else:
        require((ledger["refs"], ledger["locks"], ledger["disk"]) == (0, 0, 0),
                f"{case} resources did not return to zero")
    if result is not None:
        require(result["case"] == case, f"{case} RESULT label changed")
    return slots, named_keys


def _event_identity(event):
    return (event["slot"], event["buffer_generation"])


def _event_key(event):
    return (event["dev"], event["block"])


def reduce_cache(case, events, buffers, panic):
    states = {}
    current_slot = {}
    current_key = {}
    peak_total = 0
    peak_by_key = {}
    lock_acquire = {}
    lock_release = {}
    guard_events = []

    def total_ordinary():
        return sum(state["ordinary"] for state in states.values())

    def remember_peak(state):
        nonlocal peak_total
        peak_total = max(peak_total, total_ordinary())
        peak_by_key[state["key"]] = max(
            peak_by_key.get(state["key"], 0), state["ordinary"])

    for event in events:
        point = event["point"]
        if point not in CACHE_POINTS:
            continue
        if point == POINT["CACHE_GUARD"]:
            guard_events.append(event)
        if point in (POINT["CACHE_LOOKUP"], POINT["CACHE_GUARD"]):
            continue
        identity = _event_identity(event)
        key = _event_key(event)
        state = states.get(identity)

        if point == POINT["CACHE_PUBLISH"]:
            require(event["ref"] == 1,
                    f"{case} published buffer without exactly one reference")
            old_identity = current_slot.get(event["slot"])
            if old_identity is not None and old_identity != identity:
                old = states.get(old_identity)
                require(old is None or (old["raw"] == 0 and old["pins"] == 0 and
                                        old["owner"] is None and not old["waiters"]),
                        f"{case} retagged a referenced/pinned buffer")
                if old is not None:
                    current_key.pop(old["key"], None)
                require(identity[1] > old_identity[1],
                        f"{case} retag did not advance generation")
            duplicate = current_key.get(key)
            require(duplicate is None or duplicate == identity,
                    f"{case} published duplicate live identity for {key}")
            require(state is None, f"{case} republished the same generation")
            state = {
                "key": key, "domain": event["domain"], "raw": 1,
                "ordinary": 1, "pins": 0, "owner": None,
                "waiters": {event["pid"]}, "first": event["seq"],
            }
            states[identity] = state
            current_slot[event["slot"]] = identity
            current_key[key] = identity
            remember_peak(state)
            continue

        if point == POINT["CACHE_HIT"]:
            duplicate = current_key.get(key)
            require(duplicate is None or duplicate == identity,
                    f"{case} hit a second live identity for {key}")
            if state is None:
                require(event["ref"] >= 1,
                        f"{case} first observed hit has no reference")
                old_identity = current_slot.get(event["slot"])
                require(old_identity is None or old_identity == identity,
                        f"{case} hit a slot with a conflicting generation")
                state = {
                    "key": key, "domain": event["domain"],
                    "raw": event["ref"], "ordinary": event["ref"],
                    "pins": 0, "owner": None, "waiters": {event["pid"]},
                    "first": event["seq"],
                }
                states[identity] = state
                current_slot[event["slot"]] = identity
                current_key[key] = identity
            else:
                require(state["key"] == key and state["domain"] == event["domain"],
                        f"{case} changed key/domain inside one generation")
                require(event["ref"] == state["raw"] + 1,
                        f"{case} cache hit ref transition changed")
                require(event["pid"] not in state["waiters"] and
                        event["pid"] != state["owner"],
                        f"{case} duplicated an ordinary reference")
                state["raw"] += 1
                state["ordinary"] += 1
                state["waiters"].add(event["pid"])
            remember_peak(state)
            continue

        require(state is not None and state["key"] == key and
                state["domain"] == event["domain"] and
                current_slot.get(event["slot"]) == identity,
                f"{case} event references an unready buffer identity")
        if point == POINT["CACHE_SLEEP"]:
            require(event["ref"] == state["raw"] and
                    event["pid"] in state["waiters"] and
                    state["owner"] is None,
                    f"{case} buffer sleeplock acquisition changed")
            state["waiters"].remove(event["pid"])
            state["owner"] = event["pid"]
            lock_acquire[(event["pid"], key)] = event["seq"]
        elif point == POINT["CACHE_RELEASE"]:
            require(state["owner"] == event["pid"] and state["ordinary"] > 0 and
                    event["ref"] == state["raw"] - 1,
                    f"{case} buffer release/ref transition changed")
            state["owner"] = None
            state["raw"] -= 1
            state["ordinary"] -= 1
            lock_release[(event["pid"], key)] = event["seq"]
        elif point == POINT["CACHE_PIN"]:
            require(state["owner"] == event["pid"] and state["pins"] == 0 and
                    event["ref"] == state["raw"] + 1,
                    f"{case} duplicate or ownerless log pin")
            state["raw"] += 1
            state["pins"] = 1
        elif point == POINT["CACHE_UNPIN"]:
            require(state["owner"] == event["pid"] and state["pins"] == 1 and
                    event["ref"] == state["raw"] - 1,
                    f"{case} unmatched log unpin")
            state["raw"] -= 1
            state["pins"] = 0

    final_views = {view["slot"]: view for view in buffers}
    for slot, identity in current_slot.items():
        state = states[identity]
        view = final_views[slot]
        require(view["named"] == 1 and
                (view["dev"], view["block"]) == state["key"] and
                view["domain"] == state["domain"] and
                view["buffer_generation"] == identity[1],
                f"{case} final BUFFER snapshot contradicts event identity")
    if panic:
        require(all(state["ordinary"] == 1 and state["pins"] == 0 and
                    state["owner"] is not None and not state["waiters"]
                    for state in states.values()),
                "cache-full event reducer did not retain 30 locked references")
    else:
        require(all(state["ordinary"] == state["pins"] == 0 and
                    state["owner"] is None and not state["waiters"]
                    for state in states.values()),
                f"{case} event-derived refs/pins/owners/waiters did not drain")
    return {
        "states": states,
        "current_key": current_key,
        "peak_ordinary": peak_total,
        "peak_by_key": peak_by_key,
        "lock_acquire": lock_acquire,
        "lock_release": lock_release,
        "guard_events": guard_events,
    }


def reduce_log(case, events):
    state = {"n": 0, "outstanding": 0, "committing": 0}
    entries = set()
    completion = []
    write_completions = []
    log_events = []
    io_inflight = {}
    io_submitted = {0: 0, 1: 0}
    io_completed = {0: 0, 1: 0}
    for event in events:
        point = event["point"]
        if point in CACHE_POINTS:
            require((event["log_n"], event["outstanding"],
                     event["committing"]) ==
                    (state["n"], state["outstanding"], state["committing"]),
                    f"{case} cache event carries stale log state")
            continue
        if point in IO_POINTS:
            require((event["log_n"], event["outstanding"],
                     event["committing"]) ==
                    (state["n"], state["outstanding"], state["committing"]),
                    f"{case} IO event carries stale log state")
            identity = (event["slot"], event["buffer_generation"],
                        event["dev"], event["block"], event["aux"])
            if point == POINT["IO_SUBMIT"]:
                io_inflight[identity] = io_inflight.get(identity, 0) + 1
                io_submitted[event["aux"]] += 1
            else:
                require(io_inflight.get(identity, 0) > 0,
                        f"{case} IO completion lacks a matching submit")
                io_inflight[identity] -= 1
                io_completed[event["aux"]] += 1
                if event["aux"] == 1:
                    write_completions.append(event)
            continue
        log_events.append(event)
        n = event["log_n"]
        outstanding = event["outstanding"]
        committing = event["committing"]
        if point == POINT["LOG_WAIT_COMMIT"]:
            require((n, outstanding, committing) ==
                    (state["n"], state["outstanding"], 1),
                    f"{case} invalid commit wait/recheck state")
        elif point == POINT["LOG_WAIT_SPACE"]:
            require((n, outstanding, committing) ==
                    (state["n"], state["outstanding"], 0) and
                    n + (outstanding + 1) * MAXOPBLOCKS > LOGBLOCKS,
                    f"{case} space wait did not cross the admission bound")
        elif point == POINT["LOG_ADMIT"]:
            require(n == state["n"] and committing == 0 and
                    outstanding == state["outstanding"] + 1 and
                    n + outstanding * MAXOPBLOCKS <= LOGBLOCKS,
                    f"{case} successful admission violated capacity")
            state["outstanding"] = outstanding
        elif point == POINT["LOG_END"]:
            require(n == state["n"] and
                    outstanding == state["outstanding"] - 1 and
                    event["aux"] in (0, 1),
                    f"{case} end_op outstanding transition changed")
            if event["aux"]:
                require(outstanding == 0 and committing == 1,
                        f"{case} group commit did not start at last end_op")
            else:
                require(committing == 0,
                        f"{case} non-last end_op started a commit")
            state["outstanding"] = outstanding
            state["committing"] = committing
        elif point == POINT["LOG_APPEND"]:
            require(event["block"] not in entries and n == state["n"] + 1 and
                    outstanding == state["outstanding"] and
                    committing == state["committing"] == 0,
                    f"{case} log append/absorption state changed")
            entries.add(event["block"])
            state["n"] = n
        elif point == POINT["LOG_ABSORB"]:
            require(event["block"] in entries and
                    (n, outstanding, committing) ==
                    (state["n"], state["outstanding"], state["committing"]),
                    f"{case} absorption added or changed a log entry")
        elif point in (POINT["LOG_PAYLOAD"], POINT["LOG_HEADER"],
                       POINT["LOG_HOME"]):
            require((n, outstanding, committing) ==
                    (state["n"], state["outstanding"], state["committing"]) and
                    committing == 1 and n > 0,
                    f"{case} completion occurred outside group commit")
            completion.append(event)
            if point == POINT["LOG_HEADER"]:
                require(event["aux"] == n,
                        f"{case} nonzero header completion changed")
        elif point == POINT["LOG_CLEAR"]:
            require(state["n"] > 0 and n == 0 and outstanding == 0 and
                    committing == state["committing"] == 1 and event["aux"] == 0,
                    f"{case} zero-header clear transition changed")
            state["n"] = 0
            entries.clear()
            completion.append(event)
        elif point == POINT["LOG_COMMIT_DONE"]:
            require((state["n"], state["outstanding"], state["committing"]) ==
                    (0, 0, 1) and (n, outstanding, committing) == (0, 0, 0),
                    f"{case} committing cleared before zero header")
            state["committing"] = 0
        else:
            raise LabError(f"{case} unknown log point {point}")
    require(state == {"n": 0, "outstanding": 0, "committing": 0},
            f"{case} event-derived log ledger did not drain")
    require(not any(io_inflight.values()) and io_submitted == io_completed,
            f"{case} IO submit/completion ledger did not drain")
    return {
        "events": log_events,
        "completion": completion,
        "logical_io_submitted": io_submitted[1],
        "logical_io_completed": io_completed[1],
        "read_io_submitted": io_submitted[0],
        "read_io_completed": io_completed[0],
        "write_completions": write_completions,
    }


def _events_at(events, point):
    return [event for event in events if event["point"] == point]


def _only(events, point, case):
    matches = _events_at(events, point)
    require(len(matches) == 1,
            f"{case} expected one point {point}, got {len(matches)}")
    return matches[0]


def validate_transaction(events, cache, log):
    case = "tx"
    admit = _only(events, POINT["LOG_ADMIT"], case)
    end = _only(events, POINT["LOG_END"], case)
    pin = _only(events, POINT["CACHE_PIN"], case)
    append = _only(events, POINT["LOG_APPEND"], case)
    absorb = _only(events, POINT["LOG_ABSORB"], case)
    payload = _only(events, POINT["LOG_PAYLOAD"], case)
    header = _only(events, POINT["LOG_HEADER"], case)
    home = _only(events, POINT["LOG_HOME"], case)
    unpin = _only(events, POINT["CACHE_UNPIN"], case)
    clear = _only(events, POINT["LOG_CLEAR"], case)
    done = _only(events, POINT["LOG_COMMIT_DONE"], case)
    require(len(_events_at(events, POINT["LOG_WAIT_SPACE"])) == 0 and
            len(_events_at(events, POINT["LOG_WAIT_COMMIT"])) == 0,
            "tx unexpectedly waited for log admission")
    require(append["block"] == absorb["block"] == payload["block"] ==
            home["block"] == 1990,
            "tx home block or absorption identity changed")
    require(_event_key(pin) == _event_key(unpin) == (ROOTDEV, 1990) and
            _event_identity(pin) == _event_identity(unpin),
            "tx pin/unpin identity changed")
    require(header["block"] == clear["block"] > 0 and
            header["log_n"] == header["aux"] == 1 and
            clear["log_n"] == clear["aux"] == 0,
            "tx nonzero/zero header contract changed")
    require(admit["seq"] < pin["seq"] < append["seq"] < absorb["seq"] <
            end["seq"] < payload["seq"] < header["seq"] < home["seq"] <
            unpin["seq"] < clear["seq"] < done["seq"],
            "tx cached/logged/committed/installed/cleared order changed")
    require((end["outstanding"], end["committing"], end["aux"]) == (0, 1, 1) and
            (done["log_n"], done["outstanding"], done["committing"]) ==
            (0, 0, 0), "tx group commit boundary changed")
    identity = _event_identity(pin)
    require(identity in cache["states"] and
            cache["states"][identity]["pins"] == 0,
            "tx pin ledger was not independently balanced")
    require(log["logical_io_submitted"] == log["logical_io_completed"] == 4,
            "tx expected four synchronous write completions")
    writes = log["write_completions"]
    require(len(writes) == 4 and
            [event["block"] for event in writes] ==
            [header["block"] + 1, header["block"], home["block"], clear["block"]] and
            [event["seq"] + 1 for event in writes] ==
            [payload["seq"], header["seq"], home["seq"], clear["seq"]],
            "tx write completions are not bound to payload/header/home/clear")
    require(_event_identity(writes[1]) == _event_identity(writes[3]) and
            _event_identity(writes[2]) == identity,
            "tx header/home write completion identity changed")
    return {
        "home_block": 1990,
        "append_seq": append["seq"],
        "absorb_seq": absorb["seq"],
        "payload_complete_seq": payload["seq"],
        "header_commit_complete_seq": header["seq"],
        "home_install_complete_seq": home["seq"],
        "header_clear_complete_seq": clear["seq"],
        "pin_identity": identity,
        "logical_io_submitted": 4,
        "logical_io_completed": 4,
    }


def validate_admission(case, events):
    admits = _events_at(events, POINT["LOG_ADMIT"])
    waits = _events_at(events, POINT["LOG_WAIT_SPACE"])
    ends = _events_at(events, POINT["LOG_END"])
    require(len(waits) == 1, f"{case} expected one real capacity wait")
    wait = waits[0]
    first_admits = {}
    for event in admits:
        first_admits.setdefault(event["pid"], event)
    primary = sorted(first_admits.values(), key=lambda event: event["seq"])
    primary_ends = []
    for admit in primary:
        matches = [event for event in ends
                   if event["pid"] == admit["pid"] and
                   event["seq"] > admit["seq"]]
        require(matches, f"{case} actor did not close its admitted operation")
        primary_ends.append(matches[0])
    if case == "admission-empty":
        require(len(primary) == 4,
                "admission-empty actor ledger changed")
        before = [event for event in primary if event["seq"] < wait["seq"]]
        after = [event for event in primary if event["seq"] > wait["seq"]]
        require(len(before) == 3 and len(after) == 1 and
                len({event["pid"] for event in before}) == 3 and
                wait["pid"] not in {event["pid"] for event in before} and
                after[0]["pid"] == wait["pid"],
                "admission-empty did not admit three, wait fourth, then re-admit")
        require((wait["log_n"], wait["outstanding"]) == (0, 3) and
                any(wait["seq"] < end["seq"] < after[0]["seq"]
                    for end in primary_ends),
                "admission-empty wait/release order changed")
        relation = "0 + (3 + 1) * 10 > 30"
    else:
        require(case == "admission-used" and len(primary) == 3,
                "admission-used actor ledger changed")
        appends = _events_at(events, POINT["LOG_APPEND"])
        require(len(appends) == 1 and appends[0]["block"] == 1988 and
                appends[0]["log_n"] == 1,
                "admission-used did not hold one existing log entry")
        before = [event for event in primary if event["seq"] < wait["seq"]]
        after = [event for event in primary if event["seq"] > wait["seq"]]
        require(len(before) == 2 and len(after) == 1 and
                wait["pid"] not in {event["pid"] for event in before} and
                after[0]["pid"] == wait["pid"] and
                appends[0]["seq"] < before[1]["seq"] < wait["seq"],
                "admission-used wait actor/order changed")
        require((wait["log_n"], wait["outstanding"]) == (1, 2) and
                any(wait["seq"] < end["seq"] < after[0]["seq"]
                    for end in primary_ends),
                "admission-used wait/release state changed")
        relation = "1 + (2 + 1) * 10 > 30"
    require(_events_at(events, POINT["LOG_COMMIT_DONE"]),
            f"{case} did not close its group transaction")
    return {
        "admitted": len(primary),
        "cleanup_admissions": len(admits) - len(primary),
        "wait_pid": wait["pid"],
        "wait_seq": wait["seq"],
        "readmitted_seq": after[0]["seq"],
        "capacity_relation": relation,
    }


def _validate_result_values(case, result):
    require(result["pid1"] > 0 and result["pid2"] > 0 and
            result["pid1"] != result["pid2"],
            f"{case} actor identities changed")
    require(all(0 <= result[key] <= 255 for key in
                ("expected1", "expected2", "value1", "value2")) and
            result["value1"] == result["expected1"] and
            result["value2"] == result["expected2"],
            f"{case} block contents mixed or changed")


def _identity_for_key(cache, key):
    identities = {identity for identity, state in cache["states"].items()
                  if state["key"] == key}
    require(len(identities) == 1,
            f"key {key} did not retain exactly one generation in the case")
    return next(iter(identities))


def validate_same(result, events, cache):
    case = "same"
    _validate_result_values(case, result)
    require(result["block1"] == result["block2"] == 1989 and
            result["domain1"] == result["domain2"] and
            result["expected1"] == result["expected2"],
            "same-block discovery/result changed")
    key = (ROOTDEV, 1989)
    identity = _identity_for_key(cache, key)
    p1, p2 = result["pid1"], result["pid2"]
    acquire1 = cache["lock_acquire"].get((p1, key))
    acquire2 = cache["lock_acquire"].get((p2, key))
    release1 = cache["lock_release"].get((p1, key))
    release2 = cache["lock_release"].get((p2, key))
    hits2 = [event for event in _events_at(events, POINT["CACHE_HIT"])
             if event["pid"] == p2 and _event_key(event) == key]
    require(None not in (acquire1, acquire2, release1, release2) and
            len(hits2) == 1 and hits2[0]["ref"] == 2,
            "same-block lock/ref evidence is incomplete")
    require(acquire1 < hits2[0]["seq"] < release1 < acquire2 < release2,
            "same-block waiter acquired before the first owner released")
    require(cache["peak_by_key"].get(key) == 2,
            "same-block peak ordinary references changed")
    return {
        "block": 1989,
        "identity": identity,
        "live_identity_count": 1,
        "peak_ordinary_refs": 2,
        "first_release_seq": release1,
        "second_acquire_seq": acquire2,
    }


def validate_collision(result, cache, meta):
    case = "collision"
    _validate_result_values(case, result)
    require(result["block1"] != result["block2"] and
            result["domain1"] == result["domain2"] and
            result["expected1"] != result["expected2"],
            "collision discovery no longer selects distinct same-partition data")
    key1 = (ROOTDEV, result["block1"])
    key2 = (ROOTDEV, result["block2"])
    identity1 = _identity_for_key(cache, key1)
    identity2 = _identity_for_key(cache, key2)
    require(identity1 != identity2,
            "collision mapped two blocks to one live identity")
    acquisitions = []
    releases = []
    for pid, key in ((result["pid1"], key1), (result["pid2"], key2)):
        require((pid, key) in cache["lock_acquire"] and
                (pid, key) in cache["lock_release"],
                "collision actor did not acquire and release its buffer")
        acquisitions.append(cache["lock_acquire"][(pid, key)])
        releases.append(cache["lock_release"][(pid, key)])
    require(meta["arrived"] == 3 and meta["gate"] == 1 and
            max(acquisitions) < min(releases),
            "collision did not hold both distinct identities concurrently")
    return {
        "blocks": (result["block1"], result["block2"]),
        "partition": result["domain1"],
        "identities": (identity1, identity2),
        "values": (result["value1"], result["value2"]),
        "lock_acquire_seq": tuple(acquisitions),
        "first_release_seq": min(releases),
    }


def validate_parallel(result, events, cache, meta):
    case = "parallel"
    _validate_result_values(case, result)
    require(result["block1"] != result["block2"] and
            result["domain1"] != result["domain2"] and
            result["expected1"] != result["expected2"],
            "parallel discovery no longer selects distinct partitions/data")
    relevant = {}
    for event in cache["guard_events"]:
        if event["pid"] in (result["pid1"], result["pid2"]):
            relevant[event["pid"]] = event
    require(set(relevant) == {result["pid1"], result["pid2"]},
            "parallel gate lacks one actor arrival")
    left = relevant[result["pid1"]]
    right = relevant[result["pid2"]]
    require(left["domain"] == result["domain1"] and
            right["domain"] == result["domain2"] and
            left["hart"] != right["hart"] and {left["hart"], right["hart"]} ==
            {0, 1} and meta["arrived"] == 3 and meta["gate"] == 1,
            "parallel bounded two-hart gate did not prove lock-held overlap")
    post_guard = [event["seq"] for event in events
                  if event["pid"] in relevant and
                  event["point"] in (POINT["CACHE_HIT"],
                                     POINT["CACHE_PUBLISH"])]
    require(len(post_guard) >= 2 and min(post_guard) > max(left["seq"], right["seq"]),
            "parallel actors passed selection before both gate arrivals")
    key1 = (ROOTDEV, result["block1"])
    key2 = (ROOTDEV, result["block2"])
    return {
        "blocks": (result["block1"], result["block2"]),
        "partitions": (result["domain1"], result["domain2"]),
        "harts": (left["hart"], right["hart"]),
        "guard_seq": (left["seq"], right["seq"]),
        "identities": (_identity_for_key(cache, key1),
                       _identity_for_key(cache, key2)),
    }


def validate_full(text, events, buffers, cache, meta, ledger, full):
    require(text.count(PANIC_TEXT) == 1,
            "cache-full did not produce exactly one expected panic")
    require("PERSIST PASS" not in text and "PERSIST FAIL" not in text,
            "cache-full emitted a terminal guest result after/before panic")
    held = {(view["dev"], view["block"]): view for view in buffers
            if view["ref"] or view["locked"]}
    expected = {(ROOTDEV, block) for block in range(1000, 1000 + NBUF)}
    require(set(held) == expected and
            all(view["ref"] == view["locked"] == 1 and view["disk"] == 0
                for view in held.values()),
            "cache-full snapshot is not exactly 30 held read-only buffers")
    sleeps = _events_at(events, POINT["CACHE_SLEEP"])
    require(len(sleeps) == NBUF and len({event["pid"] for event in sleeps}) == NBUF and
            {_event_key(event) for event in sleeps} == expected,
            "cache-full did not acquire 30 distinct holder buffers")
    require(len(cache["states"]) == NBUF and meta["holders"] == NBUF and
            (ledger["refs"], ledger["locks"], ledger["disk"]) ==
            (NBUF, NBUF, 0), "cache-full reducer precondition changed")
    return {
        "held": NBUF,
        "next_block": full["next"],
        "panic": PANIC_TEXT,
        "refs": ledger["refs"],
        "locks": ledger["locks"],
        "disk_owned": ledger["disk"],
    }


def validate_case_trace(text, case, scenarios, *, panic=False):
    require(case in CASE_RUNTIME, f"unknown runtime case: {case}")
    require(panic == (case == "cache-full"),
            "panic parsing is reserved for cache-full")
    scenario, generation, cpus = _scenario_for_runtime(scenarios, case)
    del scenario
    markers = parse_markers(text)
    meta, events, buffers, ledger, result, full = _validate_marker_stream(
        markers, case, panic)
    require((markers[-1].kind == "FULL" if panic else
             markers[-1].fields == {"case": case}),
            f"{case} terminal marker changed")
    _validate_common(case, cpus, generation, meta, events, buffers, ledger,
                     result, full, panic, scenarios)
    cache = reduce_cache(case, events, buffers, panic)
    log = reduce_log(case, events)
    if case == "tx":
        summary = validate_transaction(events, cache, log)
    elif case in ("admission-empty", "admission-used"):
        summary = validate_admission(case, events)
    elif case == "same":
        summary = validate_same(result, events, cache)
    elif case == "collision":
        summary = validate_collision(result, cache, meta)
    elif case == "parallel":
        summary = validate_parallel(result, events, cache, meta)
    else:
        summary = validate_full(text, events, buffers, cache, meta, ledger, full)
    summary["ledger"] = {
        "ordinary_refs_plus_pins": ledger["refs"],
        "buffer_locks": ledger["locks"],
        "disk_owned": ledger["disk"],
        "log_n": ledger["log_n"],
        "outstanding": ledger["outstanding"],
        "committing": ledger["committing"],
        "gate_waiters": 0 if meta["gate"] in (0, 1) else meta["gate"],
        "event_overflow": meta["error"],
        "logical_io_submitted": log["logical_io_submitted"],
        "logical_io_completed": log["logical_io_completed"],
    }
    return EvidenceBundle(
        case=case, generation=generation, cpus=cpus, raw=text,
        markers=markers, meta=meta, events=events, buffers=buffers,
        ledger=ledger, result=result, full=full, summary=summary)


class SyntheticEvents:
    def __init__(self, generation):
        self.generation = generation
        self.events = []

    def cache(self, point, pid, hart, domain, block, *, slot=-1,
              ref=0, buffer_generation=0, log_n=0, outstanding=0,
              committing=0):
        self.events.append({
            "generation": self.generation,
            "seq": len(self.events) + 1,
            "point": point,
            "pid": pid,
            "hart": hart,
            "domain": domain,
            "slot": slot,
            "ref": ref,
            "dev": ROOTDEV,
            "block": block,
            "buffer_generation": buffer_generation,
            "log_n": log_n,
            "outstanding": outstanding,
            "committing": committing,
            "aux": 0,
        })

    def log(self, point, pid, *, block=0, log_n=0, outstanding=0,
            committing=0, aux=0, hart=0):
        self.events.append({
            "generation": self.generation,
            "seq": len(self.events) + 1,
            "point": point,
            "pid": pid,
            "hart": hart,
            "domain": -1,
            "slot": -1,
            "ref": -1,
            "dev": ROOTDEV,
            "block": block,
            "buffer_generation": 0,
            "log_n": log_n,
            "outstanding": outstanding,
            "committing": committing,
            "aux": aux,
        })

    def io(self, point, pid, *, block, slot, buffer_generation,
           write, log_n, committing, domain=0, ref=1, hart=0):
        self.events.append({
            "generation": self.generation,
            "seq": len(self.events) + 1,
            "point": point,
            "pid": pid,
            "hart": hart,
            "domain": domain,
            "slot": slot,
            "ref": ref,
            "dev": ROOTDEV,
            "block": block,
            "buffer_generation": buffer_generation,
            "log_n": log_n,
            "outstanding": 0,
            "committing": committing,
            "aux": write,
        })


def _synthetic_buffers(generation, named, *, held=False):
    buffers = []
    for slot in range(NBUF):
        identity = named.get(slot)
        if identity is None:
            buffers.append({
                "generation": generation, "slot": slot, "domain": 0,
                "named": 0, "ref": 0, "valid": 0, "disk": 0,
                "locked": 0, "dev": 0, "block": 0,
                "buffer_generation": 0,
            })
            continue
        domain, block, buffer_generation = identity
        buffers.append({
            "generation": generation, "slot": slot, "domain": domain,
            "named": 1, "ref": 1 if held else 0, "valid": 1, "disk": 0,
            "locked": 1 if held else 0, "dev": ROOTDEV, "block": block,
            "buffer_generation": buffer_generation,
        })
    return buffers


def _render_marker(kind, fields):
    return "PERSIST " + kind + " " + " ".join(
        f"{key}={value}" for key, value in fields.items())


def _synthetic_trace(case):
    _, generation, cpus = CASE_RUNTIME[case]
    del cpus
    stream = SyntheticEvents(generation)
    named = {}
    result = None
    arrived = gate = holders = 0

    if case == "tx":
        pid = 10
        stream.log(POINT["LOG_ADMIT"], pid, outstanding=1)
        stream.cache(POINT["CACHE_LOOKUP"], pid, 0, 0, 1990,
                     outstanding=1)
        stream.cache(POINT["CACHE_GUARD"], pid, 0, 0, 1990,
                     outstanding=1)
        stream.cache(POINT["CACHE_PUBLISH"], pid, 0, 0, 1990, slot=0,
                     ref=1, buffer_generation=1, outstanding=1)
        stream.cache(POINT["CACHE_SLEEP"], pid, 0, 0, 1990, slot=0,
                     ref=1, buffer_generation=1, outstanding=1)
        stream.cache(POINT["CACHE_PIN"], pid, 0, 0, 1990, slot=0,
                     ref=2, buffer_generation=1, outstanding=1)
        stream.log(POINT["LOG_APPEND"], pid, block=1990, log_n=1,
                   outstanding=1)
        stream.log(POINT["LOG_ABSORB"], pid, block=1990, log_n=1,
                   outstanding=1)
        stream.cache(POINT["CACHE_RELEASE"], pid, 0, 0, 1990, slot=0,
                     ref=1, buffer_generation=1, log_n=1, outstanding=1)
        stream.log(POINT["LOG_END"], pid, log_n=1, committing=1, aux=1)
        stream.io(POINT["IO_SUBMIT"], pid, block=3, slot=1,
                  buffer_generation=1, write=1, log_n=1, committing=1)
        stream.io(POINT["IO_COMPLETE"], pid, block=3, slot=1,
                  buffer_generation=1, write=1, log_n=1, committing=1)
        stream.log(POINT["LOG_PAYLOAD"], pid, block=1990, log_n=1,
                   committing=1)
        stream.io(POINT["IO_SUBMIT"], pid, block=2, slot=2,
                  buffer_generation=1, write=1, log_n=1, committing=1)
        stream.io(POINT["IO_COMPLETE"], pid, block=2, slot=2,
                  buffer_generation=1, write=1, log_n=1, committing=1)
        stream.log(POINT["LOG_HEADER"], pid, block=2, log_n=1,
                   committing=1, aux=1)
        stream.cache(POINT["CACHE_HIT"], pid, 0, 0, 1990, slot=0,
                     ref=2, buffer_generation=1, log_n=1, committing=1)
        stream.cache(POINT["CACHE_SLEEP"], pid, 0, 0, 1990, slot=0,
                     ref=2, buffer_generation=1, log_n=1, committing=1)
        stream.io(POINT["IO_SUBMIT"], pid, block=1990, slot=0,
                  buffer_generation=1, write=1, log_n=1, committing=1)
        stream.io(POINT["IO_COMPLETE"], pid, block=1990, slot=0,
                  buffer_generation=1, write=1, log_n=1, committing=1)
        stream.log(POINT["LOG_HOME"], pid, block=1990, log_n=1,
                   committing=1)
        stream.cache(POINT["CACHE_UNPIN"], pid, 0, 0, 1990, slot=0,
                     ref=1, buffer_generation=1, log_n=1, committing=1)
        stream.cache(POINT["CACHE_RELEASE"], pid, 0, 0, 1990, slot=0,
                     ref=0, buffer_generation=1, log_n=1, committing=1)
        stream.io(POINT["IO_SUBMIT"], pid, block=2, slot=2,
                  buffer_generation=1, write=1, log_n=1, committing=1)
        stream.io(POINT["IO_COMPLETE"], pid, block=2, slot=2,
                  buffer_generation=1, write=1, log_n=1, committing=1)
        stream.log(POINT["LOG_CLEAR"], pid, block=2, committing=1)
        stream.log(POINT["LOG_COMMIT_DONE"], pid)
        named = {0: (0, 1990, 1)}
    elif case == "admission-empty":
        for pid, outstanding in ((11, 1), (12, 2), (13, 3)):
            stream.log(POINT["LOG_ADMIT"], pid, outstanding=outstanding,
                       hart=pid % 2)
        stream.log(POINT["LOG_WAIT_SPACE"], 14, outstanding=3)
        stream.log(POINT["LOG_END"], 11, outstanding=2)
        stream.log(POINT["LOG_ADMIT"], 14, outstanding=3)
        stream.log(POINT["LOG_END"], 12, outstanding=2)
        stream.log(POINT["LOG_END"], 13, outstanding=1)
        stream.log(POINT["LOG_END"], 14, committing=1, aux=1)
        stream.log(POINT["LOG_COMMIT_DONE"], 14)
    elif case == "admission-used":
        stream.log(POINT["LOG_ADMIT"], 21, outstanding=1)
        stream.log(POINT["LOG_APPEND"], 21, block=1988, log_n=1,
                   outstanding=1)
        stream.log(POINT["LOG_ADMIT"], 22, log_n=1, outstanding=2)
        stream.log(POINT["LOG_WAIT_SPACE"], 23, log_n=1, outstanding=2)
        stream.log(POINT["LOG_END"], 22, log_n=1, outstanding=1)
        stream.log(POINT["LOG_ADMIT"], 23, log_n=1, outstanding=2)
        stream.log(POINT["LOG_END"], 21, log_n=1, outstanding=1)
        stream.log(POINT["LOG_END"], 23, log_n=1, committing=1, aux=1)
        stream.log(POINT["LOG_PAYLOAD"], 23, block=1988, log_n=1,
                   committing=1)
        stream.log(POINT["LOG_HEADER"], 23, block=2, log_n=1,
                   committing=1, aux=1)
        stream.log(POINT["LOG_HOME"], 23, block=1988, log_n=1,
                   committing=1)
        stream.log(POINT["LOG_CLEAR"], 23, block=2, committing=1)
        stream.log(POINT["LOG_COMMIT_DONE"], 23)
    elif case == "same":
        key = 1989
        stream.cache(POINT["CACHE_LOOKUP"], 31, 0, 0, key)
        stream.cache(POINT["CACHE_GUARD"], 31, 0, 0, key)
        stream.cache(POINT["CACHE_HIT"], 31, 0, 0, key, slot=0,
                     ref=1, buffer_generation=1)
        stream.cache(POINT["CACHE_SLEEP"], 31, 0, 0, key, slot=0,
                     ref=1, buffer_generation=1)
        stream.cache(POINT["CACHE_LOOKUP"], 32, 1, 0, key)
        stream.cache(POINT["CACHE_GUARD"], 32, 1, 0, key)
        stream.cache(POINT["CACHE_HIT"], 32, 1, 0, key, slot=0,
                     ref=2, buffer_generation=1)
        stream.cache(POINT["CACHE_RELEASE"], 31, 0, 0, key, slot=0,
                     ref=1, buffer_generation=1)
        stream.cache(POINT["CACHE_SLEEP"], 32, 1, 0, key, slot=0,
                     ref=1, buffer_generation=1)
        stream.cache(POINT["CACHE_RELEASE"], 32, 1, 0, key, slot=0,
                     ref=0, buffer_generation=1)
        named = {0: (0, key, 1)}
        result = {
            "case": case, "block1": key, "block2": key,
            "pid1": 31, "pid2": 32, "domain1": 0, "domain2": 0,
            "expected1": 81, "expected2": 81, "value1": 81, "value2": 81,
        }
    elif case in ("collision", "parallel"):
        if case == "collision":
            block1, block2, domain1, domain2 = 2, 15, 1, 1
            p1, p2 = 41, 42
        else:
            block1, block2, domain1, domain2 = 2, 3, 1, 2
            p1, p2 = 51, 52
        stream.cache(POINT["CACHE_LOOKUP"], p1, 0, domain1, block1)
        stream.cache(POINT["CACHE_GUARD"], p1, 0, domain1, block1)
        stream.cache(POINT["CACHE_LOOKUP"], p2, 1, domain2, block2)
        stream.cache(POINT["CACHE_GUARD"], p2, 1, domain2, block2)
        stream.cache(POINT["CACHE_HIT"], p1, 0, domain1, block1, slot=0,
                     ref=1, buffer_generation=1)
        stream.cache(POINT["CACHE_SLEEP"], p1, 0, domain1, block1, slot=0,
                     ref=1, buffer_generation=1)
        stream.cache(POINT["CACHE_HIT"], p2, 1, domain2, block2, slot=1,
                     ref=1, buffer_generation=1)
        stream.cache(POINT["CACHE_SLEEP"], p2, 1, domain2, block2, slot=1,
                     ref=1, buffer_generation=1)
        stream.cache(POINT["CACHE_RELEASE"], p1, 0, domain1, block1, slot=0,
                     ref=0, buffer_generation=1)
        stream.cache(POINT["CACHE_RELEASE"], p2, 1, domain2, block2, slot=1,
                     ref=0, buffer_generation=1)
        named = {0: (domain1, block1, 1), 1: (domain2, block2, 1)}
        result = {
            "case": case, "block1": block1, "block2": block2,
            "pid1": p1, "pid2": p2, "domain1": domain1,
            "domain2": domain2, "expected1": 81, "expected2": 82,
            "value1": 81, "value2": 82,
        }
        arrived, gate = 3, 1
    else:
        require(case == "cache-full", "unknown synthetic case")
        for offset in range(NBUF):
            pid = 100 + offset
            block = 1000 + offset
            domain = offset % 13
            stream.cache(POINT["CACHE_LOOKUP"], pid, 0, domain, block)
            stream.cache(POINT["CACHE_GUARD"], pid, 0, domain, block)
            stream.cache(POINT["CACHE_PUBLISH"], pid, 0, domain, block,
                         slot=offset, ref=1, buffer_generation=1)
            stream.cache(POINT["CACHE_SLEEP"], pid, 0, domain, block,
                         slot=offset, ref=1, buffer_generation=1)
            named[offset] = (domain, block, 1)
        holders = NBUF

    panic = case == "cache-full"
    buffers = _synthetic_buffers(generation, named, held=panic)
    ledger = {
        "case": case, "generation": generation,
        "refs": NBUF if panic else 0, "locks": NBUF if panic else 0,
        "disk": 0, "log_n": 0, "outstanding": 0, "committing": 0,
        "error": 0,
    }
    meta = {
        "case": case, "abi": PA_ABI, "generation": generation,
        "count": len(stream.events), "error": 0, "arrived": arrived,
        "gate": gate, "holders": holders,
    }
    markers = []
    if result is not None:
        markers.append(("RESULT", result))
    markers.append(("META", meta))
    markers.extend(("EVENT", event) for event in stream.events)
    markers.extend(("BUFFER", view) for view in buffers)
    markers.append(("LEDGER", ledger))
    if panic:
        markers.append(("FULL", {"generation": generation, "holders": NBUF,
                                  "next": 1000 + NBUF}))
    else:
        markers.append(("PASS", {"case": case}))
    text = "\n".join(_render_marker(kind, fields) for kind, fields in markers) + "\n"
    if panic:
        text += PANIC_TEXT + "\n"
    return text


def _expect_rejected(label, text, case, scenarios, *, panic=False):
    try:
        ReplaySource(text).run(case, scenarios, panic=panic)
    except LabError:
        return
    raise LabError(f"self-test mutation was accepted: {label}")


def _swap_event_payloads(text, left, right):
    lines = text.splitlines()
    indexes = [[i for i, line in enumerate(lines) if needle in line]
               for needle in (left, right)]
    require(all(len(matches) == 1 for matches in indexes),
            "self-test event swap is ambiguous")
    first, second = indexes[0][0], indexes[1][0]
    prefixes = [lines[index].split(" point=", 1)[0]
                for index in (first, second)]
    payloads = [lines[index].split(" point=", 1)[1]
                for index in (first, second)]
    lines[first] = prefixes[0] + " point=" + payloads[1]
    lines[second] = prefixes[1] + " point=" + payloads[0]
    return "\n".join(lines) + "\n"


def self_test():
    scenarios = validate_scenarios(
        json.loads(SCENARIO_PATH.read_text(encoding="utf-8")))
    good = {case: _synthetic_trace(case) for case in CASE_RUNTIME}
    for case, text in good.items():
        ReplaySource(text).run(case, scenarios, panic=(case == "cache-full"))

    tx = good["tx"]
    same = good["same"]
    collision = good["collision"]
    parallel = good["parallel"]
    full = good["cache-full"]
    swapped = tx.replace("point=27", "point=999", 1).replace(
        "point=28", "point=27", 1).replace("point=999", "point=28", 1)
    mutations = (
        ("extra-field", tx.replace(" holders=0", " forged=1 holders=0", 1),
         "tx", False),
        ("duplicate-field", tx.replace("case=tx abi=1", "case=tx case=tx abi=1", 1),
         "tx", False),
        ("missing-field", tx.replace(" abi=1", "", 1), "tx", False),
        ("non-integer", tx.replace("abi=1", "abi=one", 1), "tx", False),
        ("unknown-marker", "PERSIST UNKNOWN value=1\n" + tx, "tx", False),
        ("guest-fail", "PERSIST FAIL reason=forged\n" + tx, "tx", False),
        ("extra-pass", tx + "PERSIST PASS case=tx\n", "tx", False),
        ("seq-gap", tx.replace("seq=2 ", "seq=20 ", 1), "tx", False),
        ("unknown-point", tx.replace("point=1 ", "point=99 ", 1), "tx", False),
        ("wrong-generation", tx.replace("generation=1 seq=1",
                                          "generation=2 seq=1", 1), "tx", False),
        ("log-subject", tx.replace("point=22 pid=10 hart=0 domain=-1",
                                     "point=22 pid=10 hart=0 domain=0", 1),
         "tx", False),
        ("swapped-commit-install", swapped, "tx", False),
        ("duplicate-append", tx.replace("point=25", "point=24", 1),
         "tx", False),
        ("io-completion-without-submit", tx.replace("point=40", "point=41", 1),
         "tx", False),
        ("header-semantic-before-completion", _swap_event_payloads(
            tx, "seq=15 point=41", "seq=16 point=27"), "tx", False),
        ("dirty-ledger", tx.replace("refs=0 locks=0 disk=0",
                                      "refs=1 locks=0 disk=0", 1), "tx", False),
        ("event-overflow", tx.replace(" error=0", " error=1"),
         "tx", False),
        ("capacity-inequality", good["admission-empty"].replace(
            "point=21 pid=14 hart=0 domain=-1 slot=-1 ref=-1 dev=1 block=0 "
            "buffer_generation=0 log_n=0 outstanding=3",
            "point=21 pid=14 hart=0 domain=-1 slot=-1 ref=-1 dev=1 block=0 "
            "buffer_generation=0 log_n=0 outstanding=2", 1),
         "admission-empty", False),
        ("same-key-changed", same.replace("block2=1989", "block2=1988", 1),
         "same", False),
        ("same-ref-peak", same.replace("point=3 pid=32 hart=1 domain=0 slot=0 ref=2",
                                        "point=3 pid=32 hart=1 domain=0 slot=0 ref=3", 1),
         "same", False),
        ("collision-partition", collision.replace("domain2=1", "domain2=2", 1),
         "collision", False),
        ("collision-content", collision.replace("value2=82", "value2=81", 1),
         "collision", False),
        ("collision-no-overlap", _swap_event_payloads(
            collision, "point=5 pid=42", "point=6 pid=41"),
         "collision", False),
        ("parallel-same-hart", parallel.replace(
            "point=2 pid=52 hart=1", "point=2 pid=52 hart=0", 1),
         "parallel", False),
        ("parallel-broken-gate", parallel.replace("arrived=3 gate=1",
                                                    "arrived=1 gate=0", 1),
         "parallel", False),
        ("full-short-ledger", full.replace("refs=30 locks=30",
                                            "refs=29 locks=30", 1),
         "cache-full", True),
        ("full-no-panic", full.replace(PANIC_TEXT + "\n", ""),
         "cache-full", True),
        ("full-extra-pass", full.replace(PANIC_TEXT,
                                          "PERSIST PASS case=cache-full\n" + PANIC_TEXT),
         "cache-full", True),
        ("duplicate-buffer-slot", tx.replace("slot=1 domain=0 named=0",
                                               "slot=0 domain=0 named=0", 1),
         "tx", False),
    )
    for label, text, case, panic in mutations:
        _expect_rejected(label, text, case, scenarios, panic=panic)

    scenario_mutations = []
    mutation = copy.deepcopy(scenarios)
    mutation["unexpected"] = 1
    scenario_mutations.append(("scenario-extra-field", mutation))
    mutation = copy.deepcopy(scenarios)
    mutation["cases"][6]["cpus"] = 2
    scenario_mutations.append(("panic-cpus", mutation))
    mutation = copy.deepcopy(scenarios)
    mutation["cases"][5]["gates"][0]["iteration_budget"] = 0
    scenario_mutations.append(("unbounded-spin", mutation))
    mutation = copy.deepcopy(scenarios)
    mutation["cases"][3]["gates"][0]["point"] = "CACHE_SELECTED"
    scenario_mutations.append(("observe-only-gate", mutation))
    for label, mutation in scenario_mutations:
        try:
            validate_scenarios(mutation)
        except LabError:
            continue
        raise LabError(f"self-test mutation was accepted: {label}")

    if REPO_ROOT.is_dir():
        sampled = set(repo_file_names())
        for resource in (RUNNER, SCENARIO_PATH, FIXTURE):
            if resource.exists():
                require(resource.relative_to(REPO_ROOT).as_posix() in sampled,
                        f"repository sampling omitted {resource.name}")
    print("persistence oracle self-test passed: seven good traces accepted, "
          f"{len(mutations) + len(scenario_mutations)} mutations rejected")


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
    group_id = process.pid
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while process_group_exists(group_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    if process_group_exists(group_id):
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    require(not process_group_exists(group_id),
            f"process group remains after cleanup: {group_id}")


def checked_bytes(command, *, cwd, timeout=300, env=None):
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
        if process.returncode != 0:
            tail = output[-8000:].decode("utf-8", "replace")
            raise LabError(
                f"command failed ({process.returncode}): "
                f"{' '.join(os.fspath(item) for item in command)}\n{tail}")
        return output
    except subprocess.TimeoutExpired as exc:
        raise LabError(
            "command watchdog expired: " +
            " ".join(os.fspath(item) for item in command)) from exc
    finally:
        stop_group(process)


def checked(command, *, cwd, timeout=300, env=None):
    return checked_bytes(command, cwd=cwd, timeout=timeout, env=env).decode(
        "utf-8", "replace")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha(data):
    return hashlib.sha256(data).hexdigest()


def transcript_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_or_missing(path):
    if path.is_symlink():
        return "symlink:" + hashlib.sha256(os.readlink(path).encode()).hexdigest()
    return sha256(path) if path.is_file() else "missing"


def repo_file_names():
    visible = checked_bytes(
        ["git", "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"], cwd=REPO_ROOT).split(b"\0")
    ignored = checked_bytes(
        ["git", "ls-files", "-z", "--others", "--ignored",
         "--exclude-standard"], cwd=REPO_ROOT).split(b"\0")
    return sorted({os.fsdecode(item) for item in visible + ignored if item})


def _hash_fs_entry(digest, root, name):
    path = root / name
    digest.update(os.fsencode(name))
    digest.update(b"\0")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        digest.update(b"missing\0")
        return
    digest.update(str(stat.S_IFMT(metadata.st_mode)).encode())
    digest.update(b":")
    digest.update(str(stat.S_IMODE(metadata.st_mode)).encode())
    digest.update(b"\0")
    if stat.S_ISREG(metadata.st_mode):
        digest.update(sha256(path).encode())
    elif stat.S_ISLNK(metadata.st_mode):
        digest.update(os.fsencode(os.readlink(path)))
    else:
        digest.update(f"rdev={metadata.st_rdev}".encode())
    digest.update(b"\0")


def repo_state():
    digest = hashlib.sha256()
    index = checked_bytes(["git", "ls-files", "--stage", "-z"], cwd=REPO_ROOT)
    status = checked_bytes(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all",
         "--ignored"], cwd=REPO_ROOT)
    names = repo_file_names()
    digest.update(b"INDEX\0")
    digest.update(index)
    digest.update(b"\0FILES\0")
    for name in names:
        _hash_fs_entry(digest, REPO_ROOT, name)
    digest.update(b"STATUS\0")
    digest.update(status)
    return {
        "digest": digest.hexdigest(),
        "index_digest": bytes_sha(index),
        "status_digest": bytes_sha(status),
        "status": status.decode("utf-8", "backslashreplace") or "clean",
        "sampled_paths": len(names),
    }


def snapshot(root):
    result = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        dirnames.sort()
        filenames.sort()
        for name in dirnames + filenames:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISREG(metadata.st_mode):
                value = ("file", mode, sha256(path))
            elif stat.S_ISLNK(metadata.st_mode):
                value = ("symlink", mode, os.readlink(path))
            elif stat.S_ISDIR(metadata.st_mode):
                value = ("directory", mode, "")
            else:
                value = ("special", mode, metadata.st_rdev)
            result[relative] = value
    return result


def snapshot_digest(value):
    digest = hashlib.sha256()
    for name, fields in sorted(value.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(repr(fields).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def export_baseline(root, commit):
    require(re.fullmatch(r"[0-9a-f]{40}", commit) is not None,
            "manifest baseline_commit must be a full lowercase commit ID")
    checked(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=REPO_ROOT)
    archive = checked_bytes(
        ["git", "archive", "--format=tar", commit], cwd=REPO_ROOT)
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        stream.extractall(root, filter="data")


def stage_artifact(source, destination, *, max_bytes=2 * 1024 * 1024):
    source = source.expanduser().resolve(strict=True)
    metadata = source.lstat()
    require(stat.S_ISREG(metadata.st_mode) and not source.is_symlink(),
            f"artifact must be a regular non-symlink file: {source}")
    require(0 < metadata.st_size <= max_bytes,
            f"artifact size is outside the accepted bound: {source}")
    before = sha256(source)
    shutil.copyfile(source, destination)
    require(sha256(destination) == before and sha256(source) == before,
            f"artifact changed while staging: {source}")
    return before


def patch_numstat(root, patch):
    raw = checked_bytes(
        ["git", "apply", "--numstat", "-z", "--", os.fspath(patch)], cwd=root)
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        require(len(fields) == 3, f"malformed patch numstat: {record!r}")
        added, deleted, raw_path = fields
        require(added.isdigit() and deleted.isdigit(),
                "binary patches are not accepted")
        path = os.fsdecode(raw_path)
        require(path and not Path(path).is_absolute() and ".." not in Path(path).parts,
                f"unsafe patch path: {path!r}")
        entries.append((path, int(added), int(deleted)))
    require(entries, f"empty patch: {patch}")
    require(len({path for path, _, _ in entries}) == len(entries),
            f"patch mentions a path more than once: {patch}")
    return entries


def inspect_patch(root, patch, expected_paths, *, expected_created=frozenset()):
    entries = patch_numstat(root, patch)
    paths = {path for path, _, _ in entries}
    require(paths == set(expected_paths),
            f"patch scope changed: expected {sorted(expected_paths)}, got {sorted(paths)}")
    summary = checked(
        ["git", "apply", "--summary", "--", os.fspath(patch)], cwd=root)
    created = set(re.findall(r"^ create mode [0-7]+ (.+)$", summary,
                             flags=re.MULTILINE))
    require(created == set(expected_created),
            f"patch create/delete/rename/mode summary changed: {summary.strip()!r}")
    residual = re.sub(r"^ create mode [0-7]+ .+\n?", "", summary,
                      flags=re.MULTILINE).strip()
    require(not residual, f"unsupported patch metadata operation: {residual}")
    checked(["git", "apply", "--check", "--unidiff-zero",
             "--whitespace=error-all", "--", os.fspath(patch)], cwd=root)
    return sorted(paths)


def apply_patch_file(root, patch, *, reverse=False):
    command = ["git", "apply", "--unidiff-zero", "--whitespace=error-all"]
    if reverse:
        command.append("--reverse")
    command.extend(["--", os.fspath(patch)])
    checked(command, cwd=root)


class ExportSession:
    def __init__(self, root, original):
        self.root = root
        self.original = original
        self.applied = []
        self.cleaned = False

    def apply(self, patch):
        apply_patch_file(self.root, patch)
        self.applied.append(patch)

    def cleanup(self):
        if self.cleaned:
            return
        errors = []
        try:
            checked(["make", "clean"], cwd=self.root, timeout=180)
        except (LabError, OSError) as exc:
            errors.append(f"make clean: {exc}")
        generated = self.root / "test-xv6.out"
        if generated.exists() and generated.is_file() and not generated.is_symlink():
            try:
                generated.unlink()
            except OSError as exc:
                errors.append(f"remove test-xv6.out: {exc}")
        for patch in reversed(self.applied):
            try:
                apply_patch_file(self.root, patch, reverse=True)
            except (LabError, OSError) as exc:
                errors.append(f"reverse {Path(patch).name}: {exc}")
        try:
            restored = snapshot(self.root)
            if restored != self.original:
                before = set(self.original)
                after = set(restored)
                changed = sorted(name for name in before & after
                                 if self.original[name] != restored[name])
                errors.append(
                    "source snapshot differs after cleanup: "
                    f"added={sorted(after - before)[:8]} "
                    f"removed={sorted(before - after)[:8]} "
                    f"changed={changed[:8]}")
        except OSError as exc:
            errors.append(f"snapshot after cleanup: {exc}")
        self.cleaned = True
        if errors:
            raise LabError("; ".join(errors))

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        try:
            self.cleanup()
        except LabError as cleanup_error:
            if exception is None:
                raise
            raise LabError(f"{exception}; cleanup also failed: {cleanup_error}") \
                from exception
        return False


def load_manifest():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    require(type(data) is dict and type(data.get("release")) is dict,
            "curriculum manifest release is missing")
    return data


def check_manifest_and_anchors(root, manifest):
    baseline = manifest["release"].get("baseline_commit")
    require(type(baseline) is str, "manifest baseline_commit is missing")
    units = {unit.get("id"): unit for unit in manifest.get("units", [])
             if type(unit) is dict}
    require("core.persistence" in units, "manifest is missing core.persistence")
    unit = units["core.persistence"]
    require(unit.get("requires") == ["core.filesystem"],
            "persistence requires edge changed")
    require(set(unit.get("evidence_dimensions", [])) == {"S", "F", "B", "C", "R"},
            "persistence evidence dimensions changed")
    required_resources = {
        "resources/persistence/persistence-audit.patch",
        "resources/persistence/baseline-cache-adapter.patch",
        "resources/persistence/run-lab.py",
        "resources/persistence/scenarios.json",
        "resources/persistence/rubric.md",
        "resources/persistence/report-template.md",
    }
    require(required_resources <= set(unit.get("resources", [])),
            "persistence resources are not manifest-authoritative")
    anchors = unit.get("source_anchors")
    require(type(anchors) is list and anchors,
            "persistence source anchors are missing")
    for anchor in anchors:
        require(type(anchor) is dict and set(anchor) == {"path", "symbol"},
                "persistence source anchor schema changed")
        path = root / anchor["path"]
        require(path.is_file() and not path.is_symlink(),
                f"source anchor path missing: {anchor['path']}")
        require(anchor["symbol"] in path.read_text(encoding="utf-8"),
                f"source anchor missing: {anchor['path']}:{anchor['symbol']}")
    param = (root / "kernel/param.h").read_text(encoding="utf-8")
    require(re.search(r"^#define\s+MAXOPBLOCKS\s+10\b", param, re.MULTILINE) and
            re.search(r"^#define\s+LOGBLOCKS\s+\(MAXOPBLOCKS\s*\*\s*3\)",
                      param, re.MULTILINE) and
            re.search(r"^#define\s+NBUF\s+\(MAXOPBLOCKS\s*\*\s*3\)",
                      param, re.MULTILINE),
            "pinned NBUF/MAXOPBLOCKS/LOGBLOCKS relation changed")
    return unit, baseline


def _ordered_tokens(source, tokens, message):
    position = -1
    for token in tokens:
        next_position = source.find(token, position + 1)
        require(next_position > position, message + f": missing/order {token!r}")
        position = next_position


def check_audit_sources(root):
    guest = (root / "user/persisttrace.c").read_text(encoding="utf-8")
    for token in (
            "run_log(", "run_admission_empty(", "run_admission_used(",
            "run_same(", "run_pair(", "run_full(", "PERSIST META",
            "PERSIST EVENT", "PERSIST BUFFER", "PERSIST LEDGER",
            "PERSIST RESULT", "PERSIST FULL", "PERSIST PASS", "PERSIST FAIL"):
        require(token in guest, f"guest evidence token missing: {token}")
    header = (root / "kernel/persistenceaudit.h").read_text(encoding="utf-8")
    for name, value in POINT.items():
        require(re.search(rf"^#define\s+PA_{name}\s+{value}\b", header,
                          re.MULTILINE),
                f"fixture point ID changed: PA_{name}")
    require(re.search(r"^#define\s+PA_ABI\s+1\b", header, re.MULTILINE) and
            re.search(r"^#define\s+PA_MAX_RECORDS\s+512\b", header,
                      re.MULTILINE),
            "fixture ABI/event budget changed")
    audit = (root / "kernel/persistenceaudit.c").read_text(encoding="utf-8")
    for token in ("record_locked(", "audit.next_seq++", "audit.count >= PA_MAX_RECORDS",
                  "audit.error = 1", "spins == 10000000",
                  "cacheproject_snapshot(", "persistenceaudit_command("):
        require(token in audit, f"fixture audit seam missing: {token}")
    bio = (root / "kernel/bio.c").read_text(encoding="utf-8")
    for token in ("cacheproject_partition(", "cacheproject_snapshot(",
                  "persistenceaudit_cache_event(",
                  "persistenceaudit_cache_gate(", "PA_CACHE_PUBLISH",
                  "PA_CACHE_PIN", "PA_CACHE_UNPIN", "bget: no buffers"):
        require(token in bio, f"cache adapter seam missing: {token}")
    require("persistenceaudit_cache_gate(PA_CACHE_SLEEP" in bio,
            "cache adapter lacks the collision lock-held rendezvous")
    log = (root / "kernel/log.c").read_text(encoding="utf-8")
    _ordered_tokens(log, ("bwrite(dbuf)", "PA_LOG_HOME", "bunpin(dbuf)"),
                    "home completion/unpin hook moved")
    write_head_start = log.index("write_head(void)")
    write_head_end = log.find("recover_from_log(void)", write_head_start)
    require(write_head_end > write_head_start, "cannot isolate write_head source")
    write_head = log[write_head_start:write_head_end]
    _ordered_tokens(write_head, ("bwrite(buf)", "PA_LOG_CLEAR", "PA_LOG_HEADER"),
                    "header completion hook moved")
    write_log_start = log.index("write_log(void)")
    write_log_end = log.find("commit()", write_log_start)
    require(write_log_end > write_log_start, "cannot isolate write_log source")
    write_log = log[write_log_start:write_log_end]
    _ordered_tokens(write_log, ("bwrite(to)", "PA_LOG_PAYLOAD"),
                    "payload completion hook moved")
    install = log[log.index("install_trans(int recovering)"):log.index("read_head(void)")]
    _ordered_tokens(install, ("bwrite(dbuf)", "PA_LOG_HOME", "bunpin(dbuf)"),
                    "install completion hook moved")


def run_static_export(temp_root, manifest, scenarios, adapter, fixture):
    root = temp_root / "static-repo"
    baseline = manifest["release"]["baseline_commit"]
    export_baseline(root, baseline)
    original = snapshot(root)
    original_digest = snapshot_digest(original)
    unit, checked_baseline = check_manifest_and_anchors(root, manifest)
    del unit
    require(checked_baseline == baseline, "manifest baseline changed during export")
    inspect_patch(root, adapter, CACHE_PATCH_PATHS)
    inspect_patch(root, fixture, FIXTURE_PATHS,
                  expected_created=FIXTURE_CREATED)
    with ExportSession(root, original) as session:
        native_build = checked(["make", "-j2", "kernel/kernel"], cwd=root,
                               timeout=360)
        checked(["make", "clean"], cwd=root, timeout=180)
        require(snapshot(root) == original,
                "audit-disabled baseline build did not clean to its snapshot")
        session.apply(adapter)
        session.apply(fixture)
        check_audit_sources(root)
        instrumented_build = checked(
            ["make", "-j2", "kernel/kernel", "user/_persisttrace"],
            cwd=root, timeout=360)
    return {
        "baseline": baseline,
        "source_snapshot": original_digest,
        "native_build_sha256": transcript_sha(native_build),
        "adapter_fixture_build_sha256": transcript_sha(instrumented_build),
        "fixture_paths": sorted(FIXTURE_PATHS),
        "adapter_paths": sorted(CACHE_PATCH_PATHS),
        "scenario_cases": [case["id"] for case in scenarios["cases"]],
    }


class QemuSource:
    """Run one private xv6 image and feed transcripts to the shared reducer."""

    def __init__(self, root, cpus, timeout):
        self.timeout = timeout
        self.output = bytearray()
        self.process = subprocess.Popen(
            ["make", f"CPUS={cpus}", "qemu"], cwd=root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True)
        try:
            self._wait_for(b"$ ", 0)
        except Exception:
            stop_group(self.process)
            raise

    def _read(self, wait=1):
        ready, _, _ = select.select([self.process.stdout], [], [], wait)
        if not ready:
            return False
        chunk = os.read(self.process.stdout.fileno(), 4096)
        require(chunk, "QEMU output closed")
        self.output.extend(chunk.replace(b"\r", b""))
        return True

    def _wait_for(self, marker, start):
        deadline = time.monotonic() + self.timeout
        while marker not in self.output[start:]:
            require(time.monotonic() < deadline,
                    f"QEMU watchdog expired waiting for {marker!r}")
            self._read()

    def _write(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def command(self, command):
        start = len(self.output)
        self._write(command + "\n")
        self._wait_for(b"$ ", start)
        return bytes(self.output[start:]).decode("utf-8", "replace")

    def panic_command(self, command):
        start = len(self.output)
        self._write(command + "\n")
        self._wait_for(PANIC_TEXT.encode(), start)
        while self._read(0.1):
            pass
        return bytes(self.output[start:]).decode("utf-8", "replace")

    def close(self):
        if self.process.poll() is None:
            try:
                self._write(b"\x01x")
                self.process.wait(timeout=10)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                pass
        stop_group(self.process)


def run_case_group(root, scenarios, cases, *, cpus, timeout):
    source = QemuSource(root, cpus, timeout)
    bundles = {}
    try:
        for case in cases:
            guest_case = "log" if case == "tx" else case
            transcript = source.command(f"persisttrace {guest_case}")
            try:
                bundles[case] = ReplaySource(transcript).run(case, scenarios)
            except LabError as exc:
                raise LabError(
                    f"{case}: {exc}\n{transcript[-12000:]}") from exc
            print(f"persistence scenario passed: {case}", flush=True)
    finally:
        source.close()
    return bundles


def run_panic_case(root, scenarios, *, timeout):
    source = QemuSource(root, 1, timeout)
    try:
        transcript = source.panic_command("persisttrace cache-full")
        return ReplaySource(transcript).run(
            "cache-full", scenarios, panic=True)
    finally:
        source.close()


def inspect_orderly_image(image):
    data = image.read_bytes()
    require(len(data) == FSSIZE * BSIZE,
            "private fs.img size changed")
    superblock = struct.unpack_from("<8I", data, BSIZE)
    require(superblock[0] == 0x10203040 and superblock[1] == FSSIZE,
            "private image superblock changed")
    logstart = superblock[5]
    header_n = struct.unpack_from("<I", data, logstart * BSIZE)[0]
    home = data[1990 * BSIZE:1990 * BSIZE + 2]
    require(header_n == 0, "orderly transaction left a nonzero log header")
    require(home == b"\x51\x52",
            "orderly transaction home bytes changed")
    return {"logstart": logstart, "header_n": header_n,
            "home_block": 1990, "home_bytes": home.hex()}


def assert_usertest(command, transcript):
    require(transcript.count("ALL TESTS PASSED") == 1 and
            "SOME TESTS FAILED" not in transcript and
            "PERSIST " not in transcript,
            f"focused regression failed: {command}\n{transcript[-5000:]}")


def run_driver(root, arguments, *, cpus, timeout):
    env = os.environ.copy()
    env["CPUS"] = str(cpus)
    output = checked(["python3", "test-xv6.py", *arguments], cwd=root,
                     env=env, timeout=timeout)
    require(output.count("ALL TESTS PASSED") == 1 and
            "SOME TESTS FAILED" not in output and
            "PERSIST " not in output,
            f"driver regression failed: {' '.join(arguments)}")
    return output


def tool_environment(root):
    compiler = next((name for name in (
        "riscv64-unknown-elf-gcc", "riscv64-linux-gnu-gcc")
        if shutil.which(name)), None)
    require(compiler is not None, "RISC-V compiler is required")
    uname = os.uname()
    return {
        "host": f"{uname.sysname} {uname.release} {uname.machine}",
        "python": sys.version.split()[0],
        "make": checked(["make", "--version"], cwd=root).splitlines()[0],
        "compiler": checked([compiler, "--version"], cwd=root).splitlines()[0],
        "qemu": checked(["qemu-system-riscv64", "--version"],
                        cwd=root).splitlines()[0],
    }


def run_dynamic_export(temp_root, manifest, scenarios, candidate, fixture):
    root = temp_root / "dynamic-repo"
    baseline = manifest["release"]["baseline_commit"]
    export_baseline(root, baseline)
    original = snapshot(root)
    candidate_paths = inspect_patch(root, candidate, CACHE_PATCH_PATHS)
    fixture_paths = inspect_patch(
        root, fixture, FIXTURE_PATHS, expected_created=FIXTURE_CREATED)
    timeout = scenarios["defaults"]["watchdog_seconds"]
    bundles = {}
    focused = {}
    quick = full = None
    orderly = None
    panic_image_before = panic_image_after = None
    with ExportSession(root, original) as session:
        session.apply(candidate)
        session.apply(fixture)
        check_audit_sources(root)
        checked(["make", "-j2", "fs.img"], cwd=root, timeout=420)

        bundles.update(run_case_group(
            root, scenarios, ("tx",), cpus=1, timeout=timeout))
        orderly = inspect_orderly_image(root / "fs.img")
        bundles.update(run_case_group(
            root, scenarios,
            ("admission-empty", "admission-used", "same", "collision", "parallel"),
            cpus=2, timeout=timeout))

        panic_image_before = sha256(root / "fs.img")
        bundles["cache-full"] = run_panic_case(
            root, scenarios, timeout=timeout)
        panic_image_after = sha256(root / "fs.img")
        require(panic_image_after == panic_image_before,
                "isolated read-only panic run changed private fs.img")
        print("persistence scenario passed: cache-full", flush=True)

        qemu = QemuSource(root, 1, max(timeout, 240))
        try:
            for name in ("writebig", "bigwrite", "bigfile", "manywrites"):
                command = f"usertests {name}"
                transcript = qemu.command(command)
                assert_usertest(command, transcript)
                focused[command] = transcript
                print(f"focused regression passed: {name}", flush=True)
            transcript = qemu.command("logstress f0")
            require(transcript.count("write failed -1") == 1 and
                    "panic:" not in transcript and "PERSIST " not in transcript,
                    f"logstress boundary changed:\n{transcript[-5000:]}")
            cleanup = qemu.command("rm f0")
            require("failed" not in cleanup.lower(),
                    "logstress files did not clean up")
            focused["logstress f0"] = transcript
            print("focused boundary regression passed: logstress", flush=True)
        finally:
            qemu.close()

        checked(["make", "clean"], cwd=root, timeout=180)
        quick = run_driver(root, ["-q", "usertests"], cpus=2, timeout=600)
        print("quick regression passed: CPUS=2", flush=True)
        checked(["make", "clean"], cwd=root, timeout=180)
        full = run_driver(root, ["usertests"], cpus=1, timeout=1200)
        print("full regression passed: CPUS=1", flush=True)

    return {
        "baseline": baseline,
        "candidate_paths": candidate_paths,
        "fixture_paths": fixture_paths,
        "bundles": bundles,
        "orderly_image": orderly,
        "panic_image_before": panic_image_before,
        "panic_image_after": panic_image_after,
        "focused": focused,
        "quick": quick,
        "full": full,
        "environment": tool_environment(REPO_ROOT),
    }


def write_report(path, *, tutorial_commit, static, dynamic, scenarios,
                 candidate_hash, fixture_hash, runner_hash, scenario_hash,
                 before):
    lines = [
        "# Buffer cache、日志与事务机器证据附录", "",
        f"- tutorial commit：`{tutorial_commit}`",
        f"- pinned baseline：`{static['baseline']}`",
        f"- scenario SHA-256：`{scenario_hash}`",
        f"- fixture SHA-256：`{fixture_hash}`",
        f"- runner SHA-256：`{runner_hash}`",
        f"- candidate SHA-256：`{candidate_hash}`",
        f"- host/toolchain：`{dynamic['environment']}`",
        "- 隔离：临时 baseline export、private fs.img、独立 QEMU process groups", "",
        "## Scenario oracles", "",
    ]
    for case in CASE_RUNTIME:
        bundle = dynamic["bundles"][case]
        evidence_lines = [marker.line for marker in bundle.markers]
        if case == "cache-full":
            panic_lines = [line for line in bundle.raw.replace("\r", "").splitlines()
                           if line == PANIC_TEXT]
            require(len(panic_lines) == 1,
                    "cache-full report lacks one exact raw panic line")
            evidence_lines.extend(panic_lines)
        lines.extend([
            f"### {case}", "",
            f"- CPUS：`{bundle.cpus}`；generation：`{bundle.generation}`",
            f"- host summary：`{bundle.summary}`", "",
            "```text",
            "\n".join(evidence_lines),
            "```", "",
        ])
    lines.extend([
        "## Offline image and regressions", "",
        f"- orderly image：`{dynamic['orderly_image']}`",
        f"- panic image before/after：`{dynamic['panic_image_before']}` / "
        f"`{dynamic['panic_image_after']}`",
    ])
    for command, transcript in dynamic["focused"].items():
        lines.append(f"- `{command}`：SHA-256 `{transcript_sha(transcript)}`")
    lines.extend([
        f"- quick CPUS=2：SHA-256 `{transcript_sha(dynamic['quick'])}`",
        f"- full CPUS=1：SHA-256 `{transcript_sha(dynamic['full'])}`", "",
        "## Evidence and cleanup", "",
        "- S/F/B/C/R：分别由 static contracts、transaction、capacity/panic、"
        "two-hart gates 和 orderly private-image oracle 支持。",
        "- R 仅支持无 crash 的 home bytes 与 cleared header；不支持 host power-loss、"
        "synthetic tear、recovery 或 offline fsck。",
        f"- event schema/budget：`{scenarios['event_schema_version']}` / "
        f"`{scenarios['defaults']['event_budget']}`。",
        f"- shared repository digest：`{before['digest']}`；fs.img："
        f"`{digest_or_missing(REPO_ROOT / 'fs.img')}`。",
        "- 临时树已 make clean、按 fixture/candidate 逆序恢复并逐文件比较；"
        "所有 QEMU/driver process group 已回收。", "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def run(*, self_test_only, static_only, candidate, report):
    scenarios = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    validate_scenarios(scenarios)
    self_test()
    if self_test_only:
        return
    manifest = load_manifest()
    before = repo_state()
    before_image = digest_or_missing(REPO_ROOT / "fs.img")
    tutorial_commit = checked(["git", "rev-parse", "HEAD"],
                              cwd=REPO_ROOT).strip()
    require(BASELINE_ADAPTER.is_file() and FIXTURE.is_file(),
            "published persistence patches are missing")
    if not static_only:
        require(shutil.which("qemu-system-riscv64"),
                "qemu-system-riscv64 is required")
        require(candidate is not None,
                "--candidate is required unless --static-only is selected")
    if candidate is not None:
        candidate = candidate.expanduser().resolve(strict=True)
        require(not candidate.is_relative_to(REPO_ROOT),
                "--candidate must be outside the repository")
    if report is not None:
        report = report.expanduser().resolve()
        require(not report.is_relative_to(REPO_ROOT),
                "--report must be outside the repository")

    dynamic = None
    with tempfile.TemporaryDirectory(prefix="xv6-persistence-") as directory:
        scratch = Path(directory)
        static = run_static_export(
            scratch, manifest, scenarios, BASELINE_ADAPTER, FIXTURE)
        print("persistence static passed: manifest, patches, anchors, builds, cleanup",
              flush=True)
        if not static_only:
            staged = scratch / "candidate.patch"
            candidate_hash = stage_artifact(candidate, staged)
            dynamic = run_dynamic_export(
                scratch, manifest, scenarios, staged, FIXTURE)
            if report is not None:
                write_report(
                    report, tutorial_commit=tutorial_commit, static=static,
                    dynamic=dynamic, scenarios=scenarios,
                    candidate_hash=candidate_hash,
                    fixture_hash=sha256(FIXTURE), runner_hash=sha256(RUNNER),
                    scenario_hash=sha256(SCENARIO_PATH), before=before)
                require(report.read_text(encoding="utf-8").splitlines().count(
                    PANIC_TEXT) == 1,
                    "machine appendix did not preserve the exact panic line")

    after = repo_state()
    require(after["digest"] == before["digest"],
            "shared repository content, mode, status, or index changed")
    require(digest_or_missing(REPO_ROOT / "fs.img") == before_image,
            "shared fs.img changed")
    if static_only:
        print("persistence passed: self-test, static, build, cleanup")
    else:
        print("persistence passed: static, S/F/B/C/R, focused, quick, full, cleanup")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    require(not (args.self_test and (args.static_only or args.candidate or args.report)),
            "--self-test cannot be combined with other options")
    run(self_test_only=args.self_test, static_only=args.static_only,
        candidate=args.candidate, report=args.report)


if __name__ == "__main__":
    try:
        main()
    except (LabError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"persistence lab failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
