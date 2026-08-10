#!/usr/bin/env python3
"""Tests for refilling translated chunks into BabelDOC IR."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("refill_snowmass_babeldoc.py")


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("BabelDOC refill command is not implemented")
    spec = importlib.util.spec_from_file_location("refill_snowmass_babeldoc", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RefillSnowmassBabelDocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rights = self.root / "papers.json"
        self.rights.write_text(
            json.dumps([{"record_id": "arxiv:allowed", "publication_allowed": True}]),
            encoding="utf-8",
        )
        self.article = self.root / "translation" / "papers" / "arxiv_allowed"
        self.article.mkdir(parents=True)
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"%PDF-test\n")
        (self.article / "babeldoc_ir.xml").write_text("<document/>\n", encoding="utf-8")
        (self.article / "chunk0001.md").write_text("Source 14.\n", encoding="utf-8")
        (self.article / "output_chunk0001.md").write_text("译文 14。\n", encoding="utf-8")
        source_hash = hashlib.sha256("Source 14.\n".encode()).hexdigest()
        (self.article / "chunk_status").mkdir()
        (self.article / "chunk_status" / "chunk0001.json").write_text(
            json.dumps(
                {
                    "status": "complete",
                    "source_hash": source_hash,
                    "stages": {
                        "academic": {
                            "status": "complete",
                            "output_hash": hashlib.sha256("译文 14。\n".encode()).hexdigest(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.article / "manifest.json").write_text(
            json.dumps(
                {
                    "record_id": "arxiv:allowed",
                    "input_mode": "babeldoc_ir",
                    "source_pdf_path": str(self.pdf),
                    "babeldoc_ir_xml_file": "babeldoc_ir.xml",
                    "chunks": [
                        {
                            "id": "chunk0001",
                            "source_file": "chunk0001.md",
                            "output_file": "output_chunk0001.md",
                            "page_number": 1,
                            "paragraph_index": 2,
                            "source_hash": source_hash,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_refills_completed_outputs_and_reuses_matching_checkpoint(self) -> None:
        module = load_module()

        def fake_refill(ir_xml, **kwargs):
            kwargs["output_xml"].write_text("<translated/>\n", encoding="utf-8")
            self.assertEqual(kwargs["translations"][0].translated_text, "译文 14。\n")
            return module.BRIDGE.RefillResult(kwargs["output_xml"], 1)

        with mock.patch.object(module.BRIDGE, "refill_document_units", side_effect=fake_refill) as refill:
            self.assertEqual(module.main(["--article-dir", str(self.article), "--rights-manifest", str(self.rights)]), 0)
            self.assertEqual(module.main(["--article-dir", str(self.article), "--rights-manifest", str(self.rights)]), 0)

        self.assertEqual(refill.call_count, 1)
        status = json.loads((self.article / "refill_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["refilled_unit_count"], 1)
        self.assertEqual(status["refill_schema_version"], module.REFILL_SCHEMA_VERSION)
        self.assertEqual(status["babeldoc_version"], "0.6.4")

    def test_rights_gate_rejects_article_before_refill(self) -> None:
        module = load_module()
        self.rights.write_text(
            json.dumps([{"record_id": "arxiv:allowed", "publication_allowed": False}]),
            encoding="utf-8",
        )
        with mock.patch.object(module.BRIDGE, "refill_document_units") as refill:
            self.assertEqual(module.main(["--article-dir", str(self.article), "--rights-manifest", str(self.rights)]), 2)
        refill.assert_not_called()

    def test_duplicate_rights_records_fail_closed(self) -> None:
        module = load_module()
        self.rights.write_text(
            json.dumps(
                [
                    {"record_id": "arxiv:allowed", "publication_allowed": True},
                    {"record_id": "arxiv:allowed", "publication_allowed": False},
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "Duplicate record_id"):
            module.main(["--article-dir", str(self.article), "--rights-manifest", str(self.rights)])


if __name__ == "__main__":
    unittest.main()
