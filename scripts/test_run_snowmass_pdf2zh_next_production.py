#!/usr/bin/env python3
"""Contract tests for the staged pdf2zh-next production orchestrator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("run_snowmass_pdf2zh_next_production.py")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("pdf2zh-next production orchestrator is not implemented")
    spec = importlib.util.spec_from_file_location(
        "run_snowmass_pdf2zh_next_production", MODULE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Pdf2zhNextProductionTests(unittest.TestCase):
    def test_cli_reexecutes_into_pinned_environment_under_system_python(self) -> None:
        system_python = Path("/usr/bin/python3")
        if not system_python.is_file():
            self.skipTest("macOS system Python is unavailable")
        result = subprocess.run(
            [str(system_python), str(MODULE_PATH), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fail-closed staged production control", result.stdout)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rights_manifest = self.root / "papers.json"
        self.source_manifest = self.root / "sources.json"
        self.pdf_root = self.root / "pdfs"
        self.output_root = self.root / "production"
        self.project_control = self.root / "control"
        self.glossary = self.root / "glossary.json"
        self.pdf_root.mkdir()
        self.output_root.mkdir()
        self.project_control.mkdir()
        self.glossary.write_text(
            json.dumps({"terms": [{"source": "physics", "target": "物理"}]}),
            encoding="utf-8",
        )

    def _write_json(self, path: Path, payload: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def _seed_record(
        self,
        record_id: str,
        *,
        publication_allowed: object = True,
        frontiers: list[str] | None = None,
        page_count: int = 10,
    ) -> dict[str, object]:
        safe = record_id.replace(":", "_").replace("/", "_")
        source_pdf = self.pdf_root / f"{safe}.pdf"
        source_pdf.write_bytes(f"pdf:{record_id}".encode())
        return {
            "record_id": record_id,
            "publication_allowed": publication_allowed,
            "frontiers": list(frontiers or ["UF"]),
            "page_count": page_count,
            "source_pdf": source_pdf,
        }

    def _write_manifests(self, records: list[dict[str, object]]) -> None:
        self._write_json(
            self.rights_manifest,
            [
                {
                    "record_id": record["record_id"],
                    "publication_allowed": record["publication_allowed"],
                    "frontiers": record["frontiers"],
                    "page_count": record["page_count"],
                }
                for record in records
            ],
        )
        self._write_json(
            self.source_manifest,
            {
                "records": [
                    {
                        "record_id": record["record_id"],
                        "pdf_status": "complete",
                        "pdf_bytes": Path(record["source_pdf"]).stat().st_size,
                        "pdf_sha256": sha256_file(Path(record["source_pdf"])),
                    }
                    for record in records
                ]
            },
        )

    def test_selected_page_count_handles_ranges_and_all(self) -> None:
        module = load_module()

        self.assertEqual(module._selected_page_count("1-4"), 4)
        self.assertEqual(module._selected_page_count("1-4,7,9-10"), 7)
        self.assertEqual(module._selected_page_count("all"), 40)
        self.assertEqual(module._selected_page_count(""), 1)

    def test_new_pilot_requires_live_low_risk_source_prefilter(self) -> None:
        module = load_module()
        prefilter = self._write_json(
            self.root / "source-prefilter.json",
            {
                "records": [
                    {
                        "record_id": "arxiv:good",
                        "publication_allowed": True,
                        "eligible": True,
                        "pages": 5,
                        "images": 0,
                        "drawings": 12,
                        "numeric_citations": 3,
                        "citation_ranges": 0,
                        "reference_pages": 1,
                        "contents_pages": 0,
                        "risk_tier": "low_risk",
                        "reasons": [],
                    },
                    {
                        "record_id": "arxiv:complex",
                        "publication_allowed": True,
                        "eligible": False,
                        "pages": 16,
                        "images": 1,
                        "drawings": 22222,
                        "numeric_citations": 129,
                        "citation_ranges": 9,
                        "reference_pages": 1,
                        "contents_pages": 1,
                        "risk_tier": "complex_or_unclassified",
                        "reasons": ["dense_vector_graphics"],
                    },
                ]
            },
        )
        module._validate_low_risk_prefilter(prefilter, ("arxiv:good",))
        with self.assertRaisesRegex(RuntimeError, "low-risk gate failed"):
            module._validate_low_risk_prefilter(prefilter, ("arxiv:complex",))

    def test_text_only_medium_tier_allows_longer_text_without_images(self) -> None:
        module = load_module()
        prefilter = self._write_json(
            self.root / "medium-prefilter.json",
            {
                "records": [
                    {
                        "record_id": "arxiv:medium",
                        "publication_allowed": True,
                        "eligible": False,
                        "risk_tier": "text_only_medium",
                        "pages": 14,
                        "images": 0,
                        "drawings": 0,
                        "numeric_citations": 19,
                        "citation_ranges": 2,
                        "reference_pages": 2,
                        "contents_pages": 0,
                        "reasons": ["over_10_pages", "many_numeric_citations"],
                    }
                ]
            },
        )
        module._validate_low_risk_prefilter(
            prefilter, ("arxiv:medium",), tier="text_only_medium"
        )

    def test_figure_passthrough_tier_limits_graphics_and_keeps_them_verbatim(self) -> None:
        module = load_module()
        prefilter = self._write_json(
            self.root / "figure-prefilter.json",
            {
                "records": [
                    {
                        "record_id": "arxiv:figure",
                        "publication_allowed": True,
                        "eligible": False,
                        "risk_tier": "figure_passthrough_medium",
                        "pages": 8,
                        "images": 2,
                        "drawings": 12,
                        "numeric_citations": 26,
                        "citation_ranges": 0,
                        "reference_pages": 1,
                        "contents_pages": 0,
                        "reasons": ["many_numeric_citations"],
                    }
                ]
            },
        )
        module._validate_low_risk_prefilter(
            prefilter, ("arxiv:figure",), tier="figure_passthrough_medium"
        )

    def test_text_only_long_tier_allows_long_zero_graphics_papers(self) -> None:
        module = load_module()
        prefilter = self._write_json(
            self.root / "long-text-prefilter.json",
            {
                "records": [
                    {
                        "record_id": "arxiv:long",
                        "publication_allowed": True,
                        "eligible": False,
                        "risk_tier": "text_only_long",
                        "pages": 40,
                        "images": 0,
                        "drawings": 0,
                        "numeric_citations": 120,
                        "citation_ranges": 8,
                        "reference_pages": 5,
                        "contents_pages": 0,
                        "reasons": ["over_10_pages"],
                    }
                ]
            },
        )
        module._validate_low_risk_prefilter(
            prefilter, ("arxiv:long",), tier="text_only_long"
        )

    def test_vector_passthrough_long_tier_allows_bounded_vectors_without_images(self) -> None:
        module = load_module()
        prefilter = self._write_json(
            self.root / "vector-long-prefilter.json",
            {
                "records": [
                    {
                        "record_id": "arxiv:vector-long",
                        "publication_allowed": True,
                        "eligible": False,
                        "risk_tier": "vector_passthrough_long",
                        "pages": 30,
                        "images": 0,
                        "drawings": 500,
                        "numeric_citations": 150,
                        "citation_ranges": 10,
                        "reference_pages": 5,
                        "contents_pages": 0,
                        "reasons": ["over_10_pages"],
                    }
                ]
            },
        )
        module._validate_low_risk_prefilter(
            prefilter, ("arxiv:vector-long",), tier="vector_passthrough_long"
        )

    def test_citation_dense_passthrough_tier_keeps_figures_verbatim(self) -> None:
        module = load_module()
        prefilter = self._write_json(
            self.root / "citation-dense-prefilter.json",
            {
                "records": [
                    {
                        "record_id": "arxiv:citation-dense",
                        "publication_allowed": True,
                        "eligible": False,
                        "risk_tier": "citation_dense_passthrough_medium",
                        "pages": 20,
                        "images": 2,
                        "drawings": 500,
                        "numeric_citations": 200,
                        "citation_ranges": 20,
                        "reference_pages": 3,
                        "contents_pages": 0,
                        "reasons": ["many_numeric_citations"],
                    }
                ]
            },
        )
        module._validate_low_risk_prefilter(
            prefilter,
            ("arxiv:citation-dense",),
            tier="citation_dense_passthrough_medium",
        )

    def _plan_args(self, module, stage: str, **overrides: object):
        values = {
            "stage": stage,
            "rights_manifest": self.rights_manifest,
            "source_manifest": self.source_manifest,
            "pdf_root": self.pdf_root,
            "glossary_json": self.glossary,
            "output_root": self.output_root,
            "project_control_dir": self.project_control,
            "project_max_cost_rmb": 50.0,
            "stage_max_cost_rmb": 10.0,
            "stage_max_api_calls": 7,
            "pages": "1,2,3",
            "qps": 1,
            "pool_max_workers": 1,
        }
        values.update(overrides)
        return module.PlanArgs(**values)

    def _write_complete_paper_seal(self, paper: dict[str, object]) -> Path:
        article = Path(paper["article_dir"])
        qc = article / "qc"
        run = article / "run"
        qc.mkdir(parents=True, exist_ok=True)
        run.mkdir(parents=True, exist_ok=True)
        evidence_paths = {
            "finish": run / "finish.json",
            "preflight": Path(str(paper["preflight_path"])),
            "glossary": run / "locked-glossary.csv",
            "protection": qc / "protection.json",
            "semantic": qc / "semantic-report.json",
            "structural": qc / "structural-report.json",
            "visual": qc / "visual-review.json",
            "contact_sheet": qc / "contact-sheet.jpg",
        }
        for name, path in evidence_paths.items():
            if not path.exists():
                path.write_bytes(name.encode())
        manifest = article / "artifact-manifest.json"
        environment = article / "environment-lock.json"
        manifest.write_text("{}", encoding="utf-8")
        environment.write_text("{}", encoding="utf-8")
        qc_receipt_paths = {
            name: qc / f"{name}.json" for name in ("semantic", "structural", "visual")
        }
        for name, path in qc_receipt_paths.items():
            path.write_text(json.dumps({"kind": name}), encoding="utf-8")
        seal = {
            "schema_version": 1,
            "passed": True,
            "state": "visual_qc",
            "record_id": paper["record_id"],
            "source_pdf_sha256": paper["source_sha256"],
            "environment_lock_sha256": "e" * 64,
            "environment_lock_file_sha256": sha256_file(environment),
            "artifact_manifest_sha256": sha256_file(manifest),
            "qc_receipt_hashes": {
                name: sha256_file(path) for name, path in qc_receipt_paths.items()
            },
            "evidence_hashes": {
                name: sha256_file(path) for name, path in evidence_paths.items()
            },
        }
        paper_seal = qc / "paper-seal.json"
        paper_seal.write_text(json.dumps(seal, sort_keys=True), encoding="utf-8")
        return paper_seal

    def test_plan_uses_literal_true_rights_and_rejects_zero_budget_before_api_key(
        self,
    ) -> None:
        module = load_module()
        records = [
            self._seed_record("arxiv:2203.06843", publication_allowed=True),
            self._seed_record("arxiv:false", publication_allowed=False),
            self._seed_record("arxiv:null", publication_allowed=None),
            self._seed_record("arxiv:missing", publication_allowed="missing"),
        ]
        self._write_manifests(records)
        preflight = {"projection": {"request_cap": 1, "max_cost_rmb": 1.25}}

        with (
            mock.patch.object(
                module.ab_runner, "load_api_key", side_effect=AssertionError
            ),
            self.assertRaisesRegex(ValueError, "project budget"),
        ):
            module.plan_stage(
                self._plan_args(module, "deepseek_probe", project_max_cost_rmb=0),
                preflight_runner=lambda _config: preflight,
            )

        plan = module.plan_stage(
            self._plan_args(module, "deepseek_probe"),
            preflight_runner=lambda _config: preflight,
        )

        self.assertEqual(plan["eligible_record_count"], 1)
        self.assertEqual(plan["record_ids"], ["arxiv:2203.06843"])
        self.assertEqual(plan["papers"][0]["record_id"], "arxiv:2203.06843")

    def test_validation_keeps_paper_workers_serial_until_shared_proxy_is_safe(self) -> None:
        module = load_module()

        self.assertEqual(
            module._validate_caps(
                self._plan_args(module, "pilot5", pool_max_workers=1)
            )[:2],
            (50.0, 10.0),
        )
        with self.assertRaisesRegex(ValueError, "must not exceed 2"):
            module._validate_caps(
                self._plan_args(module, "pilot5", pool_max_workers=3)
            )

    def test_request_allocations_follow_page_weight_and_preserve_total(self) -> None:
        module = load_module()
        records = [
            {"page_count": 2},
            {"page_count": 7},
            {"page_count": 3},
            {"page_count": 31},
            {"page_count": 80},
        ]

        allocations = module._request_allocations(1280, records)

        self.assertEqual(sum(allocations), 1280)
        self.assertTrue(all(value > 0 for value in allocations))
        self.assertGreaterEqual(allocations[-1], 790)
        self.assertGreaterEqual(allocations[-2], 300)
        self.assertLessEqual(max(allocations[:3]), 75)

    def test_runtime_allocations_leave_finite_headroom_per_paper(self) -> None:
        module = load_module()
        allocations = module._runtime_request_allocations(
            [10, 25, 5], page_counts=[12, 31, 4]
        )
        self.assertEqual(allocations, [96, 248, 50])
        bounded = module._runtime_request_allocations(
            [10, 25, 5], page_counts=[12, 31, 4], total_cap=128
        )
        self.assertEqual(sum(bounded), 128)
        self.assertTrue(all(actual >= minimum for actual, minimum in zip(bounded, [10, 25, 5])))
        with self.assertRaises(ValueError):
            module._runtime_request_allocations([50], page_counts=[])

    def test_plan_assigns_disjoint_stage_cohorts_and_probe_is_pinned(self) -> None:
        module = load_module()
        probe = self._seed_record("arxiv:2203.06843", frontiers=["AA"], page_count=10)
        second = self._seed_record("arxiv:pilot-b", frontiers=["AA"], page_count=11)
        third = self._seed_record("arxiv:pilot-c", frontiers=["AA"], page_count=12)
        self._write_manifests([probe, second, third])

        stage_map = {
            "deepseek_probe": [probe],
            "pilot5": [second, third],
        }

        with mock.patch.object(
            module.batch_production,
            "select_stage_records",
            side_effect=lambda records, stage, *args, **kwargs: list(stage_map[stage]),
        ):
            deepseek = module.plan_stage(
                self._plan_args(module, "deepseek_probe"),
                preflight_runner=lambda _config: {
                    "projection": {"request_cap": 2, "max_cost_rmb": 1.0}
                },
            )
            pilot = module.plan_stage(
                self._plan_args(module, "pilot5", stage_max_api_calls=4),
                preflight_runner=lambda _config: {
                    "projection": {"request_cap": 2, "max_cost_rmb": 1.0}
                },
            )

        self.assertEqual(deepseek["record_ids"], ["arxiv:2203.06843"])
        self.assertEqual(set(deepseek["record_ids"]) & set(pilot["record_ids"]), set())

        stage_map["deepseek_probe"] = [second]
        with (
            mock.patch.object(
                module.batch_production,
                "select_stage_records",
                side_effect=lambda records, stage, *args, **kwargs: list(
                    stage_map[stage]
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "2203.06843"),
        ):
            module.plan_stage(
                self._plan_args(
                    module,
                    "deepseek_probe",
                    output_root=self.root / "mismatched-probe",
                ),
                preflight_runner=lambda _config: {
                    "projection": {"request_cap": 2, "max_cost_rmb": 1.0}
                },
            )

    def test_paid_stage_partition_covers_every_eligible_record_once(self) -> None:
        module = load_module()
        records = [
            {
                "record_id": f"arxiv:{index:04d}",
                "publication_allowed": True,
                "frontiers": ["UF"],
                "page_count": index + 1,
            }
            for index in range(100)
        ]
        cohorts = {
            stage: module._select_paid_stage_records(records, stage)
            for stage in module.STAGES
        }
        flattened = [
            str(record["record_id"])
            for stage in module.STAGES
            for record in cohorts[stage]
        ]

        self.assertEqual(len(flattened), len(records))
        self.assertEqual(
            set(flattened), {str(record["record_id"]) for record in records}
        )

    def test_plan_rejects_stage_cap_project_cap_and_aggregate_request_cap(self) -> None:
        module = load_module()
        probe = self._seed_record("arxiv:2203.06843")
        pilot = self._seed_record("arxiv:pilot")
        self._write_manifests([probe, pilot])

        with self.assertRaisesRegex(ValueError, "stage budget"):
            module.plan_stage(
                self._plan_args(module, "deepseek_probe", stage_max_cost_rmb=0),
                preflight_runner=lambda _config: {
                    "projection": {"request_cap": 1, "max_cost_rmb": 1.0}
                },
            )

        with self.assertRaisesRegex(
            ValueError, "stage budget must not exceed project budget"
        ):
            module.plan_stage(
                self._plan_args(
                    module,
                    "deepseek_probe",
                    project_max_cost_rmb=5,
                    stage_max_cost_rmb=6,
                ),
                preflight_runner=lambda _config: {
                    "projection": {"request_cap": 1, "max_cost_rmb": 1.0}
                },
            )

        with (
            mock.patch.object(
                module.batch_production,
                "select_stage_records",
                return_value=[probe, pilot],
            ),
            self.assertRaisesRegex(ValueError, "request cap"),
        ):
            module.plan_stage(
                self._plan_args(module, "pilot5", stage_max_api_calls=1),
                preflight_runner=lambda _config: {
                    "projection": {"request_cap": 1, "max_cost_rmb": 1.0}
                },
            )

    def test_launch_suppresses_duplicates_and_prepares_qc(self) -> None:
        module = load_module()
        paper = self._seed_record("arxiv:2203.06843")
        self._write_manifests([paper])
        plan = module.plan_stage(
            self._plan_args(module, "deepseek_probe", stage_max_api_calls=3),
            preflight_runner=lambda _config: {
                "projection": {"request_cap": 2, "max_cost_rmb": 1.25}
            },
        )
        planned_paper = plan["papers"][0]
        article_dir = Path(planned_paper["article_dir"])
        rendered = article_dir / "run" / "rendered"
        self.assertEqual(
            Path(planned_paper["preflight_path"]).name,
            "planned-preflight.json",
        )

        def paid_runner(config):
            rendered.mkdir(parents=True, exist_ok=True)
            (config.output_root / "preflight.json").write_text(
                json.dumps({"status": "live-preflight"}), encoding="utf-8"
            )
            mono = rendered / "translated.pdf"
            mono.write_bytes(b"translated")
            finish = {
                "status": "translated_pending_qc",
                "record_id": config.record_id,
                "source": {"sha256": sha256_file(config.source_pdf)},
                "outputs": {
                    "mono_pdf": {"path": mono.name, "sha256": sha256_file(mono)}
                },
                "glossary": {"csv_sha256": "locked"},
            }
            (config.output_root / "finish.json").write_text(
                json.dumps(finish, sort_keys=True), encoding="utf-8"
            )
            (config.output_root / "status.json").write_text(
                json.dumps(finish, sort_keys=True), encoding="utf-8"
            )
            return finish

        prepare_calls: list[str] = []

        def prepare_runner(**kwargs):
            prepare_calls.append(kwargs["record_id"])
            qc_dir = Path(kwargs["article"]) / "qc"
            qc_dir.mkdir(parents=True, exist_ok=True)
            request_path = qc_dir / "visual-review-request.json"
            request_path.write_text("{}", encoding="utf-8")
            return {
                "status": "awaiting_visual_review",
                "visual_review_request": request_path,
                "contact_sheet": qc_dir / "contact-sheet.jpg",
                "protected_pdf": Path(kwargs["article"])
                / "protected"
                / "translated.protected.pdf",
                "ir_receipt": Path(kwargs["article"]) / "ir" / "receipt.json",
                "protection": qc_dir / "protection.json",
                "semantic": qc_dir / "semantic-report.json",
                "structural": qc_dir / "structural-report.json",
            }

        first = module.launch_stage(
            plan_path=Path(plan["plan_path"]),
            paid_runner=paid_runner,
            prepare_runner=prepare_runner,
        )
        second = module.launch_stage(
            plan_path=Path(plan["plan_path"]),
            paid_runner=lambda _config: self.fail(
                "duplicate launch must be suppressed"
            ),
            prepare_runner=lambda **_kwargs: self.fail(
                "duplicate prepare must be suppressed"
            ),
        )

        self.assertEqual(first["awaiting_visual_review"], 1)
        self.assertEqual(prepare_calls, ["arxiv:2203.06843"])
        self.assertEqual(second["reused_count"], 1)

    def test_resume_reuses_only_hash_matching_finish_and_quarantines_mismatch(
        self,
    ) -> None:
        module = load_module()
        paper = self._seed_record("arxiv:2203.06843")
        self._write_manifests([paper])
        plan = module.plan_stage(
            self._plan_args(module, "deepseek_probe", stage_max_api_calls=3),
            preflight_runner=lambda _config: {
                "projection": {"request_cap": 2, "max_cost_rmb": 1.25}
            },
        )
        planned = plan["papers"][0]
        article_dir = Path(planned["article_dir"])
        run_dir = article_dir / "run"
        qc_dir = article_dir / "qc"
        run_dir.mkdir(parents=True, exist_ok=True)
        qc_dir.mkdir(parents=True, exist_ok=True)
        finish = {
            "status": "translated_pending_qc",
            "source": {"sha256": planned["source_sha256"]},
        }
        (run_dir / "finish.json").write_text(json.dumps(finish), encoding="utf-8")
        (qc_dir / "visual-review-request.json").write_text("{}", encoding="utf-8")

        resumed = module.resume_stage(Path(plan["plan_path"]))
        self.assertEqual(resumed["reused_count"], 1)
        self.assertFalse((qc_dir / "launch-quarantine.json").exists())

        finish["source"]["sha256"] = "stale"
        (run_dir / "finish.json").write_text(json.dumps(finish), encoding="utf-8")
        mismatched = module.resume_stage(Path(plan["plan_path"]))
        self.assertEqual(mismatched["quarantined_count"], 1)
        quarantine = json.loads(
            (qc_dir / "launch-quarantine.json").read_text(encoding="utf-8")
        )
        self.assertTrue(quarantine["active"])

    def test_status_reports_a_sealed_paper_before_its_retained_review_request(
        self,
    ) -> None:
        module = load_module()
        paper = self._seed_record("arxiv:2203.06843")
        self._write_manifests([paper])
        plan = module.plan_stage(
            self._plan_args(module, "deepseek_probe", stage_max_api_calls=3),
            preflight_runner=lambda _config: {
                "projection": {"request_cap": 2, "max_cost_rmb": 1.25}
            },
        )
        planned = plan["papers"][0]
        article = Path(planned["article_dir"])
        run = article / "run"
        qc = article / "qc"
        run.mkdir(parents=True, exist_ok=True)
        qc.mkdir(parents=True, exist_ok=True)
        (run / "finish.json").write_text(
            json.dumps(
                {
                    "status": "translated_pending_qc",
                    "source": {"sha256": planned["source_sha256"]},
                }
            ),
            encoding="utf-8",
        )
        (qc / "visual-review-request.json").write_text("{}", encoding="utf-8")
        (qc / "paper-seal.json").write_text("{}", encoding="utf-8")

        status = module.status_stage(Path(plan["plan_path"]))

        self.assertEqual(status["sealed_count"], 1)
        self.assertEqual(status["awaiting_visual_review"], 0)

    def test_launch_blocks_same_fingerprint_quarantine(self) -> None:
        module = load_module()
        paper = self._seed_record("arxiv:2203.06843")
        self._write_manifests([paper])
        plan = module.plan_stage(
            self._plan_args(module, "deepseek_probe", stage_max_api_calls=2),
            preflight_runner=lambda _config: {
                "projection": {"request_cap": 2, "max_cost_rmb": 1.25}
            },
        )
        planned = plan["papers"][0]
        qc_dir = Path(planned["article_dir"]) / "qc"
        qc_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = module.paper_launch_fingerprint(planned)
        (qc_dir / "launch-quarantine.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "active": True,
                    "record_id": planned["record_id"],
                    "fingerprint": fingerprint,
                }
            ),
            encoding="utf-8",
        )

        result = module.launch_stage(
            plan_path=Path(plan["plan_path"]),
            paid_runner=lambda _config: self.fail(
                "quarantined input must not relaunch"
            ),
            prepare_runner=lambda **_kwargs: self.fail(
                "quarantined input must not prepare"
            ),
        )

        self.assertEqual(result["quarantined_count"], 1)

    def test_translation_contract_change_invalidates_old_quarantine(self) -> None:
        module = load_module()
        paper = {
            "record_id": "arxiv:example",
            "source_sha256": "source",
            "preflight_sha256": "preflight",
            "pages": "all",
            "request_cap": 10,
            "stage_max_cost_rmb": 1.0,
            "translation_contract_sha256": "contract-a",
        }
        changed = dict(paper, translation_contract_sha256="contract-b")

        self.assertNotEqual(
            module.paper_launch_fingerprint(paper),
            module.paper_launch_fingerprint(changed),
        )

    def test_launch_requires_previous_stage_seal(self) -> None:
        module = load_module()
        probe = self._seed_record("arxiv:2203.06843")
        pilot = self._seed_record("arxiv:pilot")
        self._write_manifests([probe, pilot])
        with mock.patch.object(
            module.batch_production, "select_stage_records", return_value=[pilot]
        ):
            plan = module.plan_stage(
                self._plan_args(module, "pilot5", stage_max_api_calls=2),
                preflight_runner=lambda _config: {
                    "projection": {"request_cap": 2, "max_cost_rmb": 1.25}
                },
            )

        with self.assertRaisesRegex(RuntimeError, "previous stage seal"):
            module.launch_stage(
                plan_path=Path(plan["plan_path"]),
                paid_runner=lambda _config: self.fail("paid runner must not start"),
            )

    def test_status_is_read_only_for_mismatched_finish(self) -> None:
        module = load_module()
        probe = self._seed_record("arxiv:2203.06843")
        self._write_manifests([probe])
        plan = module.plan_stage(
            self._plan_args(module, "deepseek_probe", stage_max_api_calls=2),
            preflight_runner=lambda _config: {
                "projection": {"request_cap": 2, "max_cost_rmb": 1.25}
            },
        )
        paper = plan["papers"][0]
        run = Path(paper["run_dir"])
        (run / "finish.json").write_text(
            json.dumps(
                {
                    "status": "translated_pending_qc",
                    "source": {"sha256": "stale"},
                }
            ),
            encoding="utf-8",
        )

        status = module.status_stage(Path(plan["plan_path"]))

        self.assertEqual(status["mismatch_count"], 1)
        self.assertFalse(
            (Path(paper["article_dir"]) / "qc" / "launch-quarantine.json").exists()
        )

    def test_nonblocking_article_lock_suppresses_concurrent_launch(self) -> None:
        module = load_module()
        article = self.output_root / "article"

        with (
            module._paper_launch_lock(article) as first,
            module._paper_launch_lock(article) as second,
        ):
            self.assertTrue(first)
            self.assertFalse(second)

    def test_promote_requires_previous_stage_seal_and_fresh_paper_seals(self) -> None:
        module = load_module()
        probe = self._seed_record("arxiv:2203.06843")
        self._write_manifests([probe])
        deepseek_plan = module.plan_stage(
            self._plan_args(module, "deepseek_probe", stage_max_api_calls=2),
            preflight_runner=lambda _config: {
                "projection": {"request_cap": 2, "max_cost_rmb": 1.25}
            },
        )
        deepseek_paper = deepseek_plan["papers"][0]
        deepseek_qc = Path(deepseek_paper["article_dir"]) / "qc"
        deepseek_qc.mkdir(parents=True, exist_ok=True)
        paper_seal = self._write_complete_paper_seal(deepseek_paper)
        (Path(deepseek_paper["run_dir"]) / "preflight.json").write_text(
            json.dumps({"status": "mutable-live-preflight"}),
            encoding="utf-8",
        )
        stage_seal = module.promote_stage(Path(deepseek_plan["plan_path"]))
        self.assertEqual(stage_seal["stage"], "deepseek_probe")
        self.assertEqual(
            stage_seal["paper_seal_sha256s"][deepseek_paper["record_id"]],
            sha256_file(paper_seal),
        )

        pilot = self._seed_record("arxiv:pilot")
        self._write_manifests([probe, pilot])
        pilot_stage_map = {"pilot5": [pilot]}
        with mock.patch.object(
            module.batch_production,
            "select_stage_records",
            side_effect=lambda records, stage, *args, **kwargs: list(
                pilot_stage_map[stage]
            ),
        ):
            pilot_plan = module.plan_stage(
                self._plan_args(module, "pilot5", stage_max_api_calls=2),
                preflight_runner=lambda _config: {
                    "projection": {"request_cap": 2, "max_cost_rmb": 1.25}
                },
            )
        pilot_paper = pilot_plan["papers"][0]
        pilot_qc = Path(pilot_paper["article_dir"]) / "qc"
        pilot_qc.mkdir(parents=True, exist_ok=True)
        self._write_complete_paper_seal(pilot_paper)

        missing_previous = Path(pilot_plan["stage_dir"]) / "previous-stage-seal.json"
        if missing_previous.exists():
            missing_previous.unlink()
        with self.assertRaisesRegex(RuntimeError, "previous stage seal"):
            module.promote_stage(Path(pilot_plan["plan_path"]))

        previous_stage_target = (
            Path(pilot_plan["stage_dir"]) / "previous-stage-seal.json"
        )
        previous_stage_target.write_text(json.dumps(stage_seal), encoding="utf-8")
        promoted = module.promote_stage(Path(pilot_plan["plan_path"]))
        self.assertEqual(promoted["previous_stage"], "deepseek_probe")
        self.assertEqual(
            promoted["previous_stage_seal_sha256"], sha256_file(previous_stage_target)
        )

    def test_promote_rejects_incomplete_paper_seal(self) -> None:
        module = load_module()
        probe = self._seed_record("arxiv:2203.06843")
        self._write_manifests([probe])
        plan = module.plan_stage(
            self._plan_args(module, "deepseek_probe", stage_max_api_calls=2),
            preflight_runner=lambda _config: {
                "projection": {"request_cap": 2, "max_cost_rmb": 1.25}
            },
        )
        paper = plan["papers"][0]
        qc = Path(paper["article_dir"]) / "qc"
        qc.mkdir(parents=True, exist_ok=True)
        (qc / "paper-seal.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "passed": True,
                    "state": "visual_qc",
                    "record_id": paper["record_id"],
                    "source_pdf_sha256": paper["source_sha256"],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            module.promote_stage(Path(plan["plan_path"]))


if __name__ == "__main__":
    unittest.main()
