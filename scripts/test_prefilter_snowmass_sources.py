import json
import sys
import tempfile
import unittest
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prefilter_snowmass_sources import prefilter


class PrefilterTests(unittest.TestCase):
    def test_figures_are_measured_for_risk_only_and_short_paper_can_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_root = root / "sources"
            pdf_root.mkdir()
            pdf = pdf_root / "paper.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "Short paper\nReferences\n[1] Source")
            document.save(pdf)
            rights = root / "rights.json"
            rights.write_text(json.dumps([{"record_id": "arxiv:1", "publication_allowed": True}]))
            source = root / "source.json"
            source.write_text(json.dumps({"records": [{"record_id": "arxiv:1", "pdf_path": "paper.pdf"}]}))
            result = prefilter(rights_path=rights, source_manifest_path=source, pdf_root=pdf_root)
            self.assertEqual(result["eligible_count"], 1)
            self.assertEqual(result["candidates"][0]["record_id"], "arxiv:1")
            self.assertEqual(result["policy"]["figure_interior_text"], "source_verbatim")


if __name__ == "__main__":
    unittest.main()
