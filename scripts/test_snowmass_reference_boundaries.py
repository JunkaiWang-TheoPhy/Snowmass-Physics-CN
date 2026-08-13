#!/usr/bin/env python3
"""Regression tests for bibliography-boundary detection."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.snowmass_reference_boundaries import reference_boundary


class ReferenceBoundaryTests(unittest.TestCase):
    def test_detects_single_combined_reference_chunk_with_doi_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            chunks = [
                {
                    "id": "chunk0001",
                    "order": 1,
                    "page_number": 4,
                    "layout_label": "plain text",
                    "source_file": "chunk0001.md",
                },
                {
                    "id": "chunk0002",
                    "order": 2,
                    "page_number": 4,
                    "layout_label": "title",
                    "source_file": "chunk0002.md",
                },
                {
                    "id": "chunk0003",
                    "order": 3,
                    "page_number": 4,
                    "layout_label": "plain text",
                    "source_file": "chunk0003.md",
                },
            ]
            (article / "chunk0001.md").write_text("Body conclusion.\n", encoding="utf-8")
            (article / "chunk0002.md").write_text("References\n", encoding="utf-8")
            (article / "chunk0003.md").write_text(
                "{v1}A. Author. Paper One (2019). https://doi.org/10.1/one"
                "{v2}B. Author. Paper Two (2020). https://doi.org/10.1/two\n",
                encoding="utf-8",
            )

            boundary = reference_boundary(article, chunks)

            self.assertEqual(boundary["chunk_ids"], {"chunk0003"})
            self.assertEqual(
                boundary["region_chunk_ids"], {"chunk0002", "chunk0003"}
            )
            self.assertEqual(boundary["check_page_numbers"], {4})
            self.assertEqual(boundary["verbatim_page_numbers"], set())


if __name__ == "__main__":
    unittest.main()
