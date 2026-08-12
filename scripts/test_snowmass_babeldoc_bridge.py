#!/usr/bin/env python3
"""Integration tests for the BabelDOC-to-translate-book workspace bridge."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from dataclasses import dataclass
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

    def test_placeholder_validation_allows_reordering_but_rejects_identity_drift(self) -> None:
        bridge = load_bridge()

        self.assertTrue(
            bridge.placeholder_sequence_matches("A {v1} B {v2}", "甲 {v1} 乙 {v2}")
        )
        self.assertTrue(
            bridge.placeholder_sequence_matches("A {v1} B {v2}", "甲 {v2} 乙 {v1}")
        )
        self.assertFalse(
            bridge.placeholder_sequence_matches("A {v1} B {v2}", "甲 {v2} 乙 {v2}")
        )

    def test_materializes_lazy_passthrough_values_before_xml_serialization(self) -> None:
        bridge = load_bridge()

        class LazyPassthroughInstruction:
            def materialize(self) -> str:
                return "q 1 0 0 1 0 0 cm"

        @dataclass
        class GraphicState:
            passthrough_instruction: object

        @dataclass
        class Document:
            states: list[GraphicState]

        document = Document([GraphicState(LazyPassthroughInstruction())])

        returned = bridge.materialize_lazy_passthrough_instructions(document)

        self.assertIs(returned, document)
        self.assertEqual(
            document.states[0].passthrough_instruction,
            "q 1 0 0 1 0 0 cm",
        )

    def test_figure_text_gate_rejects_any_translation(self) -> None:
        bridge = load_bridge()

        bridge.require_verbatim_figure_text(0, "Euclid\n", "欧几里得\n")
        bridge.require_verbatim_figure_text(17, "Euclid\n", "Euclid\n")
        with self.assertRaisesRegex(RuntimeError, "Figure-internal text must remain verbatim"):
            bridge.require_verbatim_figure_text(17, "Euclid\n", "欧几里得\n")

    def test_table_text_gate_rejects_translation_but_leaves_caption_translatable(self) -> None:
        bridge = load_bridge()

        bridge.require_verbatim_table_text(False, "Table 1: Forecast.\n", "表1：预测。\n")
        bridge.require_verbatim_table_text(True, "Technical Maturity\n", "Technical Maturity\n")
        with self.assertRaisesRegex(RuntimeError, "Table-internal text must remain verbatim"):
            bridge.require_verbatim_table_text(True, "Technical Maturity\n", "技术成熟度\n")

    def test_resolves_table_body_from_page_geometry_not_fallback_line_label(self) -> None:
        bridge = load_bridge()
        article = self.root / "papers" / "arxiv_allowed"
        article.mkdir(parents=True)
        manifest = {
            "babeldoc_ir_json_file": "babeldoc_ir.json",
            "chunks": [
                {"id": "chunk0001", "page_number": 1, "paragraph_index": 0},
                {"id": "chunk0002", "page_number": 1, "paragraph_index": 1},
            ],
        }
        (article / "babeldoc_ir.json").write_text(
            json.dumps(
                {
                    "page": [
                        {
                            "page_layout": [
                                {
                                    "id": 7,
                                    "class_name": "table",
                                    "box": {"x": 20, "y": 200, "x2": 580, "y2": 640},
                                }
                            ],
                            "pdf_paragraph": [
                                {
                                    "layout_label": "fallback_line",
                                    "xobj_id": 0,
                                    "box": {"x": 30, "y": 580, "x2": 90, "y2": 595},
                                },
                                {
                                    "layout_label": "figure_caption",
                                    "xobj_id": 0,
                                    "box": {"x": 70, "y": 150, "x2": 540, "y2": 190},
                                },
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            bridge.resolve_table_text_chunk_ids(article, manifest),
            {"chunk0001"},
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
        self.assertEqual(manifest["ir_pipeline_version"], bridge.IR_PIPELINE_VERSION)
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

    def test_workspace_marks_figure_internal_text_as_verbatim(self) -> None:
        bridge = load_bridge()
        article = self.root / "papers" / "arxiv_allowed"
        figure_label = bridge.DocumentUnit(
            1,
            4,
            "fallback_line",
            "Euclid\n",
            0,
            translation_policy="verbatim_figure_text",
        )

        manifest = bridge.write_translation_workspace(
            article,
            record_id="arxiv:allowed",
            source_pdf=self.pdf,
            units=[figure_label],
            allowed_record_ids={"arxiv:allowed"},
        )

        self.assertEqual(
            manifest["chunks"][0]["translation_policy"],
            "verbatim_figure_text",
        )

    def test_workspace_preserves_chunk_ids_when_a_new_unit_is_inserted(self) -> None:
        bridge = load_bridge()
        article = self.root / "papers" / "arxiv_allowed"
        first_units = [
            bridge.DocumentUnit(1, 0, "text", "First paragraph.\n", 0),
            bridge.DocumentUnit(2, 0, "text", "Second paragraph.\n", 0),
        ]
        bridge.write_translation_workspace(
            article,
            record_id="arxiv:allowed",
            source_pdf=self.pdf,
            units=first_units,
            allowed_record_ids={"arxiv:allowed"},
        )

        updated = bridge.write_translation_workspace(
            article,
            record_id="arxiv:allowed",
            source_pdf=self.pdf,
            units=[
                first_units[0],
                bridge.DocumentUnit(1, 1, "fallback_line", "type\n", 0),
                first_units[1],
            ],
            allowed_record_ids={"arxiv:allowed"},
        )

        ids_by_identity = {
            (chunk["page_number"], chunk["paragraph_index"]): chunk["id"]
            for chunk in updated["chunks"]
        }
        self.assertEqual(ids_by_identity[(1, 0)], "chunk0001")
        self.assertEqual(ids_by_identity[(2, 0)], "chunk0002")
        self.assertEqual(ids_by_identity[(1, 1)], "chunk0003")
        self.assertEqual(
            [chunk["id"] for chunk in updated["chunks"]],
            ["chunk0001", "chunk0003", "chunk0002"],
        )

    def test_short_fallback_line_uses_one_character_translation_threshold(self) -> None:
        bridge = load_bridge()

        class Config:
            min_text_length = 5

        class Paragraph:
            layout_label = "fallback_line"

        class Translator:
            def __init__(self, config) -> None:
                self.translation_config = config

            def pre_translate_paragraph(self, paragraph, tracker, page_fonts, xobj_fonts):
                if len("type") < self.translation_config.min_text_length:
                    return None, None
                return "type", object()

        config = Config()
        result = bridge.pre_translate_document_paragraph(
            Translator(config), Paragraph(), object(), {}, {}
        )

        self.assertEqual(result[0], "type")
        self.assertEqual(config.min_text_length, 5)

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
        self.assertTrue(
            all(unit.structure_count <= bridge.MAX_STRUCTURE_COUNT for unit in result.units)
        )
        self.assertTrue(any("Cosmology" in unit.text for unit in result.units))

        structured = next(unit for unit in result.units if unit.structure_count > 0)
        translated = "中文译文：" + structured.text
        mixed_script = next(unit for unit in result.units if "Snowmass 2021" in unit.text)
        mixed_script_translation = (
            "提交至美国粒子物理未来社区研究会议论文集（Snowmass 2021）\n"
        )
        refilled_xml = self.root / "translated_ir.xml"
        refill = bridge.refill_document_units(
            result.ir_xml_path,
            source_pdf=fixture,
            working_dir=self.root / "babeldoc-refill-work",
            output_xml=refilled_xml,
            translations=[
                bridge.RefillTranslation(
                    page_number=mixed_script.page_number,
                    paragraph_index=mixed_script.paragraph_index,
                    source_text=mixed_script.text,
                    translated_text=mixed_script_translation,
                ),
                bridge.RefillTranslation(
                    page_number=structured.page_number,
                    paragraph_index=structured.paragraph_index,
                    source_text=structured.text,
                    translated_text=translated,
                )
            ],
        )

        self.assertEqual(refill.refilled_unit_count, 2)
        self.assertTrue(refilled_xml.is_file())
        from babeldoc.format.pdf.document_il.xml_converter import XMLConverter

        refilled = XMLConverter().read_xml(str(refilled_xml))
        paragraph = refilled.page[structured.page_number - 1].pdf_paragraph[
            structured.paragraph_index
        ]
        self.assertTrue(paragraph.unicode.startswith("中文译文："))
        self.assertEqual(translated.count("{v"), paragraph.unicode.count("{v"))

        rendered = bridge.render_translated_document(
            refilled_xml,
            source_pdf=fixture,
            working_dir=self.root / "babeldoc-render-work",
            output_dir=self.root / "rendered",
        )
        self.assertTrue(rendered.mono_pdf_path.is_file())
        self.assertTrue(rendered.dual_pdf_path.is_file())

        import pymupdf

        with pymupdf.open(rendered.mono_pdf_path) as mono, pymupdf.open(
            rendered.dual_pdf_path
        ) as dual:
            self.assertEqual(mono.page_count, 3)
            self.assertEqual(dual.page_count, 3)
            self.assertGreater(dual[0].rect.width, mono[0].rect.width)
            rendered_text = "\n".join(page.get_text() for page in mono)
            self.assertNotIn("plain text", rendered_text)
            self.assertNotIn("abandon", rendered_text)
            self.assertNotIn("S\nnowmass", rendered_text)

    def test_restore_verbatim_pages_keeps_reference_layout_in_mono_and_dual(self) -> None:
        bridge = load_bridge()
        import pymupdf

        source = self.root / "source-real.pdf"
        mono = self.root / "mono.pdf"
        dual = self.root / "dual.pdf"

        def write_pdf(
            path: Path,
            texts: list[str],
            *,
            width: float = 300,
            running_header: str | None = None,
            section_headings: dict[int, str] | None = None,
        ) -> None:
            document = pymupdf.open()
            for page_number, value in enumerate(texts, 1):
                page = document.new_page(width=width, height=400)
                if running_header is not None:
                    page.insert_text(
                        (90, 20),
                        running_header,
                        fontsize=8,
                        fontname="china-s" if any(ord(ch) > 127 for ch in running_header) else "helv",
                    )
                if section_headings and page_number in section_headings:
                    page.insert_text((30, 45), section_headings[page_number], fontsize=11)
                page.insert_text((30, 70), value)
            document.save(path)
            document.close()

        write_pdf(
            source,
            [
                "SOURCE BODY",
                "[1] REFERENCE ONE\n[2] REFERENCE TWO",
                "[3] REFERENCE THREE",
            ],
            running_header="RUNNING HEADER",
            section_headings={2: "References"},
        )
        write_pdf(
            mono,
            ["TRANSLATED BODY", "BROKEN ONE", "BROKEN TWO"],
            running_header="统一页眉",
        )
        write_pdf(dual, ["DUAL BODY", "DUAL BROKEN ONE", "DUAL BROKEN TWO"], width=600)

        report = bridge.restore_verbatim_pages(
            source_pdf=source,
            mono_pdf=mono,
            dual_pdf=dual,
            page_numbers={2, 3},
            canonical_header={"source": "RUNNING HEADER", "target": "统一页眉"},
            section_heading_translations=[
                {"source": "References", "target": "参考文献"}
            ],
        )

        self.assertEqual(report["page_numbers"], [2, 3])
        self.assertEqual(
            report["reference_numbers"],
            {"count": 3, "first": 1, "last": 3, "sequential": True},
        )
        self.assertEqual(report["canonical_header_occurrences"], 2)
        self.assertEqual(report["section_heading_occurrences"], 1)
        with pymupdf.open(mono) as document:
            self.assertIn("TRANSLATED BODY", document[0].get_text())
            self.assertIn("REFERENCE ONE", document[1].get_text())
            self.assertNotIn("BROKEN ONE", document[1].get_text())
            self.assertIn("统一页眉", document[1].get_text())
            self.assertNotIn("RUNNING HEADER", document[1].get_text())
            self.assertIn("参考文献", document[1].get_text())
            self.assertNotIn("References", document[1].get_text())
            first_span = next(
                span
                for block in document[0].get_text("dict")["blocks"]
                if "lines" in block
                for line in block["lines"]
                for span in line["spans"]
                if span["text"] == "统一页眉"
            )
            restored_span = next(
                span
                for block in document[1].get_text("dict")["blocks"]
                if "lines" in block
                for line in block["lines"]
                for span in line["spans"]
                if span["text"] == "统一页眉"
            )
            self.assertAlmostEqual(first_span["size"], restored_span["size"], places=2)
            self.assertAlmostEqual(
                first_span["bbox"][0], restored_span["bbox"][0], places=2
            )
        with pymupdf.open(dual) as document:
            text = document[1].get_text()
            self.assertEqual(text.count("REFERENCE ONE"), 2)
            self.assertNotIn("DUAL BROKEN ONE", text)

    def test_reference_number_check_ignores_citations_before_midpage_heading(self) -> None:
        bridge = load_bridge()

        self.assertEqual(
            bridge.reference_entry_numbers(
                "Body cites [14], [5], and [15].\nReferences\n[1] One\n[2] Two\n"
            ),
            [1, 2],
        )

    def test_restore_verbatim_regions_replaces_rendered_figure_and_table_content(self) -> None:
        bridge = load_bridge()
        import pymupdf

        source = self.root / "region-source.pdf"
        mono = self.root / "region-mono.pdf"
        dual = self.root / "region-dual.pdf"

        def write_source(path: Path) -> None:
            document = pymupdf.open()
            page = document.new_page(width=300, height=400)
            page.draw_rect(pymupdf.Rect(20, 70, 220, 120), color=(0, 0, 0))
            page.insert_text((30, 100), "Euclid", fontsize=12)
            page.insert_text((30, 145), "SOURCE FIGURE CAPTION", fontsize=10)
            page.draw_rect(pymupdf.Rect(20, 170, 240, 220), color=(0, 0, 0))
            page.insert_text((30, 200), "Technical Maturity", fontsize=12)
            page.insert_text((30, 245), "SOURCE TABLE CAPTION", fontsize=10)
            document.save(path)
            document.close()

        def write_rendered(path: Path, *, dual_page: bool) -> None:
            document = pymupdf.open()
            width = 600 if dual_page else 300
            page = document.new_page(width=width, height=400)
            offsets = (0, 300) if dual_page else (0,)
            for offset in offsets:
                page.draw_rect(
                    pymupdf.Rect(20 + offset, 70, 220 + offset, 120),
                    color=(1, 0, 0),
                )
                page.insert_text((30 + offset, 100), "Planck", fontsize=12)
                page.insert_text((30 + offset, 145), "CHINESE FIGURE CAPTION", fontsize=10)
                page.draw_rect(
                    pymupdf.Rect(20 + offset, 170, 240 + offset, 220),
                    color=(1, 0, 0),
                )
                page.insert_text((30 + offset, 200), "Maturity", fontsize=12)
                page.insert_text((30 + offset, 245), "CHINESE TABLE CAPTION", fontsize=10)
            document.save(path)
            document.close()

        write_source(source)
        write_rendered(mono, dual_page=False)
        write_rendered(dual, dual_page=True)
        figure_region = bridge.FigureRegion(1, 19, (20, 280, 220, 330))
        table_region = bridge.TableRegion(1, 7, (20, 180, 240, 230))

        report = bridge.restore_verbatim_regions(
            source_pdf=source,
            mono_pdf=mono,
            dual_pdf=dual,
            figure_regions=[figure_region],
            table_regions=[table_region],
        )

        self.assertEqual(
            report,
            {"verified": True, "figure_region_count": 1, "table_region_count": 1},
        )
        with pymupdf.open(mono) as document:
            text = document[0].get_text()
            self.assertIn("Euclid", text)
            self.assertNotIn("Planck", text)
            self.assertIn("Technical Maturity", text)
            self.assertIn("CHINESE FIGURE CAPTION", text)
            self.assertIn("CHINESE TABLE CAPTION", text)
        with pymupdf.open(dual) as document:
            page = document[0]
            translated_half = pymupdf.Rect(300, 0, 600, 400)
            translated_text = page.get_text(clip=translated_half)
            self.assertIn("Euclid", translated_text)
            self.assertNotIn("Planck", translated_text)
            self.assertIn("Technical Maturity", translated_text)
            self.assertIn("CHINESE FIGURE CAPTION", translated_text)
            self.assertIn("CHINESE TABLE CAPTION", translated_text)

    def test_figure_region_self_check_rejects_rendered_text_drift(self) -> None:
        bridge = load_bridge()
        import pymupdf

        source = self.root / "figure-source.pdf"
        matching = self.root / "figure-matching.pdf"
        drifted = self.root / "figure-drifted.pdf"

        def write(path: Path, label: str) -> None:
            document = pymupdf.open()
            page = document.new_page(width=300, height=400)
            page.insert_text((30, 100), label, fontsize=12)
            document.save(path)
            document.close()

        write(source, "Euclid")
        write(matching, "Euclid")
        write(drifted, "Planck")
        region = bridge.FigureRegion(
            page_number=1,
            xobj_id=7,
            box=(20, 280, 200, 330),
        )

        report = bridge.verify_verbatim_figure_regions(
            source_pdf=source,
            mono_pdf=matching,
            regions=[region],
        )
        self.assertEqual(report, {"verified": True, "region_count": 1})
        with self.assertRaisesRegex(RuntimeError, "Figure region text self-check failed"):
            bridge.verify_verbatim_figure_regions(
                source_pdf=source,
                mono_pdf=drifted,
                regions=[region],
            )

    def test_table_region_self_check_rejects_translated_or_missing_cell_text(self) -> None:
        bridge = load_bridge()
        import pymupdf

        source = self.root / "table-source.pdf"
        matching = self.root / "table-matching.pdf"
        drifted = self.root / "table-drifted.pdf"
        shifted = self.root / "table-shifted.pdf"

        def write(path: Path, label: str, *, x: float = 30) -> None:
            document = pymupdf.open()
            page = document.new_page(width=300, height=400)
            page.insert_text((x, 100), label, fontsize=12)
            document.save(path)
            document.close()

        write(source, "Technical Maturity")
        write(matching, "Technical Maturity")
        write(drifted, "Maturity")
        write(shifted, "Technical Maturity", x=90)
        region = bridge.TableRegion(
            page_number=1,
            layout_id=7,
            box=(20, 280, 220, 330),
        )

        report = bridge.verify_verbatim_table_regions(
            source_pdf=source,
            mono_pdf=matching,
            regions=[region],
        )
        self.assertEqual(report, {"verified": True, "region_count": 1})
        with self.assertRaisesRegex(RuntimeError, "Table region text self-check failed"):
            bridge.verify_verbatim_table_regions(
                source_pdf=source,
                mono_pdf=drifted,
                regions=[region],
            )
        with self.assertRaisesRegex(RuntimeError, "Table region geometry self-check failed"):
            bridge.verify_verbatim_table_regions(
                source_pdf=source,
                mono_pdf=shifted,
                regions=[region],
            )

    def test_render_result_records_verbatim_page_self_check(self) -> None:
        bridge = load_bridge()
        result = bridge.RenderedPdfResult(
            Path("mono.pdf"),
            Path("dual.pdf"),
            verbatim_pages=(19, 20),
            verbatim_verified=True,
        )

        self.assertEqual(result.verbatim_pages, (19, 20))
        self.assertTrue(result.verbatim_verified)


if __name__ == "__main__":
    unittest.main()
