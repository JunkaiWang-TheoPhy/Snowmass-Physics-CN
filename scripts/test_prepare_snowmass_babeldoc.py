#!/usr/bin/env python3
"""Tests for rights-gated BabelDOC workspace preparation."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("prepare_snowmass_babeldoc.py")


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("BabelDOC production preparer is not implemented")
    spec = importlib.util.spec_from_file_location("prepare_snowmass_babeldoc", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PrepareSnowmassBabelDocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rights = self.root / "papers.json"
        self.pdf_root = self.root / "pdfs"
        self.output_root = self.root / "workspaces"
        self.pdf_root.mkdir()
        self.rights.write_text(
            json.dumps(
                [
                    {"record_id": "arxiv:allowed", "publication_allowed": True},
                    {"record_id": "arxiv:blocked", "publication_allowed": False},
                    {"record_id": "arxiv:integer", "publication_allowed": 1},
                ]
            ),
            encoding="utf-8",
        )
        (self.pdf_root / "arxiv_allowed.pdf").write_bytes(b"%PDF-allowed\n")
        (self.pdf_root / "arxiv_blocked.pdf").write_bytes(b"%PDF-blocked\n")

    def extraction_result(self, module):
        work = self.root / "fake-ir"
        work.mkdir(exist_ok=True)
        ir_json = work / "ir.json"
        ir_xml = work / "ir.xml"
        ir_json.write_text("{}\n", encoding="utf-8")
        ir_xml.write_text("<document/>\n", encoding="utf-8")
        return module.BRIDGE.ExtractionResult(
            units=(module.BRIDGE.DocumentUnit(1, 0, "text", "Allowed text.\n", 0),),
            ir_json_path=ir_json,
            ir_xml_path=ir_xml,
            babeldoc_version="0.6.4",
        )

    def test_main_prepares_only_literal_true_records_and_is_resumable(self) -> None:
        module = load_module()
        result = self.extraction_result(module)
        args = [
            "--rights-manifest", str(self.rights),
            "--pdf-root", str(self.pdf_root),
            "--output-root", str(self.output_root),
        ]
        with mock.patch.object(module.BRIDGE, "extract_document_units", return_value=result) as extract:
            self.assertEqual(module.main(args), 0)
            (self.output_root / "papers" / "arxiv_allowed" / "chunk0001.md").write_text(
                "tampered\n", encoding="utf-8"
            )
            self.assertEqual(module.main(args), 0)
            (self.output_root / "papers" / "arxiv_allowed" / "ir_units.json").write_text(
                "{}\n", encoding="utf-8"
            )
            self.assertEqual(module.main(args), 0)
            self.assertEqual(module.main(args), 0)

        self.assertEqual(extract.call_count, 3)
        self.assertTrue((self.output_root / "papers" / "arxiv_allowed" / "manifest.json").is_file())
        self.assertFalse((self.output_root / "papers" / "arxiv_blocked").exists())
        self.assertFalse((self.output_root / "papers" / "arxiv_integer").exists())
        report = json.loads((self.output_root / "preparation_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["eligible_record_count"], 1)
        self.assertEqual(report["completed_record_count"], 1)
        self.assertEqual(report["records"][0]["status"], "reused")

    def test_explicit_blocked_record_fails_before_creating_workspace(self) -> None:
        module = load_module()
        with mock.patch.object(module.BRIDGE, "extract_document_units") as extract:
            exit_code = module.main(
                [
                    "--rights-manifest", str(self.rights),
                    "--pdf-root", str(self.pdf_root),
                    "--output-root", str(self.output_root),
                    "--record-id", "arxiv:blocked",
                ]
            )

        self.assertEqual(exit_code, 2)
        extract.assert_not_called()
        self.assertFalse((self.output_root / "papers" / "arxiv_blocked").exists())


if __name__ == "__main__":
    unittest.main()
