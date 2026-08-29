from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SealPdf2zhNextPaperTests(unittest.TestCase):
    def _json(self, path: Path, value: dict[str, object]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return path

    def _fixture(self, root: Path) -> dict[str, object]:
        from scripts import snowmass_production_contract as production

        article = root / "paper"
        article.mkdir()
        source = article / "source.pdf"
        raw = article / "run" / "raw.pdf"
        protected = article / "protected" / "final.pdf"
        ir_json = article / "ir" / "babeldoc_ir.json"
        ir_xml = article / "ir" / "babeldoc_ir.xml"
        contact = article / "qc" / "contact.jpg"
        glossary = article / "run" / "locked-glossary.csv"
        for path, payload in (
            (source, b"source-pdf"),
            (raw, b"raw-pdf"),
            (protected, b"protected-pdf"),
            (ir_json, b"{}"),
            (ir_xml, b"<document/>"),
            (contact, b"contact-sheet"),
            (glossary, "source,target\nphysics,物理\n".encode()),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        rights = self._json(
            root / "papers.json",
            {"papers": []},
        )
        rights.write_text(
            json.dumps([{"record_id": "arxiv:test", "publication_allowed": True}]),
            encoding="utf-8",
        )
        source_manifest = self._json(
            root / "sources.json",
            {
                "records": [
                    {
                        "record_id": "arxiv:test",
                        "pdf_status": "complete",
                        "pdf_sha256": sha256(source),
                        "pdf_bytes": source.stat().st_size,
                    }
                ]
            },
        )
        font = root / "font.ttc"
        cover = root / "cover.svg"
        qr = root / "qr.png"
        font.write_bytes(b"font")
        cover.write_bytes(b"cover")
        qr.write_bytes(b"qr")
        environment = production.build_environment_lock(
            root=root,
            python_executable="/isolated/python",
            python_version="3.12.13",
            installed_packages={"babeldoc": "0.6.2", "pdf2zh-next": "2.9.0"},
            babeldoc_version="0.6.2",
            ir_version="pdf2zh-next-ir-v1",
            model="deepseek-v4-flash",
            provider="deepseek",
            pricing_contract={"currency": "USD", "output": 1.32},
            execution_binding={"transport": "localhost-budget-proxy"},
            contract_versions={"pdf2zh_next_seal": 1},
            font_paths=[font],
            cover_asset_paths=[cover, qr],
            git_commit="abc123",
            git_tree="def456",
        )
        environment_path = self._json(article / "environment-lock.json", environment)
        preflight = self._json(
            article / "run" / "preflight.json",
            {
                "status": "preflight_passed",
                "source": {"sha256": sha256(source)},
                "rights": {"manifest_sha256": sha256(rights)},
                "source_identity": {"manifest_sha256": sha256(source_manifest)},
            },
        )
        finish = self._json(
            article / "run" / "finish.json",
            {
                "status": "translated_pending_qc",
                "source": {"sha256": sha256(source)},
                "budget": {
                    "project_max_cost_rmb": 1000,
                    "stage_max_cost_rmb": 10,
                    "stage_max_api_calls": 50,
                },
                "outputs": {"mono_pdf": {"sha256": sha256(raw)}},
            },
        )
        ir = self._json(
            article / "ir" / "receipt.json",
            {
                "zero_paid": True,
                "babeldoc_version": "0.6.2",
                "source_pdf_sha256": sha256(source),
                "ir_json": ir_json.name,
                "ir_json_sha256": sha256(ir_json),
                "ir_xml": ir_xml.name,
                "ir_xml_sha256": sha256(ir_xml),
            },
        )
        protection = self._json(
            article / "qc" / "protection.json",
            {
                "schema_version": 2,
                "verified": True,
                "failures": [],
                "source_pdf_sha256": sha256(source),
                "translated_pdf_sha256": sha256(raw),
                "output_pdf_sha256": sha256(protected),
                "ir_xml_sha256": sha256(ir_xml),
                "protected_regions": [],
                "selected_source_pages": [1, 2, 3, 4, 5, 6],
                "figure_region_count": 0,
                "table_region_count": 0,
                "reference_page_count": 0,
                "canonical_reference_heading_count": 0,
                "canonical_header_count": 5,
                "verbatim_text_count": 0,
            },
        )
        semantic = self._json(
            article / "qc" / "semantic-report.json",
            {
                "schema_version": 1,
                "ok": True,
                "failures": [],
                "pdf_sha256": sha256(protected),
                "glossary_sha256": sha256(glossary),
                "protection_receipt_sha256": sha256(protection),
                "findings": [],
                "checked_glossary_terms": 1,
                "forbidden_terms": [],
                "allowed_untranslated_terms": [],
                "allowed_untranslated_phrases": [],
            },
        )
        structural = self._json(
            article / "qc" / "structural-report.json",
            {
                "schema_version": 1,
                "ok": True,
                "failures": [],
                "pdf_sha256": sha256(protected),
                "page_count": 6,
                "expected_pages": 6,
                "contact_sheet_path": contact.name,
                "contact_sheet_sha256": sha256(contact),
                "protection_receipt_sha256": sha256(protection),
                "low_text_pages": [],
                "out_of_bounds": [],
                "residue": [],
                "english_prose_residue": [],
                "secondary_text_layer_excess": [],
                "secondary_only_latin_tokens": [],
                "secondary_extractor": {
                    "executable": "/opt/homebrew/bin/pdftotext",
                    "version": "pdftotext version test",
                },
                "ignored_text_region_count": 0,
            },
        )
        visual = self._json(
            article / "qc" / "visual-review.json",
            {
                "schema_version": 1,
                "record_id": "arxiv:test",
                "pdf_sha256": sha256(protected),
                "source_pdf_sha256": sha256(source),
                "contact_sheet_sha256": sha256(contact),
                "expected_pages": 6,
                "coverage": "all-pages",
                "review_kind": "human-with-vision-assist",
                "reviewer": "test-reviewer",
                "verdict": "pass",
                "findings": [],
            },
        )
        return locals()

    def test_seals_live_artifacts_and_rejects_mutated_files(self) -> None:
        from scripts.seal_snowmass_pdf2zh_next_paper import seal_paper

        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            arguments = {
                key: fixture[key]
                for key in (
                    "article",
                    "rights",
                    "source_manifest",
                    "source",
                    "preflight",
                    "finish",
                    "raw",
                    "ir",
                    "protection",
                    "protected",
                    "glossary",
                    "semantic",
                    "structural",
                    "contact",
                    "visual",
                    "environment_path",
                )
            }
            arguments["record_id"] = "arxiv:test"
            arguments["current_environment_lock"] = fixture["environment"]

            seal = seal_paper(**arguments)
            self.assertTrue(seal["passed"])
            self.assertEqual(seal["state"], "visual_qc")

            for key in (
                "protected",
                "ir_xml",
                "contact",
                "glossary",
                "rights",
                "source_manifest",
            ):
                original = fixture[key].read_bytes()
                suffix = b" " if key in {"rights", "source_manifest"} else b"mutated"
                fixture[key].write_bytes(original + suffix)
                with self.assertRaises(RuntimeError):
                    seal_paper(**arguments)
                with self.assertRaisesRegex(RuntimeError, "unchanged quarantined input"):
                    seal_paper(**arguments)
                fixture[key].write_bytes(original)

            environment = json.loads(fixture["environment_path"].read_text())
            environment["contracts"]["model"] = "wrong-model"
            fixture["environment_path"].write_text(json.dumps(environment))
            with self.assertRaises(RuntimeError):
                seal_paper(**arguments)

    def test_xobject_source_lookup_reuses_one_textpage_per_source_page(self) -> None:
        from scripts.seal_snowmass_pdf2zh_next_paper import (
            _source_pages_by_xobj_texts,
        )

        class FakePage:
            def __init__(self, page_number: int) -> None:
                self.page_number = page_number
                self.textpage_calls = 0
                self.search_calls: list[str] = []

            def get_textpage(self) -> object:
                self.textpage_calls += 1
                page = self

                class FakeTextPage:
                    def extractTEXT(self, sort: bool = False) -> str:
                        return "plot label"

                    def extractWORDS(self, delimiters: object = None) -> list[tuple]:
                        return [(0, 0, 1, 1, "plot label")]

                    def search(self, text: str) -> list[object]:
                        page.search_calls.append(text)
                        return [object()] if text == "plot label" else []

                return FakeTextPage()

        pages = [FakePage(1), FakePage(2)]
        ir_document = {
            "page": [
                {
                    "pdf_paragraph": [
                        {
                            "pdf_paragraph": [
                                {"xobj_id": 7, "unicode": "plot label"},
                            ]
                        }
                    ]
                }
            ]
        }

        result = _source_pages_by_xobj_texts(ir_document, pages)

        self.assertEqual(result, {"plot label": [1, 2]})
        self.assertEqual([page.textpage_calls for page in pages], [1, 1])
        self.assertEqual([page.search_calls for page in pages], [["plot label"], ["plot label"]])

    def test_source_text_rect_can_use_cached_textpage_without_page_text_extraction(self) -> None:
        from scripts.protect_snowmass_pdf2zh_output import _source_text_rect

        class FakeTextPage:
            def search(self, text: str, quads: int = 0) -> list[tuple[float, ...]]:
                return [(1.0, 2.0, 3.0, 4.0)]

        class FakePage:
            def search_for(self, text: str) -> list[object]:
                raise AssertionError("uncached Page.search_for was called")

            def get_text(self, *args: object, **kwargs: object) -> object:
                raise AssertionError("uncached Page.get_text was called")

        rectangle = _source_text_rect(FakePage(), "token", textpage=FakeTextPage())

        self.assertEqual(tuple(rectangle), (1.0, 2.0, 3.0, 4.0))

    def test_does_not_regress_existing_packaged_manifest(self) -> None:
        from scripts import snowmass_production_contract as production
        from scripts.seal_snowmass_pdf2zh_next_paper import seal_paper

        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            arguments = {
                key: fixture[key]
                for key in (
                    "article",
                    "rights",
                    "source_manifest",
                    "source",
                    "preflight",
                    "finish",
                    "raw",
                    "ir",
                    "protection",
                    "protected",
                    "glossary",
                    "semantic",
                    "structural",
                    "contact",
                    "visual",
                    "environment_path",
                )
            }
            arguments["record_id"] = "arxiv:test"
            arguments["current_environment_lock"] = fixture["environment"]
            seal_paper(**arguments)
            packaged = fixture["article"] / "packaged.pdf"
            packaged.write_bytes(b"packaged")
            manifest = fixture["article"] / "artifact-manifest.json"
            production.record_artifact(
                manifest_path=manifest,
                article_root=fixture["article"],
                artifact_id="packaged",
                relative_path=packaged.name,
                producer="test-packager",
                artifact_type="pdf",
                paper_stage="packaged",
                environment_lock=fixture["environment"],
                parents=("visual_qc",),
            )
            before = manifest.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "already packaged"):
                seal_paper(**arguments)

            self.assertEqual(manifest.read_bytes(), before)

    def test_rejects_incomplete_qc_evidence(self) -> None:
        from scripts.seal_snowmass_pdf2zh_next_paper import seal_paper

        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            semantic = json.loads(fixture["semantic"].read_text())
            semantic.pop("findings")
            fixture["semantic"].write_text(json.dumps(semantic))
            arguments = {
                key: fixture[key]
                for key in (
                    "article", "rights", "source_manifest", "source", "preflight",
                    "finish", "raw", "ir", "protection", "protected", "glossary",
                    "semantic", "structural", "contact", "visual", "environment_path",
                )
            }
            arguments["record_id"] = "arxiv:test"
            arguments["current_environment_lock"] = fixture["environment"]

            with self.assertRaisesRegex(RuntimeError, "semantic report evidence is incomplete"):
                seal_paper(**arguments)


if __name__ == "__main__":
    unittest.main()
