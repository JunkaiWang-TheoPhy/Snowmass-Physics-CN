from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

import fitz


class Pdf2zhSemanticAuditTests(unittest.TestCase):
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
