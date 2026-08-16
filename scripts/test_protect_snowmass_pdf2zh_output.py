from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

import fitz


class ProtectPdf2zhOutputTests(unittest.TestCase):
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
        else:
            second.insert_text((150, 60), "SOURCE HEADER\nReferences")
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
            with fitz.open(output) as document:
                first = document[0].get_text()
                second = document[1].get_text()
            self.assertIn("统一页眉", first)
            self.assertIn("统一页眉", second)
            self.assertNotIn("WRONG HEADER", first + second)
            self.assertIn("固定首页文字", first)
            self.assertNotIn("WRONG FRONTMATTER", first)
            self.assertIn("AUTHOR SOURCE", first)
            self.assertNotIn("translated author", first)
            self.assertIn("TABLE SOURCE", first)
            self.assertNotIn("translated table", first)
            self.assertIn("FIGURE SOURCE", first)
            self.assertNotIn("translated figure", first)
            self.assertIn("偏置", first)
            self.assertNotIn("偏袒", first)
            self.assertIn("参考文献", second)
            self.assertIn("[1] A. Author. Original title.", second)
            self.assertNotIn("中文标题", second)


if __name__ == "__main__":
    unittest.main()
