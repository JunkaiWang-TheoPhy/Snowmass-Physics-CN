#!/usr/bin/env python3
"""Tests for deterministic Snowmass translation QC and stage decisions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import snowmass_translation_qc as QC


RUNNER_PATH = SCRIPTS_DIR / "run_snowmass_translation.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("run_snowmass_translation", RUNNER_PATH)
assert RUNNER_SPEC and RUNNER_SPEC.loader
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER)


def completed_response(text: str, *, model: str = RUNNER.MODEL) -> dict[str, object]:
    return {
        "id": "resp_123",
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 1},
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 30,
        },
    }


class ValidateChunkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.glossary = [
            {"source": "Energy Frontier", "target": "能量前沿"},
            {"source": "CMB", "target": "宇宙微波背景"},
        ]

    def test_validate_chunk_accepts_matching_numbers_units_urls_citations_and_permitted_acronyms(self) -> None:
        report = QC.validate_chunk(
            source="The Energy Frontier reached 14 TeV in [12] at https://example.org. CMB anisotropy [[SM_0001_aaaaa]].\n",
            translated="能量前沿在 [12] 中达到 14 TeV，见 https://example.org。测量了 CMB 各向异性 [[SM_0001_aaaaa]]。\n",
            mapping={"[[SM_0001_aaaaa]]": r"$E=mc^2$"},
            glossary=self.glossary,
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.failures, ())

    def test_validate_chunk_rejects_changed_number(self) -> None:
        report = QC.validate_chunk(
            source="The detector reached 14 TeV.\n",
            translated="该探测器达到 15 TeV。\n",
            mapping={},
            glossary=[],
        )

        self.assertFalse(report.ok)
        self.assertIn("numbers_mismatch", report.failures)

    def test_validate_chunk_rejects_changed_unit(self) -> None:
        report = QC.validate_chunk(
            source="The detector reached 14 TeV.\n",
            translated="该探测器达到 14 GeV。\n",
            mapping={},
            glossary=[],
        )

        self.assertFalse(report.ok)
        self.assertIn("units_mismatch", report.failures)

    def test_validate_chunk_allows_spacing_only_change_between_number_and_unit(self) -> None:
        report = QC.validate_chunk(
            source="The line is at 21cm.\n",
            translated="该谱线位于 21 cm。\n",
            mapping={},
            glossary=[],
        )

        self.assertTrue(report.ok)

    def test_validate_chunk_does_not_treat_decade_suffix_as_seconds(self) -> None:
        report = QC.validate_chunk(
            source="The metric was retired in the 2020s.\n",
            translated="该指标在 2020 年代已不再使用。\n",
            mapping={},
            glossary=[],
        )

        self.assertTrue(report.ok)

    def test_validate_chunk_rejects_changed_url(self) -> None:
        report = QC.validate_chunk(
            source="See https://example.org/results.\n",
            translated="见 https://example.com/results。\n",
            mapping={},
            glossary=[],
        )

        self.assertFalse(report.ok)
        self.assertIn("urls_mismatch", report.failures)

    def test_validate_chunk_rejects_changed_citation(self) -> None:
        report = QC.validate_chunk(
            source="The result is discussed in [12].\n",
            translated="该结果见 [13]。\n",
            mapping={},
            glossary=[],
        )

        self.assertFalse(report.ok)
        self.assertIn("citations_mismatch", report.failures)

    def test_validate_chunk_rejects_missing_sentinel(self) -> None:
        report = QC.validate_chunk(
            source="The expression [[SM_0001_aaaaa]] is protected.\n",
            translated="该表达式被保护。\n",
            mapping={"[[SM_0001_aaaaa]]": r"$E=mc^2$"},
            glossary=[],
        )

        self.assertFalse(report.ok)
        self.assertIn("sentinels_mismatch", report.failures)

    def test_validate_chunk_accepts_reordered_sentinels_when_membership_is_exact(self) -> None:
        report = QC.validate_chunk(
            source="First [[SM_0001_aaaaa]] then [[SM_0002_bbbbb]].\n",
            translated="先 [[SM_0002_bbbbb]] 再 [[SM_0001_aaaaa]]。\n",
            mapping={
                "[[SM_0001_aaaaa]]": r"$E=mc^2$",
                "[[SM_0002_bbbbb]]": r"$p_T$",
            },
            glossary=[],
        )

        self.assertTrue(report.ok)
        self.assertNotIn("sentinels_mismatch", report.failures)

    def test_validate_chunk_rejects_duplicated_latex_reference_after_restore(self) -> None:
        report = QC.validate_chunk(
            source=r"See \ref{sec:intro} and $E=mc^2$.",
            translated=r"见 \ref{sec:intro} 和 $E=mc^2$，另见 \ref{sec:intro}。",
            mapping={},
            glossary=[],
        )

        self.assertFalse(report.ok)
        self.assertIn("protected_literals_mismatch", report.failures)

    def test_validate_chunk_rejects_locked_term_violation(self) -> None:
        report = QC.validate_chunk(
            source="The Energy Frontier report was released.\n",
            translated="Energy Frontier 报告已发布。\n",
            mapping={},
            glossary=self.glossary,
        )

        self.assertFalse(report.ok)
        self.assertIn("locked_terms_mismatch", report.failures)


class StageDecisionTests(unittest.TestCase):
    def test_stage_decision_skips_terminology_when_text_is_already_canonical(self) -> None:
        decision = QC.stage_decision(
            "terminology",
            "能量前沿的测量已经完成。\n",
            [{"source": "Energy Frontier", "target": "能量前沿"}],
        )

        self.assertFalse(decision.should_call_model)
        self.assertEqual(decision.reason, "terminology_noop_no_locked_term_conflicts")

    def test_stage_decision_runs_terminology_when_locked_term_conflict_remains(self) -> None:
        decision = QC.stage_decision(
            "terminology",
            "Energy Frontier 的测量已经完成。\n",
            [{"source": "Energy Frontier", "target": "能量前沿"}],
        )

        self.assertTrue(decision.should_call_model)
        self.assertEqual(decision.reason, "terminology_locked_term_conflict")

    def test_stage_decision_skips_anti_ai_when_no_formulaic_phrase_is_present(self) -> None:
        decision = QC.stage_decision("anti_ai", "探测器达到 14 TeV，见 [12]。\n", [])

        self.assertFalse(decision.should_call_model)
        self.assertEqual(decision.reason, "anti_ai_noop_no_markers")

    def test_stage_decision_runs_anti_ai_when_formulaic_phrase_is_present(self) -> None:
        decision = QC.stage_decision("anti_ai", "总而言之，这项研究表明探测器达到 14 TeV。\n", [])

        self.assertTrue(decision.should_call_model)
        self.assertEqual(decision.reason, "anti_ai_marker_detected")


class ProcessChunkQCIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.article_dir = self.root / "papers" / "arxiv_allowed"
        self.article_dir.mkdir(parents=True)
        self.task = {
            "article_dir": self.article_dir,
            "record_id": "arxiv:allowed",
            "chunk": {
                "id": "chunk0001",
                "source_file": "chunk0001.md",
                "output_file": "output_chunk0001.md",
                "source_hash": "source-hash",
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_process_chunk_skips_noop_conditional_stages_and_copies_prior_text_atomically(self) -> None:
        (self.article_dir / "chunk0001.md").write_text(
            "The detector reached 14 TeV in [12] at https://example.org.\n",
            encoding="utf-8",
        )

        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                self.calls += 1
                sentinels = re.findall(r"\[\[SM_[0-9]{4}_[0-9a-f]{10}\]\]", input_text)
                protected_url = sentinels[-1]
                if self.calls == 1:
                    return completed_response(f"探测器达到 14 TeV，见 [12] 和 {protected_url}。\n"), 0.1
                return completed_response(f"该探测器达到 14 TeV，见 [12] 和 {protected_url}。\n"), 0.2

        client = FakeClient()
        result = RUNNER.process_chunk(self.task, client, [{"source": "Energy Frontier", "target": "能量前沿"}])
        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "complete")
        self.assertEqual(client.calls, 2)
        self.assertEqual((self.article_dir / "stage2_chunk0001.md").read_text(encoding="utf-8"), "探测器达到 14 TeV，见 [12] 和 https://example.org。\n")
        self.assertEqual((self.article_dir / "stage3_chunk0001.md").read_text(encoding="utf-8"), "探测器达到 14 TeV，见 [12] 和 https://example.org。\n")
        self.assertEqual(
            status["stages"]["terminology"]["decision"],
            {"action": "copy_prior_text", "reason": "terminology_noop_no_locked_term_conflicts"},
        )
        self.assertEqual(
            status["stages"]["anti_ai"]["decision"],
            {"action": "copy_prior_text", "reason": "anti_ai_noop_no_markers"},
        )

    def test_process_chunk_blocks_promotion_when_qc_fails(self) -> None:
        (self.article_dir / "chunk0001.md").write_text(
            "The detector reached 14 TeV in [12] at https://example.org.\n",
            encoding="utf-8",
        )

        class FakeClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                protected_url = re.findall(r"\[\[SM_[0-9]{4}_[0-9a-f]{10}\]\]", input_text)[-1]
                return completed_response(f"探测器达到 15 TeV，见 [12] 和 {protected_url}。\n"), 0.1

        with self.assertRaises(RuntimeError):
            RUNNER.process_chunk(self.task, FakeClient(), [])

        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]
        rejected = self.article_dir / translate["rejected_candidate_file"]

        self.assertEqual(translate["status"], "failed")
        self.assertIn("numbers_mismatch", translate["qc"]["failures"])
        self.assertEqual(
            rejected.read_text(encoding="utf-8"),
            "探测器达到 15 TeV，见 [12] 和 https://example.org。\n",
        )
        self.assertFalse(translate["rejected_candidate_protected"])
        self.assertFalse((self.article_dir / "stage1_chunk0001.md").exists())

    def test_terminology_stage_rejects_extra_raw_reference_copied_from_source(self) -> None:
        (self.article_dir / "chunk0001.md").write_text(
            "Energy Frontier result in \\ref{sec:intro}.\n",
            encoding="utf-8",
        )

        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                self.calls += 1
                sentinel = re.findall(r"\[\[SM_[0-9]{4}_[0-9a-f]{10}\]\]", input_text)[-1]
                if self.calls == 1:
                    return completed_response(f"Energy Frontier 结果见 {sentinel}。\n"), 0.1
                return completed_response(f"能量前沿结果见 {sentinel}，另见 \\ref{{sec:intro}}。\n"), 0.1

        with self.assertRaises(RuntimeError):
            RUNNER.process_chunk(
                self.task,
                FakeClient(),
                [{"source": "Energy Frontier", "target": "能量前沿"}],
            )

        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        terminology = status["stages"]["terminology"]
        self.assertEqual(terminology["status"], "failed")
        self.assertIn("protected_literals_mismatch", terminology["qc"]["failures"])
        self.assertFalse((self.article_dir / "stage2_chunk0001.md").exists())


if __name__ == "__main__":
    unittest.main()
