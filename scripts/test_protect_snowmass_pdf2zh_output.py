from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz


class ProtectPdf2zhOutputTests(unittest.TestCase):
    def _make_toc_fixture(
        self,
        root: Path,
        *,
        source_rows: list[tuple[str, str, str, bool]],
        translated_lines: list[tuple[float, str] | tuple[float, float, str]],
    ) -> tuple[Path, Path, Path, Path]:
        source = root / "source.pdf"
        translated = root / "translated.pdf"
        output = root / "protected.pdf"
        ir = root / "ir.xml"

        source_document = fitz.open()
        source_page = source_document.new_page(width=612, height=792)
        source_page.insert_text((90, 100), "Contents", fontsize=14)
        for index, (section, title, destination, fused) in enumerate(source_rows):
            y = 150 + index * 22
            if fused:
                source_page.insert_text((106, y), f"{section}{title}", fontsize=10)
            else:
                source_page.insert_text((106, y), section, fontsize=10)
                source_page.insert_text((132, y), title, fontsize=10)
            source_page.insert_text((516, y), destination, fontsize=10)
        source_document.save(source)
        source_document.close()

        translated_document = fitz.open()
        translated_page = translated_document.new_page(width=612, height=792)
        translated_page.insert_text((90, 100), "目录", fontname="china-s", fontsize=14)
        for item in translated_lines:
            if len(item) == 2:
                y, text = item
                x = 106.0
            else:
                x, y, text = item
            translated_page.insert_text(
                (x, y), text, fontname="china-s", fontsize=10
            )
        translated_document.save(translated)
        translated_document.close()
        ir.write_text(
            '<document totalPages="1"><page pageNumber="0"/></document>',
            encoding="utf-8",
        )
        return source, translated, output, ir

    def test_repairs_merged_numbered_toc_rows_and_records_topology(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            output = root / "protected.pdf"
            ir = root / "ir.xml"

            source_document = fitz.open()
            source_page = source_document.new_page(width=612, height=792)
            source_page.insert_text((90, 100), "Contents", fontsize=14)
            for y, section, title, destination in (
                (150, "3.1", "Meetings with CAD Companies", "4"),
                (172, "3.2", "DARPA Conversations", "5"),
                (194, "3.3", "ICPT Engagement", "5"),
            ):
                source_page.insert_text((106, y), section, fontsize=10)
                source_page.insert_text((132, y), title, fontsize=10)
                source_page.insert_text((516, y), destination, fontsize=10)
            source_document.save(source)
            source_document.close()

            translated_document = fitz.open()
            translated_page = translated_document.new_page(width=612, height=792)
            translated_page.insert_text((90, 100), "目录", fontname="china-s", fontsize=14)
            translated_page.insert_text(
                (106, 150),
                "3.1 与CAD公司的会议.4 3.2 DARPA对话.5 3.3 ICPT合作.5",
                fontname="china-s",
                fontsize=10,
            )
            translated_document.save(translated)
            translated_document.close()
            ir.write_text(
                '<document totalPages="1"><page pageNumber="0"/></document>',
                encoding="utf-8",
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertTrue(receipt["verified"], receipt["failures"])
            self.assertEqual(receipt["repaired_toc_group_count"], 1)
            self.assertEqual(
                [item["section_id"] for item in receipt["toc_topology"]],
                ["3.1", "3.2", "3.3"],
            )
            self.assertTrue(all(item["matched"] for item in receipt["toc_topology"]))
            self.assertEqual(
                [item["source_title"] for item in receipt["toc_topology"]],
                [
                    "Meetings with CAD Companies",
                    "DARPA Conversations",
                    "ICPT Engagement",
                ],
            )
            self.assertTrue(
                all(item["source_title_sha256"] for item in receipt["toc_topology"])
            )
            with fitz.open(output) as protected:
                section_rows = {}
                for line in protected[0].get_text("dict", sort=True)["blocks"]:
                    for visual_line in line.get("lines", []):
                        text = "".join(
                            span.get("text", "") for span in visual_line.get("spans", [])
                        )
                        for section in ("3.1", "3.2", "3.3"):
                            if section in text:
                                section_rows[section] = round(visual_line["bbox"][1], 1)
            self.assertEqual(len(set(section_rows.values())), 3)

    def test_repairs_toc_rows_fused_directly_after_previous_destination(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            source, translated, output, ir = self._make_toc_fixture(
                Path(directory),
                source_rows=[
                    ("3.1", "Alpha", "4", False),
                    ("3.2", "Beta", "5", False),
                    ("3.3", "Gamma", "5", False),
                ],
                translated_lines=[(150, "3.1甲.43.2乙.53.3丙.5")],
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertTrue(receipt["verified"], receipt["failures"])
            self.assertEqual(receipt["repaired_toc_group_count"], 1)
            self.assertEqual(
                [item["section_id"] for item in receipt["repaired_toc_rows"]],
                ["3.1", "3.2", "3.3"],
            )

    def test_toc_ids_use_exact_tokens_not_prefix_substrings(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            source, translated, output, ir = self._make_toc_fixture(
                Path(directory),
                source_rows=[
                    ("1.1", "Alpha", "4", False),
                    ("11.1", "Beta", "5", False),
                ],
                translated_lines=[
                    (150, "1.1 阿尔法"),
                    (516, 150, "4"),
                    (172, "11.1 贝塔"),
                    (516, 172, "5"),
                ],
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertTrue(receipt["verified"], receipt["failures"])
            self.assertEqual(receipt["repaired_toc_group_count"], 0)
            self.assertEqual(
                [item["section_id"] for item in receipt["toc_topology"]],
                ["1.1", "11.1"],
            )

    def test_fused_double_digit_toc_id_is_still_modeled(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            source, translated, output, ir = self._make_toc_fixture(
                Path(directory),
                source_rows=[
                    ("1.1", "Alpha", "4", False),
                    ("11.1", "Beta", "5", True),
                ],
                translated_lines=[(150, "1.1 阿尔法 4 11.1 贝塔 5")],
            )
            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertTrue(receipt["verified"], receipt["failures"])
            self.assertEqual(
                [item["section_id"] for item in receipt["toc_topology"]],
                ["1.1", "11.1"],
            )

    def test_repairs_merged_top_level_toc_rows(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            source, translated, output, ir = self._make_toc_fixture(
                Path(directory),
                source_rows=[
                    ("1", "Introduction", "2", False),
                    ("2", "Methods", "3", False),
                    ("3", "Results", "7", False),
                ],
                translated_lines=[(150, "1 引言 2 2 方法 3 3 结果 7")],
            )
            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertTrue(receipt["verified"], receipt["failures"])
            self.assertEqual(receipt["repaired_toc_group_count"], 1)
            self.assertEqual(
                [item["section_id"] for item in receipt["toc_topology"]],
                ["1", "2", "3"],
            )

    def test_repairs_individually_fused_top_level_toc_rows(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            source, translated, output, ir = self._make_toc_fixture(
                Path(directory),
                source_rows=[
                    ("1", "Introduction", "2", False),
                    ("2", "Methods", "3", False),
                ],
                translated_lines=[
                    (150, "1引言2"),
                    (172, "2方法3"),
                ],
            )
            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertTrue(receipt["verified"], receipt["failures"])
            self.assertEqual(receipt["repaired_toc_group_count"], 2)
            self.assertEqual(
                [item["section_id"] for item in receipt["repaired_toc_rows"]],
                ["1", "2"],
            )

    def test_does_not_repair_body_number_sequence_far_below_toc_row(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            source, translated, output, ir = self._make_toc_fixture(
                Path(directory),
                source_rows=[
                    ("1", "Introduction", "2", False),
                    ("2", "Methods", "3", False),
                ],
                translated_lines=[
                    (150, "1 引言"),
                    (516, 150, "2"),
                    (172, "2 方法"),
                    (516, 172, "3"),
                    (400, "1正文中的普通数字2"),
                ],
            )
            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertTrue(receipt["verified"], receipt["failures"])
            self.assertEqual(receipt["repaired_toc_group_count"], 0)
            with fitz.open(output) as protected:
                self.assertIn("正文中的普通数字", protected[0].get_text())

    def test_does_not_repair_same_band_numeric_text_outside_toc_margin(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            source, translated, output, ir = self._make_toc_fixture(
                Path(directory),
                source_rows=[
                    ("1", "Introduction", "2", False),
                    ("2", "Methods", "3", False),
                ],
                translated_lines=[
                    (300, 150, "1共有2"),
                    (300, 172, "2共有3"),
                ],
            )
            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertFalse(receipt["verified"])
            self.assertEqual(receipt["repaired_toc_group_count"], 0)
            with fitz.open(output) as protected:
                self.assertIn("共有", protected[0].get_text())

    def test_does_not_repair_same_band_text_that_does_not_start_with_section(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            source, translated, output, ir = self._make_toc_fixture(
                Path(directory),
                source_rows=[
                    ("1", "Introduction", "2", False),
                    ("2", "Methods", "3", False),
                ],
                translated_lines=[
                    (150, "附注1共有2"),
                    (172, "附注2共有3"),
                ],
            )
            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertFalse(receipt["verified"])
            self.assertEqual(receipt["repaired_toc_group_count"], 0)

    def test_toc_verification_rejects_wrong_destination(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            source, translated, output, ir = self._make_toc_fixture(
                Path(directory),
                source_rows=[
                    ("3.1", "Alpha", "4", False),
                    ("3.2", "Beta", "5", False),
                ],
                translated_lines=[
                    (150, "3.1 阿尔法 999"),
                    (172, "3.2 贝塔 998"),
                ],
            )
            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertFalse(receipt["verified"])
            self.assertTrue(
                all(not item["matched"] for item in receipt["toc_topology"])
            )
            self.assertEqual(
                receipt["failures"],
                [
                    "toc_topology_mismatch:output_page_1:3.1",
                    "toc_topology_mismatch:output_page_1:3.2",
                ],
            )

    def test_counts_each_merged_toc_group_on_one_page(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            source, translated, output, ir = self._make_toc_fixture(
                Path(directory),
                source_rows=[
                    ("3.1", "Alpha", "4", False),
                    ("3.2", "Beta", "5", False),
                    ("4.1", "Gamma", "6", False),
                    ("4.2", "Delta", "7", False),
                ],
                translated_lines=[
                    (150, "3.1 阿尔法 4 3.2 贝塔 5"),
                    (194, "4.1 伽马 6 4.2 德尔塔 7"),
                ],
            )
            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertTrue(receipt["verified"], receipt["failures"])
            self.assertEqual(receipt["repaired_toc_group_count"], 2)

    def test_extracts_numeric_citation_split_across_font_spans(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _numeric_citation_markers

        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Body [", fontname="helv")
            page.insert_text((108, 100), "1", fontname="tiro")
            page.insert_text((114, 100), ", 2] tail", fontname="helv")

            self.assertEqual(_numeric_citation_markers(page), ["[1,2]"])
        finally:
            document.close()

    def test_redraws_numeric_citations_with_a_safe_standard_font(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import (
            _normalize_numeric_citation_glyphs,
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "citations.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 100), "[3, 4, 5], ", fontname="helv")
            receipts = _normalize_numeric_citation_glyphs(document)
            document.save(output, garbage=4, deflate=True)
            document.close()

            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["text"], "[3, 4, 5], ")
            self.assertEqual(receipts[0]["font"], "Times-Roman")
            with fitz.open(output) as protected:
                spans = [
                    span
                    for block in protected[0].get_text("dict")["blocks"]
                    for line in block.get("lines", [])
                    for span in line["spans"]
                    if "[3, 4, 5]" in span["text"]
                ]
            self.assertEqual(len(spans), 1)
            self.assertEqual(spans[0]["font"], "Times-Roman")

    def test_fails_when_translated_numeric_citation_sequence_differs_from_source(
        self,
    ) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            output = root / "protected.pdf"
            ir = root / "ir.xml"
            for path, text in (
                (source, "Source body [1, 2]."),
                (translated, "Translated body [1, 3]."),
            ):
                document = fitz.open()
                page = document.new_page()
                page.insert_text((72, 100), text)
                document.save(path)
                document.close()
            ir.write_text(
                '<document totalPages="1"><page pageNumber="0"/></document>',
                encoding="utf-8",
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertFalse(receipt["verified"])
            self.assertEqual(receipt["citation_conservation"][0]["source"], ["[1,2]"])
            self.assertEqual(receipt["citation_conservation"][0]["output"], ["[1,3]"])
            self.assertIn("citation_sequence_mismatch:document", receipt["failures"])

    def test_allows_citations_to_reflow_across_page_breaks_without_order_drift(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            output = root / "protected.pdf"
            ir = root / "ir.xml"
            for path, pages in (
                (
                    source,
                    [["First claim [1]. Second claim [2]."], ["No citation here."]],
                ),
                (
                    translated,
                    [["First claim [1]."], ["Second claim [2]."]],
                ),
            ):
                document = fitz.open()
                for lines in pages:
                    page = document.new_page()
                    for y, text in enumerate(lines, start=500):
                        page.insert_text((72, y), text)
                document.save(path)
                document.close()
            ir.write_text(
                '<document totalPages="2"><page pageNumber="0"/><page pageNumber="1"/></document>',
                encoding="utf-8",
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1, 2),
                ir_xml=ir,
            )

            self.assertTrue(receipt["verified"], receipt["failures"])
            self.assertTrue(receipt["document_citation_conservation"]["matched"])

    def test_allows_citation_permutation_within_one_source_line(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            output = root / "protected.pdf"
            ir = root / "ir.xml"
            source_document = fitz.open()
            source_page = source_document.new_page()
            source_page.insert_text((72, 100), "SoCal Repo [7] uses XCache [1, 2].")
            source_document.save(source)
            source_document.close()
            translated_document = fitz.open()
            translated_page = translated_document.new_page()
            translated_page.insert_text((72, 100), "XCache [1, 2] powers")
            translated_page.insert_text((72, 114), "SoCal Repo [7].")
            translated_document.save(translated)
            translated_document.close()
            ir.write_text(
                '<document totalPages="1"><page pageNumber="0"/></document>',
                encoding="utf-8",
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            conservation = receipt["citation_conservation"][0]
            self.assertTrue(receipt["verified"], receipt["failures"])
            self.assertFalse(conservation["order_preserved"])
            self.assertTrue(conservation["within_source_line_permutation"])

    def test_rejects_citation_permutation_across_source_lines(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            output = root / "protected.pdf"
            ir = root / "ir.xml"
            for path, lines in (
                (source, [(100, "First claim [7]."), (114, "Second claim [1, 2].")]),
                (translated, [(100, "Second [1, 2]."), (114, "First [7].")]),
            ):
                document = fitz.open()
                page = document.new_page()
                for y, text in lines:
                    page.insert_text((72, y), text)
                document.save(path)
                document.close()
            ir.write_text(
                '<document totalPages="1"><page pageNumber="0"/></document>',
                encoding="utf-8",
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertFalse(receipt["verified"])
            self.assertFalse(
                receipt["citation_conservation"][0][
                    "within_source_line_permutation"
                ]
            )

    def test_rejects_cross_line_citation_mixing_with_repeated_marker(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            output = root / "protected.pdf"
            ir = root / "ir.xml"
            for path, lines in (
                (source, [(100, "L1 [5]."), (114, "L2 [5] and [7].")]),
                (translated, [(100, "L1 [5] and [7]."), (114, "L2 [5].")]),
            ):
                document = fitz.open()
                page = document.new_page()
                for y, text in lines:
                    page.insert_text((72, y), text)
                document.save(path)
                document.close()
            ir.write_text(
                '<document totalPages="1"><page pageNumber="0"/></document>',
                encoding="utf-8",
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertFalse(receipt["verified"])
            self.assertFalse(
                receipt["citation_conservation"][0][
                    "within_source_line_permutation"
                ]
            )

    def test_coalesces_same_baseline_citation_line_fragments(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            output = root / "protected.pdf"
            ir = root / "ir.xml"
            source_document = fitz.open()
            source_page = source_document.new_page()
            source_page.insert_text((72, 100), "First [1].")
            source_page.insert_text((170, 100), "Second [2].")
            source_document.save(source)
            source_document.close()
            translated_document = fitz.open()
            translated_page = translated_document.new_page()
            translated_page.insert_text((72, 100), "Second [2].")
            translated_page.insert_text((170, 100), "First [1].")
            translated_document.save(translated)
            translated_document.close()
            ir.write_text(
                '<document totalPages="1"><page pageNumber="0"/></document>',
                encoding="utf-8",
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
            )

            self.assertTrue(receipt["verified"], receipt["failures"])
            self.assertEqual(
                receipt["citation_conservation"][0]["source_line_groups"],
                [["[1]", "[2]"]],
            )

    def _write_lines(
        self,
        page: fitz.Page,
        x: float,
        y: float,
        lines: list[str],
        *,
        fontname: str = "helv",
        fontsize: float = 12,
    ) -> None:
        baseline = y
        for line in lines:
            page.insert_text(
                (x, baseline),
                line,
                fontname=fontname,
                fontsize=fontsize,
            )
            baseline += fontsize * 1.2

    def _write_html_block(
        self,
        page: fitz.Page,
        rectangle: fitz.Rect,
        html: str,
    ) -> None:
        spare_height, scale = page.insert_htmlbox(rectangle, html)
        self.assertGreaterEqual(spare_height, -1e-6)
        self.assertGreater(scale, 0)

    def test_coalesces_adjacent_verbatim_lines(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _coalesce_adjacent_text

        rectangles = [
            fitz.Rect(150, 210, 460, 224),
            fitz.Rect(170, 227, 440, 238),
            fitz.Rect(72, 320, 300, 380),
        ]

        merged = _coalesce_adjacent_text(rectangles)

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0], fitz.Rect(150, 210, 460, 238))

    def test_inserts_canonical_chinese_header_in_short_source_band(self) -> None:
        """Catch regressions for the 59x7 pt author header in arXiv:2203.06843."""

        from scripts.protect_snowmass_pdf2zh_output import _insert_header

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            source_rectangle = fitz.Rect(
                499.0989990234375,
                61.267860412597656,
                558.2018432617188,
                68.24166107177734,
            )
            page.insert_text(
                (source_rectangle.x0, source_rectangle.y1 - 1),
                "WRONG HEADER",
                fontsize=6,
            )

            _insert_header(page, source_rectangle, "Sim 和 Kissel 等人。")

            header_band = fitz.Rect(450, 45, 590, 82)
            rendered = page.get_text(clip=header_band, sort=True)
            matches = page.search_for("Sim 和 Kissel 等人。")
        finally:
            document.close()

        self.assertIn("Sim 和 Kissel 等人。", rendered)
        self.assertNotIn("WRONG HEADER", rendered)
        self.assertGreaterEqual(len(matches), 1)
        rendered_rectangle = fitz.Rect(matches[0])
        for match in matches[1:]:
            rendered_rectangle |= match
        safe_patch = fitz.Rect(
            source_rectangle.x0 - 2,
            source_rectangle.y0 - 1,
            source_rectangle.x1 + 2,
            source_rectangle.y1 + 0.5,
        )
        self.assertTrue(safe_patch.contains(rendered_rectangle))

    def test_reinserts_canonical_reference_heading_after_body_redaction(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _insert_reference_heading

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            heading_rectangle = fitz.Rect(53.8, 297.86, 123.21, 308.77)
            page.insert_text((54, 309), "damaged heading", fontsize=10)

            _insert_reference_heading(page, heading_rectangle)

            rendered = page.get_text(clip=fitz.Rect(45, 288, 150, 320), sort=True)
        finally:
            document.close()

        self.assertIn("参考文献", rendered)
        self.assertNotIn("damaged heading", rendered)

    def test_redaction_expands_to_a_target_block_overlapping_the_source_region(
        self,
    ) -> None:
        from scripts.protect_snowmass_pdf2zh_output import (
            _expanded_redaction_rectangle,
        )

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            page.insert_textbox(
                fitz.Rect(50, 110, 290, 190),
                "Translated reference text that wraps well below the source clip " * 3,
                fontsize=10,
            )
            expanded = _expanded_redaction_rectangle(
                page,
                fitz.Rect(50, 100, 290, 125),
            )
        finally:
            document.close()

        self.assertGreater(expanded.y1, 160)

    def test_reference_redaction_clears_reflow_to_the_end_of_its_column(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import (
            _reference_redaction_rectangle,
        )

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            redaction = _reference_redaction_rectangle(
                page,
                fitz.Rect(315, 90, 560, 180),
            )
        finally:
            document.close()

        self.assertGreaterEqual(redaction.y1, 740)
        self.assertLess(redaction.x0, 315)
        self.assertGreater(redaction.x1, 560)

    def test_reference_clip_uses_real_text_width_instead_of_fixed_margins(self) -> None:
        """Catch truncation when a paper's references extend left of 65 pt."""

        from scripts.protect_snowmass_pdf2zh_output import _reference_clips

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            page.insert_text((52, 110), "References", fontsize=12)
            page.insert_text(
                (53, 140),
                "[1] A. Author. Complete reference entry.",
                fontsize=10,
            )

            clips = _reference_clips(document)
        finally:
            document.close()

        self.assertIn(1, clips)
        self.assertEqual(len(clips[1]), 1)
        self.assertLessEqual(clips[1][0].x0, 52)
        self.assertGreaterEqual(clips[1][0].x1, 229)

    def test_reference_clips_follow_two_column_continuation(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _reference_clips

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            page.insert_text((52, 300), "References", fontsize=12)
            page.insert_text((53, 325), "[1] A. Author. Left-column entry.", fontsize=9)
            page.insert_text(
                (320, 90),
                "Translated body before references\n[2] B. Author. Right-column entry.",
                fontsize=9,
            )

            clips = _reference_clips(document)
        finally:
            document.close()

        self.assertEqual(len(clips[1]), 2)
        left, right = sorted(clips[1], key=lambda rectangle: rectangle.x0)
        self.assertLess(left.x0, 60)
        self.assertGreater(right.x0, 300)
        self.assertGreaterEqual(right.y0, 92.6)

    def test_reference_clip_keeps_adjacent_doi_tail_in_same_source_block(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _reference_clips

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            page.insert_text((52, 300), "References", fontsize=12)
            page.insert_text((53, 325), "[1] A. Author. Left-column entry.", fontsize=9)
            page.insert_text(
                (320, 90),
                "In Conference. https://doi.org/10.1000/example\n"
                "[2] B. Author. Right-column entry.",
                fontsize=9,
            )

            clips = _reference_clips(document)
        finally:
            document.close()

        right = max(clips[1], key=lambda rectangle: rectangle.x0)
        self.assertLessEqual(right.y0, 80.4)

    def test_reference_clips_bridge_unmarked_continuations_between_entries(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _reference_clips

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            page.insert_text((52, 300), "References", fontsize=12)
            page.insert_text((53, 325), "[1] A. Author. First line.", fontsize=9)
            page.insert_text(
                (53, 342),
                "Continuation without its own reference marker.",
                fontsize=9,
            )
            page.insert_text((53, 360), "[2] B. Author. Next entry.", fontsize=9)
            page.insert_text(
                (53, 378),
                "Final continuation after the last numbered entry.",
                fontsize=9,
            )

            clips = _reference_clips(document)
        finally:
            document.close()

        self.assertEqual(len(clips[1]), 1)
        self.assertLessEqual(clips[1][0].y0, 318.6)
        self.assertGreaterEqual(clips[1][0].y1, 381.0)

    def _make_auto_discovery_fixture(
        self,
        root: Path,
        *,
        header_variants: list[str],
        source_header_variants: list[str] | None = None,
        repeated_stamp: bool = False,
    ) -> tuple[Path, Path, Path]:
        source = root / "auto-source.pdf"
        translated = root / "auto-translated.pdf"
        ir = root / "auto-ir.xml"

        source_doc = fitz.open()
        translated_doc = fitz.open()

        first_source = source_doc.new_page(width=612, height=792)
        self._write_lines(
            first_source,
            140,
            130,
            ["Synthetic White Paper Title", "with Realistic Layout"],
            fontsize=20,
        )
        self._write_lines(
            first_source,
            150,
            220,
            ["Alice Author1, Bob Builder2"],
            fontsize=11,
        )
        self._write_lines(
            first_source,
            170,
            235,
            ["for the Example Topical Group"],
            fontsize=11,
        )
        self._write_lines(
            first_source,
            104,
            258,
            ["1Institute One, City", "2Institute Two, City"],
            fontsize=8,
        )
        self._write_lines(
            first_source,
            243,
            304,
            ["September 13, 2022"],
            fontsize=14,
        )
        self._write_lines(
            first_source,
            100,
            360,
            ["Abstract", "Source abstract body."],
            fontsize=11,
        )
        first_source.insert_text((300, 750), "1")

        first_translated = translated_doc.new_page(width=612, height=792)
        self._write_lines(
            first_translated,
            140,
            130,
            ["合成白皮书标题", "以及真实版式"],
            fontname="china-s",
            fontsize=20,
        )
        self._write_lines(
            first_translated,
            150,
            220,
            ["艾丽斯作者1、鲍勃建设者2"],
            fontname="china-s",
            fontsize=11,
        )
        self._write_lines(
            first_translated,
            150,
            235,
            ["代表示例专题组"],
            fontname="china-s",
            fontsize=11,
        )
        self._write_lines(
            first_translated,
            104,
            258,
            ["1研究所一，城市", "2研究所二，城市"],
            fontname="china-s",
            fontsize=8,
        )
        self._write_lines(
            first_translated,
            243,
            304,
            ["2022年9月13日"],
            fontname="china-s",
            fontsize=14,
        )
        self._write_lines(
            first_translated,
            100,
            360,
            ["摘要", "译文摘要正文。"],
            fontname="china-s",
            fontsize=11,
        )
        first_translated.insert_text((300, 750), "1")

        if source_header_variants is None:
            source_header_variants = ["SOURCE HEADER"] * len(header_variants)
        self.assertEqual(len(source_header_variants), len(header_variants))
        for page_number, (source_header_text, header_text) in enumerate(
            zip(source_header_variants, header_variants, strict=True), start=2
        ):
            source_page = source_doc.new_page(width=612, height=792)
            source_page.insert_text((150, 60), source_header_text)
            if repeated_stamp:
                source_page.insert_text((180, 88), "REPEATING STAMP")
            source_page.insert_text((72, 120), f"Source body page {page_number}")
            source_page.insert_text((300, 750), str(page_number))

            translated_page = translated_doc.new_page(width=612, height=792)
            translated_page.insert_text(
                (150, 60),
                header_text,
                fontname="china-s",
            )
            if repeated_stamp:
                translated_page.insert_text(
                    (180, 88),
                    "保留注记",
                    fontname="china-s",
                )
            translated_page.insert_text(
                (72, 120),
                f"译文正文第{page_number}页",
                fontname="china-s",
            )
            translated_page.insert_text((300, 750), str(page_number))

        source_doc.save(source)
        translated_doc.save(translated)
        source_doc.close()
        translated_doc.close()
        page_elements = "\n".join(
            f'  <page pageNumber="{page_number}"/>'
            for page_number in range(len(header_variants) + 1)
        )
        ir.write_text(
            f'<?xml version="1.0"?>\n'
            f'<document totalPages="{len(header_variants) + 1}">\n'
            f"{page_elements}\n"
            "</document>\n",
            encoding="utf-8",
        )
        return source, translated, ir

    def _make_dual_column_frontmatter_fixture(
        self,
        root: Path,
        *,
        header_variants: list[str],
    ) -> tuple[Path, Path, Path]:
        source = root / "dual-source.pdf"
        translated = root / "dual-translated.pdf"
        ir = root / "dual-ir.xml"

        source_doc = fitz.open()
        translated_doc = fitz.open()

        first_source = source_doc.new_page(width=612, height=792)
        self._write_lines(
            first_source,
            58,
            96,
            [
                "Deploying in-network caches in support of distributed scientific",
                "data sharing",
            ],
            fontsize=17.2,
        )
        self._write_html_block(
            first_source,
            fitz.Rect(95, 130, 270, 186),
            (
                "<div style='font-family: Helvetica; font-size: 12pt; text-align: center;'>"
                "Alex Sim<br>"
                "<span style='font-size: 10pt;'>Lawrence Berkeley National Laboratory</span><br>"
                "<span style='font-size: 10pt;'>Berkeley, California, USA</span><br>"
                "<span style='font-size: 10pt;'>asim@lbl.gov</span>"
                "</div>"
            ),
        )
        self._write_html_block(
            first_source,
            fitz.Rect(360, 130, 500, 186),
            (
                "<div style='font-family: Helvetica; font-size: 12pt; text-align: center;'>"
                "Ezra Kissel and Chin Guok<br>"
                "<span style='font-size: 10pt;'>Energy Sciences Network</span><br>"
                "<span style='font-size: 10pt;'>Berkeley, California, USA</span><br>"
                "<span style='font-size: 10pt;'>{kissel,chin}@es.net</span>"
                "</div>"
            ),
        )
        self._write_lines(
            first_source,
            54,
            198,
            ["ABSTRACT", "Source abstract body."],
            fontsize=11,
        )
        first_source.insert_text((300, 750), "1")

        first_translated = translated_doc.new_page(width=612, height=792)
        self._write_lines(
            first_translated,
            58,
            96,
            ["支持分布式科学数据共享的网内缓存部署"],
            fontname="china-s",
            fontsize=17.2,
        )
        self._write_html_block(
            first_translated,
            fitz.Rect(95, 130, 270, 186),
            (
                "<div style='font-family: China-S; font-size: 12pt; text-align: center;'>"
                "亚历克斯·西姆<br>"
                "<span style='font-size: 10pt;'>劳伦斯伯克利国家实验室</span><br>"
                "<span style='font-size: 10pt;'>伯克利，加利福尼亚，美国</span><br>"
                "<span style='font-size: 10pt;'>asim@lbl.gov</span>"
                "</div>"
            ),
        )
        self._write_html_block(
            first_translated,
            fitz.Rect(360, 130, 500, 186),
            (
                "<div style='font-family: China-S; font-size: 12pt; text-align: center;'>"
                "以斯拉·基塞尔与钱国<br>"
                "<span style='font-size: 10pt;'>能源科学网络</span><br>"
                "<span style='font-size: 10pt;'>伯克利，加利福尼亚，美国</span><br>"
                "<span style='font-size: 10pt;'>{kissel,chin}@es.net</span>"
                "</div>"
            ),
        )
        self._write_lines(
            first_translated,
            350,
            158,
            ["伯克利 {kissel,chin}@es.net"],
            fontname="china-s",
            fontsize=10,
        )
        self._write_lines(
            first_translated,
            54,
            198,
            ["摘要", "译文摘要正文。"],
            fontname="china-s",
            fontsize=11,
        )
        first_translated.insert_text((300, 750), "1")

        for page_number, header_text in enumerate(header_variants, start=2):
            source_page = source_doc.new_page(width=612, height=792)
            source_page.insert_text((499, 68), "Sim and Kissel, et al.", fontsize=7)
            source_page.insert_text((72, 120), f"Source body page {page_number}")
            source_page.insert_text((300, 750), str(page_number))

            translated_page = translated_doc.new_page(width=612, height=792)
            translated_page.insert_text(
                (499, 68),
                header_text,
                fontname="china-s",
                fontsize=7,
            )
            translated_page.insert_text(
                (72, 120),
                f"译文正文第{page_number}页",
                fontname="china-s",
            )
            translated_page.insert_text((300, 750), str(page_number))

        source_doc.save(source)
        translated_doc.save(translated)
        source_doc.close()
        translated_doc.close()
        ir.write_text(
            """<?xml version="1.0"?>
<document totalPages="4">
  <page pageNumber="0"/>
  <page pageNumber="1"/>
  <page pageNumber="2"/>
  <page pageNumber="3"/>
</document>
""",
            encoding="utf-8",
        )
        return source, translated, ir

    def _make_pdf(self, path: Path, *, translated: bool) -> None:
        doc = fitz.open()
        first = doc.new_page(width=612, height=792)
        first.insert_text((150, 60), "WRONG HEADER" if translated else "SOURCE HEADER")
        first.insert_text(
            (150, 100),
            "WRONG FRONTMATTER" if translated else "SOURCE FRONTMATTER",
        )
        first.insert_text(
            (150, 140), "translated author" if translated else "AUTHOR SOURCE"
        )
        first.insert_text(
            (72, 180), "translated table" if translated else "TABLE SOURCE"
        )
        first.insert_text(
            (72, 320), "translated figure" if translated else "FIGURE SOURCE"
        )
        if translated:
            first.insert_text((72, 420), "偏袒", fontname="china-s")
        first.insert_text((72, 460), "[3, 4, 5], ", fontname="helv")
        first.insert_text((300, 750), "1")
        second = doc.new_page(width=612, height=792)
        if translated:
            second.insert_text((150, 60), "WRONG HEADER")
            second.insert_text((72, 90), "参考文献", fontname="china-s")
            second.insert_text((72, 108), "已翻译正文", fontname="china-s")
        else:
            second.insert_text((150, 60), "SOURCE HEADER\nReferences")
            second.insert_text((72, 108), "HIDDEN SOURCE PROSE")
        second.insert_text(
            (72, 125),
            "[1] 中文标题。" if translated else "[1] A. Author. Original title.",
            fontname="china-s" if translated else "helv",
        )
        second.insert_text((300, 750), "2")
        doc.save(path)
        doc.close()

    def test_restores_protected_regions_and_unifies_headers(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            output = root / "protected.pdf"
            ir = root / "ir.xml"
            self._make_pdf(source, translated=False)
            self._make_pdf(translated, translated=True)
            ir.write_text(
                """<?xml version="1.0"?>
<document totalPages="2">
  <page pageNumber="0">
    <pageLayout id="7" class_name="table"><box x="60" y="590" x2="300" y2="630"/></pageLayout>
    <pdfXobject xobjId="4"><box x="60" y="430" x2="300" y2="490"/></pdfXobject>
    <pdfParagraph xobjId="4" unicode="FIGURE SOURCE"/>
  </page>
  <page pageNumber="1"/>
</document>
""",
                encoding="utf-8",
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1, 2),
                ir_xml=ir,
                source_header="SOURCE HEADER",
                target_header="统一页眉",
                fixed_replacements=((1, "SOURCE FRONTMATTER", "固定首页文字"),),
                verbatim_texts=((1, "AUTHOR SOURCE"),),
                output_replacements=((1, "偏袒", "偏置"),),
            )

            self.assertTrue(receipt["verified"])
            self.assertEqual(receipt["figure_region_count"], 1)
            self.assertEqual(receipt["table_region_count"], 1)
            self.assertEqual(receipt["reference_page_count"], 1)
            self.assertEqual(receipt["normalized_citation_glyph_count"], 1)
            self.assertEqual(
                receipt["normalized_citation_glyphs"][0]["font"], "Times-Roman"
            )
            self.assertTrue(
                receipt["normalized_citation_glyphs"][0]["rendered_clip_sha256"]
            )
            raster_regions = [
                region
                for region in receipt["protected_regions"]
                if region["render_mode"] == "rasterized_source_clip"
            ]
            self.assertGreaterEqual(len(raster_regions), 3)
            self.assertTrue(
                all(region["source_clip_pixel_sha256"] for region in raster_regions)
            )
            with fitz.open(output) as document:
                first = document[0].get_text()
                second = document[1].get_text()
            self.assertIn("统一页眉", first)
            self.assertIn("统一页眉", second)
            self.assertNotIn("WRONG HEADER", first + second)
            self.assertIn("固定首页文字", first)
            self.assertNotIn("WRONG FRONTMATTER", first)
            self.assertNotIn("AUTHOR SOURCE", first)
            self.assertNotIn("translated author", first)
            self.assertNotIn("TABLE SOURCE", first)
            self.assertNotIn("translated table", first)
            self.assertNotIn("FIGURE SOURCE", first)
            self.assertNotIn("translated figure", first)
            self.assertIn("偏置", first)
            self.assertNotIn("偏袒", first)
            self.assertIn("参考文献", second)
            self.assertNotIn("中文标题", second)
            self.assertEqual(receipt["reference_page_count"], 1)
            reference_regions = [
                region
                for region in raster_regions
                if region["source_page"] == 2 and region["bbox"][1] > 100
            ]
            self.assertEqual(len(reference_regions), 1)
            extracted = subprocess.run(
                ["pdftotext", "-layout", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertNotIn("SOURCE HEADER", extracted)
            self.assertNotIn("SOURCE FRONTMATTER", extracted)
            self.assertNotIn("HIDDEN SOURCE PROSE", extracted)
            self.assertIn("已翻译正文", extracted)

    def test_auto_mode_discovers_header_restores_front_matter_and_records_receipt(
        self,
    ) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, translated, ir = self._make_auto_discovery_fixture(
                root,
                header_variants=["标准页眉", "标准页眉", "另一种页眉"],
            )
            output = root / "auto-protected.pdf"

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1, 2, 3, 4),
                ir_xml=ir,
                auto_header=True,
                auto_front_matter=True,
                auto_header_min_recurrence=2,
            )

            self.assertTrue(receipt["verified"])
            self.assertEqual(
                receipt["ir_xml_sha256"], hashlib.sha256(ir.read_bytes()).hexdigest()
            )
            auto_header = receipt["auto_header"]
            self.assertEqual(auto_header["source_text"], "SOURCE HEADER")
            self.assertEqual(auto_header["canonical_target"], "标准页眉")
            self.assertEqual(auto_header["source_occurrence_count"], 3)
            self.assertEqual(
                auto_header["translated_candidates"][0]["count"],
                2,
            )
            self.assertEqual(
                auto_header["translated_candidates"][0]["display_text"],
                "标准页眉",
            )
            self.assertEqual(len(auto_header["rectangles"]), 3)
            self.assertIn("source_text_sha256", auto_header)

            auto_front_matter = receipt["auto_front_matter"]
            self.assertEqual(len(auto_front_matter["blocks"]), 4)
            self.assertEqual(
                [block["source_text"] for block in auto_front_matter["blocks"]],
                [
                    "Alice Author1, Bob Builder2",
                    "for the Example Topical Group",
                    "1Institute One, City",
                    "2Institute Two, City",
                ],
            )
            for block in auto_front_matter["blocks"]:
                self.assertIn("source_text_sha256", block)
                self.assertEqual(block["page"], 1)

            with fitz.open(output) as document:
                first = document[0].get_text()
                second = document[1].get_text()
                third = document[2].get_text()
                fourth = document[3].get_text()
            combined = second + third + fourth
            self.assertEqual(combined.count("标准页眉"), 3)
            self.assertNotIn("另一种页眉", combined)
            self.assertNotIn("Alice Author1, Bob Builder2", first)
            self.assertNotIn("for the Example Topical Group", first)
            self.assertNotIn("艾丽斯作者", first)
            self.assertNotIn("代表示例专题组", first)
            self.assertNotIn("研究所一", first)
            self.assertIn("2022年9月13日", first)

    def test_auto_header_fails_closed_when_multiple_source_banners_recur(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, translated, ir = self._make_auto_discovery_fixture(
                root,
                header_variants=["标准页眉", "标准页眉", "标准页眉"],
                repeated_stamp=True,
            )

            with self.assertRaisesRegex(
                RuntimeError, "Ambiguous recurring source header candidates"
            ):
                protect_pdf(
                    source_pdf=source,
                    translated_pdf=translated,
                    output_pdf=root / "ambiguous-source.pdf",
                    selected_source_pages=(1, 2, 3, 4),
                    ir_xml=ir,
                    auto_header=True,
                    auto_header_min_recurrence=2,
                )

    def test_running_header_discovery_ignores_repeated_plot_labels_below_margin(
        self,
    ) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_running_headers

        document = fitz.open()
        try:
            document.new_page(width=612, height=792)
            for _ in range(3):
                page = document.new_page(width=612, height=792)
                page.insert_text((72, 45), "SECTION RUNNING HEADER", fontsize=8)
                page.insert_text((180, 100), "REPEATED PLOT LABEL", fontsize=9)

            headers = _discover_running_headers(
                document,
                document,
                (1, 2, 3, 4),
                {page: page - 1 for page in range(1, 5)},
                min_recurrence=3,
            )
        finally:
            document.close()

        self.assertEqual(len(headers), 1)
        self.assertEqual(headers[0]["source_text"], "SECTION RUNNING HEADER")

    def test_running_header_discovery_ignores_sparse_top_figure_labels(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_running_headers

        document = fitz.open()
        try:
            for page_number in range(1, 9):
                page = document.new_page(width=612, height=792)
                if page_number in {2, 5, 8}:
                    page.insert_text((110, 25), "SURVEY FIGURE QUESTION", fontsize=8)

            headers = _discover_running_headers(
                document,
                document,
                tuple(range(1, 9)),
                {page: page - 1 for page in range(1, 9)},
                min_recurrence=3,
            )
        finally:
            document.close()

        self.assertEqual(headers, [])

    def test_running_header_discovery_accepts_two_line_header_components(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_running_headers

        document = fitz.open()
        try:
            document.new_page(width=612, height=792)
            for index in range(4):
                page = document.new_page(width=612, height=792)
                page.insert_text(
                    (90, 45),
                    "SNOWMASS FRONTIER WHITE PAPER",
                    fontsize=8,
                )
                if index < 3:
                    page.insert_text((150, 60), "RUNNING SUBTITLE", fontsize=8)

            headers = _discover_running_headers(
                document,
                document,
                (1, 2, 3, 4, 5),
                {page: page - 1 for page in range(1, 6)},
                min_recurrence=3,
            )
        finally:
            document.close()

        self.assertEqual(len(headers), 2)
        source_texts = {header["source_text"] for header in headers}
        self.assertIn("SNOWMASS FRONTIER WHITE PAPER", source_texts)
        self.assertIn("RUNNING SUBTITLE", source_texts)
        occurrence_counts = {
            header["source_text"]: header["source_occurrence_count"]
            for header in headers
        }
        self.assertEqual(occurrence_counts["SNOWMASS FRONTIER WHITE PAPER"], 4)
        self.assertEqual(occurrence_counts["RUNNING SUBTITLE"], 3)

    def test_auto_header_canonicalizes_disjoint_alternating_header_families(
        self,
    ) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, translated, ir = self._make_auto_discovery_fixture(
                root,
                source_header_variants=[
                    "AUTHOR RUNNING HEADER",
                    "PAPER TITLE RUNNING HEADER",
                    "AUTHOR RUNNING HEADER",
                    "PAPER TITLE RUNNING HEADER",
                ],
                header_variants=[
                    "作者页眉",
                    "论文标题页眉甲",
                    "作者页眉",
                    "论文标题页眉乙",
                ],
            )
            output = root / "alternating.pdf"

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1, 2, 3, 4, 5),
                ir_xml=ir,
                auto_header=True,
                auto_front_matter=True,
                auto_header_min_recurrence=2,
            )

            self.assertTrue(receipt["verified"])
            self.assertEqual(len(receipt["auto_headers"]), 2)
            self.assertEqual(receipt["canonical_header_count"], 4)
            with fitz.open(output) as document:
                rendered = [document[index].get_text() for index in range(1, 5)]
            self.assertIn("作者页眉", rendered[0])
            self.assertIn("作者页眉", rendered[2])
            self.assertIn("论文标题页眉甲", rendered[1])
            self.assertIn("论文标题页眉甲", rendered[3])
            self.assertNotIn("论文标题页眉乙", "".join(rendered))

    def test_auto_front_matter_rejects_arxiv_and_report_lines(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(page, 120, 130, ["Synthetic Paper Title"], fontsize=20)
            self._write_lines(page, 155, 220, ["Alice Author, Bob Builder"], fontsize=11)
            self._write_lines(page, 155, 240, ["arXiv:2203.07506 [hep-ph]"], fontsize=11)
            self._write_lines(page, 155, 260, ["PREPRINT REPORT 2022-17"], fontsize=11)
            self._write_lines(page, 100, 340, ["Abstract", "Body"], fontsize=11)

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        self.assertEqual(
            [block["source_text"] for block in discovered],
            ["Alice Author, Bob Builder"],
        )

    def test_auto_front_matter_groups_wrapped_authors_into_one_contact_column(
        self,
    ) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(
                page,
                80,
                100,
                ["Synthetic Paper Title for a Major Scientific Study"],
                fontsize=18,
            )
            self._write_lines(
                page,
                125,
                150,
                [
                    "Alice Author, Bob Builder, Carol Contributor,",
                    "David Developer, Erin Expert",
                ],
                fontsize=12,
            )
            self._write_lines(
                page,
                180,
                185,
                ["Example National Laboratory", "authors@example.org"],
                fontsize=10,
            )
            self._write_lines(page, 72, 250, ["Abstract", "Body"], fontsize=11)

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        self.assertEqual(len(discovered), 1)
        self.assertIn("Alice Author", discovered[0]["source_text"])
        self.assertIn("David Developer", discovered[0]["source_text"])
        self.assertIn("Example National Laboratory", discovered[0]["source_text"])
        self.assertIn("authors@example.org", discovered[0]["source_text"])

    def test_auto_front_matter_accepts_an_author_affiliation_combined_line(
        self,
    ) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(
                page,
                100,
                100,
                ["A Wide Scientific Title for Neutrino Analysis"],
                fontsize=18,
            )
            self._write_lines(
                page,
                180,
                150,
                [
                    "C. Backhouse - University College London",
                    "c.backhouse@example.org",
                ],
                fontsize=12,
            )
            self._write_lines(page, 260, 190, ["25 March 2022"], fontsize=12)
            self._write_lines(page, 72, 230, ["Ordinary body prose starts here."], fontsize=10)

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        self.assertEqual(len(discovered), 1)
        self.assertIn("C. Backhouse", discovered[0]["source_text"])
        self.assertIn("c.backhouse@example.org", discovered[0]["source_text"])

    def test_auto_front_matter_strict_path_keeps_separate_affiliation_lines(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(
                page,
                90,
                100,
                ["A Wide Scientific Title for Detector Research"],
                fontsize=18,
            )
            self._write_lines(page, 150, 150, ["A. Author, B. Builder"], fontsize=12)
            self._write_lines(
                page,
                120,
                175,
                ["Department of Physics, Example University"],
                fontsize=10,
            )
            self._write_lines(page, 270, 230, ["Abstract", "Body"], fontsize=11)

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        identity_text = "\n".join(block["source_text"] for block in discovered)
        self.assertIn("A. Author", identity_text)
        self.assertIn("Department of Physics", identity_text)

    def test_auto_front_matter_keeps_short_institution_address(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(
                page,
                90,
                100,
                ["A Wide Scientific Title for Collider Research"],
                fontsize=18,
            )
            self._write_lines(page, 150, 150, ["A. Author, B. Builder"], fontsize=12)
            self._write_lines(
                page,
                190,
                175,
                ["CERN, Geneva, Switzerland"],
                fontsize=10,
            )
            self._write_lines(page, 270, 230, ["Abstract", "Body"], fontsize=11)

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        identity_text = "\n".join(block["source_text"] for block in discovered)
        self.assertIn("A. Author", identity_text)
        self.assertIn("CERN, Geneva, Switzerland", identity_text)

    def test_auto_front_matter_accepts_tightly_stacked_author_affiliation(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(
                page,
                121,
                70,
                ["A Cost-Effective Upgrade Path for the Fermilab Accelerator Complex"],
                fontsize=14,
            )
            self._write_lines(
                page,
                168,
                87,
                ["S. Nagaitsev and V. Lebedev, Fermilab, Batavia, IL 60510, USA"],
                fontsize=11,
            )
            self._write_lines(page, 86, 131, ["Abstract", "Body"], fontsize=12)

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        self.assertEqual(len(discovered), 1)
        self.assertIn("S. Nagaitsev", discovered[0]["source_text"])

    def test_auto_front_matter_uses_executive_summary_as_identity_boundary(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(
                page,
                72,
                77,
                [
                    "Application-driven engagement with universities, synergies",
                    "with other funding agencies",
                ],
                fontsize=20,
            )
            self._write_lines(
                page,
                72,
                144,
                [
                    "Jim Hoff (jimhoff@fnal.gov)",
                    "Seda Memik (seda@northwestern.edu)",
                ],
                fontsize=11,
            )
            self._write_lines(page, 72, 188, ["Executive Summary", "Body"], fontsize=16)

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        identity_text = "\n".join(block["source_text"] for block in discovered)
        self.assertIn("Jim Hoff", identity_text)
        self.assertIn("Seda Memik", identity_text)
        self.assertNotIn("Executive Summary", identity_text)

    def test_auto_front_matter_accepts_a_narrow_title(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(page, 216, 136, ["Particle Flow Calorimetry"], fontsize=16)
            self._write_lines(
                page,
                216,
                196,
                ["Randal Ruchti, Katja Kruger"],
                fontsize=11,
            )
            self._write_lines(
                page,
                144,
                228,
                ["Department of Physics, University of Notre Dame"],
                fontsize=11,
            )
            self._write_lines(page, 283, 313, ["ABSTRACT", "Body"], fontsize=11)

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        identity_text = "\n".join(block["source_text"] for block in discovered)
        self.assertIn("Randal Ruchti", identity_text)
        self.assertIn("Department of Physics", identity_text)

    def test_auto_front_matter_accepts_collaboration_only_identity(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(
                page,
                105,
                130,
                ["Research and Development for Future LHCb Physics"],
                fontsize=24,
            )
            self._write_lines(
                page,
                250,
                240,
                ["LHCb collaboration4", "EF4,AF2,CF7"],
                fontsize=12,
            )
            self._write_lines(
                page,
                100,
                340,
                [
                    "Abstract",
                    "The LHCb collaboration develops detectors and studies particle decays.",
                    "This body paragraph must remain outside the protected identity region.",
                ],
                fontsize=11,
            )

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        identity_text = "\n".join(block["source_text"] for block in discovered)
        self.assertIn("LHCb collaboration4", identity_text)
        self.assertIn("EF4,AF2,CF7", identity_text)
        self.assertNotIn("Research and Development", identity_text)
        self.assertNotIn("develops detectors", identity_text)

    def test_auto_front_matter_finds_author_list_after_abstract(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(
                page,
                100,
                100,
                ["Key directions for superconducting radio frequency cavities"],
                fontsize=17,
            )
            self._write_lines(page, 275, 180, ["ABSTRACT", "Ordinary abstract prose."], fontsize=11)
            self._write_lines(
                page,
                120,
                500,
                [
                    "S. Belomestnykh and S. Posen",
                    "D. Bafia, S. Balachandran, M. Bertucci, A. Burrill",
                ],
                fontsize=11,
            )

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        identity_text = "\n".join(block["source_text"] for block in discovered)
        self.assertIn("S. Belomestnykh", identity_text)
        self.assertIn("D. Bafia", identity_text)
        self.assertNotIn("Ordinary abstract prose", identity_text)

    def test_auto_front_matter_rejects_isolated_author_like_body_prose(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(page, 100, 100, ["A Wide Scientific Paper Title"], fontsize=17)
            self._write_lines(page, 275, 180, ["ABSTRACT", "Ordinary abstract prose."], fontsize=11)
            self._write_lines(
                page,
                90,
                500,
                [
                    "S. Example and J. Sample demonstrate the calibration strategy used below."
                ],
                fontsize=11,
            )

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        identity_text = "\n".join(block["source_text"] for block in discovered)
        self.assertNotIn("calibration strategy", identity_text)

    def test_front_matter_redaction_preserves_translated_title_and_abstract(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            output = root / "protected.pdf"
            ir = root / "ir.xml"

            source_document = fitz.open()
            source_page = source_document.new_page(width=612, height=792)
            self._write_lines(source_page, 100, 100, ["A Wide Scientific Paper Title"], fontsize=18)
            self._write_lines(source_page, 150, 150, ["A. Author, B. Builder"], fontsize=12)
            self._write_lines(
                source_page,
                120,
                175,
                ["Department of Physics, Example University"],
                fontsize=10,
            )
            self._write_lines(source_page, 270, 230, ["Abstract", "Source body"], fontsize=11)
            source_document.save(source)
            source_document.close()

            translated_document = fitz.open()
            translated_page = translated_document.new_page(width=612, height=792)
            translated_page.insert_textbox(
                fitz.Rect(90, 80, 520, 260),
                "TRANSLATED TITLE\n\n\nTRANSLATED AUTHORS\n"
                "TRANSLATED AFFILIATIONS\n\nABSTRACT\nTRANSLATED ABSTRACT BODY",
                fontsize=12,
            )
            translated_document.save(translated)
            translated_document.close()
            ir.write_text("<document />", encoding="utf-8")

            protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1,),
                ir_xml=ir,
                auto_header=True,
                auto_front_matter=True,
            )

            with fitz.open(output) as protected:
                rendered = protected[0].get_text(sort=True)

        self.assertIn("TRANSLATED TITLE", rendered)
        self.assertIn("TRANSLATED ABSTRACT BODY", rendered)
        self.assertNotIn("TRANSLATED AUTHORS", rendered)

    def test_auto_front_matter_allows_a_first_page_without_identity_text(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            self._write_lines(page, 170, 150, ["The Forward Physics Facility"], fontsize=21)
            self._write_lines(
                page,
                72,
                240,
                ["Ordinary body prose begins immediately below the title."],
                fontsize=11,
            )

            discovered = _discover_front_matter_lines(page)
        finally:
            document.close()

        self.assertEqual(discovered, [])

    def test_auto_front_matter_fallback_does_not_mask_unknown_runtime_errors(
        self,
    ) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _discover_front_matter_lines

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            with mock.patch(
                "scripts.protect_snowmass_pdf2zh_output."
                "_discover_front_matter_lines_strict",
                side_effect=RuntimeError("unexpected protection failure"),
            ), self.assertRaisesRegex(RuntimeError, "unexpected protection failure"):
                _discover_front_matter_lines(page)
        finally:
            document.close()

    def test_auto_header_fails_closed_on_ambiguous_majority_vote(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, translated, ir = self._make_auto_discovery_fixture(
                root,
                header_variants=["标准页眉", "另一种页眉", "标准页眉", "另一种页眉"],
            )
            output = root / "ambiguous.pdf"

            with self.assertRaisesRegex(RuntimeError, "Ambiguous canonical running header"):
                protect_pdf(
                    source_pdf=source,
                    translated_pdf=translated,
                    output_pdf=output,
                    selected_source_pages=(1, 2, 3, 4, 5),
                    ir_xml=ir,
                    auto_header=True,
                    auto_header_min_recurrence=2,
                )

    def test_auto_header_resolves_only_near_duplicate_tied_variants(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, translated, ir = self._make_auto_discovery_fixture(
                root,
                header_variants=[
                    "标准页眉。",
                    "标准页眉",
                    "标准页眉！",
                ],
            )
            output = root / "near-duplicate-tie.pdf"

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1, 2, 3, 4),
                ir_xml=ir,
                auto_header=True,
                auto_header_min_recurrence=2,
            )

            self.assertTrue(receipt["verified"])
            self.assertEqual(
                receipt["auto_header"]["canonical_target"],
                "标准页眉",
            )
            with fitz.open(output) as document:
                rendered = "".join(document[index].get_text() for index in range(1, 4))
            self.assertEqual(rendered.count("标准页眉"), 3)
            self.assertNotIn("标准页眉。", rendered)

    def test_two_page_paper_allows_front_matter_protection_without_header_vote(
        self,
    ) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, translated, ir = self._make_auto_discovery_fixture(
                root,
                header_variants=["单页页眉"],
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=root / "two-page.pdf",
                selected_source_pages=(1, 2),
                ir_xml=ir,
                auto_header=False,
                auto_front_matter=True,
            )

            self.assertTrue(receipt["verified"])
            self.assertEqual(receipt["canonical_header_count"], 0)

    def test_auto_header_allows_a_multi_page_source_with_no_running_header(
        self,
    ) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source-without-header.pdf"
            translated = root / "translated-without-header.pdf"
            ir = root / "without-header.xml"
            for path, prefix in ((source, "Source"), (translated, "译文")):
                document = fitz.open()
                for page_number in range(1, 4):
                    page = document.new_page(width=612, height=792)
                    page.insert_text(
                        (72, 180),
                        f"{prefix} body page {page_number}",
                        fontname="helv" if path == source else "china-s",
                    )
                document.save(path)
                document.close()
            ir.write_text(
                "<?xml version='1.0'?><document totalPages='3'>"
                "<page pageNumber='0'/><page pageNumber='1'/><page pageNumber='2'/>"
                "</document>",
                encoding="utf-8",
            )

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=root / "protected.pdf",
                selected_source_pages=(1, 2, 3),
                ir_xml=ir,
                auto_header=True,
                auto_header_min_recurrence=2,
            )

            self.assertTrue(receipt["verified"])
            self.assertEqual(receipt["auto_headers"], [])
            self.assertEqual(receipt["canonical_header_count"], 0)

    def test_main_accepts_auto_flags_without_explicit_headers(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, translated, ir = self._make_auto_discovery_fixture(
                root,
                header_variants=["标准页眉", "标准页眉", "另一种页眉"],
            )
            output = root / "main-protected.pdf"
            receipt_path = root / "receipt.json"

            exit_code = main(
                [
                    "--source-pdf",
                    str(source),
                    "--translated-pdf",
                    str(translated),
                    "--output-pdf",
                    str(output),
                    "--selected-source-pages",
                    "1,2,3,4",
                    "--ir-xml",
                    str(ir),
                    "--auto-header",
                    "--auto-front-matter",
                    "--auto-header-min-recurrence",
                    "2",
                    "--receipt",
                    str(receipt_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertTrue(receipt["verified"])
            self.assertEqual(receipt["auto_header"]["canonical_target"], "标准页眉")
            self.assertEqual(len(receipt["auto_front_matter"]["blocks"]), 4)

    def test_auto_front_matter_restores_authors_and_contact_lines_in_dual_columns(
        self,
    ) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, translated, ir = self._make_dual_column_frontmatter_fixture(
                root,
                header_variants=["规范页眉", "规范页眉", "变体页眉"],
            )
            output = root / "dual-protected.pdf"

            receipt = protect_pdf(
                source_pdf=source,
                translated_pdf=translated,
                output_pdf=output,
                selected_source_pages=(1, 2, 3, 4),
                ir_xml=ir,
                auto_header=True,
                auto_front_matter=True,
                auto_header_min_recurrence=2,
            )

            self.assertTrue(receipt["verified"])
            identity_blocks = receipt["auto_front_matter"]["blocks"]
            self.assertEqual(len(identity_blocks), 2)
            identity_text = "\n".join(
                block["source_text"] for block in identity_blocks
            )
            for expected in (
                "Alex Sim",
                "Lawrence Berkeley National Laboratory",
                "asim@lbl.gov",
                "Ezra Kissel and Chin Guok",
                "Energy Sciences Network",
                "{kissel,chin}@es.net",
            ):
                self.assertIn(expected, identity_text)
            self.assertTrue(
                all(
                    block["bbox"][2] - block["bbox"][0] > 120
                    and block["bbox"][3] - block["bbox"][1] > 40
                    for block in identity_blocks
                )
            )
            self.assertEqual(
                receipt["auto_header"]["source_text"], "Sim and Kissel, et al."
            )

            with fitz.open(output) as document:
                first = document[0].get_text()
                combined = document[1].get_text() + document[2].get_text() + document[3].get_text()

            self.assertNotIn("Alex Sim", first)
            self.assertNotIn("Ezra Kissel and Chin Guok", first)
            self.assertNotIn("亚历克斯·西姆", first)
            self.assertNotIn("以斯拉·基塞尔", first)
            self.assertNotIn("伯克利 {kissel,chin}@es.net", first)
            self.assertNotIn("劳伦斯伯克利国家实验室", first)
            self.assertNotIn("能源科学网络", first)
            self.assertNotIn("Lawrence Berkeley National Laboratory", first)
            self.assertNotIn("Energy Sciences Network", first)
            self.assertEqual(combined.count("规范页眉"), 3)
            self.assertNotIn("变体页眉", combined)

    def test_closes_documents_when_validation_raises(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, translated, ir = self._make_auto_discovery_fixture(
                root, header_variants=["统一页眉", "统一页眉", "统一页眉"]
            )
            real_open = fitz.open
            opened: list[fitz.Document] = []

            def tracking_open(*args: object, **kwargs: object) -> fitz.Document:
                document = real_open(*args, **kwargs)
                opened.append(document)
                return document

            with mock.patch(
                "scripts.protect_snowmass_pdf2zh_output.fitz.open",
                side_effect=tracking_open,
            ), self.assertRaisesRegex(RuntimeError, "threshold"):
                protect_pdf(
                    source_pdf=source,
                    translated_pdf=translated,
                    output_pdf=root / "unused.pdf",
                    selected_source_pages=(1, 2, 3, 4),
                    ir_xml=ir,
                    auto_header=True,
                    auto_header_min_recurrence=1,
                )

            self.assertEqual(len(opened), 2)
            self.assertTrue(all(document.is_closed for document in opened))


if __name__ == "__main__":
    unittest.main()
