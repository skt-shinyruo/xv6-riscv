#!/usr/bin/env python3

import argparse
import fnmatch
import glob
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


TUTORIAL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = TUTORIAL_ROOT.parent.parent
MANIFEST_PATH = TUTORIAL_ROOT / "curriculum.json"
UNIT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
MARKDOWN_INLINE_TARGET_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
MARKDOWN_REFERENCE_TARGET_RE = re.compile(
    r"^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(?:<([^>\n]+)>|(\S+))",
    re.MULTILINE,
)
MARKDOWN_ANGLE_TARGET_RE = re.compile(
    r"<((?:\.{1,2}/|file:|https?://)[^<>\s]+)>",
    re.IGNORECASE,
)
HTML_TARGET_RE = re.compile(
    r"<(?:a|img)\b[^>]*?\b(?:href|src)\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))",
    re.IGNORECASE,
)
VALID_STATUSES = {"planned", "draft", "verified"}
VALID_EVIDENCE = {"S", "F", "B", "C", "R"}
REQUIRED_UNIT_HEADINGS = (
    "## 问题场景与本单元成果",
    "## 前置单元与暂存黑盒",
    "## 最小模型和关键不变量",
    "## 源码追踪计划",
    "## 观察任务",
    "## 有界修改任务",
    "## Oracle、证据、失败路径和局限",
    "## 退出产物与后续单元",
)


class Validation:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)


def load_manifest(validation):
    try:
        with MANIFEST_PATH.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        validation.error(f"cannot load curriculum.json: {exc}")
        return None


def validate_release(manifest, validation):
    if manifest.get("schema_version") != 1:
        validation.error("schema_version must be 1")
    release = manifest.get("release")
    if not isinstance(release, dict):
        validation.error("release must be an object")
        return False
    for field in ("version", "status", "baseline_commit", "coverage_complete", "compatibility"):
        if field not in release:
            validation.error(f"release is missing {field}")
    if release.get("status") not in VALID_STATUSES:
        validation.error(f"invalid release status: {release.get('status')}")
    baseline = release.get("baseline_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", baseline):
        validation.error("release.baseline_commit must be a full 40-character commit")
        return False
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{baseline}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        validation.error(f"release baseline is not a local commit: {baseline}")
        return False
    return True


def validate_stages(manifest, validation):
    stages = manifest.get("stages")
    if not isinstance(stages, list) or not stages:
        validation.error("stages must be a non-empty array")
        return {}
    result = {}
    orders = set()
    for stage in stages:
        if not isinstance(stage, dict):
            validation.error("each stage must be an object")
            continue
        stage_id = stage.get("id")
        if not isinstance(stage_id, str) or not UNIT_ID_RE.fullmatch(stage_id):
            validation.error(f"invalid stage id: {stage_id}")
            continue
        if stage_id in result:
            validation.error(f"duplicate stage id: {stage_id}")
        if stage.get("order") in orders:
            validation.error(f"duplicate stage order: {stage.get('order')}")
        orders.add(stage.get("order"))
        if stage.get("kind") not in {"foundation", "core"}:
            validation.error(f"invalid stage kind for {stage_id}: {stage.get('kind')}")
        result[stage_id] = stage
    return result


def validate_unit_page(unit, validation):
    path = TUTORIAL_ROOT / unit["path"]
    if unit["status"] == "planned" and not path.exists():
        return
    if not path.is_file():
        validation.error(f"unit page does not exist: {unit['path']}")
        return
    text = path.read_text(encoding="utf-8")
    for heading in REQUIRED_UNIT_HEADINGS:
        if heading not in text:
            validation.error(f"{unit['path']} is missing heading: {heading}")


def validate_units(manifest, stages, validation):
    units = manifest.get("units")
    if not isinstance(units, list) or not units:
        validation.error("units must be a non-empty array")
        return {}
    result = {}
    paths = set()
    orders = set()
    concept_owners = {}
    introduced_black_boxes = {}
    required_fields = (
        "id", "path", "title", "stage", "order", "status", "requires", "related",
        "black_boxes", "resolves", "owns", "outcomes", "source_anchors",
        "evidence_dimensions", "resources", "reviews",
    )
    for unit in units:
        if not isinstance(unit, dict):
            validation.error("each unit must be an object")
            continue
        missing = [field for field in required_fields if field not in unit]
        if missing:
            validation.error(f"unit {unit.get('id')} is missing: {', '.join(missing)}")
            continue
        unit_id = unit["id"]
        if not isinstance(unit_id, str) or not UNIT_ID_RE.fullmatch(unit_id):
            validation.error(f"invalid unit id: {unit_id}")
            continue
        if unit_id in result:
            validation.error(f"duplicate unit id: {unit_id}")
        if unit["path"] in paths:
            validation.error(f"duplicate unit path: {unit['path']}")
        paths.add(unit["path"])
        stage = unit["stage"]
        if stage not in stages:
            validation.error(f"unknown stage for {unit_id}: {stage}")
        stage_order = stages.get(stage, {}).get("order")
        order_key = (stage_order, unit["order"])
        if order_key in orders:
            validation.error(f"duplicate unit order within stage {stage}: {unit['order']}")
        orders.add(order_key)
        if unit["status"] not in VALID_STATUSES:
            validation.error(f"invalid status for {unit_id}: {unit['status']}")
        if not isinstance(unit["outcomes"], list) or not unit["outcomes"]:
            validation.error(f"{unit_id} must declare at least one outcome")
        evidence = set(unit["evidence_dimensions"])
        if not evidence or not evidence <= VALID_EVIDENCE:
            validation.error(f"invalid evidence dimensions for {unit_id}: {unit['evidence_dimensions']}")
        for concept in unit["owns"]:
            if concept in concept_owners:
                validation.error(f"explanation {concept} is owned by both {concept_owners[concept]} and {unit_id}")
            concept_owners[concept] = unit_id
        for black_box in unit["black_boxes"]:
            if black_box in introduced_black_boxes:
                validation.error(
                    f"black box {black_box} is introduced by both {introduced_black_boxes[black_box]} and {unit_id}"
                )
            introduced_black_boxes[black_box] = unit_id
        for resource in unit["resources"] + unit["reviews"]:
            resource_path = TUTORIAL_ROOT / resource
            if unit["status"] != "planned" and not resource_path.exists():
                validation.error(f"missing resource for {unit_id}: {resource}")
        if unit["status"] == "verified" and not unit["reviews"]:
            validation.error(f"verified unit has no non-author walkthrough record: {unit_id}")
        validate_unit_page(unit, validation)
        result[unit_id] = unit
    for unit_id, unit in result.items():
        for relation in ("requires", "related"):
            for target in unit[relation]:
                if target not in result:
                    validation.error(f"{unit_id} {relation} unknown unit: {target}")
        for black_box in unit["resolves"]:
            if black_box not in introduced_black_boxes:
                validation.error(f"{unit_id} resolves unknown black box: {black_box}")
        if unit["status"] == "verified":
            for target in unit["requires"]:
                if target in result and result[target]["status"] != "verified":
                    validation.error(f"verified unit {unit_id} requires non-verified unit {target}")
    validate_acyclic_requires(result, validation)
    return result


def validate_acyclic_requires(units, validation):
    state = {}

    def visit(unit_id, path):
        if state.get(unit_id) == 1:
            validation.error("requires cycle: " + " -> ".join(path + [unit_id]))
            return
        if state.get(unit_id) == 2:
            return
        state[unit_id] = 1
        for target in units[unit_id]["requires"]:
            if target in units:
                visit(target, path + [unit_id])
        state[unit_id] = 2

    for unit_id in units:
        visit(unit_id, [])


def source_scope_config(manifest):
    scope = manifest.get("source_scope", {})
    patterns = tuple(scope.get("include_globs", []))
    excluded = frozenset(scope.get("exclude_paths", []))
    return patterns, excluded


def source_path_in_scope(path, patterns, excluded):
    return path not in excluded and any(
        fnmatch.fnmatchcase(path, pattern) for pattern in patterns
    )


def expand_source_scope(manifest):
    patterns, excluded = source_scope_config(manifest)
    result = set()
    for pattern in patterns:
        absolute_pattern = str(REPO_ROOT / pattern)
        for match in glob.glob(absolute_pattern, recursive=True):
            path = Path(match)
            if path.is_file():
                relative = path.relative_to(REPO_ROOT).as_posix()
                if source_path_in_scope(relative, patterns, excluded):
                    result.add(relative)
    return result


def baseline_source_scope(manifest, baseline, validation):
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", baseline],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        validation.error(f"cannot list files from release baseline {baseline}")
        return set()
    patterns, excluded = source_scope_config(manifest)
    return {
        path
        for path in result.stdout.splitlines()
        if source_path_in_scope(path, patterns, excluded)
    }


def validate_baseline_sources(manifest, validation, development):
    baseline = manifest["release"]["baseline_commit"]
    current_paths = expand_source_scope(manifest)
    baseline_paths = baseline_source_scope(manifest, baseline, validation)
    changed = []
    for relative in sorted(current_paths | baseline_paths):
        current_path = REPO_ROOT / relative
        if relative not in current_paths or relative not in baseline_paths:
            changed.append(relative)
            continue
        result = subprocess.run(
            ["git", "show", f"{baseline}:{relative}"],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        if result.returncode != 0 or result.stdout != current_path.read_bytes():
            changed.append(relative)
    if changed:
        detail = ", ".join(changed[:20])
        if len(changed) > 20:
            detail += f", ... ({len(changed)} total)"
        message = f"teaching source differs from release baseline {baseline}: {detail}"
        if development:
            validation.warn(message)
        else:
            validation.error(message + "; pass --development only while migrating")


def validate_source(manifest, units, validation):
    scoped_paths = expand_source_scope(manifest)
    areas = manifest.get("source_areas")
    if not isinstance(areas, list):
        validation.error("source_areas must be an array")
        return
    owned_paths = set()
    for area in areas:
        if not isinstance(area, dict):
            validation.error("each source area must be an object")
            continue
        for field in ("path", "kind", "owner", "secondary"):
            if field not in area:
                validation.error(f"source area is missing {field}: {area}")
        path = area.get("path")
        if path in owned_paths:
            validation.error(f"duplicate source coverage path: {path}")
        owned_paths.add(path)
        if path not in scoped_paths:
            validation.error(f"source area is outside source_scope or missing: {path}")
        owner = area.get("owner")
        if owner not in units:
            validation.error(f"source area {path} has unknown owner: {owner}")
        for target in area.get("secondary", []):
            if target not in units:
                validation.error(f"source area {path} has unknown secondary unit: {target}")
    if manifest.get("release", {}).get("coverage_complete"):
        missing = sorted(scoped_paths - owned_paths)
        extra = sorted(owned_paths - scoped_paths)
        if missing:
            validation.error("coverage_complete release has unowned source paths: " + ", ".join(missing))
        if extra:
            validation.error("coverage_complete release has out-of-scope paths: " + ", ".join(extra))
        for area in areas:
            owner = area.get("owner")
            if owner in units and units[owner]["status"] != "verified":
                validation.error(f"coverage_complete source owner is not verified: {area.get('path')} -> {owner}")
    for unit_id, unit in units.items():
        for anchor in unit["source_anchors"]:
            if not isinstance(anchor, dict) or set(anchor) != {"path", "symbol"}:
                validation.error(f"invalid source anchor in {unit_id}: {anchor}")
                continue
            source_path = REPO_ROOT / anchor["path"]
            if not source_path.is_file():
                validation.error(f"missing source anchor file for {unit_id}: {anchor['path']}")
                continue
            try:
                text = source_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                validation.error(f"source anchor is not UTF-8 text for {unit_id}: {anchor['path']}")
                continue
            if anchor["symbol"] not in text:
                validation.error(
                    f"source anchor token not found for {unit_id}: {anchor['path']}:{anchor['symbol']}"
                )


def markdown_link_targets(text):
    matches = []
    matches.extend(match.group(1) for match in MARKDOWN_INLINE_TARGET_RE.finditer(text))
    for match in MARKDOWN_REFERENCE_TARGET_RE.finditer(text):
        matches.append(match.group(1) or match.group(2))
    matches.extend(match.group(1) for match in MARKDOWN_ANGLE_TARGET_RE.finditer(text))
    for match in HTML_TARGET_RE.finditer(text):
        matches.append(match.group(1) or match.group(2) or match.group(3))
    return dict.fromkeys(matches)


def validate_markdown_links(validation):
    for path in TUTORIAL_ROOT.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for raw_target in markdown_link_targets(text):
            target = unquote(raw_target.strip().split()[0].strip("<>"))
            if "docs/xv6-riscv" in target or "../xv6-riscv" in target:
                validation.error(f"forbidden implementation-reference link in {path.relative_to(TUTORIAL_ROOT)}: {target}")
                continue
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_path = target.split("#", 1)[0]
            if not target_path:
                continue
            resolved = (path.parent / target_path).resolve()
            try:
                resolved.relative_to(REPO_ROOT)
            except ValueError:
                validation.error(f"link escapes repository in {path.relative_to(TUTORIAL_ROOT)}: {target}")
                continue
            if not resolved.exists():
                validation.error(f"broken link in {path.relative_to(TUTORIAL_ROOT)}: {target}")


def validate_generated_navigation(validation):
    result = subprocess.run(
        [sys.executable, str(TUTORIAL_ROOT / "tools" / "generate_navigation.py"), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        validation.error("generated navigation is stale: " + detail)


def main():
    parser = argparse.ArgumentParser(description="Validate the standalone xv6 tutorial")
    parser.add_argument(
        "--development",
        action="store_true",
        help="warn instead of failing when teaching source differs from the pinned baseline",
    )
    args = parser.parse_args()
    validation = Validation()
    manifest = load_manifest(validation)
    if manifest is not None:
        baseline_ready = validate_release(manifest, validation)
        stages = validate_stages(manifest, validation)
        units = validate_units(manifest, stages, validation)
        validate_source(manifest, units, validation)
        if baseline_ready:
            validate_baseline_sources(manifest, validation, args.development)
        validate_markdown_links(validation)
        validate_generated_navigation(validation)
    for warning in validation.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in validation.errors:
        print(f"error: {error}", file=sys.stderr)
    if validation.errors:
        print(f"tutorial validation failed with {len(validation.errors)} error(s)", file=sys.stderr)
        return 1
    print("tutorial validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
