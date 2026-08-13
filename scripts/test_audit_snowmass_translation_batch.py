#!/usr/bin/env python3
"""Tests for fail-closed multi-paper packaged-PDF QA."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.audit_snowmass_translation_batch import audit_batch


class TranslationBatchAuditTests(unittest.TestCase):
    def test_batch_derives_cover_adjusted_page_count_and_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "papers/arxiv_a/packaged/snowmass-a.zh-CN.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"pdf")
            records = [
                {
                    "record_id": "arxiv:a",
                    "page_count": 7,
                    "publication_allowed": True,
                }
            ]
            with mock.patch(
                "scripts.audit_snowmass_translation_batch.audit_pdf",
                return_value={"ok": True, "page_count": 8, "failures": []},
            ) as audit:
                report = audit_batch(records, output_root=root, qa_root=root / "qa")

            self.assertTrue(report["ok"])
            self.assertEqual(report["passed"], 1)
            audit.assert_called_once_with(
                pdf,
                expected_pages=8,
                contact_sheet_path=root / "qa/arxiv_a/contact-sheet.jpg",
            )

    def test_missing_pdf_and_blocked_record_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = [
                {"record_id": "arxiv:a", "page_count": 2, "publication_allowed": True},
                {"record_id": "arxiv:b", "page_count": 3, "publication_allowed": False},
            ]

            report = audit_batch(records, output_root=root, qa_root=root / "qa")

            self.assertFalse(report["ok"])
            self.assertEqual(report["selected"], 2)
            self.assertEqual(report["audited"], 0)
            self.assertEqual(report["failed"], 2)
            self.assertIn("missing_packaged_pdf:arxiv:a", report["failures"])
            self.assertIn("publication_not_allowed:arxiv:b", report["failures"])

    def test_empty_batch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            report = audit_batch([], output_root=root, qa_root=root / "qa")

            self.assertFalse(report["ok"])
            self.assertEqual(report["failures"], ["empty_batch"])

    def test_unsafe_or_colliding_record_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = [
                {"record_id": "..", "page_count": 2, "publication_allowed": True},
                {"record_id": "arxiv:a/b", "page_count": 2, "publication_allowed": True},
                {"record_id": "arxiv:a?b", "page_count": 2, "publication_allowed": True},
            ]

            report = audit_batch(records, output_root=root, qa_root=root / "qa")

            self.assertFalse(report["ok"])
            self.assertEqual(report["selected"], 3)
            self.assertEqual(report["audited"], 0)
            self.assertEqual(report["failed"], 3)
            self.assertIn("unsafe_record_path:..", report["failures"])
            self.assertTrue(any(item.startswith("record_path_collision:") for item in report["failures"]))


if __name__ == "__main__":
    unittest.main()
