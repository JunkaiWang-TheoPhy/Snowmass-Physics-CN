from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

import fitz


@unittest.skipUnless(
    os.environ.get("SNOWMASS_RUN_PDF2ZH_INTEGRATION") == "1",
    "set SNOWMASS_RUN_PDF2ZH_INTEGRATION=1 for the pinned BabelDOC integration",
)
class ExtractIrIntegrationTests(unittest.TestCase):
    def test_persists_debug_free_layout_ir(self) -> None:
        from scripts.extract_snowmass_pdf2zh_ir import extract_ir

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            document = fitz.open()
            page = document.new_page(width=612, height=792)
            page.insert_text(
                (72, 72), "Academic source paragraph for layout extraction."
            )
            document.save(source)
            document.close()

            receipt = extract_ir(source, root / "ir")

            self.assertTrue(receipt["zero_paid"])
            self.assertEqual(receipt["babeldoc_version"], "0.6.4")
            self.assertEqual(receipt["page_count"], 1)
            self.assertTrue((root / "ir" / "babeldoc_ir.xml").is_file())
            self.assertTrue((root / "ir" / "babeldoc_ir.json").is_file())


if __name__ == "__main__":
    unittest.main()
