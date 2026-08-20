#!/usr/bin/env python3

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


VALIDATE_PATH = Path(__file__).with_name("validate.py")
SPEC = importlib.util.spec_from_file_location("tutorial_validate", VALIDATE_PATH)
tutorial_validate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tutorial_validate)

GENERATE_PATH = Path(__file__).with_name("generate_navigation.py")
SPEC = importlib.util.spec_from_file_location("tutorial_navigation", GENERATE_PATH)
tutorial_navigation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tutorial_navigation)


class MarkdownLinkValidationTests(unittest.TestCase):
    def setUp(self):
        self.original_repo_root = tutorial_validate.REPO_ROOT
        self.original_tutorial_root = tutorial_validate.TUTORIAL_ROOT
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary_directory.name)
        self.tutorial_root = self.repo_root / "docs" / "xv6-tutorial"
        self.reference_root = self.repo_root / "docs" / "xv6-riscv"
        self.tutorial_root.mkdir(parents=True)
        self.reference_root.mkdir(parents=True)
        (self.reference_root / "page.md").write_text("# Reference\n", encoding="utf-8")
        tutorial_validate.REPO_ROOT = self.repo_root
        tutorial_validate.TUTORIAL_ROOT = self.tutorial_root

    def tearDown(self):
        tutorial_validate.REPO_ROOT = self.original_repo_root
        tutorial_validate.TUTORIAL_ROOT = self.original_tutorial_root
        self.temporary_directory.cleanup()

    def validate(self, markdown):
        (self.tutorial_root / "unit.md").write_text(markdown, encoding="utf-8")
        validation = tutorial_validate.Validation()
        tutorial_validate.validate_markdown_links(validation)
        return validation.errors

    def test_rejects_every_supported_link_form_into_reference_docs(self):
        cases = {
            "inline image": "![diagram](../xv6-riscv/page.md)\n",
            "reference definition": "[implementation]: ../xv6-riscv/page.md\n",
            "angle target": "<../xv6-riscv/page.md>\n",
            "html anchor": '<a href="../xv6-riscv/page.md">implementation</a>\n',
            "html image": '<img src="../xv6-riscv/page.md">\n',
        }
        for name, markdown in cases.items():
            with self.subTest(name=name):
                errors = self.validate(markdown)
                self.assertTrue(errors, f"{name} bypassed the independence check")
                self.assertIn("forbidden implementation-reference link", errors[0])

    def test_allows_plain_text_mentions_and_valid_internal_links(self):
        (self.tutorial_root / "peer.md").write_text("# Peer\n", encoding="utf-8")
        errors = self.validate(
            "The implementation set is named `docs/xv6-riscv/`.\n\n"
            "Continue with [the tutorial peer](peer.md).\n"
        )
        self.assertEqual([], errors)


class WalkthroughReviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.original_tutorial_root = tutorial_validate.TUTORIAL_ROOT
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.tutorial_root = Path(self.temporary_directory.name)
        (self.tutorial_root / "reviews").mkdir()
        tutorial_validate.TUTORIAL_ROOT = self.tutorial_root

    def tearDown(self):
        tutorial_validate.TUTORIAL_ROOT = self.original_tutorial_root
        self.temporary_directory.cleanup()

    def validate(self, review_path, content):
        path = self.tutorial_root / review_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        validation = tutorial_validate.Validation()
        tutorial_validate.validate_walkthrough_reviews(
            {"id": "foundation.example", "reviews": [review_path]},
            validation,
        )
        return validation.errors

    def test_rejects_review_outside_reviews_directory(self):
        errors = self.validate("templates/walkthrough-review.md", COMPLETE_WALKTHROUGH)
        self.assertIn(
            "walkthrough record must be a Markdown file under reviews/: "
            "foundation.example -> templates/walkthrough-review.md",
            errors,
        )

    def test_rejects_blank_walkthrough_template(self):
        errors = self.validate(
            "reviews/foundation.md",
            "# 非作者走查记录\n\n- 教程版本：\n\n## 观察到的卡点\n",
        )
        self.assertTrue(errors)
        self.assertTrue(any("missing or empty field" in error for error in errors))
        self.assertTrue(any("missing or empty section" in error for error in errors))

    def test_accepts_complete_walkthrough_record(self):
        errors = self.validate("reviews/foundation.md", COMPLETE_WALKTHROUGH)
        self.assertEqual([], errors)


class CoverageCompleteReleaseValidationTests(unittest.TestCase):
    def validate_release(self, version="1.0.0", status="verified", coverage=True):
        validation = tutorial_validate.Validation()
        tutorial_validate.validate_release_completion(
            {
                "release": {
                    "version": version,
                    "status": status,
                    "coverage_complete": coverage,
                }
            },
            {
                "foundation.example": {"status": "verified"},
                "core.example": {"status": "verified"},
            },
            validation,
        )
        return validation.errors

    def test_accepts_verified_coverage_complete_1_x_release(self):
        self.assertEqual([], self.validate_release())

    def test_rejects_1_x_without_verified_complete_coverage(self):
        cases = (
            ("1.0.0", "verified", False),
            ("1.0.0", "draft", True),
            ("0.1.0", "verified", True),
        )
        for version, status, coverage in cases:
            with self.subTest(version=version, status=status, coverage=coverage):
                self.assertTrue(self.validate_release(version, status, coverage))

    def test_rejects_coverage_complete_with_non_verified_unit(self):
        validation = tutorial_validate.Validation()
        tutorial_validate.validate_release_completion(
            {
                "release": {
                    "version": "1.0.0",
                    "status": "verified",
                    "coverage_complete": True,
                }
            },
            {
                "foundation.example": {"status": "verified"},
                "core.example": {"status": "draft"},
            },
            validation,
        )
        self.assertIn(
            "coverage_complete release has non-verified units: core.example",
            validation.errors,
        )


class SourceCoverageValidationTests(unittest.TestCase):
    def setUp(self):
        self.original_repo_root = tutorial_validate.REPO_ROOT
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary_directory.name)
        (self.repo_root / "kernel").mkdir()
        (self.repo_root / "kernel" / "owned.c").write_text(
            "void owned(void) {}\n", encoding="utf-8"
        )
        tutorial_validate.REPO_ROOT = self.repo_root

    def tearDown(self):
        tutorial_validate.REPO_ROOT = self.original_repo_root
        self.temporary_directory.cleanup()

    def test_coverage_complete_requires_primary_owner_anchor(self):
        validation = tutorial_validate.Validation()
        tutorial_validate.validate_source(
            {
                "release": {"coverage_complete": True},
                "source_scope": {
                    "include_globs": ["kernel/*.c"],
                    "exclude_paths": [],
                },
                "source_areas": [
                    {
                        "path": "kernel/owned.c",
                        "kind": "kernel",
                        "owner": "core.owner",
                        "secondary": [],
                    }
                ],
            },
            {"core.owner": {"status": "verified", "source_anchors": []}},
            validation,
        )
        self.assertIn(
            "coverage_complete source is not anchored by its owner: "
            "kernel/owned.c -> core.owner",
            validation.errors,
        )


class LegacyQuestionMigrationValidationTests(unittest.TestCase):
    def setUp(self):
        self.original_repo_root = tutorial_validate.REPO_ROOT
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary_directory.name)
        self.legacy_root = self.repo_root / "docs" / "questions"
        self.legacy_root.mkdir(parents=True)
        tutorial_validate.REPO_ROOT = self.repo_root

    def tearDown(self):
        tutorial_validate.REPO_ROOT = self.original_repo_root
        self.temporary_directory.cleanup()

    def validate(self):
        validation = tutorial_validate.Validation()
        tutorial_validate.validate_question_migration(
            {"release": {"coverage_complete": True}}, validation
        )
        return validation.errors

    def test_accepts_only_compatibility_readme(self):
        (self.legacy_root / "README.md").write_text(
            "Authoritative: ../xv6-tutorial/questions/README.md\n", encoding="utf-8"
        )
        self.assertEqual([], self.validate())

    def test_rejects_legacy_question_copy(self):
        (self.legacy_root / "README.md").write_text(
            "Authoritative: ../xv6-tutorial/questions/README.md\n", encoding="utf-8"
        )
        (self.legacy_root / "old.md").write_text("# old\n", encoding="utf-8")
        self.assertIn(
            "coverage_complete release retains legacy question files: old.md",
            self.validate(),
        )


class NavigationGenerationTests(unittest.TestCase):
    def setUp(self):
        self.original_root = tutorial_navigation.TUTORIAL_ROOT
        self.original_manifest = tutorial_navigation.MANIFEST_PATH
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        tutorial_navigation.TUTORIAL_ROOT = self.root
        tutorial_navigation.MANIFEST_PATH = self.root / "curriculum.json"
        manifest = {
            "release": {
                "version": "1.0.0",
                "status": "verified",
                "baseline_commit": "0" * 40,
                "coverage_complete": True,
            },
            "stages": [
                {"id": "later", "title": "Later", "kind": "core", "order": 20},
                {"id": "early", "title": "Early", "kind": "core", "order": 10},
            ],
            "units": [
                {
                    "id": "later.planned",
                    "title": "Planned question",
                    "stage": "later",
                    "order": 2,
                    "status": "planned",
                    "path": "core/planned.md",
                    "resources": ["questions/planned.md"],
                },
                {
                    "id": "early.verified",
                    "title": "Verified question",
                    "stage": "early",
                    "order": 1,
                    "status": "verified",
                    "path": "core/verified.md",
                    "resources": [
                        "questions/verified.md",
                        "questions/answers/verified.md",
                    ],
                },
            ],
        }
        tutorial_navigation.MANIFEST_PATH.write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def tearDown(self):
        tutorial_navigation.TUTORIAL_ROOT = self.original_root
        tutorial_navigation.MANIFEST_PATH = self.original_manifest
        self.temporary_directory.cleanup()

    def run_generator(self, *arguments):
        with mock.patch.object(sys, "argv", [str(GENERATE_PATH), *arguments]):
            return tutorial_navigation.main()

    def test_cli_is_deterministic_and_renders_manifest_order_and_status(self):
        self.assertEqual(0, self.run_generator())
        first = {path: path.read_bytes() for path in self.root.rglob("README.md")}
        self.assertEqual(0, self.run_generator())
        self.assertEqual(
            first, {path: path.read_bytes() for path in self.root.rglob("README.md")}
        )

        questions = (self.root / "questions" / "README.md").read_text(encoding="utf-8")
        self.assertLess(questions.index("early.verified"), questions.index("later.planned"))
        self.assertIn("[Verified question](verified.md) | `verified`", questions)
        self.assertIn(
            "Planned question (`questions/planned.md`) | `planned`", questions
        )
        stages = (self.root / "stages" / "README.md").read_text(encoding="utf-8")
        self.assertIn("| Early (`early`) | 0 | 0 | 1 |", stages)
        self.assertIn("| Later (`later`) | 1 | 0 | 0 |", stages)

    def test_check_rejects_stale_question_index(self):
        self.assertEqual(0, self.run_generator())
        (self.root / "questions" / "README.md").write_text("stale\n", encoding="utf-8")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(1, self.run_generator("--check"))
        self.assertIn(
            "stale generated navigation: questions/README.md", errors.getvalue()
        )


COMPLETE_WALKTHROUGH = """# 非作者走查记录

- 教程版本：0.1.0
- 源码基线：e6fc75076de152c5446f2c6be9bb80b848e7cc5d
- 走查时教程提交：3a6d442d00ab4b08e9f547ca1c50bedbda00c3d4
- 走查单元或连续路径：foundation.example
- 匿名入口能力：会打开终端，不预设 C、RISC-V 或 GDB 知识
- 使用环境：Linux，命令行，QEMU 和 GDB

## 观察到的卡点

首次执行时记录了一个需要修正的卡点。

## 验收产物

提交了命令记录、内存图、机器状态表和调试追踪。

## 修正与复查

修正文档后重新执行，所有 oracle 满足。

## 结果

通过。
"""


if __name__ == "__main__":
    unittest.main()
