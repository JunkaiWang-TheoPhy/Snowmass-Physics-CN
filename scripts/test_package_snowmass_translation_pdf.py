#!/usr/bin/env python3
"""Tests for Snowmass translation PDF cover packaging."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

import fitz
from pypdf import PdfReader


MODULE_PATH = Path(__file__).with_name("package_snowmass_translation_pdf.py")
SPEC = importlib.util.spec_from_file_location("package_snowmass_translation_pdf", MODULE_PATH)
PACKAGER = None
if SPEC and SPEC.loader and MODULE_PATH.exists():
    PACKAGER = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = PACKAGER
    SPEC.loader.exec_module(PACKAGER)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PackageSnowmassTranslationPdfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_pdf = self.root / "source.pdf"
        self.output_pdf = self.root / "packaged.pdf"
        self.write_source_pdf(self.source_pdf, ["Original page 1", "Original page 2"])

    def require_module(self):
        if PACKAGER is None:
            self.fail(f"Missing packager module: {MODULE_PATH}")
        return PACKAGER

    def write_source_pdf(self, path: Path, pages: list[str]) -> None:
        document = fitz.open()
        for text in pages:
            page = document.new_page()
            page.insert_textbox((72, 72, 480, 200), text, fontsize=18)
        document.save(path)
        document.close()

    def write_cjk_source_pdf(self, path: Path, text: str) -> None:
        document = fitz.open()
        page = document.new_page()
        page.insert_textbox(
            (72, 72, 520, 200),
            text,
            fontfile=str(self.require_module().SYSTEM_CJK_FONT),
            fontname="testcjk",
            fontsize=12,
        )
        document.save(path)
        document.close()

    def base_record(self, **overrides: object) -> dict[str, object]:
        record = {
            "record_id": "arxiv:2111.06932",
            "title": "A Cost-Efective Upgrade Path for the Fermilab Accelerator Complex",
            "authors_as_listed": "S. Nagaitsev et al.",
            "source_url": "https://arxiv.org/abs/2111.06932",
            "source_license": "CC-BY-4.0",
            "source_license_url": "https://creativecommons.org/licenses/by/4.0/",
            "publication_allowed": True,
            "publication_conditions": ["attribution", "indicate-changes"],
        }
        record.update(overrides)
        return record

    def visual_record(self, **overrides: object) -> dict[str, object]:
        record = self.base_record(
            record_id="arxiv:2203.07506",
            title="Cosmology and Fundamental Physics from the Three-Dimensional Large Scale Structure",
            authors_as_listed="D. N. Spergel et al.",
            source_url="https://arxiv.org/abs/2203.07506",
        )
        record.update(overrides)
        return record

    def render_cover_page(
        self,
        *,
        record: dict[str, object] | None = None,
        chinese_title: str = "三维大尺度结构中的宇宙学与基础物理",
        version: str = "v3.0",
    ) -> fitz.Page:
        packager = self.require_module()
        packager.package_translation_pdf(
            record=record or self.visual_record(),
            chinese_title=chinese_title,
            source_pdf_path=self.source_pdf,
            output_pdf_path=self.output_pdf,
            version=version,
            packaged_on=dt.date(2026, 8, 11),
        )
        document = fitz.open(self.output_pdf)
        self.addCleanup(document.close)
        return document[0]

    def union_rect_for_text(self, page: fitz.Page, text: str) -> fitz.Rect:
        rects = page.search_for(text)
        self.assertTrue(rects, f"missing text: {text}")
        rect = fitz.Rect(rects[0])
        for current in rects[1:]:
            rect |= current
        return rect

    def qr_rect(self, page: fitz.Page) -> fitz.Rect:
        rects: list[fitz.Rect] = []
        for image in page.get_images(full=True):
            rects.extend(page.get_image_rects(image[0]))
        candidates = [rect for rect in rects if rect.x0 > 350]
        self.assertTrue(candidates, f"missing QR image rects: {rects}")
        return max(candidates, key=lambda rect: rect.get_area())

    def test_requires_literal_publication_allowed_true_and_non_empty_chinese_title(self) -> None:
        packager = self.require_module()

        with self.assertRaisesRegex(ValueError, "publication_allowed"):
            packager.package_translation_pdf(
                record=self.base_record(publication_allowed=False),
                chinese_title="中文标题",
                source_pdf_path=self.source_pdf,
                output_pdf_path=self.output_pdf,
                version="v1.0",
                packaged_on=dt.date(2026, 8, 11),
            )

    def test_rejects_unresolved_babeldoc_placeholder_in_cover_title(self) -> None:
        packager = self.require_module()

        with self.assertRaisesRegex(ValueError, "placeholder"):
            packager.package_translation_pdf(
                record=self.base_record(),
                chinese_title="拟议的 e{v1}e{v2} 希格斯工厂",
                source_pdf_path=self.source_pdf,
                output_pdf_path=self.output_pdf,
                version="v1.1",
                packaged_on=dt.date(2026, 8, 11),
            )

        self.assertFalse(self.output_pdf.exists())

        with self.assertRaisesRegex(ValueError, "publication_allowed"):
            packager.package_translation_pdf(
                record=self.base_record(publication_allowed=1),
                chinese_title="中文标题",
                source_pdf_path=self.source_pdf,
                output_pdf_path=self.output_pdf,
                version="v1.0",
                packaged_on=dt.date(2026, 8, 11),
            )

        with self.assertRaisesRegex(ValueError, "Chinese title"):
            packager.package_translation_pdf(
                record=self.base_record(),
                chinese_title="  ",
                source_pdf_path=self.source_pdf,
                output_pdf_path=self.output_pdf,
                version="v1.0",
                packaged_on=dt.date(2026, 8, 11),
            )

    def test_rejects_same_resolved_source_and_output_path_before_any_write(self) -> None:
        packager = self.require_module()
        alias_output = self.root / "source-alias.pdf"
        alias_output.symlink_to(self.source_pdf)
        source_hash = sha256_file(self.source_pdf)

        with self.assertRaisesRegex(ValueError, "must differ"):
            packager.package_translation_pdf(
                record=self.base_record(),
                chinese_title="相同路径回归测试",
                source_pdf_path=self.source_pdf,
                output_pdf_path=alias_output,
                version="v1.1",
                packaged_on=dt.date(2026, 8, 11),
            )

        self.assertEqual(sha256_file(self.source_pdf), source_hash)
        self.assertFalse((self.root / "source-alias.cover.pdf").exists())
        self.assertFalse((self.root / "source-alias.json").exists())

    def test_accepts_explicit_portable_font_and_prebuilt_paper_qr(self) -> None:
        packager = self.require_module()
        qr = self.root / "paper-qr.png"
        qr.write_bytes(packager.DEFAULT_QR_IMAGE_PATH.read_bytes())

        receipt = packager.package_translation_pdf(
            record=self.visual_record(),
            chinese_title="可移植装订测试",
            source_pdf_path=self.source_pdf,
            output_pdf_path=self.output_pdf,
            version="v3.1",
            packaged_on=dt.date(2026, 8, 13),
            cjk_font_path=packager.SYSTEM_CJK_FONT,
            paper_qr_image_path=qr,
        )

        self.assertTrue(self.output_pdf.is_file())
        self.assertEqual(receipt["packaged_pdf_sha256"], sha256_file(self.output_pdf))

    def test_blocks_known_mistranslation_in_source_pdf_before_packaging(self) -> None:
        packager = self.require_module()
        self.write_cjk_source_pdf(
            self.source_pdf,
            "劳伦斯伯克利国家实验室，旋风加速器路一号",
        )

        with self.assertRaisesRegex(ValueError, "旋风加速器.*回旋加速器"):
            packager.package_translation_pdf(
                record=self.base_record(),
                chinese_title="已知误译阻断测试",
                source_pdf_path=self.source_pdf,
                output_pdf_path=self.output_pdf,
                version="v1.1",
                packaged_on=dt.date(2026, 8, 11),
            )

        self.assertFalse(self.output_pdf.exists())
        self.assertFalse((self.root / "packaged.cover.pdf").exists())
        self.assertFalse((self.root / "packaged.json").exists())

    def test_blocks_pdf_when_no_text_can_be_extracted_for_forbidden_scan(self) -> None:
        packager = self.require_module()
        document = fitz.open()
        page = document.new_page()
        page.draw_rect((72, 72, 300, 200), fill=(0.2, 0.3, 0.4))
        document.save(self.source_pdf)
        document.close()

        with self.assertRaisesRegex(ValueError, "no extractable text"):
            packager.package_translation_pdf(
                record=self.base_record(),
                chinese_title="不可抽取文本阻断测试",
                source_pdf_path=self.source_pdf,
                output_pdf_path=self.output_pdf,
                version="v1.1",
                packaged_on=dt.date(2026, 8, 11),
            )

        self.assertFalse(self.output_pdf.exists())
        self.assertFalse((self.root / "packaged.cover.pdf").exists())
        self.assertFalse((self.root / "packaged.json").exists())

    def test_cover_text_blocks_occupy_ordered_non_overlapping_vertical_regions(self) -> None:
        page = self.render_cover_page()
        ordered_rects = [
            self.union_rect_for_text(page, "Snowmass White Paper Chinese Translation Collaboration"),
            self.union_rect_for_text(page, "三维大尺度结构中的宇宙学与基础物理"),
            self.union_rect_for_text(page, "Cosmology and Fundamental Physics from the Three-Dimensional Large Scale Structure"),
            self.union_rect_for_text(page, "原作者：D. N. Spergel et al."),
            self.union_rect_for_text(page, "arXiv: 2203.07506"),
            self.union_rect_for_text(page, "本译文由中文翻译协作项目制作，不代表原作者审定或认可；如有歧义，以英文原文为准。"),
            self.union_rect_for_text(page, "原文许可证：CC-BY-4.0"),
            self.union_rect_for_text(page, "*Contact: WangTheoPhys@outlook.com"),
        ]
        for previous, current in zip(ordered_rects, ordered_rects[1:]):
            self.assertLessEqual(previous.y1, current.y0 + 0.5, (previous, current))

    def test_chinese_title_is_visible_below_banner_region(self) -> None:
        page = self.render_cover_page()
        title_rect = self.union_rect_for_text(page, "三维大尺度结构中的宇宙学与基础物理")
        banner_rect = fitz.Rect(36, 36, 559, 156)
        self.assertGreaterEqual(title_rect.y0, banner_rect.y1 + 8, title_rect)

    def test_qr_rectangle_is_within_page_and_does_not_overlap_text(self) -> None:
        page = self.render_cover_page()
        qr_rect = self.qr_rect(page)
        page_rect = page.rect
        self.assertGreaterEqual(qr_rect.x0, page_rect.x0)
        self.assertGreaterEqual(qr_rect.y0, page_rect.y0)
        self.assertLessEqual(qr_rect.x1, page_rect.x1)
        self.assertLessEqual(qr_rect.y1, page_rect.y1)

        for text in [
            "snowmass-physics-cn.netlify.app/paper/2203.07506/",
            "本译文由中文翻译协作项目制作，不代表原作者审定或认可；如有歧义，以英文原文为准。",
            "原文许可证：CC-BY-4.0",
            "适用条件：署名；注明修改",
        ]:
            self.assertTrue((qr_rect & self.union_rect_for_text(page, text)).is_empty, text)

    def test_banner_rendering_is_not_predominantly_black(self) -> None:
        page = self.render_cover_page()
        clip = fitz.Rect(180, 90, 430, 160)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1, 1), clip=clip, alpha=False)
        dark_pixels = 0
        total_pixels = len(pixmap.samples) // 3
        for offset in range(0, len(pixmap.samples), 3):
            red = pixmap.samples[offset]
            green = pixmap.samples[offset + 1]
            blue = pixmap.samples[offset + 2]
            luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
            if luminance < 30:
                dark_pixels += 1
        self.assertLess(dark_pixels / total_pixels, 0.45)

    def test_receipt_uses_portable_artifact_references_instead_of_absolute_paths(self) -> None:
        packager = self.require_module()

        receipt = packager.package_translation_pdf(
            record=self.base_record(),
            chinese_title="便携路径回归测试",
            source_pdf_path=self.source_pdf,
            output_pdf_path=self.output_pdf,
            version="v4.0",
            packaged_on=dt.date(2026, 8, 11),
        )

        persisted_receipt = json.loads(self.output_pdf.with_suffix(".json").read_text(encoding="utf-8"))
        for key, expected in {
            "source_pdf_path": "source.pdf",
            "cover_pdf_path": "packaged.cover.pdf",
            "output_pdf_path": "packaged.pdf",
            "receipt_path": "packaged.json",
        }.items():
            self.assertEqual(receipt[key], expected)
            self.assertEqual(persisted_receipt[key], expected)
            self.assertFalse(Path(receipt[key]).is_absolute(), key)

    def test_rerun_with_identical_inputs_produces_identical_cover_and_receipt_bytes(self) -> None:
        packager = self.require_module()

        first = packager.package_translation_pdf(
            record=self.base_record(),
            chinese_title="可复现性回归测试",
            source_pdf_path=self.source_pdf,
            output_pdf_path=self.output_pdf,
            version="v4.1",
            packaged_on=dt.date(2026, 8, 11),
        )
        first_cover = self.output_pdf.with_name(f"{self.output_pdf.stem}.cover.pdf").read_bytes()
        first_receipt = self.output_pdf.with_suffix(".json").read_bytes()
        first_cover_hash = sha256_file(self.output_pdf.with_name(f"{self.output_pdf.stem}.cover.pdf"))
        first_receipt_hash = sha256_file(self.output_pdf.with_suffix(".json"))
        first_packaged_hash = sha256_file(self.output_pdf)

        second = packager.package_translation_pdf(
            record=self.base_record(),
            chinese_title="可复现性回归测试",
            source_pdf_path=self.source_pdf,
            output_pdf_path=self.output_pdf,
            version="v4.1",
            packaged_on=dt.date(2026, 8, 11),
        )
        second_cover = self.output_pdf.with_name(f"{self.output_pdf.stem}.cover.pdf").read_bytes()
        second_receipt = self.output_pdf.with_suffix(".json").read_bytes()
        second_cover_hash = sha256_file(self.output_pdf.with_name(f"{self.output_pdf.stem}.cover.pdf"))
        second_receipt_hash = sha256_file(self.output_pdf.with_suffix(".json"))
        second_packaged_hash = sha256_file(self.output_pdf)

        self.assertEqual(first_cover_hash, second_cover_hash)
        self.assertEqual(first_receipt_hash, second_receipt_hash)
        self.assertEqual(first_packaged_hash, second_packaged_hash)
        self.assertEqual(first_cover, second_cover)
        self.assertEqual(first_receipt, second_receipt)
        self.assertEqual(first, second)

    def test_long_title_overflow_fails_before_producing_publication_artifacts(self) -> None:
        packager = self.require_module()
        long_title = "超长标题回归测试" * 40

        with self.assertRaisesRegex(ValueError, "text overflow"):
            packager.package_translation_pdf(
                record=self.base_record(),
                chinese_title=long_title,
                source_pdf_path=self.source_pdf,
                output_pdf_path=self.output_pdf,
                version="v4.2",
                packaged_on=dt.date(2026, 8, 11),
            )

        self.assertFalse(self.output_pdf.exists())
        self.assertFalse(self.output_pdf.with_name(f"{self.output_pdf.stem}.cover.pdf").exists())
        self.assertFalse(self.output_pdf.with_suffix(".json").exists())

    def test_long_but_publishable_title_is_fitted_without_shortening(self) -> None:
        packager = self.require_module()
        title = "Snowmass2021 宇宙学前沿白皮书：充分发挥旗舰暗能量实验的全部潜力"

        packager.package_translation_pdf(
            record=self.base_record(),
            chinese_title=title,
            source_pdf_path=self.source_pdf,
            output_pdf_path=self.output_pdf,
            version="v4.3",
            packaged_on=dt.date(2026, 8, 11),
        )

        cover = fitz.open(self.output_pdf.with_name(f"{self.output_pdf.stem}.cover.pdf"))
        try:
            self.assertIn(
                "".join(title.split()),
                "".join(cover[0].get_text().split()),
            )
        finally:
            cover.close()

    def test_prepends_cover_with_visible_fields_links_qr_and_receipt_hashes(self) -> None:
        packager = self.require_module()

        receipt = packager.package_translation_pdf(
            record=self.base_record(),
            chinese_title="费米实验室加速器复合体的低成本升级路径",
            source_pdf_path=self.source_pdf,
            output_pdf_path=self.output_pdf,
            version="v1.0",
            packaged_on=dt.date(2026, 8, 11),
        )

        cover_pdf_path = self.output_pdf.with_name(f"{self.output_pdf.stem}.cover.pdf")
        receipt_path = self.output_pdf.with_suffix(".json")
        self.assertEqual(receipt["record_id"], "arxiv:2111.06932")
        self.assertEqual(
            receipt["packaging_contract_version"],
            packager.PACKAGING_CONTRACT_VERSION,
        )
        self.assertEqual(receipt["version"], "v1.0")
        self.assertEqual(receipt["packaged_on"], "2026-08-11")
        self.assertEqual(receipt["source_pdf_path"], "source.pdf")
        self.assertEqual(receipt["cover_pdf_path"], "packaged.cover.pdf")
        self.assertEqual(receipt["output_pdf_path"], "packaged.pdf")
        self.assertEqual(receipt["receipt_path"], "packaged.json")
        self.assertTrue(self.output_pdf.is_file())
        self.assertTrue(cover_pdf_path.is_file())
        self.assertTrue(receipt_path.is_file())

        output_reader = PdfReader(str(self.output_pdf))
        source_reader = PdfReader(str(self.source_pdf))
        self.assertEqual(len(output_reader.pages), len(source_reader.pages) + 1)
        self.assertIn("Original page 1", output_reader.pages[1].extract_text())
        self.assertIn("Original page 2", output_reader.pages[2].extract_text())

        cover_document = fitz.open(self.output_pdf)
        cover_page = cover_document[0]
        cover_text = cover_page.get_text("text")
        cover_document.close()
        self.assertIn("Snowmass White Paper Chinese Translation Collaboration", cover_text)
        self.assertIn("费米实验室加速器复合体的低成本升级路径", cover_text)
        self.assertIn("A Cost-Efective Upgrade Path for the Fermilab Accelerator Complex", cover_text)
        self.assertIn("S. Nagaitsev et al.", cover_text)
        self.assertIn("中文翻译贡献者：WangTheoPhys*", cover_text)
        self.assertIn("TRANSLATION PAGE / 本论文译文页", cover_text)
        self.assertIn("snowmass-physics-cn.netlify.app/paper/2111.06932/", cover_text)
        self.assertIn("arXiv: 2111.06932", cover_text)
        self.assertIn("DOI: 未提供 / Not Provided", cover_text)
        self.assertIn("原文许可证：CC-BY-4.0", cover_text)
        self.assertIn("适用条件：署名；注明修改", cover_text)
        self.assertIn("本译文由中文翻译协作项目制作，不代表原作者审定或认可；如有歧义，以英文原文为准。", cover_text)
        self.assertIn("*Contact: WangTheoPhys@outlook.com", cover_text)

        cover_reader = PdfReader(str(self.output_pdf))
        annotations = cover_reader.pages[0].get("/Annots")
        self.assertIsNotNone(annotations)
        uris = {
            annotation.get_object()["/A"]["/URI"]
            for annotation in annotations
            if annotation.get_object().get("/A")
        }
        self.assertIn("https://arxiv.org/abs/2111.06932", uris)
        self.assertIn("https://creativecommons.org/licenses/by/4.0/", uris)
        self.assertIn("https://snowmass-physics-cn.netlify.app/paper/2111.06932/", uris)

        qr_probe = fitz.open(self.output_pdf)
        self.assertGreaterEqual(len(qr_probe[0].get_images(full=True)), 1)
        qr_probe.close()

        persisted_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted_receipt["translation_page_url"],
            "https://snowmass-physics-cn.netlify.app/paper/2111.06932/",
        )
        self.assertEqual(persisted_receipt["source_pdf_sha256"], sha256_file(self.source_pdf))
        self.assertEqual(persisted_receipt["cover_pdf_sha256"], sha256_file(cover_pdf_path))
        self.assertEqual(persisted_receipt["packaged_pdf_sha256"], sha256_file(self.output_pdf))
        self.assertEqual(persisted_receipt, receipt)

    def test_includes_dedicated_clickable_arxiv_field_distinct_from_original_link(self) -> None:
        packager = self.require_module()
        source_url = "https://example.org/papers/original.pdf"

        packager.package_translation_pdf(
            record=self.base_record(source_url=source_url),
            chinese_title="带 arXiv 字段的测试标题",
            source_pdf_path=self.source_pdf,
            output_pdf_path=self.output_pdf,
            version="v1.2",
            packaged_on=dt.date(2026, 8, 11),
        )

        reader = PdfReader(str(self.output_pdf))
        annotations = reader.pages[0].get("/Annots")
        self.assertIsNotNone(annotations)
        uris = {
            annotation.get_object()["/A"]["/URI"]
            for annotation in annotations
            if annotation.get_object().get("/A")
        }
        self.assertIn(source_url, uris)
        self.assertIn("https://arxiv.org/abs/2111.06932", uris)

        document = fitz.open(self.output_pdf)
        cover_text = document[0].get_text("text")
        document.close()
        self.assertIn("原文链接：https://example.org/papers/original.pdf", cover_text)
        self.assertIn("arXiv: 2111.06932", cover_text)
        self.assertNotIn("arXiv: 未提供", cover_text)

    def test_includes_clickable_doi_when_record_provides_one(self) -> None:
        packager = self.require_module()

        packager.package_translation_pdf(
            record=self.base_record(doi="10.1234/example-doi"),
            chinese_title="带 DOI 的测试标题",
            source_pdf_path=self.source_pdf,
            output_pdf_path=self.output_pdf,
            version="v2.0",
            packaged_on=dt.date(2026, 8, 11),
        )

        reader = PdfReader(str(self.output_pdf))
        annotations = reader.pages[0].get("/Annots")
        self.assertIsNotNone(annotations)
        uris = {
            annotation.get_object()["/A"]["/URI"]
            for annotation in annotations
            if annotation.get_object().get("/A")
        }
        self.assertIn("https://doi.org/10.1234/example-doi", uris)

        document = fitz.open(self.output_pdf)
        cover_text = document[0].get_text("text")
        document.close()
        self.assertIn("DOI: 10.1234/example-doi", cover_text)

    def test_derives_old_style_arxiv_from_source_url_without_truncating_category(self) -> None:
        packager = self.require_module()

        packager.package_translation_pdf(
            record=self.base_record(
                record_id="",
                source_url="https://arxiv.org/abs/hep-ph/9709356v2?download=1#page=3",
            ),
            chinese_title="旧式 arXiv 回归测试",
            source_pdf_path=self.source_pdf,
            output_pdf_path=self.output_pdf,
            version="v2.1",
            packaged_on=dt.date(2026, 8, 11),
        )

        reader = PdfReader(str(self.output_pdf))
        annotations = reader.pages[0].get("/Annots")
        self.assertIsNotNone(annotations)
        uris = {
            annotation.get_object()["/A"]["/URI"]
            for annotation in annotations
            if annotation.get_object().get("/A")
        }
        self.assertIn("https://arxiv.org/abs/hep-ph/9709356", uris)
        self.assertNotIn("https://arxiv.org/abs/hep-ph", uris)

        document = fitz.open(self.output_pdf)
        cover_text = document[0].get_text("text")
        document.close()
        cover_lines = cover_text.splitlines()
        self.assertIn("arXiv: hep-ph/9709356", cover_lines)
        self.assertNotIn("arXiv: hep-ph", cover_lines)

    def test_derives_modern_arxiv_from_source_url_while_stripping_valid_version_suffix(self) -> None:
        packager = self.require_module()

        packager.package_translation_pdf(
            record=self.base_record(
                record_id="",
                source_url="https://arxiv.org/abs/2111.06932v3?context=hep-ex#section",
            ),
            chinese_title="现代 arXiv 回归测试",
            source_pdf_path=self.source_pdf,
            output_pdf_path=self.output_pdf,
            version="v2.2",
            packaged_on=dt.date(2026, 8, 11),
        )

        reader = PdfReader(str(self.output_pdf))
        annotations = reader.pages[0].get("/Annots")
        self.assertIsNotNone(annotations)
        uris = {
            annotation.get_object()["/A"]["/URI"]
            for annotation in annotations
            if annotation.get_object().get("/A")
        }
        self.assertIn("https://arxiv.org/abs/2111.06932", uris)
        self.assertNotIn("https://arxiv.org/abs/2111.06932v3", uris)

        document = fitz.open(self.output_pdf)
        cover_text = document[0].get_text("text")
        document.close()
        cover_lines = cover_text.splitlines()
        self.assertIn("arXiv: 2111.06932", cover_lines)
        self.assertNotIn("arXiv: 2111.06932v3", cover_lines)


if __name__ == "__main__":
    unittest.main()
