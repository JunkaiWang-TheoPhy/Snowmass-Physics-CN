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
            module.select_stage_records(records, "baseline", explicit_ids=("arxiv:blocked",))

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
            for stage in ("baseline", "pilot10", "batch50", "remainder")
        }
        stage_ids = {
            stage: {row["record_id"] for row in selected}
            for stage, selected in stages.items()
        }

        self.assertEqual([len(stages[name]) for name in stages], [1, 10, 50, 19])
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

    def test_cli_requires_a_positive_finite_stage_request_cap(self) -> None:
        module = load_module()
        base = [
            "--stage", "baseline",
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
            stage="baseline",
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


class ArticleQCTests(unittest.TestCase):
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

    def test_manifest_output_path_cannot_escape_article_directory(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary) / "article"
            article.mkdir()
            manifest = {"chunks": [{"id": "chunk0001", "layout_label": "title", "output_file": "../other/output.md"}]}
            with self.assertRaisesRegex(RuntimeError, "escapes article directory"):
                module._chinese_title_from_manifest(article, manifest)


class RunLockTests(unittest.TestCase):
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
            stage="baseline",
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
            self.assertEqual(result["quarantined"], 1)
            self.assertEqual(result["not_started"], 0)

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
                stage="baseline",
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
                mock.patch.object(module, "_package_article", return_value=expected["package"]),
                mock.patch.object(module.refined, "run_refined_article") as translate,
            ):
                result = module._run_article(config, record, "run", object(), object())

            self.assertEqual(result, expected)
            translate.assert_not_called()

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
                stage="baseline",
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
            stage="baseline",
            through_stage="packaged",
            eligible_record_count=273,
            selected_count=2,
            results=results,
            failures=[],
            budget=budget,
        )

        self.assertEqual(metrics["cost_rmb_per_10k_source_characters"], 2.0)
        self.assertEqual(metrics["projected_total_rmb_for_eligible_records"], 1375.0)
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
            stage="baseline",
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
            stage="pilot10",
            through_stage="packaged",
            eligible_record_count=273,
            selected_count=1,
            results=[{"record_id": "arxiv:a", "status": "packaged", "source_characters": 10000}],
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
            stage="pilot10",
            through_stage="packaged",
            eligible_record_count=273,
            selected_count=2,
            results=results,
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
            stage="baseline",
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
        self.assertEqual(
            summary["style_projection"]["planned"]["anti_ai"]["normal_requests"],
            3,
        )
        self.assertEqual(
            summary["style_projection"]["planned"]["academic"]["normal_requests"],
            4,
        )

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
                        "projection_ready": False,
                        "missing_revision_chunk_ids": ["chunk0007"],
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
                with self.assertRaisesRegex(RuntimeError, "chunk0007|not ready"):
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
                        "--stage", "baseline",
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
                        "--stage", "baseline",
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
                stage="baseline",
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
            {"record_id": "arxiv:a", "status": "revision_ready", "source_characters": 7},
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
