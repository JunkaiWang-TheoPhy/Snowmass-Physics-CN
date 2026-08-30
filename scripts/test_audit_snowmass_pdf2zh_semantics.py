from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import fitz


class Pdf2zhSemanticAuditTests(unittest.TestCase):
    def test_ignores_glossary_residue_on_reference_continuation_pages(self) -> None:
        from scripts.audit_snowmass_pdf2zh_semantics import audit_semantics

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            glossary = root / "glossary.csv"
            protection = root / "protection.json"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 100), "正文 detector")
            page.insert_text((72, 200), "References")
            page.insert_text((72, 300), "[1] detector and dark energy")
            page = document.new_page()
            page.insert_text((72, 100), "[2] detector and dark energy")
            document.save(pdf)
            document.close()
            glossary.write_text(
                "source,target\ndetector,探测器\ndark energy,暗能量\n",
                encoding="utf-8",
            )
            protection.write_text(
                json.dumps({"verified": True, "protected_regions": []}),
                encoding="utf-8",
            )
            report = audit_semantics(
                pdf, glossary_csv=glossary, protection_receipt=protection
            )
            self.assertEqual(
                report["failures"], ["untranslated_glossary:detector:page_1"]
            )

    def test_reference_heading_ignores_glossary_residue_after_heading(self) -> None:
        from scripts.audit_snowmass_pdf2zh_semantics import audit_semantics

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            glossary = root / "glossary.csv"
            protection = root / "protection.json"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 100), "正文 detector")
            page.insert_text((72, 200), "References:")
            page.insert_text((72, 300), "[1] detector and Intensity Frontier")
            document.save(pdf)
            document.close()
            glossary.write_text(
                "source,target\ndetector,探测器\nIntensity Frontier,强度前沿\n",
                encoding="utf-8",
            )
            protection.write_text(
                json.dumps({"verified": True, "protected_regions": []}),
                encoding="utf-8",
            )
            report = audit_semantics(
                pdf, glossary_csv=glossary, protection_receipt=protection
            )
            self.assertFalse(report["ok"])
            self.assertEqual(
                report["failures"], ["untranslated_glossary:detector:page_1"]
            )

    def test_rejects_untranslated_term_but_ignores_protected_reference(self) -> None:
        from scripts.audit_snowmass_pdf2zh_semantics import audit_semantics

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            glossary = root / "glossary.csv"
            protection = root / "protection.json"
            document = fitz.open()
            page = document.new_page()
            page.insert_text(
                (72, 100), "轻 relics 粒子和星系偏袒", fontname="china-s"
            )
            page.insert_text((72, 300), "References: light relics")
            document.save(pdf)
            document.close()
            with glossary.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["source", "target", "tgt_lng"])
                writer.writeheader()
                writer.writerow({"source": "relics", "target": "遗迹粒子", "tgt_lng": "zh"})
            protection.write_text(
                json.dumps(
                    {
                        "verified": True,
                        "protected_regions": [
                            {"output_page": 1, "bbox": [60, 270, 400, 330]}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = audit_semantics(
                pdf, glossary_csv=glossary, protection_receipt=protection
            )

            self.assertFalse(report["ok"])
            self.assertIn("untranslated_glossary:relics:page_1", report["failures"])
            self.assertIn("forbidden:偏袒:page_1", report["failures"])
            self.assertEqual(len(report["findings"]), 2)

    def test_phrase_allowance_does_not_whitelist_the_term_globally(self) -> None:
        from scripts.audit_snowmass_pdf2zh_semantics import audit_semantics

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            glossary = root / "glossary.csv"
            protection = root / "protection.json"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 100), "Simons Observatory and another observatory")
            document.save(pdf)
            document.close()
            with glossary.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["source", "target"])
                writer.writeheader()
                writer.writerow({"source": "observatory", "target": "天文台"})
            protection.write_text(
                json.dumps({"verified": True, "protected_regions": []}),
                encoding="utf-8",
            )

            report = audit_semantics(
                pdf,
                glossary_csv=glossary,
                protection_receipt=protection,
                allowed_untranslated_phrases=("Simons Observatory",),
            )

            self.assertFalse(report["ok"])
            self.assertEqual(len(report["findings"]), 1)
            self.assertEqual(report["findings"][0]["term"], "observatory")

    def test_phrase_allowance_tolerates_pdf_line_breaks(self) -> None:
        from scripts.audit_snowmass_pdf2zh_semantics import audit_semantics

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            glossary = root / "glossary.csv"
            protection = root / "protection.json"
            document = fitz.open()
            page = document.new_page()
            page.insert_textbox((72, 72, 300, 150), "Simons\nObservatory")
            document.save(pdf)
            document.close()
            glossary.write_text("source,target\nobservatory,天文台\n", encoding="utf-8")
            protection.write_text(
                json.dumps({"verified": True, "protected_regions": []}),
                encoding="utf-8",
            )

            report = audit_semantics(
                pdf,
                glossary_csv=glossary,
                protection_receipt=protection,
                allowed_untranslated_phrases=("Simons Observatory",),
            )

            self.assertTrue(report["ok"])
