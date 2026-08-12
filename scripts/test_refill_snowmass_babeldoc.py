#!/usr/bin/env python3
"""Tests for refilling translated chunks into BabelDOC IR."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("refill_snowmass_babeldoc.py")


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("BabelDOC refill command is not implemented")
    spec = importlib.util.spec_from_file_location("refill_snowmass_babeldoc", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RefillSnowmassBabelDocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rights = self.root / "papers.json"
        self.rights.write_text(
            json.dumps([{"record_id": "arxiv:allowed", "publication_allowed": True}]),
            encoding="utf-8",
        )
        self.article = self.root / "translation" / "papers" / "arxiv_allowed"
        self.article.mkdir(parents=True)
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"%PDF-test\n")
        (self.article / "babeldoc_ir.xml").write_text("<document/>\n", encoding="utf-8")
        (self.article / "chunk0001.md").write_text("Source 14.\n", encoding="utf-8")
        (self.article / "output_chunk0001.md").write_text("译文 14。\n", encoding="utf-8")
        source_hash = hashlib.sha256("Source 14.\n".encode()).hexdigest()
        (self.article / "chunk_status").mkdir()
        (self.article / "chunk_status" / "chunk0001.json").write_text(
            json.dumps(
                {
                    "status": "complete",
                    "source_hash": source_hash,
                    "stages": {
                        "academic": {
                            "status": "complete",
                            "output_hash": hashlib.sha256("译文 14。\n".encode()).hexdigest(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.article / "manifest.json").write_text(
            json.dumps(
                {
                    "record_id": "arxiv:allowed",
                    "input_mode": "babeldoc_ir",
                    "source_pdf_path": str(self.pdf),
                    "babeldoc_ir_xml_file": "babeldoc_ir.xml",
                    "chunks": [
                        {
                            "id": "chunk0001",
                            "source_file": "chunk0001.md",
                            "output_file": "output_chunk0001.md",
                            "page_number": 1,
                            "paragraph_index": 2,
                            "source_hash": source_hash,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_refills_completed_outputs_and_reuses_matching_checkpoint(self) -> None:
        module = load_module()

        def fake_refill(ir_xml, **kwargs):
            kwargs["output_xml"].write_text("<translated/>\n", encoding="utf-8")
            self.assertEqual(kwargs["translations"][0].translated_text, "译文 14。\n")
            return module.BRIDGE.RefillResult(kwargs["output_xml"], 1)

        def fake_render(ir_xml, **kwargs):
            kwargs["output_dir"].mkdir(parents=True, exist_ok=True)
            mono = kwargs["output_dir"] / "translated_mono.pdf"
            dual = kwargs["output_dir"] / "translated_dual.pdf"
            mono.write_bytes(b"%PDF-mono\n")
            dual.write_bytes(b"%PDF-dual\n")
            return module.BRIDGE.RenderedPdfResult(mono, dual)

        with (
            mock.patch.object(module.BRIDGE, "refill_document_units", side_effect=fake_refill) as refill,
            mock.patch.object(module.BRIDGE, "render_translated_document", side_effect=fake_render) as render,
        ):
            self.assertEqual(module.main(["--article-dir", str(self.article), "--rights-manifest", str(self.rights)]), 0)
            self.assertEqual(module.main(["--article-dir", str(self.article), "--rights-manifest", str(self.rights)]), 0)

        self.assertEqual(refill.call_count, 1)
        self.assertEqual(render.call_count, 1)
        status = json.loads((self.article / "refill_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["ir_pipeline_version"], module.BRIDGE.IR_PIPELINE_VERSION)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["refilled_unit_count"], 1)
        self.assertEqual(status["refill_schema_version"], module.REFILL_SCHEMA_VERSION)
        self.assertEqual(status["babeldoc_version"], "0.6.4")
        self.assertTrue((self.article / "rendered" / "translated_mono.pdf").is_file())
        self.assertTrue((self.article / "rendered" / "translated_dual.pdf").is_file())
        self.assertIn("mono_pdf_sha256", status)
        self.assertIn("dual_pdf_sha256", status)
        self.assertTrue(status["publication_qc"]["ok"])
        self.assertEqual(status["reference_qc"]["page_numbers"], [])

    def test_reference_heading_marks_all_following_pages_verbatim(self) -> None:
        module = load_module()
        source = "References\n"
        source_hash = hashlib.sha256(source.encode()).hexdigest()
        (self.article / "chunk0001.md").write_text(source, encoding="utf-8")
        (self.article / "output_chunk0001.md").write_text(source, encoding="utf-8")
        status_path = self.article / "chunk_status" / "chunk0001.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["source_hash"] = source_hash
        status["stages"]["academic"]["output_hash"] = source_hash
        status_path.write_text(json.dumps(status), encoding="utf-8")
        manifest_path = self.article / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["chunks"][0]["source_hash"] = source_hash
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        def fake_refill(ir_xml, **kwargs):
            kwargs["output_xml"].write_text("<translated/>\n", encoding="utf-8")
            return module.BRIDGE.RefillResult(kwargs["output_xml"], 1)

        def fake_render(ir_xml, **kwargs):
            self.assertEqual(kwargs["verbatim_page_numbers"], {1})
            kwargs["output_dir"].mkdir(parents=True, exist_ok=True)
            mono = kwargs["output_dir"] / "translated_mono.pdf"
            dual = kwargs["output_dir"] / "translated_dual.pdf"
            mono.write_bytes(b"%PDF-mono\n")
            dual.write_bytes(b"%PDF-dual\n")
            return module.BRIDGE.RenderedPdfResult(
                mono,
                dual,
                verbatim_pages=(1,),
                verbatim_verified=True,
            )

        with (
            mock.patch.object(module.BRIDGE, "refill_document_units", side_effect=fake_refill),
            mock.patch.object(module.BRIDGE, "render_translated_document", side_effect=fake_render),
        ):
            self.assertEqual(
                module.main(
                    [
                        "--article-dir",
                        str(self.article),
                        "--rights-manifest",
                        str(self.rights),
                    ]
                ),
                0,
            )

        refill_status = json.loads(
            (self.article / "refill_status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(refill_status["reference_qc"]["page_numbers"], [1])
        self.assertTrue(refill_status["reference_qc"]["verified"])

    def test_rights_gate_rejects_article_before_refill(self) -> None:
        module = load_module()
        self.rights.write_text(
            json.dumps([{"record_id": "arxiv:allowed", "publication_allowed": False}]),
            encoding="utf-8",
        )
        with mock.patch.object(module.BRIDGE, "refill_document_units") as refill:
            self.assertEqual(module.main(["--article-dir", str(self.article), "--rights-manifest", str(self.rights)]), 2)
        refill.assert_not_called()

    def test_duplicate_rights_records_fail_closed(self) -> None:
        module = load_module()
        self.rights.write_text(
            json.dumps(
                [
                    {"record_id": "arxiv:allowed", "publication_allowed": True},
                    {"record_id": "arxiv:allowed", "publication_allowed": False},
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "Duplicate record_id"):
            module.main(["--article-dir", str(self.article), "--rights-manifest", str(self.rights)])

    def test_constraints_for_another_record_fail_closed(self) -> None:
        module = load_module()
        (self.article / "hard_constraints.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:other",
                    "exact_translations": [],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "Hard constraint record mismatch"):
            module.main(
                [
                    "--article-dir",
                    str(self.article),
                    "--rights-manifest",
                    str(self.rights),
                ]
            )

    def test_constraints_fall_back_to_tracked_record_policy(self) -> None:
        module = load_module()
        policy = self.root / "hard-policy.json"
        policy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "records": {
                        "arxiv:allowed": {
                            "exact_translations": [
                                {"source": "Header", "target": "统一页眉"}
                            ]
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        constraints = module._load_constraints(
            self.article,
            "arxiv:allowed",
            policy_path=policy,
        )

        self.assertEqual(constraints["record_id"], "arxiv:allowed")
        self.assertEqual(constraints["exact_translations"][0]["target"], "统一页眉")

    def test_publication_preflight_canonicalizes_headers_and_adds_first_use_terms(self) -> None:
        module = load_module()
        chunks = [
            {
                "id": "chunk0001",
                "order": 1,
                "page_number": 1,
                "paragraph_index": 0,
                "source_file": "chunk0001.md",
                "output_file": "output_chunk0001.md",
            },
            {
                "id": "chunk0002",
                "order": 2,
                "page_number": 2,
                "paragraph_index": 0,
                "source_file": "chunk0002.md",
                "output_file": "output_chunk0002.md",
            },
            {
                "id": "chunk0003",
                "order": 3,
                "page_number": 2,
                "paragraph_index": 1,
                "source_file": "chunk0003.md",
                "output_file": "output_chunk0003.md",
            },
        ]
        sources = [
            "Cosmology from structure\n",
            "Cosmology from structure\n",
            "The cosmic microwave background constrains dark matter.\n",
        ]
        outputs = [
            "从结构研究宇宙学\n",
            "结构中的宇宙学\n",
            "宇宙微波背景限制暗物质。\n",
        ]
        for chunk, source, output in zip(chunks, sources, outputs, strict=True):
            (self.article / chunk["source_file"]).write_text(source, encoding="utf-8")
            (self.article / chunk["output_file"]).write_text(output, encoding="utf-8")
        constraints = {
            "schema_version": 1,
            "exact_translations": [
                {
                    "source": "Cosmology from structure",
                    "target": "基于结构的宇宙学",
                    "scope": "all_occurrences",
                }
            ],
        }
        glossary = [
            {
                "source": "cosmic microwave background",
                "target": "宇宙微波背景",
                "acronym": "CMB",
                "first_use": True,
            },
            {"source": "dark matter", "target": "暗物质", "first_use": True},
        ]
        translations = [
            module.BRIDGE.RefillTranslation(
                page_number=chunk["page_number"],
                paragraph_index=chunk["paragraph_index"],
                source_text=source,
                translated_text=output,
            )
            for chunk, source, output in zip(chunks, sources, outputs, strict=True)
        ]

        prepared, report = module.prepare_publication_translations(
            self.article,
            {"chunks": chunks},
            translations,
            constraints=constraints,
            glossary=glossary,
        )

        self.assertTrue(report["ok"])
        self.assertEqual(prepared[0].translated_text, "基于结构的宇宙学\n")
        self.assertEqual(prepared[1].translated_text, "基于结构的宇宙学\n")
        self.assertEqual(
            prepared[2].translated_text,
            "宇宙微波背景（cosmic microwave background，CMB）限制暗物质（dark matter）。\n",
        )
        self.assertEqual(report["exact_translation_occurrences"], 2)
        self.assertEqual(report["first_use_terms"], 2)
        self.assertTrue((self.article / "publication_chunks" / "chunk0003.md").is_file())

    def test_publication_preflight_fails_when_exact_source_is_missing(self) -> None:
        module = load_module()
        translation = module.BRIDGE.RefillTranslation(1, 0, "Other source\n", "其他文本\n")

        with self.assertRaisesRegex(RuntimeError, "exact translation source was not found"):
            module.prepare_publication_translations(
                self.article,
                {
                    "chunks": [
                        {
                            "id": "chunk0001",
                            "order": 1,
                            "page_number": 1,
                            "paragraph_index": 0,
                        }
                    ]
                },
                [translation],
                constraints={
                    "schema_version": 1,
                    "exact_translations": [
                        {"source": "Required header", "target": "固定页眉"}
                    ],
                },
                glossary=[],
            )

    def test_first_use_definition_skips_title_and_starts_in_body(self) -> None:
        module = load_module()
        chunks = [
            {
                "id": "chunk0001",
                "order": 1,
                "page_number": 1,
                "paragraph_index": 0,
                "layout_label": "title",
            },
            {
                "id": "chunk0002",
                "order": 2,
                "page_number": 1,
                "paragraph_index": 1,
                "layout_label": "text",
            },
        ]
        translations = [
            module.BRIDGE.RefillTranslation(1, 0, "Dark matter", "暗物质"),
            module.BRIDGE.RefillTranslation(
                1,
                1,
                "Dark matter shapes halos.",
                "暗物质塑造晕。",
            ),
        ]

        prepared, _report = module.prepare_publication_translations(
            self.article,
            {"chunks": chunks},
            translations,
            constraints={"schema_version": 1, "exact_translations": []},
            glossary=[
                {"source": "dark matter", "target": "暗物质", "first_use": True}
            ],
        )

        self.assertEqual(prepared[0].translated_text, "暗物质")
        self.assertEqual(
            prepared[1].translated_text,
            "暗物质（dark matter）塑造晕。",
        )

    def test_verbatim_reference_pages_require_a_canonical_repeated_header(self) -> None:
        module = load_module()
        chunks = []
        for order, page_number in enumerate((19, 20), 1):
            source_file = f"header-{page_number}.md"
            (self.article / source_file).write_text("Repeated running header\n", encoding="utf-8")
            chunks.append(
                {
                    "id": f"chunk{order:04d}",
                    "order": order,
                    "page_number": page_number,
                    "source_file": source_file,
                }
            )
        constraints = {
            "schema_version": 1,
            "exact_translations": [
                {"source": "Repeated running header", "target": "统一运行页眉"}
            ],
        }

        rule = module._verbatim_header_translation(
            self.article,
            {"chunks": chunks},
            {19, 20},
            constraints,
        )

        self.assertEqual(
            rule,
            {"source": "Repeated running header", "target": "统一运行页眉"},
        )


if __name__ == "__main__":
    unittest.main()
