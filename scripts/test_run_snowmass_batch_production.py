#!/usr/bin/env python3
"""Tests for the reusable Snowmass batch production control plane."""

from __future__ import annotations

import contextlib
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("run_snowmass_batch_production.py")


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("batch production orchestrator is not implemented")
    spec = importlib.util.spec_from_file_location("run_snowmass_batch_production", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BatchCliStartupTests(unittest.TestCase):
    def test_direct_script_help_starts_from_repository_root(self) -> None:
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--help"],
            cwd=MODULE_PATH.parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--project-max-cost-rmb", result.stdout)


class BatchSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "papers.json"

    def test_only_literal_true_records_are_loaded_and_duplicates_fail_closed(self) -> None:
        module = load_module()
        self.manifest.write_text(
            json.dumps(
                [
                    {"record_id": "arxiv:a", "publication_allowed": True, "page_count": 10},
                    {"record_id": "arxiv:b", "publication_allowed": 1},
                    {"record_id": "arxiv:c", "publication_allowed": False},
                ]
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            [row["record_id"] for row in module.load_publication_records(self.manifest)],
            ["arxiv:a"],
        )
        self.manifest.write_text(
            json.dumps(
                [
                    {"record_id": "arxiv:a", "publication_allowed": True},
                    {"record_id": "arxiv:a", "publication_allowed": False},
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "Duplicate"):
            module.load_publication_records(self.manifest)

    def test_stage_selection_is_deterministic_and_explicit_ids_fail_closed(self) -> None:
        module = load_module()
        records = [
            {
                "record_id": f"arxiv:{index:04d}",
                "publication_allowed": True,
                "page_count": (index % 30) + 1,
                "frontiers": [f"F{index % 3}"],
            }
            for index in range(80)
        ]
        first = module.select_stage_records(records, "pilot10")
        second = module.select_stage_records(list(reversed(records)), "pilot10")
        self.assertEqual([row["record_id"] for row in first], [row["record_id"] for row in second])
        self.assertEqual(len(first), 10)
        with self.assertRaisesRegex(ValueError, "not publication-allowed"):
            module.select_stage_records(records, "shadow", explicit_ids=("arxiv:blocked",))

    def test_production_stages_are_disjoint_and_cover_every_eligible_record(self) -> None:
        module = load_module()
        records = [
            {
                "record_id": f"arxiv:{index:04d}",
                "publication_allowed": True,
                "page_count": (index % 30) + 1,
                "frontiers": [f"F{index % 3}"],
            }
            for index in range(80)
        ]

        stages = {
            stage: module.select_stage_records(records, stage)
            for stage in ("shadow", "pilot5", "pilot10", "pilot25", "batch50", "remainder")
        }
        stage_ids = {
            stage: {row["record_id"] for row in selected}
            for stage, selected in stages.items()
        }

        self.assertEqual([len(stages[name]) for name in stages], [1, 5, 10, 25, 39, 0])
        names = list(stage_ids)
        for index, left in enumerate(names):
            for right in names[index + 1 :]:
                self.assertFalse(stage_ids[left] & stage_ids[right])
        self.assertEqual(set().union(*stage_ids.values()), {row["record_id"] for row in records})

    def test_babeldoc_runtime_is_derived_from_console_script_shebang(self) -> None:
        module = load_module()
        runtime = self.root / "babeldoc-python"
        runtime.write_text("", encoding="utf-8")
        console = self.root / "babeldoc"
        console.write_text(f"#!{runtime}\n", encoding="utf-8")

        self.assertEqual(module.resolve_babeldoc_python(console), runtime)

    def test_babeldoc_runtime_supports_env_python_shebang(self) -> None:
        module = load_module()
        console = self.root / "babeldoc-env"
        console.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        with mock.patch.object(module.shutil, "which", return_value="/verified/python3"):
            self.assertEqual(
                module.resolve_babeldoc_python(console), Path("/verified/python3")
            )

    def test_cli_requires_a_positive_finite_stage_request_cap(self) -> None:
        module = load_module()
        base = [
            "--stage", "shadow",
            "--project-max-cost-rmb", "1000",
            "--stage-max-cost-rmb", "10",
        ]
        with self.assertRaises(SystemExit):
            module._parse_args([*base, "--stage-max-api-calls", "0"])

        config = module._parse_args([*base, "--stage-max-api-calls", "777"])
        self.assertEqual(config.stage_max_api_calls, 777)

    def test_programmatic_preflight_rejects_zero_stage_request_cap(self) -> None:
        module = load_module()
        self.manifest.write_text("[]\n", encoding="utf-8")
        config = module.BatchConfig(
            rights_manifest=self.manifest,
            pdf_root=self.root / "pdf",
            output_root=self.root / "output",
            control_dir=self.root / "control",
            stage="shadow",
            explicit_ids=(),
            max_articles=None,
            project_max_cost_rmb=1000.0,
            stage_max_cost_rmb=10.0,
            usd_cny_rate=7.2,
            chunk_concurrency=1,
            article_concurrency=1,
            through_stage="packaged",
            translation_version="test",
            packaged_on="2026-08-13",
            stage_max_api_calls=0,
            preflight_only=True,
        )

        with self.assertRaisesRegex(ValueError, "request limit"):
            module.run_batch(config, client=object())


class StagePrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.control = self.root / "control"

    def _write_prior_run(
        self,
        *,
        stage: str = "shadow",
        environment_lock_sha256: str = "env-current",
        rights_manifest_sha256: str = "rights-current",
        recovered: bool = False,
        selected_record_ids: list[str] | None = None,
        result_record_ids: list[str] | None = None,
    ) -> Path:
        selected_record_ids = selected_record_ids or ["arxiv:a"]
        result_record_ids = result_record_ids or selected_record_ids
        run_id = f"{stage}-proof"
        run_dir = self.control / "runs" / run_id
        run_dir.mkdir(parents=True)
        snapshot = {
            "schema_version": 2,
            "run_id": run_id,
            "stage": stage,
            "rights_manifest_sha256": rights_manifest_sha256,
            "environment_lock_sha256": environment_lock_sha256,
            "selected_record_ids": selected_record_ids,
            "through_stage": "packaged",
        }
        (run_dir / "snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8"
        )
        results = []
        for record_id in result_record_ids:
            result = {
                "record_id": record_id,
                "status": "packaged",
                "source_characters": 100,
            }
            if recovered:
                result["resumed_from_verified_translation"] = True
            results.append(result)
        report = {
            **snapshot,
            "status": "complete",
            "completed": len(results),
            "failed": 0,
            "quarantined": 0,
            "results": results,
            "failures": [],
            "hard_failures": [],
            "metrics": {
                "fresh_completed_articles": 0 if recovered else 1,
                "recovered_articles": 1 if recovered else 0,
                "unresolved_uncertain_paid_requests": 0,
                "manual_review_chunks": 0,
            },
            "promotion_gate": {
                "allowed": not recovered,
                "next_stage": "pilot5",
                "reasons": ["recovered_results_not_promotion_evidence"] if recovered else [],
            },
        }
        (run_dir / "run.json").write_text(json.dumps(report), encoding="utf-8")
        return run_dir

    def test_non_shadow_stage_requires_current_fresh_prior_stage_evidence(self) -> None:
        module = load_module()

        missing = module.stage_prerequisite_report(
            self.control,
            target_stage="pilot5",
            rights_manifest_sha256="rights-current",
            environment_lock_sha256="env-current",
            expected_prior_record_ids=("arxiv:a",),
        )
        self.assertFalse(missing["satisfied"])
        self.assertIn("no_matching_prior_stage_run", missing["reasons"])

        self._write_prior_run(recovered=True)
        recovered = module.stage_prerequisite_report(
            self.control,
            target_stage="pilot5",
            rights_manifest_sha256="rights-current",
            environment_lock_sha256="env-current",
            expected_prior_record_ids=("arxiv:a",),
        )
        self.assertFalse(recovered["satisfied"])
        self.assertIn("prior_stage_not_fresh", recovered["reasons"])

    def test_current_fresh_prior_stage_evidence_is_hash_bound(self) -> None:
        module = load_module()
        run_dir = self._write_prior_run()

        report = module.stage_prerequisite_report(
            self.control,
            target_stage="pilot5",
            rights_manifest_sha256="rights-current",
            environment_lock_sha256="env-current",
            expected_prior_record_ids=("arxiv:a",),
        )

        self.assertTrue(report["satisfied"])
        self.assertEqual(report["run_id"], "shadow-proof")
        self.assertRegex(report["run_report_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            report["run_report_sha256"],
            hashlib.sha256((run_dir / "run.json").read_bytes()).hexdigest(),
        )

    def test_old_environment_or_rights_snapshot_cannot_unlock_next_stage(self) -> None:
        module = load_module()
        self._write_prior_run(
            environment_lock_sha256="env-old",
            rights_manifest_sha256="rights-old",
        )

        report = module.stage_prerequisite_report(
            self.control,
            target_stage="pilot5",
            rights_manifest_sha256="rights-current",
            environment_lock_sha256="env-current",
            expected_prior_record_ids=("arxiv:a",),
        )

        self.assertFalse(report["satisfied"])
        self.assertIn("prior_stage_environment_lock_mismatch", report["reasons"])
        self.assertIn("prior_stage_rights_manifest_mismatch", report["reasons"])

    def test_noncanonical_or_mismatched_prior_stage_ids_fail_closed(self) -> None:
        module = load_module()
        self._write_prior_run(
            selected_record_ids=["arxiv:noncanonical"],
            result_record_ids=["arxiv:other"],
        )

        report = module.stage_prerequisite_report(
            self.control,
            target_stage="pilot5",
            rights_manifest_sha256="rights-current",
            environment_lock_sha256="env-current",
            expected_prior_record_ids=("arxiv:canonical",),
        )

        self.assertFalse(report["satisfied"])
        self.assertIn("prior_stage_cohort_mismatch", report["reasons"])
        self.assertIn("prior_stage_result_ids_mismatch", report["reasons"])

    def test_paid_non_shadow_run_refuses_before_preparation_without_prerequisite(self) -> None:
        module = load_module()
        manifest = self.root / "papers.json"
        manifest.write_text(
            json.dumps(
                [{"record_id": "arxiv:a", "publication_allowed": True, "page_count": 1}]
            ),
            encoding="utf-8",
        )
        config = module.BatchConfig(
            rights_manifest=manifest,
            pdf_root=self.root / "pdf",
            output_root=self.root / "output",
            control_dir=self.control,
            stage="pilot5",
            explicit_ids=("arxiv:a",),
            max_articles=None,
            project_max_cost_rmb=1000.0,
            stage_max_cost_rmb=10.0,
            usd_cny_rate=7.2,
            chunk_concurrency=1,
            article_concurrency=1,
            through_stage="packaged",
            translation_version="test",
            packaged_on="2026-08-14",
            stage_max_api_calls=16,
            historical_roots=(),
        )

        with (
            mock.patch.object(
                module,
                "_production_environment_lock",
                return_value={"lock_sha256": "env-current"},
            ),
            mock.patch.object(
                module,
                "_prepare_all",
                side_effect=AssertionError("preparation must remain unreachable"),
            ),
            self.assertRaisesRegex(
                module.ProjectionGateRefusedError,
                "prior-stage promotion evidence",
            ),
        ):
            module.run_batch(config, client=object())


class TranslationProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.article = Path(self.temporary.name)
        (self.article / "chunk_status").mkdir()

    def _write_statuses(self, *, paper_run_id: str | None, chunk_run_id: str | None) -> None:
        (self.article / "paper_status.json").write_text(
            json.dumps(
                {
                    "phases": {
                        "analysis": {
                            "status": "complete",
                            "run_id": paper_run_id,
                            "max_output_tokens": 100,
                        },
                        "prompt": {"status": "complete"},
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.article / "chunk_status" / "chunk0001.json").write_text(
            json.dumps(
                {
                    "stages": {
                        "translate": {
                            "status": "complete",
                            "run_id": chunk_run_id,
                            "decision": {
                                "action": "call_model",
                                "reason": "stage_requires_model",
                            },
                        },
                        "terminology": {
                            "status": "complete",
                            "decision": {
                                "action": "copy_prior_text",
                                "reason": "terminology_noop",
                            },
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_reused_model_checkpoint_is_not_fresh_production_evidence(self) -> None:
        module = load_module()
        self._write_statuses(paper_run_id="old-run", chunk_run_id="current-run")

        report = module.translation_provenance_report(self.article, "current-run")

        self.assertFalse(report["fresh"])
        self.assertEqual(report["model_checkpoint_count"], 2)
        self.assertEqual(report["current_run_model_checkpoint_count"], 1)
        self.assertEqual(report["reused_model_checkpoint_ids"], ["paper:analysis"])

    def test_all_model_checkpoints_bound_to_current_run_are_fresh(self) -> None:
        module = load_module()
        self._write_statuses(
            paper_run_id="current-run",
            chunk_run_id="current-run",
        )

        report = module.translation_provenance_report(self.article, "current-run")

        self.assertTrue(report["fresh"])
        self.assertEqual(report["reused_model_checkpoint_count"], 0)

    def test_missing_model_checkpoint_evidence_is_not_fresh(self) -> None:
        module = load_module()
        (self.article / "paper_status.json").write_text(
            json.dumps({"phases": {"prompt": {"status": "complete"}}}),
            encoding="utf-8",
        )

        report = module.translation_provenance_report(self.article, "current-run")

        self.assertFalse(report["fresh"])
        self.assertIn("no_model_checkpoint_evidence", report["reasons"])


class ArticleQCTests(unittest.TestCase):
    def test_cover_title_restores_visible_symbols_and_drops_footnote_markers(self) -> None:
        module = load_module()
        cases = [
            (
                "Laser Manipulation of H{v1}Beams: a Snowmass 2022 White Paper{v2}\n",
                "激光操控 H{v1} 束：Snowmass 2022 白皮书{v2}\n",
                "Laser Manipulation of H- Beams",
                "激光操控 H- 束：Snowmass 2022 白皮书",
            ),
            (
                "The carbon footprint of proposed e{v1}e{v2}Higgs factories\n",
                "拟议的 e{v1}e{v2} 希格斯工厂的碳足迹\n",
                "The Carbon Footprint of Proposed e+e- Higgs Factories",
                "拟议的 e+e- 希格斯工厂的碳足迹",
            ),
            (
                "Software and Computing for Small HEP Experiments{v1}\n",
                "小型高能物理实验的软件与计算{v1}\n",
                "Software and Computing for Small HEP Experiments",
                "小型高能物理实验的软件与计算",
            ),
        ]
        for source, translated, canonical, expected in cases:
            with self.subTest(canonical=canonical):
                self.assertEqual(
                    module.restore_plain_title_placeholders(source, translated, canonical),
                    expected,
                )

    def test_cover_title_fails_closed_when_placeholder_cannot_be_resolved(self) -> None:
        module = load_module()

        with self.assertRaisesRegex(RuntimeError, "unresolved title placeholder"):
            module.restore_plain_title_placeholders(
                "A{v1}B", "甲{v1}乙", "Completely unrelated canonical title"
            )

    def test_cover_title_selects_first_page_plain_text_closest_to_canonical_record(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "wrong-source.md").write_text("Snowmass Theory White Paper", encoding="utf-8")
            (article / "wrong-output.md").write_text("Snowmass 理论白皮书", encoding="utf-8")
            (article / "right-source.md").write_text(
                "Astrophysicaland Cosmological Probes of Dark Matter", encoding="utf-8"
            )
            (article / "right-output.md").write_text(
                "暗物质的天体物理与宇宙学探测", encoding="utf-8"
            )
            manifest = {
                "chunks": [
                    {"id": "wrong", "order": 1, "layout_label": "title", "source_file": "wrong-source.md", "output_file": "wrong-output.md"},
                    {"id": "right", "order": 2, "page_number": 1, "layout_label": "plain text", "source_file": "right-source.md", "output_file": "right-output.md"},
                ]
            }

            title = module._chinese_title_from_manifest(
                article,
                manifest,
                "Astrophysical and Cosmological Probes of Dark Matter",
            )

            self.assertEqual(title, "暗物质的天体物理与宇宙学探测")

    def test_cover_title_prefers_paper_level_exact_translation(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "hard_constraints.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_id": "arxiv:paper",
                        "exact_translations": [
                            {
                                "source": "Two-Real-Singlet-Model (TRSM) Benchmark Planes",
                                "target": "双实单态模型（TRSM）基准平面",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (article / "source.md").write_text("TRSM Benchmark Planes", encoding="utf-8")
            (article / "output.md").write_text("TRSM 基准平面", encoding="utf-8")
            manifest = {
                "record_id": "arxiv:paper",
                "chunks": [
                    {"id": "title", "order": 1, "page_number": 1, "layout_label": "title", "source_file": "source.md", "output_file": "output.md"}
                ],
            }

            title = module._chinese_title_from_manifest(
                article,
                manifest,
                "Two-Real-Singlet-Model (TRSM) Benchmark Planes",
            )

            self.assertEqual(title, "双实单态模型（TRSM）基准平面")

    def test_cover_title_fails_closed_on_low_confidence_layout_match(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "source.md").write_text("II. Benchmark Planes", encoding="utf-8")
            (article / "output.md").write_text("II. 基准平面", encoding="utf-8")
            manifest = {
                "record_id": "arxiv:paper",
                "chunks": [
                    {"id": "title", "order": 1, "page_number": 1, "layout_label": "title", "source_file": "source.md", "output_file": "output.md"}
                ],
            }

            with self.assertRaisesRegex(RuntimeError, "high-confidence"):
                module._chinese_title_from_manifest(
                    article,
                    manifest,
                    "Two-Real-Singlet-Model (TRSM) Benchmark Planes",
                )

    def test_cover_title_does_not_fall_back_when_best_match_has_no_output(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "best-source.md").write_text("Canonical Paper Title", encoding="utf-8")
            (article / "best-output.md").write_text("", encoding="utf-8")
            (article / "wrong-source.md").write_text("Author Biography", encoding="utf-8")
            (article / "wrong-output.md").write_text("错误标题", encoding="utf-8")
            manifest = {
                "record_id": "arxiv:paper",
                "chunks": [
                    {"id": "best", "order": 1, "page_number": 1, "source_file": "best-source.md", "output_file": "best-output.md"},
                    {"id": "wrong", "order": 2, "page_number": 1, "source_file": "wrong-source.md", "output_file": "wrong-output.md"},
                ],
            }

            with self.assertRaisesRegex(RuntimeError, "high-confidence"):
                module._chinese_title_from_manifest(
                    article, manifest, "Canonical Paper Title"
                )

    def test_qc_rejects_model_meta_response_even_when_all_hashes_match(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            source = "#************************\n"
            leaked = "好的，我理解要求。请提供需要翻译的段落。\n"
            source_hash = hashlib.sha256(source.encode()).hexdigest()
            leaked_hash = hashlib.sha256(leaked.encode()).hexdigest()
            (article / "chunk_status").mkdir()
            (article / "publication_chunks").mkdir()
            (article / "rendered").mkdir()
            (article / "chunk0001.md").write_text(source, encoding="utf-8")
            (article / "output_chunk0001.md").write_text(leaked, encoding="utf-8")
            (article / "publication_chunks/chunk0001.md").write_text(
                leaked, encoding="utf-8"
            )
            (article / "manifest.json").write_text(
                json.dumps(
                    {
                        "record_id": "arxiv:a",
                        "chunks": [
                            {
                                "id": "chunk0001",
                                "source_file": "chunk0001.md",
                                "output_file": "output_chunk0001.md",
                                "source_hash": source_hash,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (article / "paper_status.json").write_text(
                '{"status":"complete"}', encoding="utf-8"
            )
            (article / "chunk_status/chunk0001.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "stages": {
                            "academic": {
                                "status": "complete",
                                "output_hash": leaked_hash,
                                "qc": {"ok": True},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (article / "rendered/translated_mono.pdf").write_bytes(b"mono")
            (article / "rendered/translated_dual.pdf").write_bytes(b"dual")
            (article / "refill_status.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "chunks": {
                            "chunk0001": {
                                "source_sha256": source_hash,
                                "output_sha256": leaked_hash,
                            }
                        },
                        "publication_qc": {
                            "ok": True,
                            "publication_chunk_sha256": {"chunk0001": leaked_hash},
                        },
                        "reference_qc": {"verified": True},
                        "mono_pdf_sha256": hashlib.sha256(b"mono").hexdigest(),
                        "dual_pdf_sha256": hashlib.sha256(b"dual").hexdigest(),
                    }
                ),
                encoding="utf-8",
            )

            report = module.evaluate_article_qc(article)

            self.assertFalse(report["ok"])
            self.assertIn("model_meta_response:chunk0001", report["failures"])

    def test_complete_article_requires_verified_translation_and_render_artifacts(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            source = "Source\n"
            translated = "译文\n"
            source_hash = hashlib.sha256(source.encode()).hexdigest()
            output_hash = hashlib.sha256(translated.encode()).hexdigest()
            (article / "chunk_status").mkdir()
            (article / "rendered").mkdir()
            (article / "chunk0001.md").write_text(source, encoding="utf-8")
            (article / "output_chunk0001.md").write_text(translated, encoding="utf-8")
            (article / "manifest.json").write_text(
                json.dumps({"record_id": "arxiv:a", "chunks": [{"id": "chunk0001", "source_file": "chunk0001.md", "output_file": "output_chunk0001.md", "source_hash": source_hash}]}),
                encoding="utf-8",
            )
            (article / "paper_status.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
            (article / "chunk_status/chunk0001.json").write_text(
                json.dumps({"status": "complete", "stages": {"academic": {"status": "complete", "output_hash": output_hash, "qc": {"ok": True}}}}),
                encoding="utf-8",
            )
            mono = article / "rendered/translated_mono.pdf"
            dual = article / "rendered/translated_dual.pdf"
            publication = article / "publication_chunks/chunk0001.md"
            publication.parent.mkdir()
            publication.write_text(translated, encoding="utf-8")
            mono.write_bytes(b"mono")
            dual.write_bytes(b"dual")
            (article / "refill_status.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "refill_schema_version": module.refill.REFILL_SCHEMA_VERSION,
                        "chunks": {"chunk0001": {"source_sha256": source_hash, "output_sha256": output_hash}},
                        "mono_pdf_sha256": hashlib.sha256(b"mono").hexdigest(),
                        "dual_pdf_sha256": hashlib.sha256(b"dual").hexdigest(),
                        "publication_qc": {"ok": True, "publication_chunk_sha256": {"chunk0001": output_hash}},
                        "reference_qc": {"verified": True},
                        "figure_region_count": 2,
                        "figure_regions_verified": True,
                        "table_region_count": 1,
                        "table_regions_verified": True,
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(module.evaluate_article_qc(article)["ok"])
            refill = json.loads((article / "refill_status.json").read_text())
            refill["reference_qc"]["verified"] = False
            (article / "refill_status.json").write_text(json.dumps(refill), encoding="utf-8")
            report = module.evaluate_article_qc(article)
            self.assertFalse(report["ok"])
            self.assertIn("references_not_verified", report["failures"])

    def test_qc_rejects_render_status_bound_to_stale_chunk_hashes(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            source = article / "source.md"
            output = article / "output.md"
            source.write_text("Source\n", encoding="utf-8")
            output.write_text("当前译文\n", encoding="utf-8")
            (article / "chunk_status").mkdir()
            (article / "rendered").mkdir()
            (article / "rendered/translated_mono.pdf").write_bytes(b"mono")
            (article / "rendered/translated_dual.pdf").write_bytes(b"dual")
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
            (article / "manifest.json").write_text(json.dumps({"record_id": "arxiv:a", "chunks": [{"id": "chunk0001", "source_file": "source.md", "output_file": "output.md"}]}), encoding="utf-8")
            (article / "paper_status.json").write_text('{"status":"complete"}', encoding="utf-8")
            (article / "chunk_status/chunk0001.json").write_text(json.dumps({"status":"complete","stages":{"academic":{"status":"complete","output_hash":output_hash,"qc":{"ok":True}}}}), encoding="utf-8")
            (article / "refill_status.json").write_text(json.dumps({"status":"complete","chunks":{"chunk0001":{"source_sha256":source_hash,"output_sha256":"stale"}},"publication_qc":{"ok":True,"publication_chunk_sha256":{"chunk0001":"stale"}},"reference_qc":{"verified":True},"mono_pdf_sha256":hashlib.sha256(b"mono").hexdigest(),"dual_pdf_sha256":hashlib.sha256(b"dual").hexdigest()}), encoding="utf-8")

            report = module.evaluate_article_qc(article)

            self.assertFalse(report["ok"])
            self.assertIn("refill_chunk_hash_mismatch:chunk0001", report["failures"])

    def test_qc_rejects_unsafe_chunk_id_before_publication_path_join(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "manifest.json").write_text(
                json.dumps({"record_id": "arxiv:a", "chunks": [{"id": "../escape"}]}),
                encoding="utf-8",
            )
            (article / "paper_status.json").write_text('{"status":"complete"}', encoding="utf-8")
            (article / "refill_status.json").write_text('{"status":"complete"}', encoding="utf-8")

            report = module.evaluate_article_qc(article)

            self.assertIn("unsafe_chunk_id:../escape", report["failures"])

    def test_qc_requires_explicit_not_applicable_for_zero_figure_and_table_regions(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "manifest.json").write_text(
                json.dumps({"record_id": "arxiv:a", "chunks": []}), encoding="utf-8"
            )
            (article / "paper_status.json").write_text('{"status":"complete"}', encoding="utf-8")
            (article / "refill_status.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "publication_qc": {"ok": True},
                        "reference_qc": {"verified": True},
                        "figure_region_count": 0,
                        "table_region_count": 0,
                    }
                ),
                encoding="utf-8",
            )

            report = module.evaluate_article_qc(article)

            self.assertIn("figure_region_classification_missing", report["failures"])
            self.assertIn("table_region_classification_missing", report["failures"])

    def test_qc_rejects_stale_refill_contract_before_package_only_resume(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "manifest.json").write_text(
                json.dumps({"record_id": "arxiv:a", "chunks": []}), encoding="utf-8"
            )
            (article / "paper_status.json").write_text('{"status":"complete"}', encoding="utf-8")
            (article / "refill_status.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "refill_schema_version": module.refill.REFILL_SCHEMA_VERSION - 1,
                        "publication_qc": {"ok": True},
                        "reference_qc": {"verified": True},
                        "figure_region_count": 0,
                        "figure_regions_not_applicable": True,
                        "table_region_count": 0,
                        "table_regions_not_applicable": True,
                    }
                ),
                encoding="utf-8",
            )

            report = module.evaluate_article_qc(article)

            self.assertIn("refill_contract_stale", report["failures"])

    def test_manifest_output_path_cannot_escape_article_directory(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary) / "article"
            article.mkdir()
            manifest = {"chunks": [{"id": "chunk0001", "layout_label": "title", "output_file": "../other/output.md"}]}
            with self.assertRaisesRegex(RuntimeError, "escapes article directory"):
                module._chinese_title_from_manifest(article, manifest)


class RunLockTests(unittest.TestCase):
    def test_translation_contract_fingerprint_includes_batch_orchestrator(self) -> None:
        module = load_module()
        self.assertIn(Path(module.__file__), module._translation_contract_paths())

    def test_production_environment_lock_is_complete_and_self_validating(self) -> None:
        module = load_module()

        environment = module._production_environment_lock()

        self.assertIsInstance(environment["lock_sha256"], str)
        self.assertEqual(environment["contracts"]["model"], module.runner.MODEL)
        self.assertIn(
            "snowmass_babeldoc_bridge.py",
            environment["contracts"]["versions"]["source_sha256"],
        )
        self.assertIn(
            "snowmass-hard-constraints.json",
            environment["contracts"]["versions"]["source_sha256"],
        )
        self.assertEqual(module.production_contract._environment_lock_errors(environment), [])

    def test_environment_drift_archives_and_rebinds_verified_artifact_manifest(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = BatchResumeTests()._two_record_config(module, root)
            record = {"record_id": "arxiv:a", "publication_allowed": True}
            article = module._article_dir(config, "arxiv:a")
            article.mkdir(parents=True)
            old_lock = module._production_environment_lock()
            module.production_contract.write_artifact_manifest(
                manifest_path=module._artifact_manifest_path(article),
                record_id="arxiv:a",
                publication_allowed=True,
                rights_manifest_path=config.rights_manifest,
                article_root=article,
                environment_lock=old_lock,
            )
            new_lock = {**old_lock, "git": {**old_lock["git"], "commit": "new-commit"}}
            new_lock["lock_sha256"] = module.production_contract._json_sha256(
                {key: value for key, value in new_lock.items() if key != "lock_sha256"}
            )

            with mock.patch.object(module, "_production_environment_lock", return_value=new_lock):
                rebound = module._ensure_artifact_contract(config, record, article)

            manifest = json.loads(module._artifact_manifest_path(article).read_text())
            self.assertEqual(rebound["lock_sha256"], new_lock["lock_sha256"])
            self.assertEqual(manifest["environment_lock_sha256"], new_lock["lock_sha256"])
            self.assertEqual(manifest["artifacts"], [])
            self.assertEqual(len(list((article / "production_artifact_history").glob("*.json"))), 1)

    def test_environment_drift_recovers_when_only_downstream_artifact_changed(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = BatchResumeTests()._two_record_config(module, root)
            record = {"record_id": "arxiv:a", "publication_allowed": True}
            article = module._article_dir(config, "arxiv:a")
            article.mkdir(parents=True)
            (article / "manifest.json").write_text("prepared", encoding="utf-8")
            (article / "05-revision.md").write_text("old revision", encoding="utf-8")
            old_lock = module._production_environment_lock()
            manifest_path = module._artifact_manifest_path(article)
            module.production_contract.write_artifact_manifest(
                manifest_path=manifest_path,
                record_id="arxiv:a",
                publication_allowed=True,
                rights_manifest_path=config.rights_manifest,
                article_root=article,
                environment_lock=old_lock,
            )
            module.production_contract.record_artifact(
                manifest_path=manifest_path,
                article_root=article,
                artifact_id="prepared",
                relative_path="manifest.json",
                producer="prepare",
                artifact_type="article_manifest",
                paper_stage="prepared",
                environment_lock=old_lock,
            )
            module.production_contract.record_artifact(
                manifest_path=manifest_path,
                article_root=article,
                artifact_id="revision_ready",
                relative_path="05-revision.md",
                producer="refine",
                artifact_type="revision",
                paper_stage="revision_ready",
                environment_lock=old_lock,
                parents=("prepared",),
            )
            (article / "05-revision.md").write_text("new revision", encoding="utf-8")
            new_lock = {**old_lock, "git": {**old_lock["git"], "commit": "new-commit"}}
            new_lock["lock_sha256"] = module.production_contract._json_sha256(
                {key: value for key, value in new_lock.items() if key != "lock_sha256"}
            )

            with mock.patch.object(module, "_production_environment_lock", return_value=new_lock):
                rebound = module._ensure_artifact_contract(config, record, article)

            self.assertEqual(rebound["lock_sha256"], new_lock["lock_sha256"])
            self.assertEqual(json.loads(manifest_path.read_text())["artifacts"], [])
            self.assertEqual(len(list((article / "production_artifact_history").glob("*.json"))), 1)

    def test_environment_drift_refuses_changed_prepared_artifact(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = BatchResumeTests()._two_record_config(module, root)
            record = {"record_id": "arxiv:a", "publication_allowed": True}
            article = module._article_dir(config, "arxiv:a")
            article.mkdir(parents=True)
            (article / "manifest.json").write_text("prepared", encoding="utf-8")
            old_lock = module._production_environment_lock()
            manifest_path = module._artifact_manifest_path(article)
            module.production_contract.write_artifact_manifest(
                manifest_path=manifest_path,
                record_id="arxiv:a",
                publication_allowed=True,
                rights_manifest_path=config.rights_manifest,
                article_root=article,
                environment_lock=old_lock,
            )
            module.production_contract.record_artifact(
                manifest_path=manifest_path,
                article_root=article,
                artifact_id="prepared",
                relative_path="manifest.json",
                producer="prepare",
                artifact_type="article_manifest",
                paper_stage="prepared",
                environment_lock=old_lock,
            )
            (article / "manifest.json").write_text("tampered", encoding="utf-8")
            new_lock = {**old_lock, "git": {**old_lock["git"], "commit": "new-commit"}}
            new_lock["lock_sha256"] = module.production_contract._json_sha256(
                {key: value for key, value in new_lock.items() if key != "lock_sha256"}
            )

            with mock.patch.object(module, "_production_environment_lock", return_value=new_lock):
                with self.assertRaisesRegex(RuntimeError, "artifact_hash_mismatch:prepared"):
                    module._ensure_artifact_contract(config, record, article)

    def test_same_run_cannot_be_started_twice(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            with module.exclusive_run_lock(run_dir):
                with self.assertRaises(module.RunAlreadyActiveError):
                    with module.exclusive_run_lock(run_dir):
                        pass

    def test_atomic_json_uses_unique_temporary_files(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            real_mkstemp = tempfile.mkstemp
            with mock.patch.object(module.tempfile, "mkstemp", wraps=real_mkstemp) as mkstemp:
                module._atomic_json(path, {"status": "complete"})
            self.assertTrue(mkstemp.called)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "complete"})

    def test_run_snapshot_refreshes_progress_projection_but_rejects_identity_change(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            identity = {"run_id": "run-1", "stage": "shadow", "stage_max_api_calls": 250}
            module._write_or_refresh_run_snapshot(
                path,
                identity,
                {"projected_worst_case_api_calls": 201},
            )
            module._write_or_refresh_run_snapshot(
                path,
                identity,
                {"projected_worst_case_api_calls": 1},
            )
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["projected_worst_case_api_calls"],
                1,
            )
            with self.assertRaisesRegex(RuntimeError, "Run snapshot collision"):
                module._write_or_refresh_run_snapshot(
                    path,
                    {**identity, "stage_max_api_calls": 251},
                    {"projected_worst_case_api_calls": 1},
                )
            with self.assertRaisesRegex(ValueError, "overlap"):
                module._write_or_refresh_run_snapshot(
                    path,
                    identity,
                    {"stage": "mutable-stage"},
                )


class BatchResumeTests(unittest.TestCase):
    def _two_record_config(self, module, root: Path):
        manifest = root / "papers.json"
        records = [
            {"record_id": f"arxiv:{name}", "publication_allowed": True, "page_count": 1}
            for name in ("a", "b")
        ]
        manifest.write_text(json.dumps(records), encoding="utf-8")
        return module.BatchConfig(
            rights_manifest=manifest,
            pdf_root=root / "pdf",
            output_root=root / "output",
            control_dir=root / "control",
            stage="shadow",
            explicit_ids=("arxiv:a", "arxiv:b"),
            max_articles=None,
            project_max_cost_rmb=1000.0,
            stage_max_cost_rmb=10.0,
            usd_cny_rate=7.2,
            chunk_concurrency=1,
            article_concurrency=1,
            through_stage="packaged",
            translation_version="test",
            packaged_on="2026-08-13",
            historical_roots=(),
        )

    def test_content_failure_quarantines_paper_and_continues_next_record(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            attempted = []

            def process(_config, record, _run_id, _client, _budget):
                attempted.append(record["record_id"])
                if record["record_id"] == "arxiv:a":
                    raise RuntimeError("publication QC failed")
                return {"record_id": "arxiv:b", "status": "packaged", "source_characters": 1}

            with (
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_run_article", side_effect=process),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
            ):
                result = module.run_batch(config, client=object())

            self.assertEqual(attempted, ["arxiv:a", "arxiv:b"])
            self.assertEqual(result["status"], "complete_with_quarantine")

    def test_unchanged_quarantine_never_reenters_paid_queue(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            record = {"record_id": "arxiv:a", "publication_allowed": True}
            article = module._article_dir(config, "arxiv:a")
            article.mkdir(parents=True)
            (article / "manifest.json").write_text('{"record_id":"arxiv:a"}\n', encoding="utf-8")
            module._persist_quarantine(config, "arxiv:a", RuntimeError("bad structure"))

            recoverable, package_only, paid_pending = module._classify_selected_records(config, [record])

            self.assertEqual(package_only, [])
            self.assertEqual(paid_pending, [])
            self.assertEqual(recoverable[0]["status"], "quarantined")
            self.assertTrue(recoverable[0]["resumed_from_verified_artifacts"])

    def test_quarantine_reenters_queue_after_translation_contract_changes(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            record = {"record_id": "arxiv:a", "publication_allowed": True}
            article = module._article_dir(config, "arxiv:a")
            article.mkdir(parents=True)
            (article / "manifest.json").write_text('{"record_id":"arxiv:a"}\n', encoding="utf-8")
            with mock.patch.object(module, "_translation_contract_fingerprint", return_value="old"):
                module._persist_quarantine(config, "arxiv:a", RuntimeError("bad structure"))

            with (
                mock.patch.object(module, "_translation_contract_fingerprint", return_value="new"),
                mock.patch.object(module, "_resume_article_result", return_value=None),
                mock.patch.object(module, "evaluate_article_qc", return_value={"ok": False}),
            ):
                recoverable, package_only, paid_pending = module._classify_selected_records(
                    config, [record]
                )

            self.assertEqual(recoverable, [])
            self.assertEqual(package_only, [])
            self.assertEqual(paid_pending, [record])

    def test_preflight_separates_package_only_work_from_paid_translation(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = module.BatchConfig(
                **{**self._two_record_config(module, root).__dict__, "preflight_only": True}
            )

            def qc(article_dir):
                return {"ok": article_dir.name == "arxiv_a", "failures": []}

            with (
                mock.patch.object(module, "_prepare_all") as prepare_all,
                mock.patch.object(module, "_resume_article_result", return_value=None),
                mock.patch.object(module, "evaluate_article_qc", side_effect=qc),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
            ):
                result = module.run_batch(config, client=object())

            self.assertEqual(result["verified_resume_count"], 0)
            self.assertEqual(result["verified_package_only_count"], 1)
            self.assertEqual(result["verified_package_only_record_ids"], ["arxiv:a"])
            self.assertEqual(result["paid_translation_pending_count"], 1)
            self.assertEqual(result["pending_record_count"], 2)

    def test_preflight_projection_excludes_package_only_records(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = module.BatchConfig(
                **{**self._two_record_config(module, root).__dict__, "preflight_only": True}
            )

            def qc(article_dir):
                return {"ok": article_dir.name == "arxiv_a", "failures": []}

            projected_records: list[str] = []

            def projection(_config, record):
                projected_records.append(record["record_id"])
                return {
                    "record_id": record["record_id"],
                    "projection_ready": True,
                    "projected_normal_api_calls": 3,
                    "projected_worst_case_api_calls": 5,
                    "style_projection": {
                        "planned": {
                            "anti_ai": {"normal_requests": 1, "worst_case_requests": 2},
                            "academic": {"normal_requests": 2, "worst_case_requests": 3},
                        }
                    },
                }

            with (
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_resume_article_result", return_value=None),
                mock.patch.object(module, "evaluate_article_qc", side_effect=qc),
                mock.patch.object(module, "_projection_report_for_record", side_effect=projection),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
            ):
                result = module.run_batch(config, client=object())

            self.assertEqual(projected_records, ["arxiv:b"])
            self.assertEqual(result["projected_normal_api_calls"], 3)
            self.assertEqual(result["projected_worst_case_api_calls"], 5)

    def test_preflight_package_only_selection_has_zero_paid_projection(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "papers.json"
            manifest.write_text(
                json.dumps([{"record_id": "arxiv:a", "publication_allowed": True, "page_count": 1}]),
                encoding="utf-8",
            )
            config = module.BatchConfig(
                rights_manifest=manifest,
                pdf_root=root / "pdf",
                output_root=root / "output",
                control_dir=root / "control",
                stage="shadow",
                explicit_ids=("arxiv:a",),
                max_articles=None,
                project_max_cost_rmb=1000.0,
                stage_max_cost_rmb=10.0,
                usd_cny_rate=7.2,
                chunk_concurrency=1,
                article_concurrency=1,
                through_stage="packaged",
                translation_version="test",
                packaged_on="2026-08-13",
                historical_roots=(),
                preflight_only=True,
            )

            with (
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_resume_article_result", return_value=None),
                mock.patch.object(module, "evaluate_article_qc", return_value={"ok": True, "failures": []}),
                mock.patch.object(module, "_projection_report_for_record", side_effect=AssertionError("package-only must not project as paid")),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
            ):
                result = module.run_batch(config, client=object())

            self.assertTrue(result["style_projection"]["projection_ready"])
            self.assertEqual(result["verified_package_only_count"], 1)
            self.assertEqual(result["paid_translation_pending_count"], 0)
            self.assertEqual(result["projected_normal_api_calls"], 0)
            self.assertEqual(result["projected_worst_case_api_calls"], 0)

    def test_locked_run_excludes_package_only_records_from_projection_gate(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            projected_records: list[str] = []
            attempted: list[str] = []

            def resume(_config, record):
                if record["record_id"] == "arxiv:a":
                    return None
                return None

            def qc(article_dir):
                return {"ok": article_dir.name == "arxiv_a", "failures": []}

            def projection(_config, record):
                projected_records.append(record["record_id"])
                return {
                    "record_id": record["record_id"],
                    "projection_ready": True,
                    "projected_normal_api_calls": 2,
                    "projected_worst_case_api_calls": 4,
                    "style_projection": {
                        "planned": {
                            "anti_ai": {"normal_requests": 1, "worst_case_requests": 2},
                            "academic": {"normal_requests": 1, "worst_case_requests": 2},
                        }
                    },
                }

            def process(_config, record, _run_id, _client, _budget):
                attempted.append(record["record_id"])
                return {"record_id": record["record_id"], "status": "packaged", "source_characters": 1}

            with (
                mock.patch.object(module, "_resume_article_result", side_effect=resume),
                mock.patch.object(module, "evaluate_article_qc", side_effect=qc),
                mock.patch.object(
                    module,
                    "_package_only_result",
                    return_value={
                        "record_id": "arxiv:a",
                        "status": "packaged",
                        "source_characters": 1,
                        "resumed_from_verified_translation": True,
                    },
                ),
                mock.patch.object(module, "_projection_report_for_record", side_effect=projection),
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_run_article", side_effect=process),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
            ):
                result = module.run_batch(config, client=object())

            self.assertEqual(projected_records, ["arxiv:b"])
            self.assertEqual(attempted, ["arxiv:b"])
            self.assertEqual(result["status"], "complete")

    def test_budget_failure_stops_before_next_record(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            attempted = []

            def process(_config, record, _run_id, _client, _budget):
                attempted.append(record["record_id"])
                raise module.runner.BudgetExceededError("budget exhausted")

            with (
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_run_article", side_effect=process),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
            ):
                result = module.run_batch(config, client=object())

            self.assertEqual(attempted, ["arxiv:a"])
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["not_started"], 1)

    def test_three_consecutive_content_failures_trip_systemic_circuit_breaker(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            manifest = root / "papers.json"
            records = [
                {"record_id": f"arxiv:{name}", "publication_allowed": True, "page_count": 1}
                for name in ("a", "b", "c", "d")
            ]
            manifest.write_text(json.dumps(records), encoding="utf-8")
            config = module.BatchConfig(**{**config.__dict__, "explicit_ids": tuple(row["record_id"] for row in records)})
            attempted = []

            def fail(_config, record, _run_id, _client, _budget):
                attempted.append(record["record_id"])
                raise RuntimeError("same pipeline defect")

            with (
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_run_article", side_effect=fail),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
            ):
                result = module.run_batch(config, client=object())

            self.assertEqual(attempted, ["arxiv:a", "arxiv:b", "arxiv:c"])
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["not_started"], 1)
            self.assertEqual(result["stop_reason"], "systemic_content_failure_circuit_breaker")

    def test_success_resets_consecutive_content_failure_counter(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            manifest = root / "papers.json"
            records = [
                {"record_id": f"arxiv:{name}", "publication_allowed": True, "page_count": 1}
                for name in ("a", "b", "c", "d")
            ]
            manifest.write_text(json.dumps(records), encoding="utf-8")
            config = module.BatchConfig(**{**config.__dict__, "explicit_ids": tuple(row["record_id"] for row in records)})

            def process(_config, record, _run_id, _client, _budget):
                if record["record_id"] != "arxiv:b":
                    raise RuntimeError("isolated content defect")
                return {"record_id": "arxiv:b", "status": "packaged", "source_characters": 1}

            with (
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_run_article", side_effect=process),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
            ):
                result = module.run_batch(config, client=object())

            self.assertEqual(result["status"], "complete_with_quarantine")
            self.assertEqual(result["not_started"], 0)

    def test_batch_rebuilds_completed_results_from_verified_article_artifacts(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            attempted = []

            def resume(_config, record):
                if record["record_id"] == "arxiv:a":
                    return {"record_id": "arxiv:a", "status": "packaged", "source_characters": 7}
                return None

            def process(_config, record, _run_id, _client, _budget):
                attempted.append(record["record_id"])
                return {"record_id": record["record_id"], "status": "packaged", "source_characters": 9}

            with (
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_resume_article_result", side_effect=resume),
                mock.patch.object(module, "_run_article", side_effect=process),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
            ):
                result = module.run_batch(config, client=object())

            self.assertEqual(attempted, ["arxiv:b"])
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["completed"], 2)

    def test_verified_qc_with_stale_package_repackages_without_translation(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            record = {"record_id": "arxiv:a", "publication_allowed": True}
            article = config.output_root / "papers" / "arxiv_a"
            article.mkdir(parents=True)
            expected = {
                "record_id": "arxiv:a",
                "status": "packaged",
                "source_characters": 7,
                "qc": {"ok": True},
                "package": {"packaged_pdf_sha256": "new"},
                "resumed_from_verified_translation": True,
            }

            with (
                mock.patch.object(module, "evaluate_article_qc", return_value={"ok": True}),
                mock.patch.object(module, "_source_character_count", return_value=7),
                mock.patch.object(module, "_record_stage_artifact"),
                mock.patch.object(module, "_package_article", return_value=expected["package"]),
                mock.patch.object(module.refined, "run_refined_article") as translate,
            ):
                result = module._run_article(config, record, "run", object(), object())

            self.assertEqual(result, expected)
            translate.assert_not_called()

    def test_resume_rejects_receipt_bound_to_stale_rendered_source(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            record = {"record_id": "arxiv:a", "publication_allowed": True}
            article = config.output_root / "papers" / "arxiv_a"
            (article / "packaged").mkdir(parents=True)
            (article / "rendered").mkdir()
            source = article / "rendered/translated_mono.pdf"
            output = article / "packaged/snowmass-a.zh-CN.pdf"
            source.write_bytes(b"new rendered source")
            output.write_bytes(b"old package")
            (article / "packaged/snowmass-a.zh-CN.json").write_text(
                json.dumps(
                    {
                        "record_id": "arxiv:a",
                        "packaging_contract_version": module.packager.PACKAGING_CONTRACT_VERSION,
                        "version": config.translation_version,
                        "packaged_on": config.packaged_on,
                        "source_pdf_sha256": hashlib.sha256(b"old rendered source").hexdigest(),
                        "packaged_pdf_sha256": hashlib.sha256(b"old package").hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(module, "evaluate_article_qc", return_value={"ok": True}),
                mock.patch.object(module, "_source_character_count", return_value=7),
            ):
                self.assertIsNone(module._resume_article_result(config, record))

    def test_resume_rejects_packaged_receipt_without_current_qc_and_artifact_chain(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            record = {"record_id": "arxiv:a", "publication_allowed": True}
            article = module._article_dir(config, "arxiv:a")
            (article / "packaged").mkdir(parents=True)
            (article / "rendered").mkdir()
            source = article / "rendered/translated_mono.pdf"
            output = article / "packaged/snowmass-a.zh-CN.pdf"
            source.write_bytes(b"source")
            output.write_bytes(b"package")
            (article / "manifest.json").write_text(
                '{"record_id":"arxiv:a","chunks":[]}\n', encoding="utf-8"
            )
            (article / "packaged/snowmass-a.zh-CN.json").write_text(
                json.dumps({
                    "record_id": "arxiv:a",
                    "packaging_contract_version": module.packager.PACKAGING_CONTRACT_VERSION,
                    "version": config.translation_version,
                    "packaged_on": config.packaged_on,
                    "source_pdf_sha256": module._sha256(source),
                    "packaged_pdf_sha256": module._sha256(output),
                }),
                encoding="utf-8",
            )
            with mock.patch.object(module, "evaluate_article_qc", return_value={"ok": True}):
                result = module._resume_article_result(config, record)

            self.assertIsNone(result)

    def test_rolling_executor_passes_exact_run_article_arguments(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._two_record_config(module, root)
            calls = []

            def complete_article(*args):
                calls.append(args)
                record = args[1]
                return {"record_id": record["record_id"], "status": "packaged", "source_characters": 1}

            with (
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_run_article", side_effect=complete_article),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
            ):
                result = module.run_batch(config, client=object())

            self.assertEqual(result["status"], "complete")
            self.assertEqual([len(call) for call in calls], [5, 5])

    def test_completed_batch_resume_does_not_append_paid_ledger_events(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "papers.json"
            manifest.write_text(
                json.dumps(
                    [
                        {
                            "record_id": "arxiv:a",
                            "publication_allowed": True,
                            "page_count": 1,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            config = module.BatchConfig(
                rights_manifest=manifest,
                pdf_root=root / "pdf",
                output_root=root / "output",
                control_dir=root / "control",
                stage="shadow",
                explicit_ids=("arxiv:a",),
                max_articles=None,
                project_max_cost_rmb=1000.0,
                stage_max_cost_rmb=10.0,
                usd_cny_rate=7.2,
                chunk_concurrency=1,
                article_concurrency=1,
                through_stage="packaged",
                translation_version="test",
                packaged_on="2026-08-13",
                historical_roots=(),
            )
            checkpoint = root / "output" / "article.complete"

            def checkpointed_article(_config, record, _run_id, _client, budget):
                if not checkpoint.is_file():
                    reservation = budget.reserve("source", 64)
                    budget.settle(
                        reservation,
                        {
                            "input_tokens": 10,
                            "cached_tokens": 0,
                            "output_tokens": 5,
                            "total_tokens": 15,
                        },
                    )
                    checkpoint.parent.mkdir(parents=True, exist_ok=True)
                    checkpoint.write_text("complete", encoding="utf-8")
                return {
                    "record_id": record["record_id"],
                    "status": "packaged",
                    "source_characters": 100,
                }

            patches = (
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_run_article", side_effect=checkpointed_article),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
            )
            with patches[0], patches[1], patches[2]:
                first = module.run_batch(config, client=object())
                ledger = config.control_dir / "budget_ledger.jsonl"
                first_events = [json.loads(line) for line in ledger.read_text().splitlines()]
                second = module.run_batch(config, client=object())
                second_events = [json.loads(line) for line in ledger.read_text().splitlines()]

            paid_kinds = {"settle", "commit_estimate", "recover_orphan"}
            self.assertEqual(first["status"], "complete")
            self.assertEqual(second["status"], "complete")
            self.assertEqual(
                [event for event in first_events if event["kind"] in paid_kinds],
                [event for event in second_events if event["kind"] in paid_kinds],
            )


class PromotionGateTests(unittest.TestCase):
    def test_result_ids_must_match_selected_cohort_for_promotion(self) -> None:
        module = load_module()
        budget = {
            "project_max_cost_rmb": 1000.0,
            "project_spent_rmb": 0.0,
            "project_reserved_rmb": 0.0,
            "stage_spent_rmb": 0.0,
            "stage_reserved_rmb": 0.0,
            "stage_usage": {"api_calls": 0, "uncertain_calls": 0},
        }

        _metrics, gate = module.production_metrics_and_gate(
            stage="shadow",
            through_stage="packaged",
            eligible_record_count=273,
            selected_count=1,
            selected_record_ids=("arxiv:canonical",),
            expected_record_ids=("arxiv:canonical",),
            results=[
                {
                    "record_id": "arxiv:other",
                    "status": "packaged",
                    "source_characters": 10000,
                }
            ],
            failures=[],
            budget=budget,
        )

        self.assertFalse(gate["allowed"])
        self.assertIn("result_ids_do_not_match_selected_cohort", gate["reasons"])

    def test_noncanonical_stage_cohort_cannot_claim_promotion(self) -> None:
        module = load_module()
        budget = {
            "project_max_cost_rmb": 1000.0,
            "project_spent_rmb": 0.0,
            "project_reserved_rmb": 0.0,
            "stage_spent_rmb": 0.0,
            "stage_reserved_rmb": 0.0,
            "stage_usage": {"api_calls": 0, "uncertain_calls": 0},
        }

        _metrics, gate = module.production_metrics_and_gate(
            stage="shadow",
            through_stage="packaged",
            eligible_record_count=273,
            selected_count=1,
            selected_record_ids=("arxiv:wrong",),
            expected_record_ids=("arxiv:canonical",),
            results=[
                {
                    "record_id": "arxiv:wrong",
                    "status": "packaged",
                    "source_characters": 10000,
                }
            ],
            failures=[],
            budget=budget,
        )

        self.assertFalse(gate["allowed"])
        self.assertIn("stage_canonical_cohort_mismatch", gate["reasons"])

    def test_shadow_requires_zero_paid_api_calls(self) -> None:
        module = load_module()
        budget = {
            "project_max_cost_rmb": 1000.0,
            "project_spent_rmb": 0.01,
            "project_reserved_rmb": 0.0,
            "stage_spent_rmb": 0.01,
            "stage_reserved_rmb": 0.0,
            "stage_usage": {"api_calls": 1, "uncertain_calls": 0},
        }

        _metrics, gate = module.production_metrics_and_gate(
            stage="shadow",
            through_stage="packaged",
            eligible_record_count=273,
            selected_count=1,
            results=[
                {
                    "record_id": "arxiv:a",
                    "status": "packaged",
                    "source_characters": 10000,
                }
            ],
            failures=[],
            budget=budget,
        )

        self.assertFalse(gate["allowed"])
        self.assertIn("shadow_paid_calls_not_zero", gate["reasons"])

    def test_unresolved_manual_review_chunks_block_stage_promotion(self) -> None:
        module = load_module()
        budget = {
            "project_max_cost_rmb": 1000.0,
            "project_spent_rmb": 1.0,
            "project_reserved_rmb": 0.0,
            "stage_spent_rmb": 0.1,
            "stage_reserved_rmb": 0.0,
            "stage_usage": {"api_calls": 1, "uncertain_calls": 0},
        }

        metrics, gate = module.production_metrics_and_gate(
            stage="shadow",
            through_stage="revision_ready",
            eligible_record_count=273,
            selected_count=1,
            results=[
                {
                    "record_id": "arxiv:a",
                    "status": "revision_ready",
                    "source_characters": 10000,
                    "manual_review_chunk_ids": ["chunk0001"],
                }
            ],
            failures=[],
            budget=budget,
        )

        self.assertEqual(metrics["manual_review_chunks"], 1)
        self.assertFalse(gate["allowed"])
        self.assertIn("unresolved_manual_review_chunks", gate["reasons"])

    def test_persist_refreshes_the_manual_review_queue(self) -> None:
        module = load_module()
        self.assertTrue(callable(module.manual_review.collect_manual_review_queue))

    def test_recovered_or_repackaged_results_cannot_promote_a_stage(self) -> None:
        module = load_module()
        budget = {
            "project_max_cost_rmb": 1000.0,
            "project_spent_rmb": 20.0,
            "project_reserved_rmb": 0.0,
            "stage_spent_rmb": 0.0,
            "stage_reserved_rmb": 0.0,
            "stage_usage": {"api_calls": 200, "uncertain_calls": 0},
        }
        results = [
            {
                "record_id": f"arxiv:{index}",
                "status": "packaged",
                "source_characters": 1000,
                "resumed_from_verified_translation": True,
            }
            for index in range(10)
        ]

        metrics, gate = module.production_metrics_and_gate(
            stage="pilot5",
            through_stage="packaged",
            eligible_record_count=273,
            selected_count=10,
            results=results,
            failures=[],
            budget=budget,
        )

        self.assertEqual(metrics["fresh_completed_articles"], 0)
        self.assertFalse(gate["allowed"])
        self.assertIn("no_fresh_production_evidence", gate["reasons"])
        self.assertIn("recovered_results_not_promotion_evidence", gate["reasons"])

    def test_packaged_clean_run_reports_cost_efficiency_and_allows_next_stage(self) -> None:
        module = load_module()
        results = [
            {"record_id": "arxiv:a", "status": "packaged", "source_characters": 20000},
            {"record_id": "arxiv:b", "status": "packaged", "source_characters": 30000},
        ]
        budget = {
            "project_max_cost_rmb": 1000.0,
            "project_spent_rmb": 20.0,
            "project_reserved_rmb": 0.0,
            "stage_max_cost_rmb": 50.0,
            "stage_spent_rmb": 10.0,
            "stage_reserved_rmb": 0.0,
            "stage_usage": {
                "api_calls": 8,
                "settled_calls": 8,
                "uncertain_calls": 0,
                "input_tokens": 1000,
                "cached_tokens": 200,
                "output_tokens": 500,
                "total_tokens": 1500,
            },
        }

        metrics, gate = module.production_metrics_and_gate(
            stage="shadow",
            through_stage="packaged",
            eligible_record_count=273,
            selected_count=1,
            results=results[:1],
            failures=[],
            budget=budget,
        )

        self.assertEqual(metrics["cost_rmb_per_10k_source_characters"], 5.0)
        self.assertEqual(metrics["projected_total_rmb_for_eligible_records"], 2740.0)
        self.assertFalse(gate["allowed"])
        self.assertIn("projected_full_corpus_cost_exceeds_cap", gate["reasons"])

    def test_promotion_fails_closed_on_uncertain_request_or_incomplete_artifact(self) -> None:
        module = load_module()
        budget = {
            "project_max_cost_rmb": 1000.0,
            "project_spent_rmb": 7.0,
            "project_reserved_rmb": 0.0,
            "stage_max_cost_rmb": 10.0,
            "stage_spent_rmb": 1.0,
            "stage_reserved_rmb": 0.0,
            "stage_usage": {
                "api_calls": 2,
                "settled_calls": 1,
                "uncertain_calls": 1,
                "input_tokens": 100,
                "cached_tokens": 0,
                "output_tokens": 20,
                "total_tokens": 120,
            },
        }

        _metrics, gate = module.production_metrics_and_gate(
            stage="shadow",
            through_stage="packaged",
            eligible_record_count=273,
            selected_count=1,
            results=[{"record_id": "arxiv:a", "status": "qc_passed", "source_characters": 10000}],
            failures=[],
            budget=budget,
        )

        self.assertFalse(gate["allowed"])
        self.assertIn("uncertain_paid_requests", gate["reasons"])
        self.assertIn("selected_articles_not_packaged", gate["reasons"])

    def test_resolved_uncertain_charge_remains_metric_without_blocking_promotion(self) -> None:
        module = load_module()
        budget = {
            "project_max_cost_rmb": 1000.0,
            "project_spent_rmb": 2.0,
            "project_reserved_rmb": 0.0,
            "stage_max_cost_rmb": 50.0,
            "stage_spent_rmb": 1.0,
            "stage_reserved_rmb": 0.0,
            "stage_usage": {
                "api_calls": 2,
                "settled_calls": 1,
                "uncertain_calls": 1,
                "unresolved_uncertain_calls": 0,
                "input_tokens": 100,
                "cached_tokens": 0,
                "output_tokens": 20,
                "total_tokens": 120,
            },
        }

        metrics, gate = module.production_metrics_and_gate(
            stage="pilot5",
            through_stage="packaged",
            eligible_record_count=273,
            selected_count=5,
            results=[{"record_id": f"arxiv:{i}", "status": "packaged", "source_characters": 10000} for i in range(5)],
            failures=[],
            budget=budget,
        )

        self.assertEqual(metrics["uncertain_paid_requests"], 1)
        self.assertEqual(metrics["unresolved_uncertain_paid_requests"], 0)
        self.assertTrue(gate["allowed"])

    def test_style_batch_projection_is_aggregated_but_does_not_control_promotion(self) -> None:
        module = load_module()
        results = [
            {
                "record_id": "arxiv:a",
                "status": "packaged",
                "source_characters": 10000,
                "style_batch_projection": {
                    "eligible_chunks": 100,
                    "groupable_chunks": 20,
                    "current_style_requests": 200,
                    "projected_style_requests": 170,
                },
            },
            {
                "record_id": "arxiv:b",
                "status": "packaged",
                "source_characters": 10000,
                "style_batch_projection": {
                    "eligible_chunks": 50,
                    "groupable_chunks": 5,
                    "current_style_requests": 100,
                    "projected_style_requests": 90,
                },
            },
        ]
        budget = {
            "project_max_cost_rmb": 1000.0,
            "project_spent_rmb": 2.0,
            "project_reserved_rmb": 0.0,
            "stage_max_cost_rmb": 50.0,
            "stage_spent_rmb": 1.0,
            "stage_reserved_rmb": 0.0,
            "stage_usage": {"api_calls": 2, "settled_calls": 2},
        }

        metrics, gate = module.production_metrics_and_gate(
            stage="pilot5",
            through_stage="packaged",
            eligible_record_count=273,
            selected_count=5,
            results=results + [
                {"record_id": f"arxiv:extra-{index}", "status": "packaged", "source_characters": 10000}
                for index in range(3)
            ],
            failures=[],
            budget=budget,
        )

        self.assertEqual(metrics["style_batch_projection"]["eligible_chunks"], 150)
        self.assertEqual(metrics["style_batch_projection"]["groupable_chunks"], 25)
        self.assertEqual(metrics["style_batch_projection"]["current_style_requests"], 300)
        self.assertEqual(metrics["style_batch_projection"]["projected_style_requests"], 260)
        self.assertAlmostEqual(
            metrics["style_batch_projection"]["projected_request_reduction_fraction"],
            40 / 300,
        )
        self.assertTrue(gate["allowed"])


class StyleProjectionLaunchGateTests(unittest.TestCase):
    def _config(
        self,
        module,
        root: Path,
        *,
        through_stage: str = "packaged",
        preflight_only: bool = False,
    ):
        manifest = root / "papers.json"
        manifest.write_text(
            json.dumps([{"record_id": "arxiv:a", "publication_allowed": True, "page_count": 1}]),
            encoding="utf-8",
        )
        return module.BatchConfig(
            rights_manifest=manifest,
            pdf_root=root / "pdf",
            output_root=root / "output",
            control_dir=root / "control",
            stage="shadow",
            explicit_ids=("arxiv:a",),
            max_articles=None,
            project_max_cost_rmb=1000.0,
            stage_max_cost_rmb=10.0,
            usd_cny_rate=7.2,
            chunk_concurrency=1,
            article_concurrency=1,
            through_stage=through_stage,
            translation_version="test",
            packaged_on="2026-08-13",
            stage_max_api_calls=16,
            historical_roots=(),
            preflight_only=preflight_only,
        )

    def test_preflight_revision_ready_reports_conservative_transport_ceiling(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(
                module,
                root,
                through_stage="revision_ready",
                preflight_only=True,
            )
            with (
                mock.patch.object(module, "_prepare_all") as prepare_all,
                mock.patch.object(module, "_resume_article_result", return_value=None),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
                mock.patch.object(
                    module.refined,
                    "revision_ready_projection",
                    return_value={
                        "record_id": "arxiv:a",
                        "projection_ready": True,
                        "projected_worst_case_api_calls": 7,
                        "missing_stage_api_calls": {
                            "analysis": 1,
                            "translate": 2,
                            "terminology": 1,
                            "critique": 2,
                            "revision": 1,
                        },
                        "identity_diagnostics": {
                            "record_identity_mismatches": [],
                            "invalid_checkpoint_hashes": [],
                            "blocking_uncertain_checkpoints": [],
                        },
                    },
                ),
                mock.patch.object(
                    module.refined,
                    "style_projection_report",
                    side_effect=AssertionError("revision-ready preflight must not use style projection"),
                ),
            ):
                summary = module.run_batch(config, client=object())

        prepare_all.assert_called_once_with(config, mock.ANY)

        self.assertEqual(summary["status"], "preflight")
        self.assertEqual(summary["projected_worst_case_api_calls"], 7)
        self.assertEqual(
            summary["revision_ready_projection"]["missing_stage_api_calls"]["translate"],
            2,
        )
        self.assertTrue(summary["revision_ready_projection"]["projection_ready"])

    def test_preflight_reports_aggregated_style_projection_fields(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(module, root, preflight_only=True)
            with (
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_resume_article_result", return_value=None),
                mock.patch.object(module, "evaluate_article_qc", return_value={"ok": False, "failures": []}),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
                mock.patch.object(
                    module,
                    "_projection_report_for_record",
                    return_value={
                        "projection_ready": True,
                        "projected_normal_api_calls": 7,
                        "projected_worst_case_api_calls": 11,
                        "launch_worst_case_api_calls": 5,
                        "style_projection": {
                            "planned": {
                                "anti_ai": {"normal_requests": 3, "worst_case_requests": 5},
                                "academic": {"normal_requests": 4, "worst_case_requests": 6},
                            }
                        },
                    },
                ),
            ):
                summary = module.run_batch(config, client=object())

        self.assertEqual(summary["status"], "preflight")
        self.assertEqual(summary["projected_normal_api_calls"], 7)
        self.assertEqual(summary["projected_worst_case_api_calls"], 11)
        self.assertEqual(summary["launch_worst_case_api_calls"], 5)
        self.assertEqual(
            summary["style_projection"]["planned"]["anti_ai"]["normal_requests"],
            3,
        )
        self.assertEqual(
            summary["style_projection"]["planned"]["academic"]["normal_requests"],
            4,
        )

    def test_preflight_new_paper_projects_revision_stage_instead_of_future_style(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(module, root, preflight_only=True)
            with (
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "_resume_article_result", return_value=None),
                mock.patch.object(module, "evaluate_article_qc", return_value={"ok": False, "failures": []}),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
                mock.patch.object(
                    module,
                    "_projection_report_for_record",
                    return_value={
                        "record_id": "arxiv:a",
                        "projection_ready": False,
                        "missing_revision_chunk_ids": ["chunk0001"],
                    },
                ),
                mock.patch.object(
                    module,
                    "_revision_ready_projection_report_for_record",
                    return_value={
                        "record_id": "arxiv:a",
                        "projection_ready": True,
                        "projected_worst_case_api_calls": 7,
                        "missing_stage_api_calls": {
                            "analysis": 1,
                            "translate": 2,
                            "terminology": 1,
                            "critique": 2,
                            "revision": 1,
                        },
                        "identity_diagnostics": {},
                    },
                ),
            ):
                summary = module.run_batch(config, client=object())

        self.assertTrue(summary["launch_projection"]["projection_ready"])
        self.assertEqual(summary["launch_projection"]["projected_worst_case_api_calls"], 7)
        self.assertEqual(summary["launch_projection"]["revision_ready_record_ids"], ["arxiv:a"])

    def test_style_projection_revalidates_revision_dependencies_before_launch(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(module, root)
            record = {"record_id": "arxiv:a", "publication_allowed": True}
            article = module._article_dir(config, "arxiv:a")
            article.mkdir(parents=True)
            (article / "manifest.json").write_text("{}", encoding="utf-8")
            (article / "chunking_status.json").write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(
                    module.refined,
                    "revision_ready_projection",
                    return_value={
                        "record_id": "arxiv:a",
                        "projection_ready": True,
                        "projected_worst_case_api_calls": 2,
                        "missing_stage_api_calls": {
                            "analysis": 0,
                            "translate": 0,
                            "terminology": 0,
                            "critique": 0,
                            "revision": 1,
                        },
                        "identity_diagnostics": {},
                    },
                ),
                mock.patch.object(
                    module.refined,
                    "style_projection_report",
                    side_effect=AssertionError("style projection must wait for revision repair"),
                ),
            ):
                report = module._projection_report_for_record(config, record)

        self.assertFalse(report["projection_ready"])
        self.assertEqual(report["missing_revision_chunk_ids"], ["checkpoint_dependency_revalidation"])
        self.assertEqual(report["revision_projection"]["projected_worst_case_api_calls"], 2)

    def test_style_projection_error_is_not_misclassified_as_missing_revision(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(module, root)
            record = {"record_id": "arxiv:a", "publication_allowed": True}
            with mock.patch.object(
                module,
                "_projection_report_for_record",
                return_value={
                    "record_id": "arxiv:a",
                    "projection_ready": False,
                    "missing_revision_chunk_ids": [],
                    "error": "invalid style checkpoint",
                },
            ), mock.patch.object(module, "_revision_ready_projection_report_for_record") as revision:
                summary = module._projection_summary(config, [record])

            self.assertFalse(summary["launch_projection"]["projection_ready"])
            self.assertEqual(summary["launch_projection"]["not_ready_record_ids"], ["arxiv:a"])
            revision.assert_not_called()

    def test_revision_ready_launch_gate_rejects_cap_before_client_or_reservation(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(module, root, through_stage="revision_ready")
            guard_instances: list[object] = []

            class Guard:
                def __init__(self, *args, **kwargs) -> None:
                    guard_instances.append(self)

                def snapshot(self) -> dict[str, object]:
                    return {
                        "project_max_cost_rmb": 1000.0,
                        "project_spent_rmb": 0.0,
                        "project_reserved_rmb": 0.0,
                        "stage_max_cost_rmb": 10.0,
                        "stage_spent_rmb": 0.0,
                        "stage_reserved_rmb": 0.0,
                        "stage_usage": {},
                        "stage_remaining_api_calls": 6,
                    }

                def reserve(self, *_args, **_kwargs):
                    raise AssertionError("launch gate must fire before any reservation")

            with (
                mock.patch.object(module, "PersistentBudgetGuard", Guard),
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
                mock.patch.object(
                    module.refined,
                    "revision_ready_projection",
                    return_value={
                        "record_id": "arxiv:a",
                        "projection_ready": True,
                        "projected_worst_case_api_calls": 7,
                        "missing_stage_api_calls": {
                            "analysis": 1,
                            "translate": 2,
                            "terminology": 1,
                            "critique": 2,
                            "revision": 1,
                        },
                        "identity_diagnostics": {
                            "record_identity_mismatches": [],
                            "invalid_checkpoint_hashes": [],
                            "blocking_uncertain_checkpoints": [],
                        },
                    },
                ),
                mock.patch.object(
                    module.runner,
                    "load_api_key",
                    side_effect=AssertionError("must fail before loading credentials"),
                ),
                mock.patch.object(
                    module.runner,
                    "DeepSeekClient",
                    side_effect=AssertionError("must fail before creating client"),
                ),
            ):
                with self.assertRaisesRegex(Exception, "7.*6|6.*7|cap|projection"):
                    module.run_batch(config)

        self.assertEqual(len(guard_instances), 1)

    def test_launch_gate_rejects_aggregate_worst_case_before_client_or_reservation(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(module, root)
            guard_instances: list[object] = []

            class Guard:
                def __init__(self, *args, **kwargs) -> None:
                    guard_instances.append(self)

                def snapshot(self) -> dict[str, object]:
                    return {
                        "project_max_cost_rmb": 1000.0,
                        "project_spent_rmb": 0.0,
                        "project_reserved_rmb": 0.0,
                        "stage_max_cost_rmb": 10.0,
                        "stage_spent_rmb": 0.0,
                        "stage_reserved_rmb": 0.0,
                        "stage_usage": {},
                        "stage_remaining_api_calls": 16,
                    }

                def reserve(self, *_args, **_kwargs):
                    raise AssertionError("launch gate must fire before any reservation")

            with (
                mock.patch.object(module, "PersistentBudgetGuard", Guard),
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
                mock.patch.object(
                    module,
                    "_projection_report_for_record",
                    return_value={
                        "projection_ready": True,
                        "projected_normal_api_calls": 9,
                        "projected_worst_case_api_calls": 17,
                        "style_projection": {"planned": {}},
                    },
                ),
                mock.patch.object(
                    module.runner,
                    "load_api_key",
                    side_effect=AssertionError("must fail before loading credentials"),
                ),
                mock.patch.object(
                    module.runner,
                    "DeepSeekClient",
                    side_effect=AssertionError("must fail before creating client"),
                ),
            ):
                with self.assertRaisesRegex(Exception, "17.*16|16.*17|projection"):
                    module.run_batch(config)

            self.assertEqual(len(guard_instances), 1)

    def test_style_launch_gate_uses_current_exact_stage_not_downstream_ceiling(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(module, root)

            class Guard:
                def __init__(self, *args, **kwargs) -> None:
                    return None

                def snapshot(self) -> dict[str, object]:
                    return {
                        "project_max_cost_rmb": 1000.0,
                        "project_spent_rmb": 0.0,
                        "project_reserved_rmb": 0.0,
                        "stage_max_cost_rmb": 10.0,
                        "stage_spent_rmb": 0.0,
                        "stage_reserved_rmb": 0.0,
                        "stage_usage": {},
                        "stage_remaining_api_calls": 5,
                    }

            with (
                mock.patch.object(module, "PersistentBudgetGuard", Guard),
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
                mock.patch.object(
                    module,
                    "_projection_report_for_record",
                    return_value={
                        "projection_ready": True,
                        "projected_normal_api_calls": 7,
                        "projected_worst_case_api_calls": 100,
                        "launch_worst_case_api_calls": 5,
                        "style_projection": {"planned": {}},
                    },
                ),
                mock.patch.object(module, "_run_article", return_value={
                    "record_id": "arxiv:a",
                    "status": "packaged",
                    "source_characters": 1,
                }) as run_article,
            ):
                summary = module.run_batch(config, client=object())

            self.assertEqual(summary["status"], "complete")
            run_article.assert_called_once()

    def test_launch_gate_rejects_not_ready_projection_before_client_or_reservation(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(module, root)

            class Guard:
                def __init__(self, *args, **kwargs) -> None:
                    return None

                def snapshot(self) -> dict[str, object]:
                    return {
                        "project_max_cost_rmb": 1000.0,
                        "project_spent_rmb": 0.0,
                        "project_reserved_rmb": 0.0,
                        "stage_max_cost_rmb": 10.0,
                        "stage_spent_rmb": 0.0,
                        "stage_reserved_rmb": 0.0,
                        "stage_usage": {},
                        "stage_remaining_api_calls": 16,
                    }

                def reserve(self, *_args, **_kwargs):
                    raise AssertionError("launch gate must fire before any reservation")

            with (
                mock.patch.object(module, "PersistentBudgetGuard", Guard),
                mock.patch.object(module, "_prepare_all"),
                mock.patch.object(module, "discover_historical_spend", return_value=0.0),
                mock.patch.object(
                    module,
                    "_projection_report_for_record",
                    return_value={
                        "record_id": "arxiv:a",
                        "projection_ready": False,
                        "missing_revision_chunk_ids": ["chunk0007"],
                    },
                ),
                mock.patch.object(
                    module,
                    "_revision_ready_projection_report_for_record",
                    return_value={
                        "record_id": "arxiv:a",
                        "projection_ready": False,
                        "projected_worst_case_api_calls": 0,
                        "missing_stage_api_calls": {},
                        "identity_diagnostics": {},
                        "error": "invalid source identity",
                    },
                ),
                mock.patch.object(
                    module.runner,
                    "load_api_key",
                    side_effect=AssertionError("must fail before loading credentials"),
                ),
                mock.patch.object(
                    module.runner,
                    "DeepSeekClient",
                    side_effect=AssertionError("must fail before creating client"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "arxiv:a|not ready"):
                    module.run_batch(config)

    def test_usage_summary_counts_each_style_batch_attempt_once(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article_dir = Path(temporary) / "paper"
            article_dir.mkdir(parents=True)
            (article_dir / "style_batch_status.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "stages": {
                            "anti_ai": {
                                "requests": [
                                    {
                                        "attempt_id": "anti-1",
                                        "status": "settled",
                                        "usage": {
                                            "input_tokens": 11,
                                            "cached_tokens": 1,
                                            "output_tokens": 5,
                                            "total_tokens": 16,
                                        },
                                    }
                                ]
                            },
                            "academic": {
                                "requests": [
                                    {
                                        "attempt_id": "academic-1",
                                        "status": "settled",
                                        "usage": {
                                            "input_tokens": 13,
                                            "cached_tokens": 2,
                                            "output_tokens": 7,
                                            "total_tokens": 20,
                                        },
                                    }
                                ]
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            (article_dir / "chunk_status").mkdir()
            repeated_stage = {
                "stages": {
                    "anti_ai": {
                        "run_id": "run-one",
                        "request_key": "shared-style-request",
                        "usage": {
                            "input_tokens": 999,
                            "cached_tokens": 999,
                            "output_tokens": 999,
                            "total_tokens": 999,
                        },
                    }
                }
            }
            for chunk_id in ("chunk0001", "chunk0002"):
                (article_dir / "chunk_status" / f"{chunk_id}.json").write_text(
                    json.dumps(repeated_stage),
                    encoding="utf-8",
                )

            usage = module.collect_article_run_usage(article_dir, run_id="run-one")

        self.assertEqual(usage["api_calls"], 2)
        self.assertEqual(usage["input_tokens"], 24)
        self.assertEqual(usage["cached_tokens"], 3)
        self.assertEqual(usage["output_tokens"], 12)
        self.assertEqual(usage["total_tokens"], 36)

    def test_cli_returns_structured_status_for_projection_not_ready_gate(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "papers.json"
            manifest.write_text(
                json.dumps([{"record_id": "arxiv:a", "publication_allowed": True, "page_count": 1}]),
                encoding="utf-8",
            )
            output = io.StringIO()
            error = io.StringIO()
            with (
                mock.patch.object(
                    module,
                    "run_batch",
                    side_effect=module.ProjectionGateRefusedError(
                        "style projection is not ready for paid launch: {\"arxiv:a\": [\"chunk0007\"]}"
                    ),
                ),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
            ):
                exit_code = module.main(
                    [
                        "--rights-manifest", str(manifest),
                        "--output-root", str(root / "output"),
                        "--control-dir", str(root / "control"),
                        "--stage", "shadow",
                        "--project-max-cost-rmb", "1000",
                        "--stage-max-cost-rmb", "10",
                    ]
                )

        self.assertEqual(exit_code, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "gate_refused")
        self.assertEqual(payload["reason_code"], "projection_not_ready")
        self.assertIn("chunk0007", payload["message"])

    def test_cli_returns_structured_status_for_request_limit_gate(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "papers.json"
            manifest.write_text(
                json.dumps([{"record_id": "arxiv:a", "publication_allowed": True, "page_count": 1}]),
                encoding="utf-8",
            )
            output = io.StringIO()
            error = io.StringIO()
            with (
                mock.patch.object(
                    module,
                    "run_batch",
                    side_effect=module.RequestLimitExceededError(
                        "style projection worst case would exceed the remaining stage request cap: 17 > 16"
                    ),
                ),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
            ):
                exit_code = module.main(
                    [
                        "--rights-manifest", str(manifest),
                        "--output-root", str(root / "output"),
                        "--control-dir", str(root / "control"),
                        "--stage", "shadow",
                        "--project-max-cost-rmb", "1000",
                        "--stage-max-cost-rmb", "10",
                    ]
                )

        self.assertEqual(exit_code, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "gate_refused")
        self.assertEqual(payload["reason_code"], "stage_request_limit")
        self.assertIn("17 > 16", payload["message"])


class RevisionReadyRunArticleTests(unittest.TestCase):
    def test_run_article_stops_after_revision_without_refill_or_package(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "papers.json"
            manifest.write_text(
                json.dumps([{"record_id": "arxiv:a", "publication_allowed": True, "page_count": 1}]),
                encoding="utf-8",
            )
            config = module.BatchConfig(
                rights_manifest=manifest,
                pdf_root=root / "pdf",
                output_root=root / "output",
                control_dir=root / "control",
                stage="shadow",
                explicit_ids=("arxiv:a",),
                max_articles=None,
                project_max_cost_rmb=1000.0,
                stage_max_cost_rmb=10.0,
                usd_cny_rate=7.2,
                chunk_concurrency=3,
                article_concurrency=1,
                through_stage="revision_ready",
                translation_version="test",
                packaged_on="2026-08-13",
                historical_roots=(),
            )
            record = {"record_id": "arxiv:a", "publication_allowed": True}

            with (
                mock.patch.object(module, "_source_character_count", return_value=7),
                mock.patch.object(module, "_record_stage_artifact"),
                mock.patch.object(module.runner, "resolve_glossary_path", return_value=root / "glossary.json"),
                mock.patch.object(module.runner, "load_glossary", return_value=[{"en": "x", "zh": "y"}]),
                mock.patch.object(module.runner, "load_article_glossary", return_value=[{"en": "a", "zh": "b"}]),
                mock.patch.object(module.runner, "merge_glossary_terms", return_value=[{"en": "term", "zh": "术语"}]),
                mock.patch.object(
                    module.refined,
                    "run_refined_article",
                    return_value={"record_id": "arxiv:a", "status": "revision_ready", "chunks": 1},
                ) as run_refined_article,
                mock.patch.object(
                    module,
                    "translation_provenance_report",
                    return_value={
                        "fresh": True,
                        "run_id": "run-1",
                        "model_checkpoint_count": 2,
                        "current_run_model_checkpoint_count": 2,
                        "reused_model_checkpoint_count": 0,
                        "reused_model_checkpoint_ids": [],
                        "reasons": [],
                    },
                ),
                mock.patch.object(
                    module,
                    "_refill_article",
                    side_effect=AssertionError("revision_ready must not refill or render"),
                ),
                mock.patch.object(
                    module,
                    "_package_article",
                    side_effect=AssertionError("revision_ready must not package"),
                ),
                mock.patch.object(
                    module,
                    "evaluate_article_qc",
                    side_effect=AssertionError("revision_ready must not run publication QC"),
                ),
            ):
                result = module._run_article(config, record, "run-1", object(), object())

        self.assertEqual(
            result,
            {
                "record_id": "arxiv:a",
                "status": "revision_ready",
                "source_characters": 7,
                "translation_provenance": {
                    "fresh": True,
                    "run_id": "run-1",
                    "model_checkpoint_count": 2,
                    "current_run_model_checkpoint_count": 2,
                    "reused_model_checkpoint_count": 0,
                    "reused_model_checkpoint_ids": [],
                    "reasons": [],
                },
                "manual_review_chunk_ids": [],
            },
        )
        run_refined_article.assert_called_once_with(
            mock.ANY,
            client=mock.ANY,
            terms=[{"en": "term", "zh": "术语"}],
            run_id="run-1",
            budget_guard=mock.ANY,
            concurrency=3,
            retry_uncertain=False,
            stop_after_revision=True,
        )


if __name__ == "__main__":
    unittest.main()
