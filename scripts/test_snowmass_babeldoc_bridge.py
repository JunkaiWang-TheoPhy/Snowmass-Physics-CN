#!/usr/bin/env python3
"""Integration tests for the BabelDOC-to-translate-book workspace bridge."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("snowmass_babeldoc_bridge.py")
RUN_STATE = Path("/Users/Zhuanz/.agents/skills/translate-book/scripts/run_state.py")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_bridge():
    if not MODULE_PATH.exists():
        raise AssertionError("BabelDOC workspace bridge is not implemented")
    spec = importlib.util.spec_from_file_location("snowmass_babeldoc_bridge", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BabelDocWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"%PDF-test-fixture\n")

    def test_rights_gate_rejects_blocked_record_before_writing_workspace(self) -> None:
        bridge = load_bridge()
        article = self.root / "papers" / "arxiv_blocked"
        unit = bridge.DocumentUnit(
            page_number=1,
            paragraph_index=0,
            layout_label="text",
            text="Blocked paragraph.",
            structure_count=0,
        )

        with self.assertRaisesRegex(PermissionError, "arxiv:blocked"):
            bridge.write_translation_workspace(
                article,
                record_id="arxiv:blocked",
                source_pdf=self.pdf,
                units=[unit],
                allowed_record_ids={"arxiv:allowed"},
            )

        self.assertFalse(article.exists())

    def test_placeholder_validation_rejects_reordered_formula_identities(self) -> None:
        bridge = load_bridge()

        self.assertTrue(
            bridge.placeholder_sequence_matches("A {v1} B {v2}", "甲 {v1} 乙 {v2}")
        )
        self.assertFalse(
            bridge.placeholder_sequence_matches("A {v1} B {v2}", "甲 {v2} 乙 {v1}")
        )

    def test_exported_units_are_consumable_by_translate_book_run_state(self) -> None:
        bridge = load_bridge()
        article = self.root / "papers" / "arxiv_allowed"
        ir_json = self.root / "styles_and_formulas.json"
        ir_xml = self.root / "styles_and_formulas.xml"
        ir_json.write_text('{"pages": []}\n', encoding="utf-8")
        ir_xml.write_text("<document/>\n", encoding="utf-8")
        units = [
            bridge.DocumentUnit(
                page_number=1,
                paragraph_index=2,
                layout_label="title",
                text="Forecasting assumptions\n",
                structure_count=0,
            ),
            bridge.DocumentUnit(
                page_number=2,
                paragraph_index=5,
                layout_label="text",
                text="The detector records 2800 events at {v1}.\n",
                structure_count=1,
            ),
        ]

        manifest = bridge.write_translation_workspace(
            article,
            record_id="arxiv:allowed",
            source_pdf=self.pdf,
            units=units,
            allowed_record_ids={"arxiv:allowed"},
            ir_json_path=ir_json,
            ir_xml_path=ir_xml,
        )

        self.assertEqual([item["id"] for item in manifest["chunks"]], ["chunk0001", "chunk0002"])
        self.assertEqual(manifest["input_mode"], "babeldoc_ir")
        self.assertEqual(manifest["babeldoc_version"], "0.6.4")
        self.assertEqual(manifest["chunks"][1]["page_number"], 2)
        self.assertEqual(manifest["chunks"][1]["paragraph_index"], 5)
        self.assertEqual(manifest["chunks"][1]["structure_count"], 1)
        self.assertEqual((article / "chunk0002.md").read_text(encoding="utf-8"), units[1].text)
        self.assertEqual((article / "babeldoc_ir.json").read_text(encoding="utf-8"), ir_json.read_text(encoding="utf-8"))
        self.assertEqual((article / "babeldoc_ir.xml").read_text(encoding="utf-8"), ir_xml.read_text(encoding="utf-8"))
        self.assertEqual(manifest["babeldoc_ir_xml_sha256"], sha256_file(ir_xml))
        self.assertEqual(manifest["ir_units_sha256"], sha256_file(article / "ir_units.json"))

        result = subprocess.run(
            [sys.executable, str(RUN_STATE), "plan", str(article)],
            capture_output=True,
            text=True,
            check=True,
        )
        plan = json.loads(result.stdout)
        self.assertEqual(plan["translation_chunk_ids"], ["chunk0001", "chunk0002"])

        status = json.loads((article / "chunking_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["record_id"], "arxiv:allowed")
        self.assertEqual(status["input_mode"], "babeldoc_ir")
        self.assertEqual(status["unit_count"], 2)

    def test_workspace_export_is_idempotent_for_unchanged_units(self) -> None:
        bridge = load_bridge()
        article = self.root / "papers" / "arxiv_allowed"
        unit = bridge.DocumentUnit(1, 0, "text", "Stable paragraph.\n", 0)

        first = bridge.write_translation_workspace(
            article,
            record_id="arxiv:allowed",
            source_pdf=self.pdf,
            units=[unit],
            allowed_record_ids={"arxiv:allowed"},
        )
        second = bridge.write_translation_workspace(
            article,
            record_id="arxiv:allowed",
            source_pdf=self.pdf,
            units=[unit],
            allowed_record_ids={"arxiv:allowed"},
        )

        self.assertEqual(first["source_pdf_sha256"], second["source_pdf_sha256"])
        self.assertEqual(first["chunks"], second["chunks"])

    def test_extracts_real_babeldoc_paragraph_ir_without_translation(self) -> None:
        bridge = load_bridge()
        if importlib.util.find_spec("babeldoc") is None:
            self.skipTest("run this integration with BabelDOC's managed Python")
        fixture = Path(
            "tmp/pdfs/snowmass_babeldoc_probe/arxiv_2203.07506-pages1-3.pdf"
        ).resolve()
        if not fixture.exists():
            self.skipTest("three-page Snowmass PDF fixture is unavailable")
        self.assertTrue(
            hasattr(bridge, "extract_document_units"),
            "BabelDOC IR extraction seam is not implemented",
        )

        result = bridge.extract_document_units(
            fixture,
            working_dir=self.root / "babeldoc-work",
        )

        self.assertGreater(len(result.units), 10)
        self.assertTrue(result.ir_json_path.is_file())
        self.assertTrue(result.ir_xml_path.is_file())
        self.assertEqual(result.babeldoc_version, "0.6.4")
        self.assertTrue(all(1 <= unit.page_number <= 3 for unit in result.units))
        self.assertTrue(any(unit.structure_count > 0 for unit in result.units))
        self.assertTrue(all(unit.structure_count <= 40 for unit in result.units))
        self.assertTrue(any("Cosmology" in unit.text for unit in result.units))

        structured = next(unit for unit in result.units if unit.structure_count > 0)
        translated = "中文译文：" + structured.text
        refilled_xml = self.root / "translated_ir.xml"
        refill = bridge.refill_document_units(
            result.ir_xml_path,
            source_pdf=fixture,
            working_dir=self.root / "babeldoc-refill-work",
            output_xml=refilled_xml,
            translations=[
                bridge.RefillTranslation(
                    page_number=structured.page_number,
                    paragraph_index=structured.paragraph_index,
                    source_text=structured.text,
                    translated_text=translated,
                )
            ],
        )

        self.assertEqual(refill.refilled_unit_count, 1)
        self.assertTrue(refilled_xml.is_file())
        from babeldoc.format.pdf.document_il.xml_converter import XMLConverter

        refilled = XMLConverter().read_xml(str(refilled_xml))
        paragraph = refilled.page[structured.page_number - 1].pdf_paragraph[
            structured.paragraph_index
        ]
        self.assertTrue(paragraph.unicode.startswith("中文译文："))
        self.assertEqual(translated.count("{v"), paragraph.unicode.count("{v"))


if __name__ == "__main__":
    unittest.main()
