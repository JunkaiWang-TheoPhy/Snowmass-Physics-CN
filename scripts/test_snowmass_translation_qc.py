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


def structure_slot_payload(input_text: str) -> dict[str, object]:
    start = input_text.index('{"protocol":"snowmass-text-slots-v1"')
    payload, _ = json.JSONDecoder().raw_decode(input_text[start:])
    return payload


def slot_response(input_text: str, values: list[str]) -> dict[str, object]:
    payload = structure_slot_payload(input_text)
    slots = payload["slots"]
    if len(slots) != len(values):
        raise AssertionError(f"expected {len(values)} slot values, got {len(slots)}")
    return completed_response(
        json.dumps(
            {
                "translations": {
                    slot["id"]: value for slot, value in zip(slots, values)
                }
            },
            ensure_ascii=False,
        )
    )


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

    def test_validate_chunk_treats_thousands_separator_as_format_not_value_loss(self) -> None:
        report = QC.validate_chunk(
            source="The samples are 2800 and 34000.\n",
            translated="样本数为 2,800 和 34,000。\n",
            mapping={},
            glossary=[],
        )

        self.assertTrue(report.ok)
        self.assertNotIn("numbers_mismatch", report.failures)

    def test_validate_chunk_handles_pdf_glued_decimal_after_chinese_reflow(self) -> None:
        report = QC.validate_chunk(
            source="receiver with0.2 - 1.1GHz bandwidth",
            translated="接收机配备0.2–1.1GHz带宽",
            mapping={},
            glossary=[],
        )

        self.assertTrue(report.ok)
        self.assertNotIn("protected_literals_mismatch", report.failures)

    def test_validate_chunk_rejects_changed_unit(self) -> None:
        report = QC.validate_chunk(
            source="The detector reached 14 TeV.\n",
            translated="该探测器达到 14 GeV。\n",
            mapping={},
            glossary=[],
        )

        self.assertFalse(report.ok)
        self.assertIn("units_mismatch", report.failures)

    def test_validate_chunk_rejects_new_parenthesis_residue(self) -> None:
        report = QC.validate_chunk(
            source="linear (ILC) or circular (FCC-ee) collider.\n",
            translated="直线（ILC）或环形（FCC-ee））对撞机。\n",
            mapping={},
            glossary=[],
        )

        self.assertFalse(report.ok)
        self.assertIn("parentheses_mismatch", report.failures)

    def test_validate_chunk_preserves_source_inherited_parenthesis_residue(self) -> None:
        report = QC.validate_chunk(
            source="diagnostics ) and extraction.\n",
            translated="诊断）以及引出。\n",
            mapping={},
            glossary=[],
        )

        self.assertTrue(report.ok)

    def test_validate_chunk_allows_ascii_unit_literal_next_to_chinese_text(self) -> None:
        report = QC.validate_chunk(
            source="The line is at 21cm.\n",
            translated="该谱线位于 21cm处。\n",
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

    def test_validate_chunk_parses_tex_url_before_outer_brace_and_chinese_text(self) -> None:
        report = QC.validate_chunk(
            source=r"See \footnote{\url{https://example.org/a_b}}, while computing.",
            translated=r"计算时见 \footnote{\url{https://example.org/a_b}}。",
            mapping={},
            glossary=[],
        )

        self.assertTrue(report.ok)
        self.assertNotIn("protected_literals_mismatch", report.failures)

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

    def test_locked_term_phrase_exception_preserves_foreign_proper_name(self) -> None:
        report = QC.validate_chunk(
            source="Université Catholique, Chemin du Cyclotron, Belgium.\n",
            translated="鲁汶天主教大学，Chemin du Cyclotron，比利时。\n",
            mapping={},
            glossary=[
                {
                    "source": "cyclotron",
                    "target": "回旋加速器",
                    "exclude_phrases": ["Chemin du Cyclotron"],
                }
            ],
        )

        self.assertTrue(report.ok)

    def test_validate_chunk_accepts_mixed_script_term_with_spacing_difference(self) -> None:
        report = QC.validate_chunk(
            source="CMB lensing constrains the model.\n",
            translated="CMB引力透镜约束了该模型。\n",
            mapping={},
            glossary=[{"source": "CMB lensing", "target": "CMB 引力透镜"}],
        )

        self.assertTrue(report.ok)


class StageDecisionTests(unittest.TestCase):
    def test_anti_ai_is_always_an_independent_model_pass(self) -> None:
        clean = QC.stage_decision("anti_ai", "该结果满足实验约束。", [])
        marked = QC.stage_decision("anti_ai", "值得注意的是，该结果满足实验约束。", [])
        academic = QC.stage_decision("academic", "该结果满足实验约束。", [])

        self.assertTrue(clean.should_call_model)
        self.assertEqual(clean.reason, "anti_ai_refined_review_required")
        self.assertTrue(marked.should_call_model)
        self.assertTrue(academic.should_call_model)

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

    def test_stage_decision_skips_term_inside_configured_proper_name_phrase(self) -> None:
        decision = QC.stage_decision(
            "terminology",
            "Université Catholique, Chemin du Cyclotron, Belgium.\n",
            [
                {
                    "source": "cyclotron",
                    "target": "回旋加速器",
                    "exclude_phrases": ["Chemin du Cyclotron"],
                }
            ],
        )

        self.assertFalse(decision.should_call_model)

    def test_stage_decision_treats_mixed_script_spacing_as_canonical(self) -> None:
        decision = QC.stage_decision(
            "terminology",
            "CMB lensing（CMB引力透镜）已经完成。\n",
            [{"source": "CMB lensing", "target": "CMB 引力透镜"}],
        )

        self.assertFalse(decision.should_call_model)

    def test_stage_decision_runs_anti_ai_when_no_locked_marker_is_present(self) -> None:
        decision = QC.stage_decision("anti_ai", "探测器达到 14 TeV，见 [12]。\n", [])

        self.assertTrue(decision.should_call_model)
        self.assertEqual(decision.reason, "anti_ai_refined_review_required")

    def test_stage_decision_runs_anti_ai_when_formulaic_phrase_is_present(self) -> None:
        decision = QC.stage_decision("anti_ai", "总而言之，这项研究表明探测器达到 14 TeV。\n", [])

        self.assertTrue(decision.should_call_model)
        self.assertEqual(decision.reason, "anti_ai_marker_detected")

    def test_stage_decision_always_runs_academic_naturalization(self) -> None:
        decision = QC.stage_decision("academic", "这是准确、自然的学术中文。\n", [])

        self.assertTrue(decision.should_call_model)
        self.assertEqual(decision.reason, "academic_naturalization_required")


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

    def test_process_chunk_runs_refined_anti_ai_and_academic_passes(self) -> None:
        (self.article_dir / "chunk0001.md").write_text(
            "The detector reached 14 TeV in [12] at https://example.org.\n",
            encoding="utf-8",
        )

        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                self.calls += 1
                if self.calls == 1:
                    values = ["探测器达到 ", "，见 [", "] 和 "]
                    return slot_response(input_text, values), 0.1
                markers = re.findall(r"<ANCHOR_[0-9]{4}>", input_text)
                if len(markers) != 3:
                    raise AssertionError(f"expected 3 anchor markers, got {markers}")
                return completed_response(
                    json.dumps(
                        {
                            "translation": (
                                f"该探测器达到 {markers[0]}，见 "
                                f"[{markers[1]}] 和 {markers[2]}。\n"
                            )
                        },
                        ensure_ascii=False,
                    )
                ), 0.2

        client = FakeClient()
        result = RUNNER.process_chunk(self.task, client, [{"source": "Energy Frontier", "target": "能量前沿"}])
        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "complete")
        self.assertEqual(client.calls, 3)
        self.assertEqual((self.article_dir / "stage2_chunk0001.md").read_text(encoding="utf-8"), "探测器达到 14 TeV，见 [12] 和 https://example.org。\n")
        self.assertEqual((self.article_dir / "stage3_chunk0001.md").read_text(encoding="utf-8"), "该探测器达到 14 TeV，见 [12] 和 https://example.org。\n")
        self.assertEqual(
            status["stages"]["terminology"]["decision"],
            {"action": "copy_prior_text", "reason": "terminology_noop_no_locked_term_conflicts"},
        )
        self.assertEqual(
            status["stages"]["anti_ai"]["decision"],
            {"action": "call_model", "reason": "anti_ai_refined_review_required"},
        )

    def test_process_chunk_blocks_promotion_when_qc_fails(self) -> None:
        (self.article_dir / "chunk0001.md").write_text(
            "The detector recorded 14 events in [12] at https://example.org.\n",
            encoding="utf-8",
        )

        class FakeClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                values = ["探测器记录了15个事件，原值为 ", "，见 [", "] 和 "]
                return slot_response(input_text, values), 0.1

        with self.assertRaises(RuntimeError):
            RUNNER.process_chunk(self.task, FakeClient(), [])

        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]
        rejected = self.article_dir / translate["rejected_candidate_file"]

        self.assertEqual(translate["status"], "failed")
        self.assertIn("numbers_mismatch", translate["error"])
        rejected_text = rejected.read_text(encoding="utf-8")
        self.assertIn("15", rejected_text)
        self.assertIn("14", rejected_text)
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
                if self.calls == 1:
                    return slot_response(input_text, ["Energy Frontier 结果见 "]), 0.1
                return slot_response(
                    input_text,
                    ["能量前沿结果见，另见 \\ref{sec:intro} "],
                ), 0.1

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
