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


if __name__ == "__main__":
    unittest.main()
