#!/usr/bin/env python3

import importlib.util
import tempfile
import unittest
from pathlib import Path


VALIDATE_PATH = Path(__file__).with_name("validate.py")
SPEC = importlib.util.spec_from_file_location("tutorial_validate", VALIDATE_PATH)
tutorial_validate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tutorial_validate)


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
