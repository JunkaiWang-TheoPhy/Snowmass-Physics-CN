import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional


SCRIPT = Path(__file__).with_name("build_snowmass_production_state.py")


class BuildSnowmassProductionStateTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="snowmass-state-test-"))
        self.runs_root = self.temp_dir / "runs"
        self.runs_root.mkdir()
        self.rights_manifest = self.temp_dir / "rights.json"
        self.publication_registry = self.temp_dir / "publication-registry.json"
        self.source_prefilter = self.temp_dir / "source-prefilter.json"
        self.output_path = self.temp_dir / "state.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir)

    def test_ordering_prefers_newer_passed_seal_to_older_active_quarantine(self) -> None:
        record_id = "arxiv:2203.00001"
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        self._write_active_quarantine(revision=2, record_id=record_id)
        self._write_paper_seal(revision=3, record_id=record_id)

        state = self._run_script()

        self.assertEqual(state["current_revision"], 3)
        self.assertEqual(state["state_counts"], {"sealed": 1})
        self.assertEqual(state["candidate_exclusion_ids"], [record_id])
        record = self._record(state, record_id)
        self.assertEqual(record["state"], "sealed")
        self.assertEqual(record["evidence_counts"]["paper_seal"], 1)
        self.assertEqual(record["evidence_counts"]["active_quarantine"], 1)

    def test_same_revision_seal_and_active_quarantine_is_ambiguous(self) -> None:
        record_id = "arxiv:2203.00002"
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        self._write_active_quarantine(revision=4, record_id=record_id)
        self._write_paper_seal(revision=4, record_id=record_id)

        state = self._run_script()

        self.assertEqual(state["state_counts"], {"ambiguous": 1})
        self.assertEqual(self._record(state, record_id)["state"], "ambiguous")

    def test_rights_gate_blocks_even_with_passed_seal(self) -> None:
        record_id = "arxiv:2203.00003"
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": False}]
        )
        self._write_paper_seal(revision=5, record_id=record_id)

        state = self._run_script()

        self.assertEqual(state["state_counts"], {"rights_blocked": 1})
        self.assertEqual(self._record(state, record_id)["state"], "rights_blocked")

    def test_newer_active_quarantine_supersedes_older_passed_seal(self) -> None:
        record_id = "arxiv:2203.00004"
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        self._write_paper_seal(revision=2, record_id=record_id)
        self._write_active_quarantine(revision=6, record_id=record_id)

        state = self._run_script()

        self.assertEqual(state["state_counts"], {"quarantined": 1})
        self.assertEqual(self._record(state, record_id)["state"], "quarantined")

    def test_excludes_legacy_non_current_directories(self) -> None:
        record_id = "arxiv:2203.00005"
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        legacy = (
            self.runs_root
            / "pdf2zh_next_production_v999"
            / "stages"
            / "batch50"
            / "papers"
            / "arxiv_2203.00005"
            / "qc"
        )
        legacy.mkdir(parents=True)
        (legacy / "quarantine.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "active": True,
                    "record_id": record_id,
                    "fingerprint": "legacy-fingerprint",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._write_paper_seal(revision=7, record_id=record_id)

        state = self._run_script()
        record = self._record(state, record_id)

        self.assertEqual(record["state"], "sealed")
        self.assertTrue(
            all("pdf2zh_next_production_v999" not in path for path in record["evidence_paths"])
        )

    def test_emits_only_portable_relative_evidence_paths(self) -> None:
        record_id = "arxiv:2203.00006"
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        self._write_paper_seal(revision=8, record_id=record_id)
        self._write_finish(revision=8, record_id=record_id, status="translated_pending_qc")

        state = self._run_script()
        record = self._record(state, record_id)

        self.assertGreaterEqual(len(record["evidence_paths"]), 2)
        for path in record["evidence_paths"]:
            self.assertFalse(Path(path).is_absolute(), path)
            self.assertNotIn("\\", path)

    def test_rejects_duplicate_record_ids(self) -> None:
        record_id = "arxiv:2203.00007"
        self._write_rights_manifest(
            [
                {"record_id": record_id, "publication_allowed": True},
                {"record_id": record_id, "publication_allowed": True},
            ]
        )

        result = self._run_script_raw()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate record_id", result.stderr)

    def test_rejects_empty_arxiv_ids(self) -> None:
        self._write_rights_manifest([{"record_id": "arxiv:", "publication_allowed": True}])

        result = self._run_script_raw()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("empty arxiv record_id", result.stderr)

    def test_marks_publication_hash_mismatch_when_registry_does_not_match_receipt(self) -> None:
        record_id = "arxiv:2203.00008"
        protected_pdf_sha256 = "p" * 64
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        self._write_paper_seal(
            revision=9,
            record_id=record_id,
            protected_pdf_sha256=protected_pdf_sha256,
        )
        receipt_path = self._write_publication_receipt(
            record_id=record_id,
            packaged_pdf_sha256="r" * 64,
            source_pdf_sha256=protected_pdf_sha256,
        )
        self.publication_registry.write_text(
            json.dumps(
                [
                    {
                        "record_id": record_id,
                        "publication_allowed": True,
                        "publication_translation_sha256": "m" * 64,
                        "publication_receipt_path": str(
                            receipt_path.relative_to(self.publication_registry.parent)
                        ),
                    }
                ],
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        state = self._run_script(publication_registry=True)

        self.assertEqual(state["state_counts"], {"publication_mismatch": 1})
        self.assertEqual(
            self._record(state, record_id)["state"], "publication_mismatch"
        )

    def test_plan_only_record_is_planned_and_excluded_from_candidates(self) -> None:
        record_id = "arxiv:2203.00009"
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        self._write_plan(revision=9, stage="deepseek_probe", record_ids=[record_id])

        state = self._run_script()

        self.assertEqual(state["summary"]["planned_count"], 1)
        self.assertEqual(state["summary"]["rights_allowed_unstarted_count"], 0)
        self.assertEqual(state["candidate_exclusion_ids"], [record_id])
        record = self._record(state, record_id)
        self.assertEqual(record["state"], "planned")
        self.assertEqual(record["latest_plan_stage"], "deepseek_probe")
        self.assertIn("plan.json", record["latest_plan_path"])
        self.assertEqual(record["winning_evidence_path"], record["latest_plan_path"])

    def test_plan_with_run_evidence_but_no_finish_is_incomplete(self) -> None:
        record_id = "arxiv:2203.00010"
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        self._write_plan(revision=10, stage="pilot5", record_ids=[record_id])
        self._write_status(revision=10, record_id=record_id, status="running")

        state = self._run_script()

        self.assertEqual(state["summary"]["planned_or_incomplete_count"], 1)
        record = self._record(state, record_id)
        self.assertEqual(record["state"], "incomplete")
        self.assertEqual(record["latest_plan_stage"], "pilot5")
        self.assertTrue(record["winning_evidence_path"].endswith("status.json"))

    def test_rejects_malformed_plan_record_ids(self) -> None:
        self._write_rights_manifest([])
        self._write_plan(revision=11, stage="batch50", record_ids=["arxiv:"])

        result = self._run_script_raw()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("empty arxiv record_id", result.stderr)

    def test_publication_registry_discovers_unique_receipt_without_explicit_path(self) -> None:
        record_id = "arxiv:2203.00011"
        protected_pdf_sha256 = "a" * 64
        packaged_pdf_sha256 = "b" * 64
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        self._write_paper_seal(
            revision=12,
            record_id=record_id,
            protected_pdf_sha256=protected_pdf_sha256,
        )
        self._write_publication_receipt(
            record_id=record_id,
            packaged_pdf_sha256=packaged_pdf_sha256,
            source_pdf_sha256=protected_pdf_sha256,
            version="v7",
        )
        self.publication_registry.write_text(
            json.dumps(
                [
                    {
                        "record_id": record_id,
                        "publication_translation_sha256": packaged_pdf_sha256,
                    }
                ],
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        state = self._run_script(publication_registry=True)

        self.assertEqual(state["summary"]["published_count"], 1)
        record = self._record(state, record_id)
        self.assertEqual(record["state"], "published")
        self.assertIn("/site/pdfs/arxiv/2203.00011/v7/", f"/{record['winning_evidence_path']}")

    def test_publication_receipt_discovery_requires_unique_match(self) -> None:
        record_id = "arxiv:2203.00012"
        protected_pdf_sha256 = "c" * 64
        packaged_pdf_sha256 = "d" * 64
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        self._write_paper_seal(
            revision=13,
            record_id=record_id,
            protected_pdf_sha256=protected_pdf_sha256,
        )
        self._write_publication_receipt(
            record_id=record_id,
            packaged_pdf_sha256=packaged_pdf_sha256,
            source_pdf_sha256=protected_pdf_sha256,
            version="v1",
        )
        self._write_publication_receipt(
            record_id=record_id,
            packaged_pdf_sha256=packaged_pdf_sha256,
            source_pdf_sha256=protected_pdf_sha256,
            version="v2",
        )
        self.publication_registry.write_text(
            json.dumps(
                [
                    {
                        "record_id": record_id,
                        "publication_translation_sha256": packaged_pdf_sha256,
                    }
                ],
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        state = self._run_script(publication_registry=True)

        self.assertEqual(state["summary"]["publication_mismatch_count"], 1)
        self.assertEqual(
            self._record(state, record_id)["state"], "publication_mismatch"
        )

    def test_explicit_receipt_path_must_stay_within_repo_root(self) -> None:
        record_id = "arxiv:2203.00013"
        protected_pdf_sha256 = "e" * 64
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        self._write_paper_seal(
            revision=14,
            record_id=record_id,
            protected_pdf_sha256=protected_pdf_sha256,
        )
        self.publication_registry.write_text(
            json.dumps(
                [
                    {
                        "record_id": record_id,
                        "publication_translation_sha256": "f" * 64,
                        "publication_receipt_path": "../outside.json",
                    }
                ],
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        result = self._run_script_raw(publication_registry=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("publication receipt path escapes repo root", result.stderr)

    def test_summary_counts_include_candidates_and_blocked_rights_separately(self) -> None:
        blocked = "arxiv:2203.00014"
        candidate = "arxiv:2203.00015"
        planned = "arxiv:2203.00016"
        self._write_rights_manifest(
            [
                {"record_id": blocked, "publication_allowed": False},
                {"record_id": candidate, "publication_allowed": True},
                {"record_id": planned, "publication_allowed": True},
            ]
        )
        self._write_plan(revision=15, stage="pilot10", record_ids=[planned])

        state = self._run_script()

        self.assertEqual(state["summary"]["rights_blocked_count"], 1)
        self.assertEqual(state["summary"]["planned_count"], 1)
        self.assertEqual(state["summary"]["rights_allowed_unstarted_count"], 1)
        self.assertEqual(self._record(state, candidate)["state"], "candidate")

    def test_prefilter_untouched_pool_uses_union_of_risk_tiers(self) -> None:
        candidate = "arxiv:2203.00017"
        planned = "arxiv:2203.00018"
        sealed = "arxiv:2203.00019"
        self._write_rights_manifest(
            [
                {"record_id": candidate, "publication_allowed": True},
                {"record_id": planned, "publication_allowed": True},
                {"record_id": sealed, "publication_allowed": True},
            ]
        )
        self._write_plan(revision=16, stage="pilot5", record_ids=[planned])
        self._write_paper_seal(revision=16, record_id=sealed)
        self._write_source_prefilter(
            {
                "low_risk": [{"record_id": planned, "publication_allowed": True}],
                "text_only_medium": [
                    {"record_id": candidate, "publication_allowed": True}
                ],
                "vector_passthrough_long": [
                    {"record_id": sealed, "publication_allowed": True}
                ],
            }
        )

        state = self._run_script(source_prefilter=True)

        self.assertEqual(
            state["untouched_prefilter_candidate_ids"],
            [candidate],
        )
        self.assertEqual(
            state["summary"]["untouched_prefilter_candidate_count"], 1
        )
        self.assertEqual(
            state["summary"]["untouched_prefilter_candidate_counts_by_tier"],
            {"text_only_medium": 1},
        )
        self.assertEqual(self._record(state, candidate)["risk_tier"], "text_only_medium")
        self.assertEqual(self._record(state, planned)["risk_tier"], "low_risk")
        self.assertEqual(
            self._record(state, sealed)["risk_tier"], "vector_passthrough_long"
        )

    def test_prefilter_requires_schema_version_three(self) -> None:
        record_id = "arxiv:2203.00020"
        self._write_rights_manifest(
            [{"record_id": record_id, "publication_allowed": True}]
        )
        self.source_prefilter.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "candidates_by_risk_tier": {
                        "low_risk": [
                            {
                                "record_id": record_id,
                                "publication_allowed": True,
                                "risk_tier": "low_risk",
                            }
                        ]
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        result = self._run_script_raw(source_prefilter=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source prefilter schema_version must be 3", result.stderr)

    def test_prefilter_rejects_non_rights_allowed_records(self) -> None:
        allowed = "arxiv:2203.00021"
        blocked = "arxiv:2203.00022"
        missing = "arxiv:2203.00023"
        self._write_rights_manifest(
            [
                {"record_id": allowed, "publication_allowed": True},
                {"record_id": blocked, "publication_allowed": False},
            ]
        )
        self._write_source_prefilter(
            {
                "low_risk": [
                    {"record_id": allowed, "publication_allowed": True},
                    {"record_id": blocked, "publication_allowed": True},
                    {"record_id": missing, "publication_allowed": True},
                ]
            }
        )

        result = self._run_script_raw(source_prefilter=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source prefilter record_id is not rights-allowed", result.stderr)

    def _run_script(
        self,
        *,
        publication_registry: bool = False,
        source_prefilter: bool = False,
    ) -> dict:
        result = self._run_script_raw(
            publication_registry=publication_registry,
            source_prefilter=source_prefilter,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return json.loads(self.output_path.read_text(encoding="utf-8"))

    def _run_script_raw(
        self,
        *,
        publication_registry: bool = False,
        source_prefilter: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCRIPT),
            "--runs-root",
            str(self.runs_root),
            "--rights-manifest",
            str(self.rights_manifest),
            "--output",
            str(self.output_path),
        ]
        if publication_registry:
            command.extend(
                ["--publication-registry", str(self.publication_registry)]
            )
        if source_prefilter:
            command.extend(["--source-prefilter", str(self.source_prefilter)])
        return subprocess.run(
            command,
            cwd=self.temp_dir,
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_rights_manifest(self, records: list[dict]) -> None:
        self.rights_manifest.write_text(
            json.dumps(records, sort_keys=True),
            encoding="utf-8",
        )

    def _write_paper_seal(
        self,
        *,
        revision: int,
        record_id: str,
        protected_pdf_sha256: Optional[str] = None,
    ) -> None:
        qc_dir = self._article_dir(revision, record_id) / "qc"
        qc_dir.mkdir(parents=True, exist_ok=True)
        (qc_dir / "paper-seal.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "passed": True,
                    "state": "visual_qc",
                    "record_id": record_id,
                    "protected_pdf_sha256": protected_pdf_sha256 or "s" * 64,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _write_active_quarantine(self, *, revision: int, record_id: str) -> None:
        qc_dir = self._article_dir(revision, record_id) / "qc"
        qc_dir.mkdir(parents=True, exist_ok=True)
        (qc_dir / "quarantine.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "active": True,
                    "record_id": record_id,
                    "fingerprint": f"fingerprint-{revision}",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _write_finish(self, *, revision: int, record_id: str, status: str) -> None:
        run_dir = self._article_dir(revision, record_id) / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "finish.json").write_text(
            json.dumps({"record_id": record_id, "status": status}, sort_keys=True),
            encoding="utf-8",
        )

    def _write_status(self, *, revision: int, record_id: str, status: str) -> None:
        run_dir = self._article_dir(revision, record_id) / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "status.json").write_text(
            json.dumps({"record_id": record_id, "status": status}, sort_keys=True),
            encoding="utf-8",
        )

    def _write_publication_receipt(
        self,
        *,
        record_id: str,
        packaged_pdf_sha256: str,
        source_pdf_sha256: str,
        version: str = "v1",
    ) -> Path:
        short_id = record_id.split(":", 1)[1]
        receipt_dir = self.temp_dir / "site" / "pdfs" / "arxiv" / short_id / version
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = receipt_dir / f"snowmass-{short_id}.zh-CN.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "record_id": record_id,
                    "packaged_pdf_sha256": packaged_pdf_sha256,
                    "source_pdf_sha256": source_pdf_sha256,
                    "receipt_path": receipt_path.name,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return receipt_path

    def _article_dir(self, revision: int, record_id: str) -> Path:
        safe_id = record_id.replace(":", "_")
        return (
            self.runs_root
            / f"pdf2zh_next_production_probe_current_v{revision}"
            / "stages"
            / "batch50"
            / "papers"
            / safe_id
        )

    def _write_plan(self, *, revision: int, stage: str, record_ids: list[str]) -> None:
        stage_dir = (
            self.runs_root
            / f"pdf2zh_next_production_probe_current_v{revision}"
            / "stages"
            / stage
        )
        stage_dir.mkdir(parents=True, exist_ok=True)
        papers = [
            {
                "record_id": record_id,
                "article_dir": str(self._article_dir(revision, record_id)),
                "run_dir": str(self._article_dir(revision, record_id) / "run"),
            }
            for record_id in record_ids
        ]
        (stage_dir / "plan.json").write_text(
            json.dumps(
                {
                    "stage": stage,
                    "record_ids": record_ids,
                    "papers": papers,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _write_source_prefilter(
        self, candidates_by_risk_tier: dict[str, list[dict]]
    ) -> None:
        normalized = {}
        for tier, records in candidates_by_risk_tier.items():
            normalized[tier] = []
            for record in records:
                copied = dict(record)
                copied.setdefault("risk_tier", tier)
                normalized[tier].append(copied)
        self.source_prefilter.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "candidates_by_risk_tier": normalized,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _record(self, payload: dict, record_id: str) -> dict:
        for record in payload["records"]:
            if record["record_id"] == record_id:
                return record
        self.fail(f"missing record: {record_id}")


if __name__ == "__main__":
    unittest.main()
