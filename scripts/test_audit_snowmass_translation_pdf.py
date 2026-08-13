#!/usr/bin/env python3
"""Tests for reusable Snowmass packaged-PDF visual and residue QA."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import fitz

from scripts.audit_snowmass_translation_pdf import audit_pdf


class PackagedPdfAuditTests(unittest.TestCase):
    def test_missing_pdf_returns_structured_failure_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.pdf"

            report = audit_pdf(missing)

            self.assertFalse(report["ok"])
            self.assertIsNone(report["pdf_sha256"])
            self.assertTrue(any(item.startswith("unreadable_pdf:") for item in report["failures"]))

    def _write_pdf(self, path: Path, pages: list[str]) -> None:
        document = fitz.open()
        for text in pages:
            page = document.new_page(width=595, height=842)
            page.insert_textbox(
                (72, 72, 520, 770),
                text,
                fontfile="/System/Library/Fonts/STHeiti Medium.ttc",
                fontname="testcjk",
                fontsize=12,
            )
        document.save(path)
        document.close()

    def test_clean_pdf_passes_and_writes_contact_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "paper.pdf"
            sheet = root / "contact.jpg"
            self._write_pdf(pdf, ["中文学术译文第一页", "References\n[1] Example"])

            report = audit_pdf(pdf, expected_pages=2, contact_sheet_path=sheet)

            self.assertTrue(report["ok"])
            self.assertEqual(report["page_count"], 2)
            self.assertEqual(report["failures"], [])
            self.assertTrue(sheet.is_file())

    def test_residue_and_model_meta_response_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "paper.pdf"
            self._write_pdf(
                pdf,
                ["正文残留 [[SM_TOKEN]]", "好的，我理解要求。请提供需要翻译的段落。"],
            )

            report = audit_pdf(pdf, expected_pages=2)

            self.assertFalse(report["ok"])
            self.assertIn("residue:placeholder:page_1", report["failures"])
            self.assertIn("model_meta_response:page_2", report["failures"])

    def test_page_count_mismatch_and_low_text_page_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "paper.pdf"
            self._write_pdf(pdf, ["", "有效正文"])

            report = audit_pdf(pdf, expected_pages=3)

            self.assertFalse(report["ok"])
            self.assertIn("page_count_mismatch:2!=3", report["failures"])
            self.assertIn("low_text_page:1", report["failures"])

    def test_malformed_pdf_returns_a_failed_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "broken.pdf"
            pdf.write_bytes(b"not a pdf")

            report = audit_pdf(pdf, expected_pages=1)

            self.assertFalse(report["ok"])
            self.assertEqual(report["page_count"], None)
            self.assertTrue(
                any(failure.startswith("unreadable_pdf:") for failure in report["failures"])
            )


if __name__ == "__main__":
    unittest.main()
