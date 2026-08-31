#!/usr/bin/env python3
"""Tests for reusable Snowmass packaged-PDF visual and residue QA."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz

from scripts.audit_snowmass_translation_pdf import (
    audit_pdf,
    secondary_extractor_identity,
)


class PackagedPdfAuditTests(unittest.TestCase):
    def test_secondary_extractor_identity_is_explicit(self) -> None:
        identity = secondary_extractor_identity()

        self.assertTrue(Path(identity["executable"]).is_file())
        self.assertIn("pdftotext version", identity["version"])

    def test_missing_pdf_returns_structured_failure_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.pdf"

            report = audit_pdf(missing)

            self.assertFalse(report["ok"])
            self.assertIsNone(report["pdf_sha256"])
            self.assertTrue(
                any(item.startswith("unreadable_pdf:") for item in report["failures"])
            )

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

            report = audit_pdf(
                pdf,
                expected_pages=2,
                contact_sheet_path=sheet,
                protection_receipt_sha256="a" * 64,
            )

            self.assertTrue(report["ok"])
            self.assertEqual(report["page_count"], 2)
            self.assertEqual(report["failures"], [])
            self.assertEqual(report["secondary_extractor"], secondary_extractor_identity())
            self.assertEqual(report["protection_receipt_sha256"], "a" * 64)
            self.assertTrue(sheet.is_file())
            self.assertEqual(
                report["contact_sheet_sha256"],
                hashlib.sha256(sheet.read_bytes()).hexdigest(),
            )

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

    def test_low_text_page_is_allowed_when_most_of_it_is_a_protected_raster(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "protected-reference-page.pdf"
            self._write_pdf(pdf, [""])

            report = audit_pdf(
                pdf,
                expected_pages=1,
                ignored_text_regions={1: [(50.0, 50.0, 550.0, 750.0)]},
            )

            self.assertTrue(report["ok"])
            self.assertEqual(report["low_text_pages"], [])

    def test_image_only_page_is_not_classified_as_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "image-only-reference-page.pdf"
            document = fitz.open()
            page = document.new_page(width=595, height=842)
            page.draw_rect((72, 72, 520, 770), color=(0, 0, 0), fill=(1, 1, 1))
            document.save(pdf)
            document.close()

            report = audit_pdf(pdf, expected_pages=1)

            self.assertTrue(report["ok"])
            self.assertEqual(report["low_text_pages"], [])

    def test_rejects_isolated_latin_word_at_right_page_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "paper.pdf"
            document = fitz.open()
            page = document.new_page(width=595, height=842)
            page.insert_textbox(
                (72, 72, 500, 300),
                "这是完整的中文学术正文，用于确保页面具有足够的可提取文本。",
                fontfile="/System/Library/Fonts/STHeiti Medium.ttc",
                fontname="testcjk",
                fontsize=12,
            )
            page.insert_text((505, 700), "will", fontsize=10)
            document.save(pdf)
            document.close()

            report = audit_pdf(pdf)

            self.assertFalse(report["ok"])
            self.assertIn("isolated_latin_edge_word:will:page_1", report["failures"])

            protected = audit_pdf(
                pdf,
                ignored_text_regions={1: [(490.0, 680.0, 550.0, 730.0)]},
            )
            self.assertTrue(protected["ok"])
            self.assertEqual(protected["isolated_latin_edge_words"], [])

    def test_allows_source_proven_edge_proper_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            output = root / "output.pdf"
            self._write_pdf(source, ["Scuola"])
            document = fitz.open()
            page = document.new_page(width=595, height=842)
            page.insert_text((72, 200), "中文正文", fontname="china-s")
            page.insert_text((72, 740), "Scuola", fontsize=10)
            document.save(output)
            document.close()

            report = audit_pdf(output, source_pdf=source)

            self.assertTrue(report["ok"])
            self.assertEqual(report["isolated_latin_edge_words"], [])

    def test_malformed_pdf_returns_a_failed_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "broken.pdf"
            pdf.write_bytes(b"not a pdf")

            report = audit_pdf(pdf, expected_pages=1)

            self.assertFalse(report["ok"])
            self.assertEqual(report["page_count"], None)
            self.assertTrue(
                any(
                    failure.startswith("unreadable_pdf:")
                    for failure in report["failures"]
                )
            )

    def test_rejects_mixed_lowercase_fragment_near_page_bottom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "fragment.pdf"
            document = fitz.open()
            page = document.new_page(width=595, height=842)
            page.insert_text((72, 100), "完整正文", fontname="china-s", fontsize=12)
            page.insert_text(
                (72, 740),
                "组合 ation of spectroscopic measurements",
                fontname="china-s",
                fontsize=10,
            )
            document.save(pdf)
            document.close()

            report = audit_pdf(pdf)

            self.assertFalse(report["ok"])
            self.assertIn("mixed_script_bottom_fragment:page_1", report["failures"])

    def test_rejects_multiple_unknown_english_fragments_above_bottom_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_pdf = root / "source.pdf"
            output_pdf = root / "output.pdf"
            source = fitz.open()
            source_page = source.new_page(width=595, height=842)
            source_page.insert_text((72, 700), "Affiliation with a PUI faculty member")
            source.save(source_pdf)
            source.close()
            output = fitz.open()
            output_page = output.new_page(width=595, height=842)
            output_page.insert_text(
                (72, 680),
                "Affilia 与一个非PUI研究组的合作也使PUI教职人员更有可能……\nber",
                fontname="china-s",
                fontsize=10,
            )
            output.save(output_pdf)
            output.close()

            report = audit_pdf(output_pdf, source_pdf=source_pdf)

            self.assertFalse(report["ok"])
            self.assertIn("cross_page_english_fragment:page_1", report["failures"])

    def test_allows_source_url_and_product_name_in_bottom_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "url-label.pdf"
            document = fitz.open()
            page = document.new_page(width=1000, height=842)
            page.insert_text(
                (72, 740),
                "2cms-xcache 镜像：https://hub.docker.com/r/opensciencegrid/cms-xcache",
                fontname="china-s",
                fontsize=10,
            )
            document.save(pdf)
            document.close()

            report = audit_pdf(pdf)

            self.assertTrue(report["ok"])
            self.assertNotIn("mixed_script_bottom_fragment:page_1", report["failures"])

    def test_allows_two_word_technical_term_in_bottom_caption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "technical-term.pdf"
            document = fitz.open()
            page = document.new_page(width=595, height=842)
            page.insert_text((72, 740), "此理论属于 cohomological type。", fontname="china-s", fontsize=10)
            document.save(pdf)
            document.close()

            report = audit_pdf(pdf)

            self.assertTrue(report["ok"])
            self.assertNotIn("mixed_script_bottom_fragment:page_1", report["failures"])

    def test_allows_scientific_unit_token_in_bottom_caption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "unit-caption.pdf"
            document = fitz.open()
            page = document.new_page(width=595, height=842)
            page.insert_text(
                (72, 740),
                "图 3：1-MeV-neq 通量图，参数见表 1。",
                fontname="china-s",
                fontsize=10,
            )
            document.save(pdf)
            document.close()

            report = audit_pdf(pdf)

            self.assertTrue(report["ok"])

    def test_rejects_long_english_prose_residue_inside_chinese_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "fragment.pdf"
            document = fitz.open()
            page = document.new_page(width=595, height=842)
            page.insert_textbox(
                (72, 360, 520, 520),
                "这是中文正文 provide compelling science reach on their own 并继续中文。",
                fontname="china-s",
                fontsize=12,
            )
            document.save(pdf)
            document.close()

            report = audit_pdf(pdf)

            self.assertFalse(report["ok"])
            self.assertIn("english_prose_residue:page_1", report["failures"])

    def test_allows_academic_english_terms_in_chinese_parentheticals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "terms.pdf"
            self._write_pdf(
                pdf,
                ["标准模型（standard model）与暗物质（dark matter）是本文讨论对象。"],
            )

            report = audit_pdf(pdf)

            self.assertTrue(report["ok"])
            self.assertEqual(report["english_prose_residue"], [])

    def test_allows_cpp_namespace_identifiers_before_chinese_prose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "cpp-identifiers.pdf"
            document = fitz.open()
            first = document.new_page(width=595, height=842)
            first.insert_textbox(
                (72, 100, 520, 300),
                "这是中文首页的正常学术正文，用于确认页面具有足够的可提取文本。" * 8,
                fontname="china-s",
                fontsize=12,
            )
            second = document.new_page(width=595, height=842)
            second.insert_textbox(
                (72, 180, 520, 260),
                "std::parallel::execution（std::par）自 C++17 标准以来一直是 C++ 的一部分。",
                fontname="china-s",
                fontsize=12,
            )
            document.save(pdf)
            document.close()

            report = audit_pdf(pdf)

            self.assertTrue(report["ok"])
            self.assertEqual(report["english_prose_residue"], [])

    def test_rejects_english_only_prose_block_after_first_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "fragment.pdf"
            document = fitz.open()
            first = document.new_page(width=595, height=842)
            first.insert_text((72, 100), "中文首页", fontname="china-s")
            second = document.new_page(width=595, height=842)
            second.insert_text(
                (72, 180), "While high redshift spectroscopic surveys provide compelling science"
            )
            document.save(pdf)
            document.close()

            report = audit_pdf(pdf)

            self.assertFalse(report["ok"])
            self.assertIn("english_prose_residue:page_2", report["failures"])

    def test_rejects_excess_hidden_text_seen_by_secondary_extractor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "hidden.pdf"
            self._write_pdf(pdf, ["这是可见的中文正文，长度正常。"])

            with mock.patch(
                "scripts.audit_snowmass_translation_pdf._secondary_page_texts",
                return_value=["hidden english duplicate layer " * 80],
            ):
                report = audit_pdf(pdf)

            self.assertFalse(report["ok"])
            self.assertIn("secondary_text_layer_excess:page_1", report["failures"])

    def test_rejects_small_secondary_only_english_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "hidden.pdf"
            self._write_pdf(pdf, ["这是可见的中文正文，长度正常。"])

            with mock.patch(
                "scripts.audit_snowmass_translation_pdf._secondary_page_texts",
                return_value=["quiet hidden english text layer residue"],
            ):
                report = audit_pdf(pdf)

            self.assertFalse(report["ok"])
            self.assertIn("secondary_text_layer_excess:page_1", report["failures"])

    def test_rejects_standalone_punctuation_fragment_near_page_bottom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "fragment.pdf"
            document = fitz.open()
            page = document.new_page(width=595, height=842)
            page.insert_text((72, 100), "完整正文", fontname="china-s", fontsize=12)
            page.insert_text((72, 740), "s.", fontsize=10)
            document.save(pdf)
            document.close()

            report = audit_pdf(pdf)

            self.assertFalse(report["ok"])
            self.assertIn("isolated_latin_edge_word:s.:page_1", report["failures"])


if __name__ == "__main__":
    unittest.main()
