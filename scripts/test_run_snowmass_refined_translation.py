#!/usr/bin/env python3
"""Tests for genuine paper-level refined Snowmass orchestration."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("run_snowmass_refined_translation.py")


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("Refined Snowmass orchestrator is not implemented")
    spec = importlib.util.spec_from_file_location("run_snowmass_refined_translation", MODULE_PATH)
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

    def test_refined_artifacts_causally_gate_final_translation_and_resume(self) -> None:
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
                    return completed_response("原文自然段落。", "anti-ai"), 0.1
                if "final Chinese naturalization" in instructions:
                    return completed_response("原文最终学术段落。", "academic"), 0.1
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

        resumed = module.run_refined_article(
            self.article,
            client=NoCallClient(),
            terms=[{"source": "Original", "target": "原文"}],
            run_id="run-two",
        )
        self.assertEqual(resumed["status"], "complete")
        status = json.loads((self.article / "paper_status.json").read_text(encoding="utf-8"))
        self.assertTrue(all(item["status"] == "complete" for item in status["phases"].values()))

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

    def test_chunk_barrier_retries_qc_failure_but_not_uncertain_request(self) -> None:
        module = load_module()
        chunk = {"id": "chunk0001"}
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


if __name__ == "__main__":
    unittest.main()
