#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snowmass_constraint_compiler as compiler


class SnowmassConstraintCompilerTests(unittest.TestCase):
    def test_compiles_exact_and_object_policies_into_chunk_directives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "title.md").write_text("References\n", encoding="utf-8")
            (article / "figure.md").write_text("Euclid\n", encoding="utf-8")
            manifest = {
                "record_id": "arxiv:a",
                "chunks": [
                    {"id": "title", "source_file": "title.md", "translation_policy": "translate"},
                    {"id": "figure", "source_file": "figure.md", "translation_policy": "verbatim_figure_text"},
                ],
            }
            constraints = {
                "schema_version": 1,
                "record_id": "arxiv:a",
                "exact_translations": [{"source": "References", "target": "参考文献"}],
                "forbidden_translations": [],
            }

            plan = compiler.compile_constraint_plan(article, manifest, constraints)

            self.assertEqual(plan["chunk_directives"]["title"]["fixed_translation"], "参考文献\n")
            self.assertEqual(plan["chunk_directives"]["figure"]["policy"], "verbatim_source")
            self.assertEqual(plan["source_hashes"]["title"], compiler.sha256(article / "title.md"))

    def test_plan_loader_rejects_stale_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            source = article / "chunk.md"
            source.write_text("Source\n", encoding="utf-8")
            manifest = {
                "record_id": "arxiv:a",
                "chunks": [{"id": "chunk", "source_file": "chunk.md"}],
            }
            constraints = {
                "schema_version": 1,
                "record_id": "arxiv:a",
                "exact_translations": [],
                "forbidden_translations": [],
            }
            compiler.write_constraint_plan(
                article, compiler.compile_constraint_plan(article, manifest, constraints)
            )
            source.write_text("Changed\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "stale"):
                compiler.load_constraint_plan(article, manifest, constraints)

    def test_plan_loader_rejects_tampered_plan_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "chunk.md").write_text("Source\n", encoding="utf-8")
            manifest = {
                "record_id": "arxiv:a",
                "chunks": [{"id": "chunk", "source_file": "chunk.md"}],
            }
            constraints = {
                "schema_version": 1,
                "record_id": "arxiv:a",
                "exact_translations": [],
                "forbidden_translations": [],
            }
            plan = compiler.compile_constraint_plan(article, manifest, constraints)
            plan["chunk_directives"]["chunk"] = {
                "policy": "fixed_translation",
                "fixed_translation": "伪造译文\n",
            }
            unsigned = dict(plan)
            unsigned.pop("plan_sha256")
            plan["plan_sha256"] = compiler._json_sha256(unsigned)
            compiler.write_constraint_plan(article, plan)

            with self.assertRaisesRegex(RuntimeError, "hash"):
                compiler.load_constraint_plan(article, manifest, constraints)

    def test_plan_loader_rejects_changed_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "chunk.md").write_text("Source\n", encoding="utf-8")
            manifest = {
                "record_id": "arxiv:a",
                "chunks": [{"id": "chunk", "source_file": "chunk.md"}],
            }
            original = {
                "schema_version": 1,
                "record_id": "arxiv:a",
                "exact_translations": [],
                "forbidden_translations": [],
            }
            changed = dict(original, forbidden_translations=[{"source": "Source", "target": "伪造"}])
            compiler.write_constraint_plan(
                article, compiler.compile_constraint_plan(article, manifest, original)
            )

            with self.assertRaisesRegex(RuntimeError, "constraints"):
                compiler.load_constraint_plan(article, manifest, changed)

    def test_plan_loader_requires_compiled_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, "missing"):
                compiler.load_constraint_plan(
                    article,
                    {"record_id": "arxiv:a", "chunks": []},
                    {"schema_version": 1, "record_id": "arxiv:a", "exact_translations": [], "forbidden_translations": []},
                )


if __name__ == "__main__":
    unittest.main()
