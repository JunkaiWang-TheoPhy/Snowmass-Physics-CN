#!/usr/bin/env python3
"""Tests for genuine paper-level refined Snowmass orchestration."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("run_snowmass_refined_translation.py")
PRODUCTION_MODULE_PATH = Path(__file__).with_name("run_snowmass_batch_production.py")


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("Refined Snowmass orchestrator is not implemented")
    spec = importlib.util.spec_from_file_location("run_snowmass_refined_translation", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_production_module():
    spec = importlib.util.spec_from_file_location(
        "run_snowmass_batch_production", PRODUCTION_MODULE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def completed_response(text: str, response_id: str) -> dict[str, object]:
    return {
        "id": response_id,
        "model": "deepseek-v4-flash",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 10,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 20,
        },
    }


class RefinedOrchestratorTests(unittest.TestCase):
    def test_reference_detection_ignores_table_of_contents_entry(self) -> None:
        module = load_module()
        chunks = []
        rows = [
            ("chunk0001", "CONTENTS\n", "title", 1),
            ("chunk0002", "References\n", "fallback_line", 1),
            ("chunk0003", "I. INTRODUCTION\n", "title", 2),
            ("chunk0004", "Body text.\n", "plain text", 2),
            ("chunk0005", "ACKNOWLEDGMENTS\n", "title", 18),
            ("chunk0006", "Supported by the collaboration.\n", "plain text", 18),
            ("chunk0007", "References\n", "title", 18),
            ("chunk0008", "A. Author. Paper title. Journal 1 (2022) 1.\n", "plain text", 18),
            ("chunk0009", "B. Author. Another title. arXiv:2201.00001 (2022).\n", "plain text", 19),
            ("chunk0010", "DATA AVAILABILITY\n", "title", 19),
            ("chunk0011", "Data are available on request.\n", "plain text", 19),
        ]
        for order, (chunk_id, text, label, page) in enumerate(rows, 1):
            source_file = f"{chunk_id}.md"
            (self.article / source_file).write_text(text, encoding="utf-8")
            chunks.append(
                {
                    "id": chunk_id,
                    "order": order,
                    "source_file": source_file,
                    "layout_label": label,
                    "page_number": page,
                }
            )

        self.assertEqual(
            module._reference_chunk_ids(self.article, chunks),
            {"chunk0008", "chunk0009"},
        )

    def test_reference_detection_accepts_fragmented_entries_and_running_headers(self) -> None:
        module = load_module()
        rows = [
            ("chunk0001", "Body text.\n", "plain text", 1),
            ("chunk0002", "References\n", "title", 24),
            (
                "chunk0003",
                "{v1}V. C. Rubin and W. K. Ford, Jr., “Rotation of the Andromeda "
                "Nebula,” Astroph. J. 159 (1970) 379.{v2}\n",
                "plain text",
                24,
            ),
            (
                "chunk0004",
                "Snowmass2021 Theory Frontier: Astrophysical and Cosmological Probes "
                "of Dark Matter\n",
                "abandon",
                25,
            ),
            (
                "chunk0005",
                "of neutral hydrogen in spiral galaxies,” Astron. J. 86 (1981) 1825.\n",
                "plain text",
                25,
            ),
            (
                "chunk0006",
                "D. Clowe et al., “A Direct Empirical Proof,” Astrophys. J. Lett. "
                "648 no. 2, (Sept., 2006) L109–L113.\n",
                "plain text",
                25,
            ),
        ]
        chunks = []
        for order, (chunk_id, text, label, page) in enumerate(rows, 1):
            source_file = f"{chunk_id}.md"
            (self.article / source_file).write_text(text, encoding="utf-8")
            chunks.append(
                {
                    "id": chunk_id,
                    "order": order,
                    "source_file": source_file,
                    "layout_label": label,
                    "page_number": page,
                }
            )

        self.assertEqual(
            module._reference_chunk_ids(self.article, chunks),
            {"chunk0003", "chunk0004", "chunk0005", "chunk0006"},
        )

    def test_reference_detection_rejects_early_contents_heading_before_quoted_body(self) -> None:
        module = load_module()
        rows = [
            ("chunk0001", "CONTENTS\n", "title", 1),
            ("chunk0002", "References\n", "title", 1),
            (
                "chunk0003",
                "We discuss “Rotation of the Andromeda Nebula” in Astroph. J. "
                "159 (1970) as a motivating example.\n",
                "plain text",
                2,
            ),
            (
                "chunk0004",
                "A second section revisits “Neutral hydrogen in spiral galaxies” "
                "from Astron. J. 86 (1981).\n",
                "plain text",
                2,
            ),
            ("chunk0005", "Main analysis continues.\n", "plain text", 3),
            ("chunk0006", "Conclusion.\n", "title", 10),
        ]
        chunks = []
        for order, (chunk_id, text, label, page) in enumerate(rows, 1):
            source_file = f"{chunk_id}.md"
            (self.article / source_file).write_text(text, encoding="utf-8")
            chunks.append(
                {
                    "id": chunk_id,
                    "order": order,
                    "source_file": source_file,
                    "layout_label": label,
                    "page_number": page,
                }
            )

        self.assertEqual(module._reference_chunk_ids(self.article, chunks), set())

    def test_sharded_critique_covers_late_chunks_and_is_resumable(self) -> None:
        module = load_module()
        chunks = []
        for index in range(1, 7):
            chunk_id = f"chunk{index:04d}"
            source_file = f"{chunk_id}.md"
            output_file = f"output_{chunk_id}.md"
            source_text = f"English source {index} " + ("x" * 40) + "\n"
            (self.article / source_file).write_text(source_text, encoding="utf-8")
            (self.article / f"stage2_{chunk_id}.md").write_text(
                f"中文草稿 {index} " + ("中" * 40) + "\n",
                encoding="utf-8",
            )
            chunks.append(
                {
                    "id": chunk_id,
                    "order": index,
                    "source_file": source_file,
                    "output_file": output_file,
                    "source_hash": module.runner.text_hash(source_text),
                    "babeldoc_unit_id": f"p{index:04d}-i0000",
                }
            )
        status = {"record_id": "arxiv:allowed", "phases": {}}
        calls: list[str] = []

        class Client:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                calls.append(input_text)
                ids = sorted(set(re.findall(r"chunk\d{4}", input_text)))
                findings = "\n".join(
                    f"- {chunk_id}: 核对该块的事实表达。" for chunk_id in ids
                )
                return completed_response(
                    "## Accuracy\n"
                    + findings
                    + "\n\n## Native Voice\n- NO_ACTIONABLE_FINDINGS\n\n"
                    "## Notes & Adaptation\n- NO_ACTIONABLE_FINDINGS\n\n"
                    "## Summary\n- 分片检查完成。\n",
                    f"shard-{len(calls)}",
                ), 0.1

        critique = module._run_sharded_critique(
            article_dir=self.article,
            chunks=chunks,
            client=Client(),
            status=status,
            status_path=self.article / "paper_status.json",
            run_id="run-one",
            budget_guard=None,
            retry_uncertain=False,
            shard_char_limit=230,
        )

        self.assertGreater(len(calls), 1)
        self.assertIn("chunk0001:", critique)
        self.assertIn("chunk0006:", critique)
        self.assertLessEqual(len(re.findall(r"^- chunk\d{4}:", critique, re.M)), 30)
        first_call_count = len(calls)

        resumed = module._run_sharded_critique(
            article_dir=self.article,
            chunks=chunks,
            client=Client(),
            status=status,
            status_path=self.article / "paper_status.json",
            run_id="run-two",
            budget_guard=None,
            retry_uncertain=False,
            shard_char_limit=230,
        )

        self.assertEqual(resumed, critique)
        self.assertEqual(len(calls), first_call_count)

    def test_sharded_critique_prompt_enforces_machine_bounded_output(self) -> None:
        module = load_module()
        source_text = "English source.\n"
        (self.article / "chunk0001.md").write_text(source_text, encoding="utf-8")
        (self.article / "stage2_chunk0001.md").write_text("中文草稿。\n", encoding="utf-8")
        chunks = [
            {
                "id": "chunk0001",
                "order": 1,
                "source_file": "chunk0001.md",
                "output_file": "output_chunk0001.md",
                "source_hash": module.runner.text_hash(source_text),
                "babeldoc_unit_id": "p0001-i0000",
            }
        ]
        calls: list[tuple[str, int]] = []

        class Client:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                calls.append((instructions, max_output_tokens))
                if len(calls) == 1:
                    response = completed_response("过长输出", "first")
                    response["status"] = "incomplete"
                    response["incomplete_details"] = {"reason": "max_output_tokens"}
                    return response, 0.1
                return completed_response(
                    "## Accuracy\n- NO_ACTIONABLE_FINDINGS\n\n"
                    "## Native Voice\n- NO_ACTIONABLE_FINDINGS\n\n"
                    "## Notes & Adaptation\n- NO_ACTIONABLE_FINDINGS\n\n"
                    "## Summary\n- NO_ACTIONABLE_FINDINGS\n",
                    "compact",
                ), 0.1

        module._run_sharded_critique(
            article_dir=self.article,
            chunks=chunks,
            client=Client(),
            status={"record_id": "arxiv:allowed", "phases": {}},
            status_path=self.article / "paper_status.json",
            run_id="run-one",
            budget_guard=None,
            retry_uncertain=False,
        )

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(limit == module.CRITIQUE_SHARD_MAX_OUTPUT_TOKENS for _, limit in calls))
        self.assertIn(
            f"at most {module.CRITIQUE_SHARD_MAX_FINDINGS} actionable lines",
            calls[0][0],
        )
        self.assertIn(
            f"at most {module.CRITIQUE_SHARD_MAX_FINDING_CHARACTERS} characters",
            calls[0][0],
        )
        self.assertIn("original instructions' stricter count", calls[1][0])
        self.assertNotIn("no more than 30", calls[1][0])

    def test_sharded_critique_repairs_non_exact_chunk_labels_auditably(self) -> None:
        module = load_module()
        chunks = []
        for index in range(1, 3):
            chunk_id = f"chunk{index:04d}"
            source_text = f"English source {index}.\n"
            (self.article / f"{chunk_id}.md").write_text(source_text, encoding="utf-8")
            (self.article / f"stage2_{chunk_id}.md").write_text(
                f"中文草稿 {index}。\n", encoding="utf-8"
            )
            chunks.append(
                {
                    "id": chunk_id,
                    "order": index,
                    "source_file": f"{chunk_id}.md",
                    "output_file": f"output_{chunk_id}.md",
                    "source_hash": module.runner.text_hash(source_text),
                    "babeldoc_unit_id": f"p{index:04d}-i0000",
                }
            )
        calls: list[str] = []

        class Client:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                calls.append(instructions)
                if len(calls) == 1:
                    text = (
                        "## Accuracy\n- chunk0001-0002: range label is invalid\n\n"
                        "## Native Voice\n- NO_ACTIONABLE_FINDINGS\n\n"
                        "## Notes & Adaptation\n- NO_ACTIONABLE_FINDINGS\n\n"
                        "## Summary\n- NO_ACTIONABLE_FINDINGS\n"
                    )
                else:
                    text = (
                        "## Accuracy\n- chunk0001: exact repaired finding\n\n"
                        "## Native Voice\n- NO_ACTIONABLE_FINDINGS\n\n"
                        "## Notes & Adaptation\n- NO_ACTIONABLE_FINDINGS\n\n"
                        "## Summary\n- NO_ACTIONABLE_FINDINGS\n"
                    )
                return completed_response(text, f"response-{len(calls)}"), 0.1

        status = {"record_id": "arxiv:allowed", "phases": {}}
        critique = module._run_sharded_critique(
            article_dir=self.article,
            chunks=chunks,
            client=Client(),
            status=status,
            status_path=self.article / "paper_status.json",
            run_id="run-one",
            budget_guard=None,
            retry_uncertain=False,
        )

        self.assertEqual(len(calls), 2)
        self.assertIn("STRUCTURE-REPAIR", calls[1])
        self.assertIn("chunk0001", calls[1])
        self.assertIn("chunk0002", calls[1])
        self.assertIn("- chunk0001: exact repaired finding", critique)
        self.assertEqual(status["phases"]["critique_shard_repair_0001"]["status"], "complete")
        self.assertTrue((self.article / "critique_shard_repairs/shard0001.md").exists())

    def test_sharded_critique_round_robins_before_global_cap(self) -> None:
        module = load_module()
        shard_outputs = []
        for shard in range(1, 11):
            lines = "\n".join(
                f"- chunk{shard:02d}{item:02d}: issue {shard}-{item}"
                for item in range(1, 5)
            )
            shard_outputs.append(
                "## Accuracy\n"
                + lines
                + "\n\n## Native Voice\n- NO_ACTIONABLE_FINDINGS\n\n"
                "## Notes & Adaptation\n- NO_ACTIONABLE_FINDINGS\n\n"
                "## Summary\n- done\n"
            )

        merged = module._merge_sharded_critiques(shard_outputs, max_findings=30)

        self.assertEqual(len(re.findall(r"^- chunk\d{4}:", merged, re.M)), 30)
        for shard in range(1, 11):
            self.assertIn(f"chunk{shard:02d}01:", merged)

    def test_shard_merge_accepts_bounded_per_section_model_interpretation(self) -> None:
        module = load_module()
        sections = []
        next_chunk = 1
        for heading in ("Accuracy", "Native Voice", "Notes & Adaptation"):
            lines = []
            for _ in range(module.CRITIQUE_SHARD_MAX_FINDINGS):
                lines.append(f"- chunk{next_chunk:04d}: issue {next_chunk}")
                next_chunk += 1
            sections.append(f"## {heading}\n" + "\n".join(lines))
        output = "\n\n".join(sections) + "\n\n## Summary\n- done\n"

        merged = module._merge_sharded_critiques([output])

        self.assertEqual(
            len(re.findall(r"^- chunk\d{4}:", merged, re.M)),
            module.CRITIQUE_SHARD_VALIDATION_MAX_FINDINGS,
        )

    def test_shard_merge_accepts_minor_chunk_line_format_variants(self) -> None:
        module = load_module()
        output = (
            "## Accuracy\nchunk0001: issue one\n1. chunk0002: issue two\n\n"
            "## Native Voice\n- NO_ACTIONABLE_FINDINGS\n\n"
            "## Notes & Adaptation\n- NO_ACTIONABLE_FINDINGS\n\n"
            "## Summary\n- done\n"
        )

        merged = module._merge_sharded_critiques([output])

        self.assertIn("- chunk0001: issue one", merged)
        self.assertIn("- chunk0002: issue two", merged)

    def test_shard_merge_splits_one_overlong_finding_without_losing_content(self) -> None:
        module = load_module()
        output = (
            "## Accuracy\n"
            "- chunk0001: first defect needs correction；second independent defect also needs correction。\n\n"
            "## Native Voice\n- NO_ACTIONABLE_FINDINGS\n\n"
            "## Notes & Adaptation\n- NO_ACTIONABLE_FINDINGS\n\n"
            "## Summary\n- done\n"
        )

        merged = module._merge_sharded_critiques(
            [output],
            max_finding_characters=55,
        )

        findings = re.findall(r"^- chunk0001: (.+)$", merged, re.M)
        self.assertEqual(len(findings), 2)
        self.assertEqual(
            "".join(findings),
            "first defect needs correction；second independent defect also needs correction。",
        )
        self.assertTrue(all(len("chunk0001: " + item) <= 55 for item in findings))

    def test_shard_merge_rejects_unparseable_actionable_content(self) -> None:
        module = load_module()
        output = (
            "## Accuracy\n- The conclusion changes the source modality.\n\n"
            "## Native Voice\n- NO_ACTIONABLE_FINDINGS\n\n"
            "## Notes & Adaptation\n- NO_ACTIONABLE_FINDINGS\n\n"
            "## Summary\n- done\n"
        )

        with self.assertRaisesRegex(RuntimeError, "unparseable actionable"):
            module._merge_sharded_critiques([output])

    def test_shard_merge_rejects_more_than_bounded_section_fallback(self) -> None:
        module = load_module()
        lines = "\n".join(
            f"- chunk{index:04d}: issue {index}"
            for index in range(1, module.CRITIQUE_SHARD_VALIDATION_MAX_FINDINGS + 2)
        )
        output = (
            "## Accuracy\n" + lines + "\n\n"
            "## Native Voice\n- NO_ACTIONABLE_FINDINGS\n\n"
            "## Notes & Adaptation\n- NO_ACTIONABLE_FINDINGS\n\n"
            "## Summary\n- done\n"
        )

        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            module._merge_sharded_critiques([output])

    def test_shard_bound_deterministically_caps_model_overflow(self) -> None:
        module = load_module()
        lines = "\n".join(
            f"- chunk{index:04d}: ranked issue {index}"
            for index in range(1, module.CRITIQUE_SHARD_VALIDATION_MAX_FINDINGS + 5)
        )
        output = (
            "## Accuracy\n" + lines + "\n\n"
            "## Native Voice\n- NO_ACTIONABLE_FINDINGS\n\n"
            "## Notes & Adaptation\n- NO_ACTIONABLE_FINDINGS\n\n"
            "## Summary\n- done\n"
        )

        bounded = module._bound_shard_critique(output)

        findings = re.findall(r"^- chunk\d{4}:", bounded, re.M)
        self.assertEqual(
            len(findings),
            module.CRITIQUE_SHARD_VALIDATION_MAX_FINDINGS,
        )
        self.assertIn("chunk0001:", bounded)
        self.assertNotIn("chunk0013:", bounded)

    def test_revision_context_hash_changes_when_effective_critique_changes(self) -> None:
        module = load_module()

        first = module._revision_context_signature(
            "chunk0001", "# Actionable critique for this chunk only\n- chunk0001: first"
        )
        second = module._revision_context_signature(
            "chunk0001", "# Actionable critique for this chunk only\n- chunk0001: second"
        )

        self.assertNotEqual(first, second)

    def test_completed_legacy_revision_never_receives_full_paper_critique(self) -> None:
        module = load_module()
        status_dir = self.article / "chunk_status"
        status_dir.mkdir(exist_ok=True)
        (status_dir / "chunk0001.json").write_text(
            json.dumps(
                {
                    "stages": {
                        "revision": {
                            "status": "complete",
                            "paper_context_scope": "paper_full",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        critique = "- chunk0001: local issue\n- chunk0002: unrelated 2021 issue\n"

        context = module._revision_context_preserving_completed_checkpoint(
            self.article, "chunk0001", critique
        )

        self.assertIn("chunk0001", context)
        self.assertNotIn("chunk0002", context)
        self.assertNotIn("2021", context)

    def test_valid_legacy_critique_is_reused_for_identical_source_and_draft(self) -> None:
        module = load_module()
        source = "<!-- chunk0001 -->\nEnglish source.\n"
        draft = "<!-- chunk0001 -->\n中文草稿。\n"
        instructions = "Return a critique."
        input_text = f"ENGLISH SOURCE:\n{source}\n\nCHINESE DRAFT:\n{draft}"
        critique = (
            "## Accuracy\n- chunk0001: 无。\n\n## Native Voice\n- 无。\n\n"
            "## Notes & Adaptation\n- 无。\n\n## Summary\n- 通过。\n"
        )
        path = self.article / "04-critique.md"
        path.write_text(critique, encoding="utf-8")
        status = {
            "phases": {
                "critique": {
                    "status": "complete",
                    "input_hash": module._paper_phase_input_hash(
                        instructions, input_text, 4000
                    ),
                    "output_hash": module.runner.text_hash(critique),
                }
            }
        }

        reused = module._valid_legacy_critique(
            article_dir=self.article,
            status=status,
            instructions=instructions,
            source=source,
            draft=draft,
        )

        self.assertEqual(reused, critique)

    def test_paper_phase_compacts_once_after_output_limit(self) -> None:
        module = load_module()
        status = {"record_id": "arxiv:allowed", "phases": {}}
        status_path = self.article / "paper_status.json"
        calls: list[str] = []

        class BudgetGuard:
            usd_cny_rate = 7.2

            def __init__(self) -> None:
                self.events: list[tuple[str, str]] = []
                self.counter = 0

            def reserve(self, input_text, max_output_tokens, *, uncertainty_key=None):
                self.counter += 1
                reservation = f"reservation-{self.counter}"
                self.events.append(("reserve", str(uncertainty_key)))
                return reservation

            def settle(self, reservation, usage):
                self.events.append(("settle", reservation))

            def resolve_uncertain(self, uncertainty_key):
                self.events.append(("resolve", uncertainty_key))

        budget = BudgetGuard()

        class Client:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                calls.append(instructions)
                if len(calls) == 1:
                    response = completed_response("过长输出", "first")
                    response["status"] = "incomplete"
                    response["incomplete_details"] = {"reason": "max_output_tokens"}
                    response["usage"]["output_tokens"] = max_output_tokens
                    response["usage"]["total_tokens"] = max_output_tokens + 10
                    return response, 0.1
                return completed_response(
                    "## Accuracy\n- chunk0001: 无。\n\n## Summary\n通过。\n",
                    "compact",
                ), 0.1

        text = module._run_paper_model_phase(
            phase_name="critique",
            output_path=self.article / "04-critique.md",
            instructions="Return a bounded critique.",
            input_text="source and draft",
            max_output_tokens=4000,
            client=Client(),
            status=status,
            status_path=status_path,
            run_id="run-one",
            budget_guard=budget,
        )

        self.assertEqual(len(calls), 2)
        self.assertIn("OUTPUT-COMPRESSION RETRY", calls[1])
        self.assertIn("## Accuracy", text)
        self.assertEqual(status["phases"]["critique"]["status"], "complete")
        self.assertEqual(len(status["phases"]["critique"]["output_retries"]), 1)
        self.assertEqual(
            budget.events,
            [
                ("reserve", "arxiv:allowed:critique:paper"),
                ("settle", "reservation-1"),
                ("resolve", "arxiv:allowed:critique:paper"),
                ("reserve", "arxiv:allowed:critique:paper:compression-1"),
                ("settle", "reservation-2"),
                ("resolve", "arxiv:allowed:critique:paper:compression-1"),
            ],
        )
        ledger = (self.article / "api_cost_ledger.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event_id": "first"', ledger)
        self.assertIn('"event_id": "compact"', ledger)

    def test_paper_phase_fails_after_one_compaction_retry(self) -> None:
        module = load_module()
        status = {"record_id": "arxiv:allowed", "phases": {}}
        calls = 0

        class Client:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                nonlocal calls
                calls += 1
                response = completed_response("过长输出", f"incomplete-{calls}")
                response["status"] = "incomplete"
                response["incomplete_details"] = {"reason": "max_output_tokens"}
                return response, 0.1

        with self.assertRaisesRegex(module.runner.IncompleteResponseError, "max_output_tokens"):
            module._run_paper_model_phase(
                phase_name="critique",
                output_path=self.article / "04-critique.md",
                instructions="Return a bounded critique.",
                input_text="source and draft",
                max_output_tokens=4000,
                client=Client(),
                status=status,
                status_path=self.article / "paper_status.json",
                run_id="run-one",
                budget_guard=None,
            )

        self.assertEqual(calls, 2)
        self.assertEqual(status["phases"]["critique"]["status"], "failed")

    def test_hard_exact_translations_bind_repeated_pdf_headers_once(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "hard_constraints.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_id": "arxiv:allowed",
                        "exact_translations": [
                            {
                                "source": "Repeated running header",
                                "target": "统一运行页眉",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            mapping = module._hard_exact_translations(article, "arxiv:allowed")

        self.assertEqual(mapping, {"repeated running header": "统一运行页眉"})

    def test_hard_exact_translations_use_tracked_policy_when_article_has_none(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article = root / "article"
            article.mkdir()
            policy = root / "policy.json"
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

            mapping = module._hard_exact_translations(
                article,
                "arxiv:allowed",
                policy_path=policy,
            )

        self.assertEqual(mapping, {"header": "统一页眉"})

    def test_hard_exact_translations_reject_factually_invalid_target(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article = root / "article"
            article.mkdir()
            policy = root / "policy.json"
            policy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "records": {
                            "arxiv:allowed": {
                                "exact_translations": [
                                    {
                                        "source": "The detector reached 14 TeV.",
                                        "target": "该探测器达到 15 TeV。",
                                    }
                                ]
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "failed deterministic QC"):
                module._hard_exact_translations(
                    article,
                    "arxiv:allowed",
                    policy_path=policy,
                )

    def test_hard_exact_translations_reject_reordered_structure_placeholders(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article = root / "article"
            article.mkdir()
            policy = root / "policy.json"
            policy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "records": {
                            "arxiv:allowed": {
                                "exact_translations": [
                                    {
                                        "source": "First {v1}, then {v2}.",
                                        "target": "先是{v2}，再是{v1}。",
                                    }
                                ]
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "placeholder order"):
                module._hard_exact_translations(
                    article,
                    "arxiv:allowed",
                    policy_path=policy,
                )

    def test_tracked_mu3e_figure_caption_preserves_formula_placeholder_position(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            mapping = module._hard_exact_translations(
                Path(temporary),
                "arxiv:2204.00001",
            )

        source = (
            "figure 10. expected sensitivity to prompt dark photon decays in "
            "{v1}in the first phase of the mu3e experiment."
        )
        self.assertEqual(
            mapping[source],
            "图10。Mu3e 实验第一阶段对 {v1} 过程中瞬发暗光子衰变的预期灵敏度。",
        )

    def test_tracked_microelectronics_front_matter_preserves_page_numbers(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            mapping = module._hard_exact_translations(
                Path(temporary),
                "arxiv:2203.08973",
            )

        self.assertEqual(
            mapping[
                "submittedto the proceedings of the us community study on the "
                "future of particle physics (snowmass 2021)"
            ],
            "提交至美国粒子物理未来研究社区会议（Snowmass 2021）论文集",
        )
        self.assertEqual(
            mapping["3 status of currentinitiatives 4"],
            "3 当前举措的现状 4",
        )

    def test_hard_exact_translations_merge_local_overrides_with_tracked_baseline(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article = root / "article"
            article.mkdir()
            (article / "hard_constraints.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_id": "arxiv:allowed",
                        "exact_translations": [
                            {"source": "Header", "target": "本地页眉"},
                            {"source": "Local only", "target": "仅本地"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            policy = root / "policy.json"
            policy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "records": {
                            "arxiv:allowed": {
                                "exact_translations": [
                                    {"source": "Header", "target": "跟踪页眉"},
                                    {"source": "Tracked only", "target": "仅跟踪"},
                                ]
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            mapping = module._hard_exact_translations(
                article,
                "arxiv:allowed",
                policy_path=policy,
            )

        self.assertEqual(
            mapping,
            {"header": "本地页眉", "tracked only": "仅跟踪", "local only": "仅本地"},
        )

    def test_chunk_critique_context_is_local_and_marks_noop(self) -> None:
        module = load_module()
        critique = "chunk0001: 修正甲。\nchunk0002: 修正乙。\n"

        self.assertEqual(
            module._critique_context_for_chunk(critique, "chunk0001"),
            "# Actionable critique for this chunk only\nchunk0001: 修正甲。",
        )
        self.assertIn(
            module.NO_ACTIONABLE_CRITIQUE,
            module._critique_context_for_chunk(critique, "chunk0003"),
        )

    def test_chunk_critique_context_ignores_ids_mentioned_inside_another_finding(self) -> None:
        module = load_module()
        critique = "- chunk0005: 与chunk0016统一术语。\n"

        self.assertIn(
            module.NO_ACTIONABLE_CRITIQUE,
            module._critique_context_for_chunk(critique, "chunk0016"),
        )

    def test_figure_internal_policy_has_priority_over_fragment_heuristics(self) -> None:
        module = load_module()

        reason = module._chunk_passthrough_reason(
            {"id": "chunk0007"},
            reference_ids=set(),
            fragile_fragment_ids={"chunk0007"},
            figure_text_ids={"chunk0007"},
        )

        self.assertEqual(reason, "figure_internal_text_passthrough")

    def test_table_internal_policy_has_priority_over_fragment_heuristics(self) -> None:
        module = load_module()

        reason = module._chunk_passthrough_reason(
            {"id": "chunk0008"},
            reference_ids=set(),
            fragile_fragment_ids={"chunk0008"},
            figure_text_ids=set(),
            table_text_ids={"chunk0008"},
        )

        self.assertEqual(reason, "table_internal_text_passthrough")

    def test_nonlinguistic_symbol_line_is_passthrough(self) -> None:
        module = load_module()

        reason = module._chunk_passthrough_reason(
            {"id": "chunk0099", "source_text": "#**************\n"},
            reference_ids=set(),
            fragile_fragment_ids=set(),
            figure_text_ids=set(),
        )

        self.assertEqual(reason, "nonlinguistic_symbol_passthrough")

    def test_manual_correction_is_source_hash_pinned_and_updates_final_checkpoint(self) -> None:
        module = load_module()
        source = "samples)\n"
        source_hash = hashlib.sha256(source.encode()).hexdigest()
        chunk = {
            "id": "chunk0001",
            "source_file": "chunk0001.md",
            "output_file": "output_chunk0001.md",
            "source_hash": source_hash,
        }
        (self.article / "chunk0001.md").write_text(source, encoding="utf-8")
        (self.article / "output_chunk0001.md").write_text("错误文本\n", encoding="utf-8")
        status_dir = self.article / "chunk_status"
        status_dir.mkdir()
        (status_dir / "chunk0001.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "chunk_id": "chunk0001",
                    "stages": {
                        "academic": {
                            "status": "complete",
                            "output_hash": hashlib.sha256("错误文本\n".encode()).hexdigest(),
                            "qc": {"ok": True, "failures": []},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.article / "manual_corrections.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "corrections": [
                        {
                            "chunk_id": "chunk0001",
                            "source_hash": source_hash,
                            "replacement": "样本）\n",
                            "reason": "repair a PDF line-break fragment",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        applied = module._apply_manual_corrections(
            self.article, "arxiv:allowed", [chunk]
        )

        self.assertEqual(applied, 1)
        self.assertEqual(
            (self.article / "output_chunk0001.md").read_text(encoding="utf-8"),
            "样本）\n",
        )
        status = json.loads((status_dir / "chunk0001.json").read_text(encoding="utf-8"))
        academic = status["stages"]["academic"]
        self.assertTrue(academic["manual_correction_applied"])
        self.assertEqual(academic["manual_correction_reason"], "repair a PDF line-break fragment")

    def test_manual_correction_rejects_changed_source(self) -> None:
        module = load_module()
        chunk = {
            "id": "chunk0001",
            "source_file": "chunk0001.md",
            "output_file": "output_chunk0001.md",
            "source_hash": "current-source-hash",
        }
        (self.article / "manual_corrections.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "corrections": [
                        {
                            "chunk_id": "chunk0001",
                            "source_hash": "stale-source-hash",
                            "replacement": "校订。\n",
                            "reason": "stale correction",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "source hash mismatch"):
            module._apply_manual_corrections(
                self.article, "arxiv:allowed", [chunk]
            )

    def test_manual_correction_can_explicitly_audit_numeric_localization(self) -> None:
        module = load_module()
        source = "September 13, 2022\n"
        source_hash = hashlib.sha256(source.encode()).hexdigest()
        chunk = {
            "id": "chunk0001",
            "source_file": "chunk0001.md",
            "output_file": "output_chunk0001.md",
            "source_hash": source_hash,
        }
        (self.article / "chunk0001.md").write_text(source, encoding="utf-8")
        (self.article / "output_chunk0001.md").write_text("九月13，2022\n", encoding="utf-8")
        status_dir = self.article / "chunk_status"
        status_dir.mkdir()
        (status_dir / "chunk0001.json").write_text(
            json.dumps({"stages": {"academic": {"status": "complete"}}}),
            encoding="utf-8",
        )
        (self.article / "manual_corrections.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "corrections": [
                        {
                            "chunk_id": "chunk0001",
                            "source_hash": source_hash,
                            "replacement": "2022年9月13日\n",
                            "reason": "localize an English month name",
                            "allow_numeric_localization": True,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        applied = module._apply_manual_corrections(
            self.article, "arxiv:allowed", [chunk]
        )

        self.assertEqual(applied, 1)
        academic = json.loads(
            (status_dir / "chunk0001.json").read_text(encoding="utf-8")
        )["stages"]["academic"]
        self.assertTrue(academic["manual_correction_numeric_localization"])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.article = Path(self.temporary.name) / "papers" / "arxiv_allowed"
        self.article.mkdir(parents=True)
        source = "Original paragraph.\n"
        source_hash = hashlib.sha256(source.encode()).hexdigest()
        (self.article / "chunk0001.md").write_text(source, encoding="utf-8")
        (self.article / "manifest.json").write_text(
            json.dumps(
                {
                    "record_id": "arxiv:allowed",
                    "input_mode": "babeldoc_ir",
                    "chunks": [
                        {
                            "id": "chunk0001",
                            "order": 1,
                            "source_file": "chunk0001.md",
                            "output_file": "output_chunk0001.md",
                            "source_hash": source_hash,
                            "babeldoc_unit_id": "p0001-i0000",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.article / "chunking_status.json").write_text(
            json.dumps({"record_id": "arxiv:allowed"}), encoding="utf-8"
        )
        module = load_module()
        manifest = json.loads(
            (self.article / "manifest.json").read_text(encoding="utf-8")
        )
        constraints = module.runner.constraint_compiler.load_constraints(
            self.article,
            "arxiv:allowed",
            module.TRACKED_HARD_CONSTRAINTS,
        )
        module.runner.constraint_compiler.write_constraint_plan(
            self.article,
            module.runner.constraint_compiler.compile_constraint_plan(
                self.article,
                manifest,
                constraints,
            ),
        )

    def _write_local_glossary(self, terms: list[dict[str, object]]) -> None:
        (Path(self.temporary.name) / "global_glossary.json").write_text(
            json.dumps({"terms": terms}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _compile_current_constraint_plan(self) -> None:
        module = load_module()
        manifest = json.loads(
            (self.article / "manifest.json").read_text(encoding="utf-8")
        )
        constraints = module.runner.constraint_compiler.load_constraints(
            self.article,
            "arxiv:allowed",
            module.TRACKED_HARD_CONSTRAINTS,
        )
        module.runner.constraint_compiler.write_constraint_plan(
            self.article,
            module.runner.constraint_compiler.compile_constraint_plan(
                self.article,
                manifest,
                constraints,
            ),
        )

    def _rewrite_manifest_source_hash(self, source_file: str = "chunk0001.md") -> None:
        manifest_path = self.article / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_hash = hashlib.sha256(
            (self.article / source_file).read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
        manifest["chunks"][0]["source_hash"] = source_hash
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self._compile_current_constraint_plan()

    def _replace_with_short_chunks(self, count: int) -> None:
        chunks = []
        for index in range(1, count + 1):
            chunk_id = f"chunk{index:04d}"
            source_file = f"{chunk_id}.md"
            source = f"Original paragraph {index}.\n"
            (self.article / source_file).write_text(source, encoding="utf-8")
            chunks.append(
                {
                    "id": chunk_id,
                    "order": index,
                    "source_file": source_file,
                    "output_file": f"output_{chunk_id}.md",
                    "source_hash": hashlib.sha256(source.encode()).hexdigest(),
                    "babeldoc_unit_id": f"p{index:04d}-i0000",
                }
            )
        (self.article / "manifest.json").write_text(
            json.dumps(
                {
                    "record_id": "arxiv:allowed",
                    "input_mode": "babeldoc_ir",
                    "chunks": chunks,
                }
            ),
            encoding="utf-8",
        )
        self._compile_current_constraint_plan()

    def test_batched_draft_passes_translate_plain_chunks_in_one_request(self) -> None:
        module = load_module()
        self._replace_with_short_chunks(3)
        manifest = json.loads((self.article / "manifest.json").read_text(encoding="utf-8"))
        chunks = manifest["chunks"]
        constraint_plan = module.runner.constraint_compiler.load_constraint_plan(
            self.article,
            manifest,
            module.runner.constraint_compiler.load_constraints(
                self.article,
                "arxiv:allowed",
                module.TRACKED_HARD_CONSTRAINTS,
            ),
        )

        def chunk_task(chunk):
            return {
                "article_dir": self.article,
                "record_id": "arxiv:allowed",
                "chunk": chunk,
                "passthrough": False,
                "passthrough_reason": None,
                "fixed_translation": None,
                "fixed_translation_reason": None,
                "constraint_plan_sha256": constraint_plan["plan_sha256"],
                "retry_uncertain": False,
            }

        class BatchClient:
            calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                self.calls += 1
                payload = json.loads(input_text)
                translations = {
                    item["id"]: item["text"].replace(
                        "Original paragraph", "原文段落"
                    )
                    for item in payload["chunks"]
                }
                return {
                    "id": "draft-batch",
                    "status": "completed",
                    "model": "fake-model",
                    "output_text": json.dumps(
                        {"translations": translations}, ensure_ascii=False
                    ),
                    "usage": {
                        "input_tokens": 30,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 30,
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": 60,
                    },
                }, 0.1

        client = BatchClient()
        results = module.run_batched_draft_passes(
            self.article,
            chunks=chunks,
            chunk_task=chunk_task,
            terms=[],
            prompt="保持准确、完整。",
            client=client,
            budget_guard=None,
            run_id="draft-batch-test",
        )

        self.assertEqual(client.calls, 1)
        self.assertEqual(results["translate"].normal_requests, 1)
        self.assertEqual(results["translate"].recovery_requests, 0)
        self.assertEqual(results["terminology"].normal_requests, 0)
        for chunk in chunks:
            status = json.loads(
                (
                    self.article
                    / "chunk_status"
                    / f"{chunk['id']}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(status["stages"]["translate"]["status"], "complete")
            self.assertEqual(status["stages"]["terminology"]["status"], "complete")

    def _write_chunk_stage(
        self,
        module,
        *,
        stage: str,
        text: str = "",
        status: str = "complete",
        record_id: str = "arxiv:allowed",
        chunk_id: str = "chunk0001",
        source_file: str = "chunk0001.md",
        source_hash: str | None = None,
        extra_stage: dict[str, object] | None = None,
    ) -> None:
        status_path = self.article / "chunk_status" / f"{chunk_id}.json"
        if status_path.exists():
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        else:
            payload = {
                "schema_version": 1,
                "record_id": record_id,
                "chunk_id": chunk_id,
                "source_file": source_file,
                "source_hash": source_hash
                or hashlib.sha256(
                    (self.article / source_file).read_text(encoding="utf-8").encode("utf-8")
                ).hexdigest(),
                "stages": {},
            }
        payload["record_id"] = record_id
        payload["chunk_id"] = chunk_id
        payload["source_file"] = source_file
        if source_hash is not None:
            payload["source_hash"] = source_hash
        stage_payload = {"status": status}
        if status == "complete":
            output_path = module.runner.stage_output_path(
                self.article,
                chunk_id,
                "output_chunk0001.md",
                stage,
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(text, encoding="utf-8")
            stage_payload.update(
                {
                    "output_file": output_path.name,
                    "output_hash": module.runner.text_hash(text),
                    "qc": {"ok": True, "failures": []},
                }
            )
        if extra_stage:
            stage_payload.update(extra_stage)
        payload.setdefault("stages", {})[stage] = stage_payload
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_revision_ready_projection_counts_fresh_manifest_conservatively(self) -> None:
        module = load_module()
        self._write_local_glossary([{"source": "Original", "target": "原文"}])

        report = module.revision_ready_projection(self.article)

        self.assertTrue(report["projection_ready"])
        self.assertEqual(report["record_id"], "arxiv:allowed")
        self.assertEqual(
            report["missing_stage_api_calls"],
            {
                "analysis": 1,
                "translate": 1,
                "terminology": 1,
                "critique": 2,
                "revision": 1,
            },
        )
        self.assertEqual(report["projected_worst_case_api_calls"], 9)
        self.assertEqual(report["qc_retry_reserve_api_calls"], 3)
        self.assertEqual(report["identity_diagnostics"]["record_identity_mismatches"], [])
        self.assertEqual(report["identity_diagnostics"]["blocking_uncertain_checkpoints"], [])

    def test_revision_ready_projection_shards_unknown_critique_inputs(self) -> None:
        module = load_module()
        self._replace_with_short_chunks(40)
        self._write_local_glossary([])

        report = module.revision_ready_projection(self.article)

        chunks = json.loads((self.article / "manifest.json").read_text(encoding="utf-8"))["chunks"]
        expected_shards = module._projected_unknown_critique_shard_count(
            chunks,
            source_texts={
                chunk["id"]: (self.article / chunk["source_file"]).read_text(encoding="utf-8")
                for chunk in chunks
            },
        )
        self.assertEqual(report["missing_stage_api_calls"]["critique"], 2 * expected_shards)
        self.assertLess(report["missing_stage_api_calls"]["critique"], 80)

    def test_revision_ready_projection_caps_unknown_revision_targets(self) -> None:
        module = load_module()
        self._replace_with_short_chunks(40)
        self._write_local_glossary([])

        report = module.revision_ready_projection(self.article)

        self.assertEqual(
            report["missing_stage_api_calls"]["revision"],
            module.CRITIQUE_GLOBAL_MAX_FINDINGS,
        )

    def test_revision_ready_projection_uses_costliest_thirty_revision_targets(self) -> None:
        module = load_module()
        self._replace_with_short_chunks(40)
        self._write_local_glossary([])
        original_plan = module._planned_stage_model_subrequests

        def weighted_plan(**kwargs):
            plan = original_plan(**kwargs)
            if kwargs["stage"] == "revision":
                chunk_number = int(str(kwargs["chunk"]["id"])[-4:])
                plan["model_subrequest_count"] = 2 if chunk_number <= 5 else 1
            return plan

        with mock.patch.object(
            module,
            "_planned_stage_model_subrequests",
            side_effect=weighted_plan,
        ):
            report = module.revision_ready_projection(self.article)

        self.assertEqual(report["missing_stage_api_calls"]["revision"], 35)

    def test_critique_draft_projection_bound_is_enforced(self) -> None:
        module = load_module()
        chunks = json.loads(
            (self.article / "manifest.json").read_text(encoding="utf-8")
        )["chunks"]
        source_texts = {"chunk0001": "short source"}
        allowed = module._critique_draft_character_bound(source_texts["chunk0001"])

        module._require_critique_drafts_within_projection_bound(
            chunks,
            source_texts=source_texts,
            draft_texts={"chunk0001": "中" * allowed},
        )
        with self.assertRaisesRegex(RuntimeError, "exceeds the preflight projection bound"):
            module._require_critique_drafts_within_projection_bound(
                chunks,
                source_texts=source_texts,
                draft_texts={"chunk0001": "中" * (allowed + 1)},
            )

    def test_critique_revision_target_cap_is_enforced_before_revision(self) -> None:
        module = load_module()
        self._replace_with_short_chunks(module.CRITIQUE_GLOBAL_MAX_FINDINGS + 1)
        chunks = json.loads(
            (self.article / "manifest.json").read_text(encoding="utf-8")
        )["chunks"]
        critique = "\n".join(
            f"- {chunk['id']}: revise" for chunk in chunks
        )

        with self.assertRaisesRegex(RuntimeError, "exceeds the revision target cap"):
            module._require_critique_revision_targets_within_bound(critique, chunks)

        module._require_critique_revision_targets_within_bound(
            "\n".join(
                f"- {chunk['id']}: revise"
                for chunk in chunks[: module.CRITIQUE_GLOBAL_MAX_FINDINGS]
            ),
            chunks,
        )

    def test_revision_ready_projection_reuses_valid_translate_checkpoint_only(self) -> None:
        module = load_module()
        self._write_local_glossary([{"source": "Original", "target": "原文"}])
        self._write_chunk_stage(module, stage="translate", text="初稿段落。\n")

        report = module.revision_ready_projection(self.article)

        self.assertTrue(report["projection_ready"])
        self.assertEqual(
            report["missing_stage_api_calls"],
            {
                "analysis": 1,
                "translate": 0,
                "terminology": 1,
                "critique": 1,
                "revision": 1,
            },
        )
        self.assertEqual(report["projected_worst_case_api_calls"], 6)
        self.assertEqual(report["qc_retry_reserve_api_calls"], 2)

    def test_revision_ready_projection_returns_zero_after_complete_revision_ready_state(self) -> None:
        module = load_module()

        class FakeClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                if "paper-level content analysis" in instructions:
                    return completed_response(
                        "## Content Summary\n测试论文。\n\n## Terminology\nOriginal → 原文\n\n"
                        "## Tone & Style\n学术。\n\n## Translation Challenges\n- 无。\n",
                        "analysis",
                    ), 0.1
                if "critical review" in instructions:
                    return completed_response(
                        "## Accuracy\n- chunk0001: 无。\n\n## Native Voice\n- chunk0001: 调整句法。\n\n"
                        "## Notes & Adaptation\n- 无。\n\n## Summary\n- 完成。\n",
                        "critique",
                    ), 0.1
                if "first faithful translation pass" in instructions:
                    return completed_response("原文初稿段落。\n", "draft"), 0.1
                if "refined revision pass" in instructions:
                    return completed_response("原文修订段落。\n", "revision"), 0.1
                raise AssertionError(instructions)

        result = module.run_refined_article(
            self.article,
            client=FakeClient(),
            terms=[{"source": "Original", "target": "原文"}],
            run_id="projection-ready",
            stop_after_revision=True,
        )

        self.assertEqual(result["status"], "revision_ready")
        report = module.revision_ready_projection(self.article)
        self.assertTrue(report["projection_ready"])
        self.assertEqual(
            report["missing_stage_api_calls"],
            {
                "analysis": 0,
                "translate": 0,
                "terminology": 0,
                "critique": 0,
                "revision": 0,
            },
        )
        self.assertEqual(report["projected_worst_case_api_calls"], 0)

    def test_revision_ready_projection_invalidates_model_checkpoint_after_exact_rule_added(self) -> None:
        module = load_module()
        manifest = json.loads((self.article / "manifest.json").read_text(encoding="utf-8"))
        original_constraints = {
            "schema_version": 1,
            "record_id": "arxiv:allowed",
            "exact_translations": [],
            "forbidden_translations": [],
        }
        module.runner.constraint_compiler.write_constraint_plan(
            self.article,
            module.runner.constraint_compiler.compile_constraint_plan(
                self.article,
                manifest,
                original_constraints,
            ),
        )
        (self.article / "hard_constraints.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "exact_translations": [
                        {"source": "Original paragraph.", "target": "精确译文段落。"}
                    ],
                }
            ),
            encoding="utf-8",
        )

        report = module.revision_ready_projection(self.article)

        self.assertFalse(report["projection_ready"])
        self.assertIn(
            "constraint_plan",
            report["identity_diagnostics"]["invalid_checkpoint_hashes"],
        )

        self._compile_current_constraint_plan()
        for stage in ("translate", "terminology", "revision"):
            self._write_chunk_stage(
                module,
                stage=stage,
                text="旧模型译文。\n",
                extra_stage={"execution_policy": "model_pipeline"},
            )
        recompiled = module.revision_ready_projection(self.article)

        self.assertTrue(recompiled["projection_ready"])
        self.assertGreater(recompiled["missing_stage_api_calls"]["critique"], 0)
        self.assertGreater(recompiled["projected_worst_case_api_calls"], 0)

    def test_revision_ready_projection_fails_closed_on_record_identity_mismatch(self) -> None:
        module = load_module()
        (self.article / "paper_status.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:other",
                    "phases": {},
                }
            ),
            encoding="utf-8",
        )

        report = module.revision_ready_projection(self.article)

        self.assertFalse(report["projection_ready"])
        self.assertEqual(report["projected_worst_case_api_calls"], 0)
        self.assertTrue(report["identity_diagnostics"]["record_identity_mismatches"])

    def test_revision_ready_projection_fails_closed_on_uncertain_chunk_phase(self) -> None:
        module = load_module()
        self._write_local_glossary([{"source": "Original", "target": "原文"}])
        self._write_chunk_stage(
            module,
            stage="translate",
            status="uncertain",
            extra_stage={"error": "transport status unknown"},
        )

        report = module.revision_ready_projection(self.article)

        self.assertFalse(report["projection_ready"])
        self.assertEqual(report["projected_worst_case_api_calls"], 0)
        self.assertIn(
            "chunk0001/translate",
            report["identity_diagnostics"]["blocking_uncertain_checkpoints"],
        )

    def test_structure_dense_projection_matches_actual_stage_subrequest_fanout(self) -> None:
        module = load_module()
        source = " ".join(f"part $x_{{{index}}}$" for index in range(30)) + "\n"
        (self.article / "chunk0001.md").write_text(source, encoding="utf-8")
        self._rewrite_manifest_source_hash()
        self._write_local_glossary([{"source": "part", "target": "部分"}])
        chunk = json.loads((self.article / "manifest.json").read_text(encoding="utf-8"))["chunks"][0]

        translate_plan = module._planned_stage_model_subrequests(
            article_dir=self.article,
            chunk=chunk,
            stage="translate",
            current=source,
            terms=[{"source": "part", "target": "部分"}],
            paper_context="",
            stage_status={},
        )
        terminology_plan = module._planned_stage_model_subrequests(
            article_dir=self.article,
            chunk=chunk,
            stage="terminology",
            current=source,
            terms=[{"source": "part", "target": "部分"}],
            paper_context="",
            stage_status={},
        )
        revision_plan = module._planned_stage_model_subrequests(
            article_dir=self.article,
            chunk=chunk,
            stage="revision",
            current=source,
            terms=[{"source": "part", "target": "部分"}],
            paper_context="# Actionable critique for this chunk only\n- chunk0001: refine wording",
            stage_status={},
        )

        self.assertGreater(translate_plan["model_subrequest_count"], 1)
        self.assertEqual(translate_plan["model_subrequest_count"], 2)
        self.assertEqual(terminology_plan["model_subrequest_count"], 2)
        self.assertEqual(revision_plan["model_subrequest_count"], 2)

        class DenseClient:
            def __init__(self, *, replace_part: bool) -> None:
                self.calls = 0
                self.replace_part = replace_part

            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                self.calls += 1
                match = re.search(r'(\{"protocol":.+)', input_text, re.S)
                if match is None:
                    raise AssertionError("missing structure-slot payload")
                payload, _offset = json.JSONDecoder().raw_decode(match.group(1))
                translations = {}
                for item in payload["slots"]:
                    text = item["text"]
                    if self.replace_part:
                        text = text.replace("part", "部分")
                    translations[item["id"]] = text
                return completed_response(
                    json.dumps({"translations": translations}, ensure_ascii=False),
                    f"dense-{self.calls}",
                ), 0.1

        def task() -> dict[str, object]:
            return {
                "article_dir": self.article,
                "record_id": "arxiv:allowed",
                "chunk": chunk,
                "passthrough": False,
                "passthrough_reason": None,
                "fixed_translation": None,
                "fixed_translation_reason": None,
                "retry_uncertain": False,
            }

        translate_client = DenseClient(replace_part=False)
        module.runner.process_chunk(
            task(),
            translate_client,
            [],
            run_id="dense-translate",
            stages=("translate",),
        )
        self.assertEqual(translate_client.calls, translate_plan["model_subrequest_count"])

        translate_path = module.runner.stage_output_path(
            self.article,
            "chunk0001",
            "output_chunk0001.md",
            "translate",
        )
        terminology_client = DenseClient(replace_part=True)
        module.runner.process_chunk(
            task(),
            terminology_client,
            [{"source": "part", "target": "部分"}],
            run_id="dense-terminology",
            stages=("terminology",),
            initial_text_path=translate_path,
        )
        self.assertEqual(
            terminology_client.calls,
            terminology_plan["model_subrequest_count"],
        )

        terminology_path = module.runner.stage_output_path(
            self.article,
            "chunk0001",
            "output_chunk0001.md",
            "terminology",
        )
        revision_client = DenseClient(replace_part=True)
        module.runner.process_chunk(
            task(),
            revision_client,
            [{"source": "part", "target": "部分"}],
            run_id="dense-revision",
            stages=("revision",),
            paper_context="# Actionable critique for this chunk only\n- chunk0001: refine wording",
            paper_context_identity="# Actionable critique for this chunk only\n- chunk0001: refine wording",
            initial_text_path=terminology_path,
        )
        self.assertEqual(revision_client.calls, revision_plan["model_subrequest_count"])

        for stage_name in ("translate", "terminology", "revision"):
            status = json.loads(
                (self.article / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                len(status["stages"][stage_name]["subrequests"]),
                module._planned_stage_model_subrequests(
                    article_dir=self.article,
                    chunk=chunk,
                    stage=stage_name,
                    current=source,
                    terms=[{"source": "part", "target": "部分"}],
                    paper_context=(
                        "# Actionable critique for this chunk only\n- chunk0001: refine wording"
                        if stage_name == "revision"
                        else ""
                    ),
                    stage_status={},
                )["model_subrequest_count"],
            )

        report = module.revision_ready_projection(self.article)

        self.assertTrue(report["projection_ready"])
        self.assertEqual(report["missing_stage_api_calls"]["analysis"], 1)
        self.assertEqual(report["missing_stage_api_calls"]["translate"], 0)
        self.assertEqual(report["missing_stage_api_calls"]["terminology"], 0)
        self.assertEqual(report["missing_stage_api_calls"]["critique"], 1)
        self.assertEqual(report["missing_stage_api_calls"]["revision"], 2)

    def test_structure_dense_projection_uses_source_bound_when_downstream_stage_input_is_missing(self) -> None:
        module = load_module()
        source = " ".join(f"part $x_{{{index}}}$" for index in range(30)) + "\n"
        (self.article / "chunk0001.md").write_text(source, encoding="utf-8")
        self._rewrite_manifest_source_hash()
        self._write_local_glossary([{"source": "part", "target": "部分"}])

        report = module.revision_ready_projection(self.article)

        self.assertTrue(report["projection_ready"])
        self.assertEqual(report["missing_stage_api_calls"]["translate"], 2)
        self.assertEqual(report["missing_stage_api_calls"]["terminology"], 2)
        self.assertEqual(report["missing_stage_api_calls"]["revision"], 2)
        self.assertGreaterEqual(report["projected_worst_case_api_calls"], 7)

    def test_refined_artifacts_causally_gate_final_translation_and_resume(self) -> None:
        module = load_module()

        class FakeClient:
            calls = 0
            paper_phase_limits: dict[str, int] = {}
            test_case = self

            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                self.calls += 1
                if "paper-level content analysis" in instructions:
                    self.paper_phase_limits["analysis"] = max_output_tokens
                    return completed_response(
                        "## Content Summary\n测试论文。\n\n## Terminology\nOriginal → 原文\n\n"
                        "## Tone & Style\n学术。\n\n## Translation Challenges\n- 无。\n",
                        "analysis",
                    ), 0.1
                if "critical review" in instructions:
                    self.paper_phase_limits["critique"] = max_output_tokens
                    if "chunk0001" not in input_text or "初稿段落" not in input_text:
                        raise AssertionError("critique did not receive tagged merged draft")
                    return completed_response(
                        "## Accuracy\n- chunk0001: 无。\n\n## Native Voice\n- chunk0001: 调整句法。\n\n"
                        "## Notes & Adaptation\n- 无。\n\n## Summary\n0 个事实错误，1 项表达改进。\n",
                        "critique",
                    ), 0.1
                if "first faithful translation pass" in instructions:
                    if "## Content Summary" not in input_text:
                        raise AssertionError("draft did not receive 02-prompt context")
                    return completed_response("原文初稿段落。", "draft"), 0.1
                if "refined revision pass" in instructions:
                    if "chunk0001: 调整句法" not in input_text:
                        raise AssertionError("revision did not receive critique")
                    return completed_response("原文修订段落。", "revision"), 0.1
                if "AI-mannerism cleanup pass" in instructions:
                    payload = json.loads(input_text)
                    return {
                        "id": "anti-ai",
                        "status": "completed",
                        "model": "fake-style-model",
                        "output_text": json.dumps(
                            {"translations": {"chunk0001": "原文自然段落。"}},
                            ensure_ascii=False,
                        ),
                        "usage": {
                            "input_tokens": 10,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 10,
                            "output_tokens_details": {"reasoning_tokens": 0},
                            "total_tokens": 20,
                        },
                    }, 0.1
                if "final Chinese naturalization" in instructions:
                    payload = json.loads(input_text)
                    self.test_case.assertEqual(payload["chunks"][0]["id"], "chunk0001")
                    return {
                        "id": "academic",
                        "status": "completed",
                        "model": "fake-style-model",
                        "output_text": json.dumps(
                            {"translations": {"chunk0001": "原文最终学术段落。"}},
                            ensure_ascii=False,
                        ),
                        "usage": {
                            "input_tokens": 10,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 10,
                            "output_tokens_details": {"reasoning_tokens": 0},
                            "total_tokens": 20,
                        },
                    }, 0.1
                raise AssertionError(instructions)

        client = FakeClient()
        result = module.run_refined_article(
            self.article,
            client=client,
            terms=[{"source": "Original", "target": "原文"}],
            run_id="run-one",
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(client.calls, 6)
        self.assertEqual(
            client.paper_phase_limits,
            {"analysis": 4000, "critique": 4000},
        )
        for filename in (
            "01-analysis.md",
            "02-prompt.md",
            "03-draft.md",
            "04-critique.md",
            "05-revision.md",
            "translation.md",
        ):
            self.assertTrue((self.article / filename).is_file(), filename)
        self.assertEqual(
            (self.article / "03-draft.md").read_text(encoding="utf-8"),
            "<!-- chunk0001 p0001-i0000 -->\n原文初稿段落。\n",
        )
        self.assertEqual(
            (self.article / "05-revision.md").read_text(encoding="utf-8"),
            "<!-- chunk0001 p0001-i0000 -->\n原文修订段落。\n",
        )
        self.assertEqual(
            (self.article / "translation.md").read_text(encoding="utf-8"),
            "<!-- chunk0001 p0001-i0000 -->\n原文最终学术段落。\n",
        )
        expected_cost = 6 * module.runner.estimate_cost_rmb(
            {
                "input_tokens": 10,
                "cached_tokens": 0,
                "output_tokens": 10,
                "total_tokens": 20,
            }
        )
        self.assertAlmostEqual(
            module.existing_article_cost_rmb(
                self.article, module.runner.DEFAULT_USD_CNY_RATE
            ),
            expected_cost,
        )

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("valid refined checkpoints must resume without API calls")

        with mock.patch.object(
            module,
            "_require_critique_drafts_within_projection_bound",
            side_effect=AssertionError("legacy critique reuse must bypass a new shard bound"),
        ):
            resumed = module.run_refined_article(
                self.article,
                client=NoCallClient(),
                terms=[{"source": "Original", "target": "原文"}],
                run_id="run-two",
            )
        self.assertEqual(resumed["status"], "complete")
        status = json.loads((self.article / "paper_status.json").read_text(encoding="utf-8"))
        self.assertTrue(all(item["status"] == "complete" for item in status["phases"].values()))

    def test_stop_after_revision_returns_revision_ready_without_running_style_passes(self) -> None:
        module = load_module()

        class FakeClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                if "paper-level content analysis" in instructions:
                    return completed_response(
                        "## Content Summary\n测试论文。\n\n## Terminology\nOriginal → 原文\n\n"
                        "## Tone & Style\n学术。\n\n## Translation Challenges\n- 无。\n",
                        "analysis",
                    ), 0.1
                if "critical review" in instructions:
                    return completed_response(
                        "## Accuracy\n- chunk0001: 无。\n\n## Native Voice\n- chunk0001: 调整句法。\n\n"
                        "## Notes & Adaptation\n- 无。\n\n## Summary\n- 完成。\n",
                        "critique",
                    ), 0.1
                if "first faithful translation pass" in instructions:
                    return completed_response("原文初稿段落。\n", "draft"), 0.1
                if "refined revision pass" in instructions:
                    return completed_response("原文修订段落。\n", "revision"), 0.1
                raise AssertionError(instructions)

        with mock.patch.object(
            module,
            "run_batched_final_style_passes",
            side_effect=AssertionError("style passes must not run"),
        ):
            result = module.run_refined_article(
                self.article,
                client=FakeClient(),
                terms=[{"source": "Original", "target": "原文"}],
                run_id="run-stop-after-revision",
                stop_after_revision=True,
            )

        self.assertEqual(
            result,
            {"record_id": "arxiv:allowed", "status": "revision_ready", "chunks": 1},
        )
        self.assertEqual(
            (self.article / "05-revision.md").read_text(encoding="utf-8"),
            "<!-- chunk0001 p0001-i0000 -->\n原文修订段落。\n",
        )
        status = json.loads((self.article / "paper_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "revision_ready")
        self.assertEqual(status["phases"]["revision_merge"]["status"], "complete")
        self.assertFalse((self.article / "translation.md").exists())

    def test_stop_after_revision_rerun_preserves_valid_complete_state_without_style_rerun(self) -> None:
        module = load_module()

        class FakeClient:
            calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                self.calls += 1
                if "paper-level content analysis" in instructions:
                    return completed_response(
                        "## Content Summary\n测试论文。\n\n## Terminology\nOriginal → 原文\n\n"
                        "## Tone & Style\n学术。\n\n## Translation Challenges\n- 无。\n",
                        "analysis",
                    ), 0.1
                if "critical review" in instructions:
                    return completed_response(
                        "## Accuracy\n- chunk0001: 无。\n\n## Native Voice\n- chunk0001: 调整句法。\n\n"
                        "## Notes & Adaptation\n- 无。\n\n## Summary\n0 个事实错误，1 项表达改进。\n",
                        "critique",
                    ), 0.1
                if "first faithful translation pass" in instructions:
                    return completed_response("原文初稿段落。\n", "draft"), 0.1
                if "refined revision pass" in instructions:
                    return completed_response("原文修订段落。\n", "revision"), 0.1
                if "AI-mannerism cleanup pass" in instructions:
                    return {
                        "id": "anti-ai",
                        "status": "completed",
                        "model": "fake-style-model",
                        "output_text": json.dumps(
                            {"translations": {"chunk0001": "原文自然段落。"}},
                            ensure_ascii=False,
                        ),
                        "usage": {
                            "input_tokens": 10,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 10,
                            "output_tokens_details": {"reasoning_tokens": 0},
                            "total_tokens": 20,
                        },
                    }, 0.1
                if "final Chinese naturalization" in instructions:
                    return {
                        "id": "academic",
                        "status": "completed",
                        "model": "fake-style-model",
                        "output_text": json.dumps(
                            {"translations": {"chunk0001": "原文最终学术段落。"}},
                            ensure_ascii=False,
                        ),
                        "usage": {
                            "input_tokens": 10,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 10,
                            "output_tokens_details": {"reasoning_tokens": 0},
                            "total_tokens": 20,
                        },
                    }, 0.1
                raise AssertionError(instructions)

        first_result = module.run_refined_article(
            self.article,
            client=FakeClient(),
            terms=[{"source": "Original", "target": "原文"}],
            run_id="run-complete",
        )
        self.assertEqual(first_result["status"], "complete")

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("complete rerun must not call the API")

        with mock.patch.object(
            module,
            "run_batched_final_style_passes",
            side_effect=AssertionError("style passes must not rerun"),
        ):
            rerun_result = module.run_refined_article(
                self.article,
                client=NoCallClient(),
                terms=[{"source": "Original", "target": "原文"}],
                run_id="run-stop-after-complete",
                stop_after_revision=True,
            )

        self.assertEqual(
            rerun_result,
            {"record_id": "arxiv:allowed", "status": "complete", "chunks": 1},
        )
        status = json.loads((self.article / "paper_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["phases"]["final_merge"]["status"], "complete")
        self.assertEqual(
            (self.article / "translation.md").read_text(encoding="utf-8"),
            "<!-- chunk0001 p0001-i0000 -->\n原文最终学术段落。\n",
        )

    def test_final_style_uses_ordered_exact_id_batches(self) -> None:
        module = load_module()
        module.load_neighbor_context = lambda *_args, **_kwargs: ""
        module._reference_chunk_ids = lambda _article_dir, _chunks: {"chunk0004"}

        chunks = []
        for index, source_text in enumerate(
            (
                "Alpha body source.\n",
                "Beta body source.\n",
                "Gamma body source.\n",
                "A. Author. Reference entry.\n",
            ),
            1,
        ):
            chunk_id = f"chunk{index:04d}"
            source_hash = hashlib.sha256(source_text.encode()).hexdigest()
            source_file = f"{chunk_id}.md"
            output_file = f"output_{chunk_id}.md"
            (self.article / source_file).write_text(source_text, encoding="utf-8")
            chunks.append(
                {
                    "id": chunk_id,
                    "order": index,
                    "source_file": source_file,
                    "output_file": output_file,
                    "source_hash": source_hash,
                    "babeldoc_unit_id": f"p{index:04d}-i0000",
                }
            )
        (self.article / "manifest.json").write_text(
            json.dumps(
                {
                    "record_id": "arxiv:allowed",
                    "input_mode": "babeldoc_ir",
                    "chunks": chunks,
                }
            ),
            encoding="utf-8",
        )
        self._compile_current_constraint_plan()

        style_calls: list[tuple[str, tuple[str, ...]]] = []
        release_third_anti_ai = __import__("threading").Event()
        labels = {
            "chunk0001": "阿尔法",
            "chunk0002": "贝塔",
            "chunk0003": "伽马",
        }

        def style_response(
            stage: str,
            ids: tuple[str, ...],
            *,
            response_id: str,
        ) -> dict[str, object]:
            if len(ids) == 1:
                chunk_id = ids[0]
                text = (
                    f"自然润色 {chunk_id}。\n"
                    if stage == "anti_ai"
                    else f"学术润色 {chunk_id}。\n"
                )
                return completed_response(text, response_id)
            translations = {
                chunk_id: (
                    f"自然润色 {chunk_id}。\n"
                    if stage == "anti_ai"
                    else f"学术润色 {chunk_id}。\n"
                )
                for chunk_id in ids
            }
            return {
                "id": response_id,
                "status": "completed",
                "model": "fake-style-model",
                "output_text": json.dumps(
                    {"translations": translations},
                    ensure_ascii=False,
                ),
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 10,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 20,
                },
            }

        class FakeClient:
            paper_phase_limits: dict[str, int] = {}

            @staticmethod
            def _chunk_ids_from_text(input_text: str) -> tuple[str, ...]:
                if '"protocol": "snowmass-style-batch-v1"' in input_text:
                    payload = json.loads(input_text)
                    return tuple(chunk["id"] for chunk in payload["chunks"])
                markers = {
                    "chunk0001": ("Alpha body source", "初稿 阿尔法 正文", "修订 阿尔法 正文", "自然润色 阿尔法 正文"),
                    "chunk0002": ("Beta body source", "初稿 贝塔 正文", "修订 贝塔 正文", "自然润色 贝塔 正文"),
                    "chunk0003": ("Gamma body source", "初稿 伽马 正文", "修订 伽马 正文", "自然润色 伽马 正文"),
                }
                for chunk_id, candidates in markers.items():
                    if any(candidate in input_text for candidate in candidates):
                        return (chunk_id,)
                raise AssertionError(f"cannot infer chunk id from input: {input_text}")

            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                if "paper-level content analysis" in instructions:
                    self.paper_phase_limits["analysis"] = max_output_tokens
                    return completed_response(
                        "## Content Summary\n测试论文。\n\n## Terminology\n- 无。\n\n"
                        "## Tone & Style\n学术。\n\n## Translation Challenges\n- 无。\n",
                        "analysis",
                    ), 0.1
                if "critical review" in instructions:
                    self.paper_phase_limits["critique"] = max_output_tokens
                    return completed_response(
                        "## Accuracy\n- chunk0001: 无。\n- chunk0002: 无。\n- chunk0003: 无。\n\n"
                        "## Native Voice\n- chunk0001: 调整句法。\n- chunk0002: 调整句法。\n- chunk0003: 调整句法。\n\n"
                        "## Notes & Adaptation\n- 无。\n\n## Summary\n- 完成。\n",
                        "critique",
                    ), 0.1
                if "first faithful translation pass" in instructions:
                    chunk_id = self._chunk_ids_from_text(input_text)[0]
                    return completed_response(
                        f"初稿 {labels[chunk_id]} 正文。\n",
                        f"draft-{chunk_id}",
                    ), 0.1
                if "refined revision pass" in instructions:
                    chunk_id = self._chunk_ids_from_text(input_text)[0]
                    return completed_response(
                        f"修订 {labels[chunk_id]} 正文。\n",
                        f"revision-{chunk_id}",
                    ), 0.1
                if "AI-mannerism cleanup pass" in instructions:
                    ids = self._chunk_ids_from_text(input_text)
                    style_calls.append(("anti_ai", ids))
                    if ids == ("chunk0003",):
                        released = release_third_anti_ai.wait(timeout=5)
                        if not released:
                            raise AssertionError("anti_ai gate did not release chunk0003")
                    if '"protocol": "snowmass-style-batch-v1"' in input_text:
                        payload = json.loads(input_text)
                        return {
                            "id": "anti-ai-" + "-".join(ids),
                            "status": "completed",
                            "model": "fake-style-model",
                            "output_text": json.dumps(
                                {
                                    "translations": {
                                        chunk["id"]: chunk["text"]
                                        for chunk in payload["chunks"]
                                    }
                                },
                                ensure_ascii=False,
                            ),
                            "usage": {
                                "input_tokens": 10,
                                "input_tokens_details": {"cached_tokens": 0},
                                "output_tokens": 10,
                                "output_tokens_details": {"reasoning_tokens": 0},
                                "total_tokens": 20,
                            },
                        }, 0.1
                    return style_response("anti_ai", ids, response_id="anti-ai-" + "-".join(ids)), 0.1
                if "final Chinese naturalization" in instructions:
                    ids = self._chunk_ids_from_text(input_text)
                    style_calls.append(("academic", ids))
                    release_third_anti_ai.set()
                    if '"protocol": "snowmass-style-batch-v1"' in input_text:
                        payload = json.loads(input_text)
                        return {
                            "id": "academic-" + "-".join(ids),
                            "status": "completed",
                            "model": "fake-style-model",
                            "output_text": json.dumps(
                                {
                                    "translations": {
                                        chunk["id"]: chunk["text"]
                                        for chunk in payload["chunks"]
                                    }
                                },
                                ensure_ascii=False,
                            ),
                            "usage": {
                                "input_tokens": 10,
                                "input_tokens_details": {"cached_tokens": 0},
                                "output_tokens": 10,
                                "output_tokens_details": {"reasoning_tokens": 0},
                                "total_tokens": 20,
                            },
                        }, 0.1
                    return style_response("academic", ids, response_id="academic-" + "-".join(ids)), 0.1
                raise AssertionError(instructions)

        result = module.run_refined_article(
            self.article,
            client=FakeClient(),
            terms=[],
            run_id="run-batched-style",
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            style_calls,
            [
                ("anti_ai", ("chunk0001", "chunk0002", "chunk0003")),
                ("academic", ("chunk0001", "chunk0002", "chunk0003")),
            ],
        )
        projection = json.loads(
            (self.article / "style_batch_projection.json").read_text(encoding="utf-8")
        )
        self.assertEqual(projection["execution_mode"], "exact_id_batching")
        self.assertEqual(projection["schema_version"], 1)
        self.assertEqual(projection["eligible_chunks"], 3)
        self.assertEqual(projection["groupable_chunks"], 3)
        self.assertEqual(projection["non_groupable_chunks"], 0)
        self.assertEqual(projection["current_style_requests"], 6)
        self.assertEqual(projection["projected_style_requests"], 2)
        self.assertAlmostEqual(projection["projected_request_reduction_fraction"], 2 / 3)
        self.assertEqual(projection["projected_groups"], 1)
        self.assertEqual(projection["max_group_size"], 3)
        self.assertGreater(projection["max_group_characters"], 0)
        self.assertEqual(projection["groups"], [["chunk0001", "chunk0002", "chunk0003"]])
        self.assertEqual(
            projection["planned"]["anti_ai"]["normal_batches"],
            [["chunk0001", "chunk0002", "chunk0003"]],
        )
        self.assertEqual(
            projection["planned"]["academic"]["normal_batches"],
            [["chunk0001", "chunk0002", "chunk0003"]],
        )
        self.assertEqual(projection["planned"]["anti_ai"]["worst_case_requests"], 2)
        self.assertEqual(projection["planned"]["academic"]["worst_case_requests"], 2)
        self.assertEqual(projection["actual"]["anti_ai"]["normal_requests"], 1)
        self.assertEqual(projection["actual"]["academic"]["normal_requests"], 1)
        self.assertEqual(projection["actual"]["anti_ai"]["recovery_requests"], 0)
        self.assertEqual(projection["actual"]["academic"]["recovery_requests"], 0)
        request_chunk_ids = [
            chunk_id
            for _stage, ids in style_calls
            for chunk_id in ids
        ]
        self.assertNotIn("chunk0004", request_chunk_ids)
        production = load_production_module()
        metrics, _gate = production.production_metrics_and_gate(
            stage="pilot10",
            through_stage="translated",
            eligible_record_count=1,
            selected_count=1,
            results=[
                {
                    "status": "translated",
                    "source_characters": 100,
                    "style_batch_projection": projection,
                }
            ],
            failures=[],
            budget={
                "stage_spent_rmb": 1.0,
                "project_spent_rmb": 1.0,
                "stage_usage": {"api_calls": 1},
            },
        )
        self.assertEqual(metrics["style_batch_projection"]["papers"], 1)
        self.assertEqual(metrics["style_batch_projection"]["eligible_chunks"], 3)
        self.assertEqual(metrics["style_batch_projection"]["groupable_chunks"], 3)
        self.assertEqual(metrics["style_batch_projection"]["current_style_requests"], 6)
        self.assertEqual(metrics["style_batch_projection"]["projected_style_requests"], 2)
        self.assertAlmostEqual(
            metrics["style_batch_projection"]["projected_request_reduction_fraction"],
            2 / 3,
        )
        for chunk in chunks:
            output_path = module.runner.stage_output_path(
                self.article,
                str(chunk["id"]),
                str(chunk["output_file"]),
                "academic",
            )
            self.assertTrue(output_path.is_file(), chunk["id"])

    def test_existing_cost_recovers_partial_subrequests_and_uncertain_replay(self) -> None:
        module = load_module()
        usage = {
            "input_tokens": 100,
            "cached_tokens": 0,
            "output_tokens": 50,
            "total_tokens": 150,
        }
        status_dir = self.article / "chunk_status"
        status_dir.mkdir(parents=True, exist_ok=True)
        (status_dir / "chunk0001.json").write_text(
            json.dumps(
                {
                    "stages": {
                        "translate": {
                            "status": "failed",
                            "subrequests": [{"status": "complete", "usage": usage}],
                            "uncertain_replays": [{"conservative_cost_rmb": 0.123}],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        expected = module.runner.estimate_cost_rmb(
            usage, module.runner.DEFAULT_USD_CNY_RATE
        ) + 0.123
        self.assertAlmostEqual(
            module.existing_article_cost_rmb(
                self.article, module.runner.DEFAULT_USD_CNY_RATE
            ),
            expected,
        )

    def test_existing_cost_includes_paper_phase_uncertain_replay(self) -> None:
        module = load_module()
        (self.article / "paper_status.json").write_text(
            json.dumps(
                {
                    "phases": {
                        "critique": {
                            "status": "complete",
                            "uncertain_replays": [
                                {"conservative_cost_rmb": 0.456}
                            ],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        self.assertAlmostEqual(
            module.existing_article_cost_rmb(
                self.article, module.runner.DEFAULT_USD_CNY_RATE
            ),
            0.456,
        )

    def test_immutable_cost_ledger_overrides_mutable_checkpoint_totals(self) -> None:
        module = load_module()
        (self.article / "cost_ledger_baseline.json").write_text(
            json.dumps({"cost_rmb": 2.0}), encoding="utf-8"
        )
        events = [
            {"event_id": "response-one", "cost_rmb": 0.1},
            {"event_id": "response-one", "cost_rmb": 0.1},
            {"event_id": "response-two", "cost_rmb": 0.2},
        ]
        (self.article / "api_cost_ledger.jsonl").write_text(
            "\n".join(json.dumps(item) for item in events) + "\n",
            encoding="utf-8",
        )

        self.assertAlmostEqual(
            module.existing_article_cost_rmb(
                self.article, module.runner.DEFAULT_USD_CNY_RATE
            ),
            2.3,
        )

    def test_cli_fails_closed_before_loading_credentials(self) -> None:
        module = load_module()
        rights = Path(self.temporary.name) / "rights.json"
        rights.write_text(
            json.dumps(
                [
                    {"record_id": "arxiv:allowed", "publication_allowed": False},
                    {"record_id": "arxiv:other", "publication_allowed": True},
                ]
            ),
            encoding="utf-8",
        )

        def forbidden_key_load() -> str:
            raise AssertionError("rights rejection must happen before credential access")

        module.runner.load_api_key = forbidden_key_load
        result = module.main(
            [
                "--article-dir",
                str(self.article),
                "--rights-manifest",
                str(rights),
                "--max-cost-rmb",
                "1",
            ]
        )
        self.assertEqual(result, 2)

    def test_style_projection_only_returns_without_loading_credentials(self) -> None:
        module = load_module()
        rights = Path(self.temporary.name) / "rights.json"
        rights.write_text(
            json.dumps([{"record_id": "arxiv:allowed", "publication_allowed": True}]),
            encoding="utf-8",
        )
        revision_output = module.runner.stage_output_path(
            self.article,
            "chunk0001",
            "output_chunk0001.md",
            "revision",
        )
        revision_output.parent.mkdir(parents=True, exist_ok=True)
        revision_output.write_text("修订段落。\n", encoding="utf-8")
        (self.article / "04-critique.md").write_text(
            "## Accuracy\n- chunk0001: 调整。\n\n## Native Voice\n- 无。\n\n"
            "## Notes & Adaptation\n- 无。\n\n## Summary\n- 完成。\n",
            encoding="utf-8",
        )
        (self.article / "chunk_status").mkdir(exist_ok=True)
        (self.article / "chunk_status/chunk0001.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "chunk_id": "chunk0001",
                    "source_file": "chunk0001.md",
                    "source_hash": hashlib.sha256("Original paragraph.\n".encode()).hexdigest(),
                    "stages": {
                        "revision": {
                            "status": "complete",
                            "output_file": revision_output.name,
                            "output_hash": module.runner.text_hash("修订段落。\n"),
                            "paper_context_scope": "chunk_local",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        buffer = io.StringIO()
        with (
            mock.patch.object(module.runner, "load_api_key", side_effect=AssertionError("must not load credentials")),
            mock.patch.object(module.runner, "DeepSeekClient", side_effect=AssertionError("must not create client")),
            mock.patch("sys.stdout", buffer),
        ):
            exit_code = module.main(
                [
                    "--article-dir",
                    str(self.article),
                    "--rights-manifest",
                    str(rights),
                    "--max-cost-rmb",
                    "1",
                    "--style-projection-only",
                ]
            )

        self.assertEqual(exit_code, 0)
        report = json.loads(buffer.getvalue())
        self.assertTrue(report["projection_ready"])
        self.assertEqual(report["record_id"], "arxiv:allowed")
        self.assertEqual(
            report["style_projection"]["planned"]["anti_ai"]["normal_requests"],
            1,
        )
        self.assertEqual(
            report["style_projection"]["planned"]["academic"]["normal_requests"],
            1,
        )
        self.assertEqual(report["projected_normal_api_calls"], 2)
        self.assertEqual(report["projected_worst_case_api_calls"], 4)

    def test_cli_rejects_stop_after_revision_with_style_projection_only(self) -> None:
        module = load_module()

        error = io.StringIO()
        with (
            mock.patch.object(
                module.runner,
                "load_api_key",
                side_effect=AssertionError("must not load credentials"),
            ),
            mock.patch.object(
                module.runner,
                "DeepSeekClient",
                side_effect=AssertionError("must not create client"),
            ),
            mock.patch("sys.stderr", error),
        ):
            with self.assertRaises(SystemExit) as raised:
                module.main(
                    [
                        "--article-dir",
                        str(self.article),
                        "--max-cost-rmb",
                        "1",
                        "--style-projection-only",
                        "--stop-after-revision",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn(
            "--stop-after-revision cannot be combined with --style-projection-only",
            error.getvalue(),
        )

    def test_style_projection_only_reports_missing_revision_chunks_without_guessing(self) -> None:
        module = load_module()
        rights = Path(self.temporary.name) / "rights.json"
        rights.write_text(
            json.dumps([{"record_id": "arxiv:allowed", "publication_allowed": True}]),
            encoding="utf-8",
        )

        buffer = io.StringIO()
        with (
            mock.patch.object(module.runner, "load_api_key", side_effect=AssertionError("must not load credentials")),
            mock.patch.object(module.runner, "DeepSeekClient", side_effect=AssertionError("must not create client")),
            mock.patch("sys.stdout", buffer),
        ):
            exit_code = module.main(
                [
                    "--article-dir",
                    str(self.article),
                    "--rights-manifest",
                    str(rights),
                    "--max-cost-rmb",
                    "1",
                    "--style-projection-only",
                ]
            )

        self.assertEqual(exit_code, 0)
        report = json.loads(buffer.getvalue())
        self.assertFalse(report["projection_ready"])
        self.assertEqual(report["missing_revision_chunk_ids"], ["chunk0001"])
        self.assertNotIn("projected_normal_api_calls", report)

    def test_style_projection_report_uses_conservative_academic_ceiling_when_anti_ai_is_pending(self) -> None:
        module = load_module()
        article = Path(self.temporary.name) / "projection-article"
        article.mkdir()
        chunks = []
        for index in range(1, 4):
            chunk_id = f"chunk{index:04d}"
            source_file = f"{chunk_id}.md"
            output_file = f"output_{chunk_id}.md"
            source_text = f"{index}-" + ("a" * 7_000) + "\n"
            (article / source_file).write_text(source_text, encoding="utf-8")
            revision_output = module.runner.stage_output_path(
                article,
                chunk_id,
                output_file,
                "revision",
            )
            revision_output.parent.mkdir(parents=True, exist_ok=True)
            revision_output.write_text(source_text, encoding="utf-8")
            chunks.append(
                {
                    "id": chunk_id,
                    "order": index,
                    "source_file": source_file,
                    "output_file": output_file,
                    "source_hash": module.runner.text_hash(source_text),
                }
            )
            stage_status = {
                "schema_version": 1,
                "record_id": "arxiv:allowed",
                "chunk_id": chunk_id,
                "source_file": source_file,
                "stages": {
                    "revision": {
                        "status": "complete",
                        "output_file": revision_output.name,
                        "output_hash": module.runner.text_hash(source_text),
                    }
                },
            }
            (article / "chunk_status").mkdir(exist_ok=True)
            (article / "chunk_status" / f"{chunk_id}.json").write_text(
                json.dumps(stage_status),
                encoding="utf-8",
            )
        (article / "manifest.json").write_text(
            json.dumps({"record_id": "arxiv:allowed", "chunks": chunks}),
            encoding="utf-8",
        )
        (article / "chunking_status.json").write_text(
            json.dumps({"record_id": "arxiv:allowed"}),
            encoding="utf-8",
        )
        (article / "04-critique.md").write_text("", encoding="utf-8")

        report = module.style_projection_report(article, terms=[])

        self.assertTrue(report["projection_ready"])
        academic = report["style_projection"]["planned"]["academic"]
        self.assertEqual(academic["semantics"], "conservative")
        self.assertEqual(academic["estimated_normal_requests"], 2)
        self.assertEqual(academic["estimated_worst_case_requests"], 4)
        self.assertEqual(academic["normal_requests"], 3)
        self.assertEqual(academic["worst_case_requests"], 6)
        self.assertEqual(report["projected_normal_api_calls"], 4)
        self.assertEqual(report["projected_worst_case_api_calls"], 10)
        self.assertEqual(report["launch_worst_case_api_calls"], 4)

    def test_style_stage_capacity_is_rechecked_against_live_remaining_calls(self) -> None:
        module = load_module()

        class Guard:
            def snapshot(self):
                return {"stage_remaining_api_calls": 3}

        module._require_style_request_capacity(
            Guard(), stage="anti_ai", worst_case_requests=3
        )
        with self.assertRaisesRegex(
            module.runner.BudgetExceededError,
            "academic style projection worst case.*4 > 3",
        ):
            module._require_style_request_capacity(
                Guard(), stage="academic", worst_case_requests=4
            )

    def test_chunk_barrier_retries_qc_failure_but_not_uncertain_request(self) -> None:
        module = load_module()
        chunk = {"id": "chunk0001", "source_file": "chunk0001.md"}
        calls: list[int] = []

        def flaky(_chunk, attempt):
            calls.append(attempt)
            if attempt == 0:
                raise RuntimeError("QC failed: numbers_mismatch")
            return {"status": "complete"}

        module._run_chunk_barrier(
            [chunk],
            concurrency=1,
            invoke=flaky,
            phase="draft",
            record_id="arxiv:allowed",
        )
        self.assertEqual(calls, [0, 1])

        uncertain_calls: list[int] = []

        def uncertain(_chunk, attempt):
            uncertain_calls.append(attempt)
            return {"status": "uncertain"}

        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            module._run_chunk_barrier(
                [chunk],
                concurrency=1,
                invoke=uncertain,
                phase="draft",
                record_id="arxiv:allowed",
            )
        self.assertEqual(uncertain_calls, [0])

    def test_chunk_barrier_preserves_budget_failure_type(self) -> None:
        module = load_module()
        chunk = {"id": "chunk0001", "source_file": "chunk0001.md"}

        def exhausted(_chunk, _attempt):
            raise module.runner.BudgetExceededError("request cap exhausted")

        with self.assertRaisesRegex(
            module.runner.BudgetExceededError,
            "request cap exhausted",
        ):
            module._run_chunk_barrier(
                [chunk],
                concurrency=1,
                invoke=exhausted,
                phase="revision",
                record_id="arxiv:allowed",
            )

    def test_revision_projection_reserves_one_qc_retry_for_chunk_model_work(self) -> None:
        module = load_module()
        with mock.patch.object(
            module,
            "_planned_stage_model_subrequests",
            wraps=module._planned_stage_model_subrequests,
        ):
            report = module.revision_ready_projection(self.article)

        chunk_calls = sum(
            report["missing_stage_api_calls"][name]
            for name in ("translate", "terminology", "revision")
        )
        paper_calls = sum(
            report["missing_stage_api_calls"][name]
            for name in ("analysis", "critique")
        )
        self.assertEqual(
            report["projected_worst_case_api_calls"],
            paper_calls + 2 * chunk_calls,
        )
        self.assertEqual(report["qc_retry_reserve_api_calls"], chunk_calls)

    def test_retry_uncertain_requires_recorded_budget_contract(self) -> None:
        module = load_module()
        status = {
            "record_id": "arxiv:allowed",
            "phases": {
                "analysis": {
                    "status": "uncertain",
                    "input_hash": module._paper_phase_input_hash(
                        "Analyze.",
                        "paper body",
                        4000,
                    ),
                }
            },
        }

        class BudgetGuard:
            def unresolved_uncertain_cost(self, _uncertainty_key: str) -> float:
                return 0.0

        class NoCallClient:
            def complete(self, *_args, **_kwargs):
                raise AssertionError("must stay blocked without replay contract")

        with self.assertRaisesRegex(module.runner.AmbiguousTransportError, "recorded budget contract"):
            module._run_paper_model_phase(
                phase_name="analysis",
                output_path=self.article / "01-analysis.md",
                instructions="Analyze.",
                input_text="paper body",
                max_output_tokens=4000,
                client=NoCallClient(),
                status=status,
                status_path=self.article / "paper_status.json",
                run_id="run-one",
                budget_guard=BudgetGuard(),
                retry_uncertain=True,
            )

    def test_chunk_barrier_allows_only_one_paid_qc_correction_retry(self) -> None:
        module = load_module()
        chunk = {"id": "chunk0001", "source_file": "chunk0001.md"}
        calls: list[int] = []

        def repeatedly_flaky(_chunk, attempt):
            calls.append(attempt)
            if attempt < 1:
                raise RuntimeError("transient structure protocol failure")
            return {"status": "complete"}

        module._run_chunk_barrier(
            [chunk],
            concurrency=1,
            invoke=repeatedly_flaky,
            phase="draft",
            record_id="arxiv:allowed",
        )

        self.assertEqual(calls, [0, 1])

    def test_chunk_barrier_still_fails_closed_after_bounded_retries(self) -> None:
        module = load_module()
        chunk = {"id": "chunk0001", "source_file": "chunk0001.md"}
        calls: list[int] = []

        def always_fails(_chunk, attempt):
            calls.append(attempt)
            raise RuntimeError("persistent structure protocol failure")

        with self.assertRaisesRegex(RuntimeError, "persistent structure protocol failure"):
            module._run_chunk_barrier(
                [chunk],
                concurrency=1,
                invoke=always_fails,
                phase="draft",
                record_id="arxiv:allowed",
            )

        self.assertEqual(calls, [0, 1])

    def test_chunk_barrier_emits_bounded_progress_heartbeats(self) -> None:
        module = load_module()
        chunks = [{"id": f"chunk{index:04d}"} for index in range(1, 6)]
        events: list[dict[str, object]] = []

        module._run_chunk_barrier(
            chunks,
            concurrency=2,
            invoke=lambda _chunk, _attempt: {"status": "complete"},
            phase="revision",
            record_id="arxiv:allowed",
            progress_callback=events.append,
            heartbeat_every=2,
        )

        self.assertEqual([event["completed"] for event in events], [2, 4, 5])
        self.assertTrue(all(event["total"] == 5 for event in events))
        self.assertTrue(all(event["phase"] == "revision" for event in events))
        self.assertTrue(all(event["attempt"] == 0 for event in events))

    def test_retry_context_reports_literal_parenthesis_and_term_differences(self) -> None:
        module = load_module()
        chunk = {"id": "chunk0001", "source_file": "chunk0001.md"}
        source = self.article / "chunk0001.md"
        source.write_text(
            "Cyclotron result (1 TeV) remains valid.\n",
            encoding="utf-8",
        )
        rejected = self.article / "rejected.md"
        rejected.write_text(
            "Cyclotron 结果（2 TeV 仍然有效。\n",
            encoding="utf-8",
        )
        status_dir = self.article / "chunk_status"
        status_dir.mkdir(parents=True, exist_ok=True)
        (status_dir / "chunk0001.json").write_text(
            json.dumps(
                {
                    "stages": {
                        "translate": {
                            "status": "failed",
                            "error": (
                                "QC failed: numbers_mismatch, units_mismatch, "
                                "parentheses_mismatch, locked_terms_mismatch"
                            ),
                            "rejected_candidate_file": "rejected.md",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        context = module._qc_retry_context(
            self.article,
            chunk,
            "WHOLE PAPER ANALYSIS 999",
            1,
            terms=[{"source": "Cyclotron", "target": "回旋加速器"}],
        )

        self.assertNotIn("WHOLE PAPER ANALYSIS", context)
        self.assertIn("missing numeric literals: 1", context)
        self.assertIn("added numeric literals: 2", context)
        self.assertIn("source unit literals: 1TeV", context)
        self.assertIn("source parenthesis residue: open=0, closing=0", context)
        self.assertIn("required locked terms: Cyclotron => 回旋加速器", context)


if __name__ == "__main__":
    unittest.main()
