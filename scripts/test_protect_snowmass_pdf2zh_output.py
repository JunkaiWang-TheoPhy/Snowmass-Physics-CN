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

    def _make_auto_discovery_fixture(
        self,
        root: Path,
        *,
        header_variants: list[str],
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

        for page_number, header_text in enumerate(header_variants, start=2):
            source_page = source_doc.new_page(width=612, height=792)
            source_page.insert_text((150, 60), "SOURCE HEADER")
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
            self.assertEqual(len(auto_front_matter["blocks"]), 2)
            self.assertEqual(
                [block["source_text"] for block in auto_front_matter["blocks"]],
                [
                    "Alice Author1, Bob Builder2",
                    "for the Example Topical Group",
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
            self.assertIn("研究所一", first)
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
            self.assertEqual(len(receipt["auto_front_matter"]["blocks"]), 2)

    def test_auto_front_matter_restores_only_author_lines_in_dual_column_blocks(
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
            self.assertEqual(
                [block["source_text"] for block in receipt["auto_front_matter"]["blocks"]],
                ["Alex Sim", "Ezra Kissel and Chin Guok"],
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
            self.assertIn("劳伦斯伯克利国家实验室", first)
            self.assertIn("能源科学网络", first)
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
