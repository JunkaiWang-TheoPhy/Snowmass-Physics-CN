#!/usr/bin/env python3
"""Tests for fail-closed Snowmass publication artifact QC."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import fitz

from scripts.snowmass_publication_qc import validate_pdf_forbidden_translations


class PublicationPdfQCTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "translation.pdf"
        self.policy = self.root / "policy.json"
        self.policy.write_text(
            json.dumps(
                {
                    "forbidden_translations": [
                        {"text": "旋风加速器", "replacement": "回旋加速器"}
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _write_pdf(self, text: str | None) -> None:
        document = fitz.open()
        page = document.new_page()
        if text is None:
            page.draw_rect((72, 72, 300, 200), fill=(0.2, 0.3, 0.4))
        else:
            page.insert_textbox(
                (72, 72, 520, 200),
                text,
                fontfile="/System/Library/Fonts/STHeiti Medium.ttc",
                fontname="testcjk",
                fontsize=12,
            )
        document.save(self.pdf)
        document.close()

    def test_rejects_known_mistranslation(self) -> None:
        self._write_pdf("劳伦斯伯克利国家实验室，旋风加速器路一号")

        with self.assertRaisesRegex(ValueError, "旋风加速器.*回旋加速器"):
            validate_pdf_forbidden_translations(self.pdf, self.policy)

    def test_rejects_pdf_without_extractable_text(self) -> None:
        self._write_pdf(None)

        with self.assertRaisesRegex(ValueError, "no extractable text"):
            validate_pdf_forbidden_translations(self.pdf, self.policy)

    def test_accepts_checked_pdf_without_forbidden_terms(self) -> None:
        self._write_pdf("劳伦斯伯克利国家实验室，回旋加速器路一号")

        validate_pdf_forbidden_translations(self.pdf, self.policy)


if __name__ == "__main__":
    unittest.main()
