#!/usr/bin/env python3
"""Regression tests for the rights gate in the Snowmass translator."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import ssl
import tempfile
import unittest
from unittest import mock
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run_snowmass_translation.py")
SPEC = importlib.util.spec_from_file_location("run_snowmass_translation", MODULE_PATH)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def completed_response(text: str = "译文\n", *, model: str = RUNNER.MODEL) -> dict[str, object]:
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


class RightsGateTests(unittest.TestCase):
    def test_loader_accepts_only_explicit_publication_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / "papers.json"
            manifest_path.write_text(
                json.dumps(
                    [
                        {"record_id": "arxiv:allowed", "publication_allowed": True},
                        {"record_id": "arxiv:blocked", "publication_allowed": False},
                        {"record_id": "arxiv:unknown", "publication_allowed": None},
                        {"record_id": "arxiv:missing"},
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                RUNNER.load_allowed_record_ids(manifest_path),
                {"arxiv:allowed"},
            )

    def test_task_collection_excludes_records_outside_the_rights_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            papers = root / "papers"
            for slug, record_id in (
                ("arxiv_allowed", "arxiv:allowed"),
                ("arxiv_blocked", "arxiv:blocked"),
            ):
                article = papers / slug
                article.mkdir(parents=True)
                (article / "manifest.json").write_text(
                    json.dumps(
                        {
                            "babeldoc_ir_json_file": "babeldoc_ir.json",
                            "chunks": [
                                {
                                    "id": "chunk0001",
                                    "order": 1,
                                    "source_file": "chunk0001.md",
                                    "output_file": "output_chunk0001.md",
                                    "page_number": 1,
                                    "paragraph_index": 0,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                (article / "babeldoc_ir.json").write_text(
                    json.dumps(
                        {
                            "page": [
                                {
                                    "pdf_paragraph": [
                                        {"xobj_id": 3 if record_id == "arxiv:allowed" else 0}
                                    ]
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                (article / "chunking_status.json").write_text(
                    json.dumps({"record_id": record_id}),
                    encoding="utf-8",
                )

            tasks = RUNNER.collect_tasks(
                root,
                max_articles=0,
                max_chunks=0,
                article_filter=None,
                allowed_record_ids={"arxiv:allowed"},
            )

            self.assertEqual(
                [(task["record_id"], task["chunk"]["id"]) for task in tasks],
                [("arxiv:allowed", "chunk0001")],
            )
            self.assertTrue(tasks[0]["passthrough"])
            self.assertEqual(
                tasks[0]["passthrough_reason"], "figure_internal_text_passthrough"
            )

    def test_explicit_glossary_path_overrides_root_default(self) -> None:
        root = Path("translation-root")
        explicit = Path("locked-terms.json")

        self.assertEqual(RUNNER.resolve_glossary_path(root, explicit), explicit)
        self.assertEqual(
            RUNNER.resolve_glossary_path(root, None),
            RUNNER.TRACKED_GLOBAL_GLOSSARY,
        )

    def test_root_glossary_overrides_tracked_fallback_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "global_glossary.json"
            local.write_text('{"terms": []}\n', encoding="utf-8")

            self.assertEqual(RUNNER.resolve_glossary_path(root, None), local)

    def test_main_loads_external_glossary_for_production_root_and_records_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "production"
            article = root / "papers" / "arxiv_allowed"
            article.mkdir(parents=True)
            (article / "chunk0001.md").write_text("source\n", encoding="utf-8")
            (article / "manifest.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {
                                "id": "chunk0001",
                                "order": 1,
                                "source_file": "chunk0001.md",
                                "output_file": "output_chunk0001.md",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (article / "chunking_status.json").write_text(
                json.dumps({"record_id": "arxiv:allowed"}), encoding="utf-8"
            )
            rights = base / "papers.json"
            rights.write_text(
                json.dumps([{"record_id": "arxiv:allowed", "publication_allowed": True}]),
                encoding="utf-8",
            )
            glossary = base / "locked.json"
            expected_terms = [{"source": "dark matter", "target": "暗物质"}]
            glossary.write_text(json.dumps({"terms": expected_terms}), encoding="utf-8")
            observed_terms: list[list[dict[str, str]]] = []

            def fake_process(
                task: dict[str, object],
                client: object,
                terms: list[dict[str, str]],
                run_id: str,
                budget_guard: object,
            ) -> dict[str, str]:
                observed_terms.append(terms)
                return {"record_id": "arxiv:allowed", "chunk_id": "chunk0001", "status": "complete"}

            with (
                mock.patch.object(RUNNER, "load_api_key", return_value="test-key"),
                mock.patch.object(RUNNER, "DeepSeekClient", return_value=object()),
                mock.patch.object(RUNNER, "process_chunk", side_effect=fake_process),
                mock.patch.object(RUNNER, "finalize_run_states"),
            ):
                exit_code = RUNNER.main(
                    [
                        "--root", str(root),
                        "--rights-manifest", str(rights),
                        "--glossary", str(glossary),
                        "--concurrency", "1",
                        "--max-cost-rmb", "1",
                    ]
                )

            summary = json.loads((root / "translation_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(observed_terms, [expected_terms])
            self.assertEqual(summary["glossary"], str(glossary))


class NeighborContextTests(unittest.TestCase):
    def test_reuses_translate_book_neighbor_context_for_middle_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "chunk0001.md").write_text("Previous evidence.\n", encoding="utf-8")
            (article / "chunk0002.md").write_text("Current claim.\n", encoding="utf-8")
            (article / "chunk0003.md").write_text("Next qualification.\n", encoding="utf-8")

            self.assertTrue(
                hasattr(RUNNER, "load_neighbor_context"),
                "translate-book neighbor context is not integrated",
            )
            context = RUNNER.load_neighbor_context(article, "chunk0002.md")

            self.assertIn("Previous chunk excerpt (chunk0001.md, read-only)", context)
            self.assertIn("Previous evidence.", context)
            self.assertIn("Next chunk excerpt (chunk0003.md, read-only)", context)
            self.assertIn("Next qualification.", context)


class UsageAccountingTests(unittest.TestCase):
    def test_cost_uses_official_usd_v4_flash_rates_and_pinned_exchange_rate(self) -> None:
        usage = {"input_tokens": 1_000_000, "cached_tokens": 250_000, "output_tokens": 500_000}

        cost_usd = RUNNER.estimate_cost_usd(usage)
        cost_rmb = RUNNER.estimate_cost_rmb(usage, usd_cny_rate=7.2)

        self.assertAlmostEqual(cost_usd, 0.2457)
        self.assertAlmostEqual(cost_rmb, 1.76904)

    def test_cost_rejects_invalid_exchange_rate(self) -> None:
        with self.assertRaises(ValueError):
            RUNNER.estimate_cost_rmb(
                {"input_tokens": 1_000_000, "cached_tokens": 250_000, "output_tokens": 500_000},
                usd_cny_rate=0,
            )

    def test_collect_run_usage_aggregates_completed_stage_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary) / "papers" / "paper"
            status_dir = article / "chunk_status"
            status_dir.mkdir(parents=True)
            (status_dir / "chunk0001.json").write_text(
                json.dumps(
                    {
                        "stages": {
                            "translate": {
                                "status": "complete",
                                "run_id": "run-current",
                                "usage": {"input_tokens": 100, "cached_tokens": 20, "output_tokens": 50},
                            },
                            "terminology": {
                                "status": "complete",
                                "run_id": "run-previous",
                                "usage": {"input_tokens": 999},
                            },
                            "academic": {
                                "status": "failed",
                                "run_id": "run-current",
                                "usage": {"input_tokens": 10, "cached_tokens": 0, "output_tokens": 1},
                            },
                            "anti_ai": {"status": "complete"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            task = {"article_dir": article, "chunk": {"id": "chunk0001"}}

            usage = RUNNER.collect_run_usage([task, task], "run-current")

            self.assertEqual(usage["api_calls"], 2)
            self.assertEqual(usage["input_tokens"], 110)
            self.assertEqual(usage["cached_tokens"], 20)
            self.assertEqual(usage["output_tokens"], 51)
            self.assertAlmostEqual(usage["estimated_cost_rmb"], RUNNER.estimate_cost_rmb(usage))

            resumed_usage = RUNNER.collect_run_usage([task], "checkpoint-only-rerun")
            self.assertEqual(resumed_usage["api_calls"], 0)
            self.assertEqual(resumed_usage["estimated_cost_rmb"], 0)

    def test_summary_result_drops_internal_path_objects(self) -> None:
        result = {
            "record_id": "arxiv:allowed",
            "chunk_id": "chunk0001",
            "status": "uncertain",
            "article_dir": Path("papers/arxiv_allowed"),
        }

        public = RUNNER.summary_result(result)

        self.assertNotIn("article_dir", public)
        self.assertEqual(public["status"], "uncertain")
        json.dumps(public)


class GlossaryMergeTests(unittest.TestCase):
    def test_article_terms_override_global_terms_by_casefolded_source(self) -> None:
        global_terms = [
            {"source": "Light Relics", "target": "全局译名"},
            {"source": "dark matter", "target": "暗物质"},
        ]
        article_terms = [
            {"source": "light relics", "target": "轻遗迹粒子", "note": "paper-specific"},
            {"source": "spectral index running", "target": "谱指数的跑动"},
        ]

        merged = RUNNER.merge_glossary_terms(global_terms, article_terms)

        self.assertEqual(
            merged,
            [
                {"source": "light relics", "target": "轻遗迹粒子", "note": "paper-specific"},
                {"source": "dark matter", "target": "暗物质"},
                {"source": "spectral index running", "target": "谱指数的跑动"},
            ],
        )

    def test_load_article_glossary_returns_empty_when_file_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(RUNNER.load_article_glossary(Path(temporary)), [])

    def test_select_glossary_terms_keeps_only_terms_present_in_source_or_aliases(self) -> None:
        terms = [
            {"source": "dark matter", "target": "暗物质"},
            {"source": "cosmic microwave background", "aliases": ["CMB"], "target": "宇宙微波背景"},
            {"source": "light relic", "target": "轻遗迹粒子"},
        ]

        selected = RUNNER.select_glossary_terms(
            "CMB lensing constrains light relic particles.",
            terms,
        )

        self.assertEqual(selected, terms[1:])


class BudgetGuardTests(unittest.TestCase):
    def test_zero_budget_is_rejected_instead_of_meaning_unlimited(self) -> None:
        with self.assertRaises(ValueError):
            RUNNER.BudgetGuard(0.0)

    def test_reservation_settles_to_reported_v4_flash_cost(self) -> None:
        guard = RUNNER.BudgetGuard(1.0, usd_cny_rate=7.2)

        reservation = guard.reserve("source text", 4096)
        guard.settle(
            reservation,
            {"input_tokens": 1000, "cached_tokens": 200, "output_tokens": 500},
        )

        snapshot = guard.snapshot()
        self.assertEqual(snapshot["active_reservations"], 0)
        self.assertEqual(snapshot["usd_cny_rate"], 7.2)
        self.assertAlmostEqual(
            snapshot["spent_rmb"],
            RUNNER.estimate_cost_rmb(
                {"input_tokens": 1000, "cached_tokens": 200, "output_tokens": 500},
                usd_cny_rate=7.2,
            ),
        )

    def test_reservation_rejects_request_that_would_exceed_cap(self) -> None:
        guard = RUNNER.BudgetGuard(0.001)

        with self.assertRaises(RUNNER.BudgetExceededError):
            guard.reserve("large request", 4096)

        self.assertEqual(guard.snapshot()["active_reservations"], 0)

    def test_ambiguous_call_commits_conservative_reservation(self) -> None:
        guard = RUNNER.BudgetGuard(1.0)
        reservation = guard.reserve("source text", 4096)

        guard.commit_estimate(reservation)

        snapshot = guard.snapshot()
        self.assertEqual(snapshot["active_reservations"], 0)
        self.assertGreater(snapshot["spent_rmb"], 0)


class RunHistoryTests(unittest.TestCase):
    def test_write_run_summary_preserves_immutable_per_run_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = {"run_id": "run-one", "completed": 1}
            second = {"run_id": "run-two", "completed": 2}

            RUNNER.write_run_summary(root, first)
            RUNNER.write_run_summary(root, second)

            self.assertEqual(
                json.loads((root / "runs" / "run-one" / "run.json").read_text(encoding="utf-8")),
                first,
            )
            self.assertEqual(
                json.loads((root / "runs" / "run-two" / "run.json").read_text(encoding="utf-8")),
                second,
            )
            self.assertEqual(
                json.loads((root / "translation_summary.json").read_text(encoding="utf-8")),
                second,
            )

    def test_write_run_summary_refuses_to_mutate_existing_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            RUNNER.write_run_summary(root, {"run_id": "run-one", "completed": 1})

            with self.assertRaises(RuntimeError):
                RUNNER.write_run_summary(root, {"run_id": "run-one", "completed": 2})


class ResponseValidationTests(unittest.TestCase):
    def test_validate_response_accepts_completed_response(self) -> None:
        parsed = RUNNER.validate_response(completed_response("最终译文"), RUNNER.MODEL)

        self.assertEqual(parsed.text, "最终译文\n")
        self.assertEqual(parsed.response_id, "resp_123")
        self.assertEqual(parsed.model, RUNNER.MODEL)
        self.assertEqual(parsed.status, "completed")
        self.assertEqual(parsed.output_hash, hashlib.sha256("最终译文\n".encode("utf-8")).hexdigest())
        self.assertEqual(parsed.usage["total_tokens"], 30)

    def test_incomplete_response_is_not_accepted(self) -> None:
        with self.assertRaises(RUNNER.IncompleteResponseError):
            RUNNER.validate_response(
                {
                    "id": "resp_123",
                    "status": "incomplete",
                    "model": RUNNER.MODEL,
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
                RUNNER.MODEL,
            )

    def test_failed_response_is_not_accepted(self) -> None:
        with self.assertRaises(RUNNER.FailedResponseError):
            RUNNER.validate_response(
                {
                    "id": "resp_123",
                    "status": "failed",
                    "model": RUNNER.MODEL,
                    "error": {"message": "backend rejected the prompt"},
                },
                RUNNER.MODEL,
            )

    def test_wrong_model_is_not_accepted(self) -> None:
        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(completed_response(model="deepseek-chat"), RUNNER.MODEL)

    def test_completed_response_without_output_is_not_accepted(self) -> None:
        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(
                {
                    "id": "resp_123",
                    "status": "completed",
                    "model": RUNNER.MODEL,
                    "output": [],
                    "usage": {"total_tokens": 0},
                },
                RUNNER.MODEL,
            )

    def test_completed_response_with_malformed_output_is_not_accepted(self) -> None:
        response = completed_response()
        response["output"] = {"type": "message"}

        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(response, RUNNER.MODEL)

    def test_completed_response_with_malformed_content_is_not_accepted(self) -> None:
        response = completed_response()
        response["output"] = [{"type": "message", "content": ["not-an-object"]}]

        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(response, RUNNER.MODEL)

    def test_malformed_input_token_details_are_not_accepted(self) -> None:
        response = completed_response()
        response["usage"]["input_tokens_details"] = ["not-an-object"]

        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(response, RUNNER.MODEL)

    def test_malformed_output_token_details_are_not_accepted(self) -> None:
        response = completed_response()
        response["usage"]["output_tokens_details"] = "not-an-object"

        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(response, RUNNER.MODEL)


class RequestKeyAndCheckpointTests(unittest.TestCase):
    def test_article_artifact_path_rejects_absolute_and_parent_escape(self) -> None:
        article = Path("/tmp/article")
        for value in ("../other.md", "/tmp/other.md", "nested/../../other.md"):
            with self.subTest(value=value), self.assertRaises(RUNNER.UnsafeArticlePathError):
                RUNNER.article_artifact_path(article, value)

    def test_request_key_is_deterministic_for_same_payload(self) -> None:
        first = RUNNER.request_key(
            stage="translate",
            model=RUNNER.MODEL,
            instructions="translate carefully",
            input_text="source chunk",
            max_output_tokens=4096,
        )
        second = RUNNER.request_key(
            stage="translate",
            model=RUNNER.MODEL,
            instructions="translate carefully",
            input_text="source chunk",
            max_output_tokens=4096,
        )
        other = RUNNER.request_key(
            stage="translate",
            model=RUNNER.MODEL,
            instructions="translate carefully",
            input_text="source chunk",
            max_output_tokens=4097,
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_nonempty_stale_output_is_not_a_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "stage1_chunk0001.md"
            output.write_text("旧结果\n", encoding="utf-8")
            status = {
                "status": "complete",
                "request_key": "old-key",
                "output_hash": hashlib.sha256("旧结果\n".encode("utf-8")).hexdigest(),
            }

            self.assertFalse(RUNNER.checkpoint_is_valid(status, output, "new-key"))

    def test_output_hash_mismatch_is_not_a_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "stage1_chunk0001.md"
            output.write_text("当前结果\n", encoding="utf-8")
            status = {
                "status": "complete",
                "request_key": "expected-key",
                "output_hash": hashlib.sha256("旧结果\n".encode("utf-8")).hexdigest(),
            }

            self.assertFalse(RUNNER.checkpoint_is_valid(status, output, "expected-key"))

    def test_matching_request_key_hash_and_passing_qc_form_a_valid_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "stage1_chunk0001.md"
            output.write_text("当前结果\n", encoding="utf-8")
            status = {
                "status": "complete",
                "request_key": "expected-key",
                "output_hash": hashlib.sha256("当前结果\n".encode("utf-8")).hexdigest(),
                "qc": {"ok": True, "failures": []},
            }

            self.assertTrue(RUNNER.checkpoint_is_valid(status, output, "expected-key"))

    def test_matching_legacy_checkpoint_without_qc_is_not_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "stage1_chunk0001.md"
            output.write_text("旧版本结果\n", encoding="utf-8")
            status = {
                "status": "complete",
                "request_key": "expected-key",
                "output_hash": hashlib.sha256("旧版本结果\n".encode("utf-8")).hexdigest(),
            }

            self.assertFalse(RUNNER.checkpoint_is_valid(status, output, "expected-key"))

    def test_completed_qc_valid_checkpoint_survives_prompt_contract_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "stage1_chunk0001.md"
            output.write_text("既有有效译文。\n", encoding="utf-8")
            status = {
                "status": "complete",
                "request_key": "old-prompt-key",
                "output_hash": hashlib.sha256(
                    "既有有效译文。\n".encode("utf-8")
                ).hexdigest(),
                "qc": {"ok": True, "failures": []},
            }

            self.assertTrue(
                RUNNER.checkpoint_is_reusable_after_contract_change(status, output)
            )


class ProcessChunkTests(unittest.TestCase):
    def test_refinement_input_does_not_expose_original_literals_or_unrelated_text(self) -> None:
        source = (
            "Submitted for Snowmass 2021. "
            "A later sentence contains 2800 detectors."
        )
        current = (
            '{"protocol":"snowmass-anchor-template-v1",'
            '"source_template":"提交至 Snowmass <ANCHOR_0000>。"}'
        )

        request = RUNNER.stage_input("revision", source, current, "")

        self.assertNotIn("2021", request)
        self.assertNotIn("2800", request)
        self.assertNotIn("A later sentence", request)
        self.assertIn(current, request)

    def test_plain_refinement_input_retains_original_source_context(self) -> None:
        request = RUNNER.stage_input(
            "revision",
            "Original source needed for fidelity.",
            "当前译文。",
            "",
        )

        self.assertIn("Original source needed for fidelity.", request)

    def test_structure_slot_requests_enable_official_json_output_mode(self) -> None:
        structured = RUNNER.build_request_payload(
            "Return JSON. STRUCTURE-SLOT PROTOCOL", "{}", 128
        )
        fallback = RUNNER.build_request_payload(
            "Return JSON. STRUCTURE-ANCHOR FALLBACK PROTOCOL", "{}", 128
        )
        plain = RUNNER.build_request_payload("Translate", "text", 128)

        self.assertEqual(structured["text"]["format"], {"type": "json_object"})
        self.assertEqual(fallback["text"]["format"], {"type": "json_object"})
        self.assertEqual(plain["text"]["format"], {"type": "text"})

    def test_structure_dense_input_splits_losslessly_at_bounded_density(self) -> None:
        module = RUNNER
        protected = " ".join(
            f"part{i} [[SM_{i:04d}_{i:010x}]]" for i in range(66)
        )

        segments = module.split_protected_model_input(protected, 4)

        self.assertEqual("".join(segments), protected)
        self.assertEqual(len(segments), 17)
        self.assertTrue(
            all(len(module._MODEL_SENTINEL_RE.findall(item)) <= 4 for item in segments)
        )

    def test_structure_only_segments_are_copied_without_model_calls(self) -> None:
        source = "$a$ $b$ $c$ $d$ $e$\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("structure-only segments must not call the model")

        result = RUNNER.process_chunk(
            self.task,
            NoCallClient(),
            [],
            stages=("translate",),
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            (self.article_dir / "stage1_chunk0001.md").read_text(encoding="utf-8"),
            source,
        )

    def test_structure_slots_never_expose_anchors_and_restore_deterministically(self) -> None:
        source = "Before [[SM_0001_0000000001]] middle [[SMU_0002_NUMBER_0000000002]] after"

        payload, anchors, parts = RUNNER.build_structure_slot_input(source)

        self.assertNotIn("[[SM_", payload)
        request = json.loads(payload)
        self.assertIn("<ANCHOR_0000>", request["source_context"])
        response = json.dumps(
            {
                "translations": {
                    item["id"]: f"译:{item['text']}" for item in request["slots"]
                }
            },
            ensure_ascii=False,
        )
        restored = RUNNER.restore_structure_slot_output(response, anchors, parts)
        self.assertEqual(RUNNER._MODEL_SENTINEL_RE.findall(restored), list(anchors))
        self.assertIn("译:Before ", restored)

    def test_structure_slots_reject_missing_text_identity(self) -> None:
        payload, anchors, parts = RUNNER.build_structure_slot_input(
            "Before [[SM_0001_0000000001]] after"
        )
        self.assertTrue(json.loads(payload)["slots"])

        with self.assertRaises(RUNNER.StructureMismatchError):
            RUNNER.restore_structure_slot_output(
                '{"translations":{"T0000":"译文"}}', anchors, parts
            )

    def test_structure_slots_accept_only_extra_closing_brace_suffix(self) -> None:
        _, anchors, parts = RUNNER.build_structure_slot_input(
            "Before [[SM_0001_0000000001]] after"
        )
        response = '{"translations":{"T0000":"前","T0001":"后"}}}'

        restored = RUNNER.restore_structure_slot_output(response, anchors, parts)

        self.assertEqual(
            RUNNER._MODEL_SENTINEL_RE.findall(restored), list(anchors)
        )

    def test_structure_slots_reject_suspiciously_truncated_text_island(self) -> None:
        _, anchors, parts = RUNNER.build_structure_slot_input(
            "Constraints on neutrino masses and light relics "
            "[[SM_0001_0000000001]] are discussed here"
        )
        response = json.dumps(
            {"translations": {"T0000": "约束", "T0001": "在此讨论"}},
            ensure_ascii=False,
        )

        with self.assertRaisesRegex(
            RUNNER.StructureMismatchError, "suspiciously short"
        ):
            RUNNER.restore_structure_slot_output(response, anchors, parts)

    def test_anchor_fallback_hides_real_nodes_and_allows_syntax_repositioning(self) -> None:
        source = "given parameters [[SM_0001_0000000001]], calculate the matrix"
        payload, anchors, markers = RUNNER.build_structure_anchor_input(source)
        self.assertNotIn("[[SM_", payload)
        response = json.dumps(
            {"translation": f"在给定参数{markers[0]}的情况下，计算矩阵"},
            ensure_ascii=False,
        )

        restored = RUNNER.restore_structure_anchor_output(response, anchors, markers)

        self.assertEqual(
            restored, "在给定参数[[SM_0001_0000000001]]的情况下，计算矩阵"
        )

    def test_anchor_fallback_allows_a_valid_anchor_permutation(self) -> None:
        source = (
            "bin [[SM_0001_0000000001]] at redshift "
            "[[SM_0002_0000000002]]"
        )
        _, anchors, markers = RUNNER.build_structure_anchor_input(source)
        response = json.dumps(
            {"translation": f"红移{markers[1]}处的区间{markers[0]}"},
            ensure_ascii=False,
        )

        restored = RUNNER.restore_structure_anchor_output(response, anchors, markers)

        self.assertEqual(
            restored,
            "红移[[SM_0002_0000000002]]处的区间[[SM_0001_0000000001]]",
        )

    def test_failed_anchor_fallback_returns_to_strict_slot_protocol(self) -> None:
        correction_retry = "# QC-CORRECTION RETRY 2\nPreserve every protected value."

        self.assertTrue(
            RUNNER.should_use_structure_anchor_fallback(
                "revision",
                {"error": "Structure-slot response is suspiciously short"},
                correction_retry,
            )
        )
        self.assertFalse(
            RUNNER.should_use_structure_anchor_fallback(
                "revision",
                {"error": "Anchor-template response changed anchor identity or count"},
                correction_retry,
            )
        )
        self.assertFalse(
            RUNNER.should_use_structure_anchor_fallback(
                "translate",
                {"error": "Structure-slot response is suspiciously short"},
                correction_retry,
            )
        )

    def test_translate_uses_one_anchor_retry_for_invalid_slot_value(self) -> None:
        self.assertTrue(
            RUNNER.should_use_structure_anchor_fallback(
                "translate",
                {"error": "Invalid structure-slot value for T0001"},
                "",
            )
        )

    def test_structure_failure_retries_with_one_anchor_per_segment(self) -> None:
        self.assertEqual(RUNNER.structure_segment_limit({}), 4)
        self.assertEqual(
            RUNNER.structure_segment_limit(
                {"error": "Anchor-template response changed anchor identity or count"}
            ),
            1,
        )

    def test_english_month_names_do_not_invent_arabic_month_numbers(self) -> None:
        source = "For reference, the January 2021 capacity was 558 PB."
        candidate = "作为参考，1月份2021时的容量为558 PB。"

        normalized = RUNNER.normalize_source_month_names(source, candidate)

        self.assertEqual(normalized, "作为参考，一月2021时的容量为558 PB。")
        self.assertTrue(RUNNER.validate_chunk(source, normalized, {}, []).ok)

    def test_month_normalization_preserves_a_numeric_day_from_source(self) -> None:
        source = "On January 1, 2021, the run started."
        candidate = "运行于1月1日2021开始。"

        normalized = RUNNER.normalize_source_month_names(source, candidate)

        self.assertEqual(normalized, "运行于一月1日2021开始。")
        self.assertTrue(RUNNER.validate_chunk(source, normalized, {}, []).ok)

    def test_month_normalization_folds_duplicate_protected_year(self) -> None:
        source = "In January 2020, the council met."
        candidate = "2020年1月2020，理事会召开会议。"

        normalized = RUNNER.normalize_source_month_names(source, candidate)

        self.assertEqual(normalized, "2020年一月，理事会召开会议。")
        self.assertTrue(RUNNER.validate_chunk(source, normalized, {}, []).ok)

    def test_source_month_years_are_localized_before_model_protection(self) -> None:
        source = "In January 2020 and June 2021, the council met."

        localized = RUNNER.localize_source_month_years(source)

        self.assertEqual(localized, "In 2020年一月 and 2021年六月, the council met.")
        protected, _mapping, nodes = RUNNER.protect_stage_text(localized)
        self.assertNotIn("January", protected)
        self.assertNotIn("June", protected)
        self.assertEqual([node.value for node in nodes], ["2020", "2021"])

    def test_academic_polish_uses_anchor_protocol_for_chinese_word_order(self) -> None:
        source = "参数 $x$ 与 $y$。\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")
        initial = self.article_dir / "stage3_chunk0001.md"
        initial.write_text(source, encoding="utf-8")
        observed_instructions: list[str] = []

        class AnchorClient:
            def complete(
                self, instructions: str, input_text: str, max_output_tokens: int
            ) -> tuple[dict[str, object], float]:
                observed_instructions.append(instructions)
                markers = re.findall(r"<ANCHOR_[0-9]{4}>", input_text)
                return completed_response(
                    json.dumps(
                        {"translation": f"参数{markers[1]}与{markers[0]}。"},
                        ensure_ascii=False,
                    )
                ), 0.1

        result = RUNNER.process_chunk(
            self.task,
            AnchorClient(),
            [],
            stages=("academic",),
            initial_text_path=initial,
        )

        self.assertEqual(result["status"], "complete")
        self.assertIn("STRUCTURE-ANCHOR FALLBACK PROTOCOL", observed_instructions[0])
        self.assertNotIn("STRUCTURE-SLOT PROTOCOL", observed_instructions[0])

    def test_failed_style_stage_falls_back_to_qc_valid_prior_text_without_api(self) -> None:
        source = "PUMA has 50% occupancy.\n"
        prior = "PUMA的占用率为50%。\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")
        initial = self.article_dir / "stage3_chunk0001.md"
        initial.write_text(prior, encoding="utf-8")
        status_dir = self.article_dir / "chunk_status"
        status_dir.mkdir()
        (status_dir / "chunk0001.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "chunk_id": "chunk0001",
                    "stages": {
                        "academic": {
                            "status": "failed",
                            "error": "Structure-slot response changed text-slot identities",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("failed optional style stage must use the valid prior text")

        result = RUNNER.process_chunk(
            self.task,
            NoCallClient(),
            [],
            stages=("academic",),
            initial_text_path=initial,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            (self.article_dir / "output_chunk0001.md").read_text(encoding="utf-8"),
            prior,
        )
        status = json.loads((status_dir / "chunk0001.json").read_text(encoding="utf-8"))
        self.assertEqual(status["stages"]["academic"]["status"], "complete")
        self.assertTrue(status["stages"]["academic"]["fallback_to_prior_stage"])

    def test_style_candidate_qc_failure_falls_back_in_same_run(self) -> None:
        source = "PUMA has 50% occupancy.\n"
        prior = "PUMA的占用率为50%。\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")
        initial = self.article_dir / "stage3_chunk0001.md"
        initial.write_text(prior, encoding="utf-8")

        class BadCandidateClient:
            def complete(
                self, instructions: str, input_text: str, max_output_tokens: int
            ) -> tuple[dict[str, object], float]:
                return completed_response("PUMA的占用率为60%。\n"), 0.1

        result = RUNNER.process_chunk(
            self.task,
            BadCandidateClient(),
            [],
            stages=("academic",),
            initial_text_path=initial,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            (self.article_dir / "output_chunk0001.md").read_text(encoding="utf-8"),
            prior,
        )

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
        (self.article_dir / "chunk0001.md").write_text("Original source paragraph.\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_process_chunk_persists_request_key_and_output_hash_for_completed_stage(self) -> None:
        class FakeClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                return completed_response("阶段产物"), 0.75

        result = RUNNER.process_chunk(self.task, FakeClient(), [])
        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]

        self.assertEqual(result["status"], "complete")
        self.assertEqual(translate["status"], "complete")
        self.assertTrue(translate["request_key"])
        self.assertEqual(translate["output_hash"], hashlib.sha256("阶段产物\n".encode("utf-8")).hexdigest())
        self.assertEqual(translate["raw_response"]["status"], "completed")

    def test_refinement_request_does_not_attach_neighbor_source_context(self) -> None:
        (self.article_dir / "chunk0002.md").write_text(
            "Neighbor-only evidence contains 999 detectors.\n", encoding="utf-8"
        )
        initial = self.article_dir / "stage2_chunk0001.md"
        self.task["chunk"]["source_hash"] = "source-hash-with-number"
        (self.article_dir / "chunk0001.md").write_text(
            "Original source paragraph 2021.\n", encoding="utf-8"
        )
        initial.write_text("当前译文 2021。\n", encoding="utf-8")
        observed_inputs: list[str] = []

        class FakeClient:
            def complete(
                self, instructions: str, input_text: str, max_output_tokens: int
            ) -> tuple[dict[str, object], float]:
                observed_inputs.append(input_text)
                marker = re.search(r"<ANCHOR_[0-9]{4}>", input_text)
                if marker is None:
                    raise AssertionError("anchor fallback request omitted its abstract marker")
                return completed_response(
                    json.dumps(
                        {"translation": f"修订后的当前译文 {marker.group(0)}。"},
                        ensure_ascii=False,
                    )
                ), 0.1

        RUNNER.process_chunk(
            self.task,
            FakeClient(),
            [],
            stages=("revision",),
            initial_text_path=initial,
            paper_context="# QC-CORRECTION RETRY 2",
        )

        self.assertEqual(len(observed_inputs), 1)
        self.assertNotIn("Neighbor-only evidence", observed_inputs[0])
        self.assertNotIn("999", observed_inputs[0])

    def test_failed_segmented_refinement_retry_omits_whole_source_context(self) -> None:
        (self.article_dir / "chunk0001.md").write_text(
            "Whole original source contains trailing 999 evidence.\n", encoding="utf-8"
        )
        initial = self.article_dir / "stage_revision_chunk0001.md"
        initial.write_text(
            "前导句。\n后文{v1}甲{v2}乙{v3}丙{v4}丁{v5}。\n",
            encoding="utf-8",
        )
        status_dir = self.article_dir / "chunk_status"
        status_dir.mkdir()
        (status_dir / "chunk0001.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "chunk_id": "chunk0001",
                    "stages": {
                        "anti_ai": {
                            "status": "failed",
                            "error": "QC failed: numbers_mismatch",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        observed_inputs: list[str] = []

        class StopAfterCaptureClient:
            def complete(
                self, instructions: str, input_text: str, max_output_tokens: int
            ) -> tuple[dict[str, object], float]:
                observed_inputs.append(input_text)
                raise RuntimeError("stop after capture")

        with mock.patch.object(
            RUNNER,
            "stage_decision",
            return_value=RUNNER.StageDecision(True, "test_forced_model_call"),
        ), self.assertRaisesRegex(RuntimeError, "stop after capture"):
            RUNNER.process_chunk(
                self.task,
                StopAfterCaptureClient(),
                [],
                stages=("anti_ai",),
                initial_text_path=initial,
            )

        self.assertEqual(len(observed_inputs), 1)
        self.assertNotIn("Whole original source", observed_inputs[0])
        self.assertNotIn("999", observed_inputs[0])

    def test_completed_stage_does_not_reactivate_stale_bounded_retry_flag(self) -> None:
        (self.article_dir / "chunk0001.md").write_text(
            "Whole original source remains valid context.\n", encoding="utf-8"
        )
        initial = self.article_dir / "stage_revision_chunk0001.md"
        initial.write_text(
            "前导句。\n后文{v1}甲{v2}乙{v3}丙{v4}丁{v5}。\n",
            encoding="utf-8",
        )
        status_dir = self.article_dir / "chunk_status"
        status_dir.mkdir()
        (status_dir / "chunk0001.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "chunk_id": "chunk0001",
                    "stages": {
                        "anti_ai": {
                            "status": "complete",
                            "bounded_segmented_retry": True,
                            "request_key": "stale-key",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        observed_inputs: list[str] = []

        class StopAfterCaptureClient:
            def complete(
                self, instructions: str, input_text: str, max_output_tokens: int
            ) -> tuple[dict[str, object], float]:
                observed_inputs.append(input_text)
                raise RuntimeError("stop after capture")

        with mock.patch.object(
            RUNNER,
            "stage_decision",
            return_value=RUNNER.StageDecision(True, "test_forced_model_call"),
        ), self.assertRaisesRegex(RuntimeError, "stop after capture"):
            RUNNER.process_chunk(
                self.task,
                StopAfterCaptureClient(),
                [],
                stages=("anti_ai",),
                initial_text_path=initial,
            )

        self.assertEqual(len(observed_inputs), 1)
        self.assertIn("Whole original source remains valid context", observed_inputs[0])

    def test_failed_retry_recovers_still_valid_prior_stage_output(self) -> None:
        (self.article_dir / "chunk0001.md").write_text(
            "Dark energy constrains expansion.\n", encoding="utf-8"
        )
        initial = self.article_dir / "stage1_chunk0001.md"
        initial.write_text("dark energy 约束膨胀。\n", encoding="utf-8")
        prior_output = self.article_dir / "stage2_chunk0001.md"
        prior_output.write_text("暗能量约束膨胀。\n", encoding="utf-8")
        status_dir = self.article_dir / "chunk_status"
        status_dir.mkdir()
        (status_dir / "chunk0001.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "chunk_id": "chunk0001",
                    "stages": {
                        "terminology": {
                            "status": "failed",
                            "request_key": "failed-key",
                            "error": "QC failed: locked_terms_mismatch",
                            "output_file": "stage2_chunk0001.md",
                            "rejected_candidate_file": "rejected_candidates/failed.md",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        calls = 0

        class FakeClient:
            def complete(
                self, instructions: str, input_text: str, max_output_tokens: int
            ) -> tuple[dict[str, object], float]:
                nonlocal calls
                calls += 1
                return completed_response("暗能量约束膨胀。\n"), 0.1

        result = RUNNER.process_chunk(
            self.task,
            FakeClient(),
            [{"source": "dark energy", "target": "暗能量"}],
            stages=("terminology",),
            initial_text_path=initial,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(calls, 0)
        self.assertEqual(prior_output.read_text(encoding="utf-8"), "暗能量约束膨胀。\n")

    def test_reference_passthrough_writes_all_stages_without_model_calls(self) -> None:
        self.task["passthrough"] = True

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("reference section must remain verbatim")

        result = RUNNER.process_chunk(self.task, NoCallClient(), [])

        self.assertEqual(result["status"], "complete")
        for filename in (
            "stage1_chunk0001.md",
            "stage2_chunk0001.md",
            "stage3_chunk0001.md",
            "output_chunk0001.md",
        ):
            self.assertEqual(
                (self.article_dir / filename).read_text(encoding="utf-8"),
                "Original source paragraph.\n",
            )
        status = json.loads(
            (self.article_dir / "chunk_status/chunk0001.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            all(
                stage["decision"]["reason"] == "reference_section_passthrough"
                for stage in status["stages"].values()
            )
        )

    def test_fixed_document_text_is_reused_across_all_stages_without_model_calls(self) -> None:
        self.task["fixed_translation"] = "统一运行页眉\n"
        self.task["fixed_translation_reason"] = "hard_exact_translation"

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("fixed document text must not call the model")

        result = RUNNER.process_chunk(self.task, NoCallClient(), [])

        self.assertEqual(result["status"], "complete")
        for filename in (
            "stage1_chunk0001.md",
            "stage2_chunk0001.md",
            "stage3_chunk0001.md",
            "output_chunk0001.md",
        ):
            self.assertEqual(
                (self.article_dir / filename).read_text(encoding="utf-8"),
                "统一运行页眉\n",
            )
        status = json.loads(
            (self.article_dir / "chunk_status/chunk0001.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            all(
                stage["decision"]["reason"] == "hard_exact_translation"
                for stage in status["stages"].values()
            )
        )

    def test_structure_dense_stage_uses_resumable_bounded_subrequests(self) -> None:
        source = " ".join(
            f"part $x_{{{index}}}$" for index in range(66)
        ) + "\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")

        class DenseClient:
            calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                self.calls += 1
                self.assert_no_anchor = not RUNNER._MODEL_SENTINEL_RE.search(input_text)
                payload = json.loads(input_text.split("\n\n", 1)[0])
                translated = {
                    item["id"]: item["text"] for item in payload["slots"]
                }
                return completed_response(
                    json.dumps({"translations": translated}, ensure_ascii=False)
                ), 0.1

        client = DenseClient()
        result = RUNNER.process_chunk(
            self.task,
            client,
            [],
            run_id="dense-run",
            stages=("translate",),
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(client.calls, 17)
        self.assertTrue(client.assert_no_anchor)
        status = json.loads(
            (self.article_dir / "chunk_status" / "chunk0001.json").read_text(
                encoding="utf-8"
            )
        )
        translate = status["stages"]["translate"]
        self.assertEqual(translate["structure_segment_count"], 17)
        self.assertEqual(len(translate["subrequests"]), 17)
        self.assertTrue(
            all(item["status"] == "complete" for item in translate["subrequests"])
        )
        translated = (self.article_dir / "stage1_chunk0001.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(re.findall(r"\$[^$]+\$", translated), re.findall(r"\$[^$]+\$", source))

        # Simulate interruption after all paid subrequests completed but before
        # the merged stage artifact became durable.
        (self.article_dir / "stage1_chunk0001.md").unlink()
        translate["status"] = "running"
        status["status"] = "running"
        (self.article_dir / "chunk_status" / "chunk0001.json").write_text(
            json.dumps(status), encoding="utf-8"
        )

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("valid dense checkpoint must not replay API calls")

        resumed = RUNNER.process_chunk(
            self.task,
            NoCallClient(),
            [],
            run_id="resume-run",
            stages=("translate",),
        )
        self.assertEqual(resumed["status"], "complete")

    def test_process_chunk_can_stop_at_refined_draft_barrier_with_paper_context(self) -> None:
        class DraftClient:
            calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                self.calls += 1
                if "PAPER ANALYSIS CONTEXT" not in input_text:
                    raise AssertionError("paper-level refined context was not injected")
                return completed_response("初稿段落"), 0.1

        client = DraftClient()
        result = RUNNER.process_chunk(
            self.task,
            client,
            [],
            stages=("translate", "terminology"),
            paper_context="PAPER ANALYSIS CONTEXT",
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(client.calls, 1)
        self.assertEqual(
            (self.article_dir / "stage2_chunk0001.md").read_text(encoding="utf-8"),
            "初稿段落\n",
        )
        self.assertFalse((self.article_dir / "output_chunk0001.md").exists())

    def test_process_chunk_revision_uses_terminology_draft_and_local_critique(self) -> None:
        terminology = self.article_dir / "stage2_chunk0001.md"
        terminology.write_text("术语统一稿。\n", encoding="utf-8")

        class RevisionClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                if "术语统一稿" not in input_text or "chunk0001: 修正句法" not in input_text:
                    raise AssertionError("revision did not receive its draft and critique slice")
                return completed_response("修订稿。"), 0.1

        result = RUNNER.process_chunk(
            self.task,
            RevisionClient(),
            [],
            stages=("revision",),
            initial_text_path=terminology,
            paper_context="chunk0001: 修正句法",
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            (self.article_dir / "stage_revision_chunk0001.md").read_text(encoding="utf-8"),
            "修订稿。\n",
        )

    def test_revision_without_actionable_chunk_critique_is_qc_passthrough(self) -> None:
        terminology = self.article_dir / "stage2_chunk0001.md"
        terminology.write_text("术语统一稿。\n", encoding="utf-8")

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("no-op revision must not call the API")

        result = RUNNER.process_chunk(
            self.task,
            NoCallClient(),
            [],
            stages=("revision",),
            initial_text_path=terminology,
            paper_context="NO_ACTIONABLE_CHUNK_CRITIQUE: chunk0001",
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            (self.article_dir / "stage_revision_chunk0001.md").read_text(
                encoding="utf-8"
            ),
            "术语统一稿。\n",
        )

    def test_process_chunk_protects_and_restores_math_citations_and_urls_around_api_call(self) -> None:
        source = "Equation $E=mc^2$ follows \\cite{einstein}; see https://example.org/paper.\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")

        class ProtectingClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                self.assertions(input_text)
                payload = json.loads(input_text.split("\n\n", 1)[0])
                translations = {
                    item["id"]: "方程及相关说明" for item in payload["slots"]
                }
                return completed_response(json.dumps({"translations": translations})), 0.1

            @staticmethod
            def assertions(input_text: str) -> None:
                if "$E=mc^2$" in input_text or "\\cite{einstein}" in input_text or "https://example.org/paper" in input_text:
                    raise AssertionError("protected literals must not be sent directly in the translatable text")

        with mock.patch.object(RUNNER, "STAGES", ("translate",)):
            result = RUNNER.process_chunk(self.task, ProtectingClient(), [])

        output = (self.article_dir / "stage1_chunk0001.md").read_text(encoding="utf-8")
        self.assertEqual(result["status"], "complete")
        self.assertIn("$E=mc^2$", output)
        self.assertIn("\\cite{einstein}", output)
        self.assertIn("https://example.org/paper", output)
        self.assertNotIn("[[SM_", output)

    def test_process_chunk_protects_ordinary_integers_and_balanced_tex_urls(self) -> None:
        source = r"The sample has 2800 events; source {\url{https://example.org/a}}中文。" + "\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")

        class ProtectingClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                if "2800" in input_text or r"\url{https://example.org/a}" in input_text:
                    raise AssertionError("numbers and balanced TeX URLs must be protected")
                payload = json.loads(input_text.split("\n\n", 1)[0])
                translations = {
                    item["id"]: "样本及来源。" for item in payload["slots"]
                }
                return completed_response(json.dumps({"translations": translations})), 0.1

        with mock.patch.object(RUNNER, "STAGES", ("translate",)):
            result = RUNNER.process_chunk(self.task, ProtectingClient(), [])

        output = (self.article_dir / "stage1_chunk0001.md").read_text(encoding="utf-8")
        self.assertEqual(result["status"], "complete")
        self.assertIn("2800", output)
        self.assertIn(r"\url{https://example.org/a}", output)
        self.assertNotIn("[[SM", output)

    def test_process_chunk_rejects_model_output_that_drops_text_slot(self) -> None:
        (self.article_dir / "chunk0001.md").write_text("Equation $E=mc^2$.\n", encoding="utf-8")

        class DroppingClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                return completed_response("方程。"), 0.1

        with (
            mock.patch.object(RUNNER, "STAGES", ("translate",)),
            self.assertRaises(RUNNER.StructureMismatchError),
        ):
            RUNNER.process_chunk(self.task, DroppingClient(), [])

        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]
        self.assertEqual(translate["status"], "failed")
        self.assertIn("valid JSON", translate["error"])
        self.assertEqual(
            translate["subrequests"][0]["structure_slot_protocol"],
            RUNNER.STRUCTURE_SLOT_PROTOCOL,
        )
        self.assertFalse((self.article_dir / "stage1_chunk0001.md").exists())

    def test_stage_instructions_make_sentinel_contract_explicit(self) -> None:
        instructions = RUNNER.stage_instructions("translate", "")

        self.assertIn("exactly once", instructions)
        self.assertIn("[[SM_0000_...]]", instructions)
        self.assertIn("[[SMU_0000_TYPE_...]]", instructions)
        self.assertIn("same order", instructions)
        self.assertIn("[[SM_", instructions)
        self.assertIn("pronoun", instructions)
        self.assertIn("Arabic numeral", instructions)
        self.assertIn(r"\%", instructions)

    def test_process_chunk_reprocesses_legacy_checkpoint_without_qc(self) -> None:
        class InitialClient:
            calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                self.calls += 1
                return completed_response("阶段产物"), 0.1

        initial_client = InitialClient()
        RUNNER.process_chunk(self.task, initial_client, [])
        status_path = self.article_dir / "chunk_status" / "chunk0001.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["stages"]["translate"].pop("qc")
        status_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")

        class RepairClient:
            calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                self.calls += 1
                return completed_response("阶段产物"), 0.1

        repair_client = RepairClient()
        result = RUNNER.process_chunk(self.task, repair_client, [])
        repaired_status = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "complete")
        self.assertEqual(repair_client.calls, 1)
        self.assertEqual(repaired_status["stages"]["translate"]["qc"], {"ok": True, "failures": []})

    def test_process_chunk_reuses_modern_checkpoints_without_paid_calls(self) -> None:
        class InitialClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                return completed_response("阶段产物"), 0.1

        RUNNER.process_chunk(self.task, InitialClient(), [])

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                raise AssertionError("modern checkpoints must resume without a paid API call")

        result = RUNNER.process_chunk(self.task, NoCallClient(), [])

        self.assertEqual(result["status"], "complete")

    def test_process_chunk_revalidates_rejected_candidate_without_paid_call(self) -> None:
        (self.article_dir / "chunk0001.md").write_text("Value 14.\n", encoding="utf-8")

        class InitialClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                payload = json.loads(input_text.split("\n\n", 1)[0])
                translations = {
                    item["id"]: "数值。" for item in payload["slots"]
                }
                return completed_response(json.dumps({"translations": translations})), 0.1

        rejected_qc = mock.Mock()
        rejected_qc.ok = False
        rejected_qc.failures = ("numbers_mismatch",)
        rejected_qc.to_dict.return_value = {"ok": False, "failures": ["numbers_mismatch"]}
        with (
            mock.patch.object(RUNNER, "STAGES", ("translate",)),
            mock.patch.object(RUNNER, "validate_chunk", return_value=rejected_qc),
            self.assertRaises(RuntimeError),
        ):
            RUNNER.process_chunk(self.task, InitialClient(), [])

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                raise AssertionError("a now-valid quarantined candidate must be recovered without an API call")

        with mock.patch.object(RUNNER, "STAGES", ("translate",)):
            result = RUNNER.process_chunk(self.task, NoCallClient(), [])

        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]
        self.assertEqual(result["status"], "complete")
        self.assertTrue(translate["recovered_from_rejected_candidate"])
        recovered = (self.article_dir / "stage1_chunk0001.md").read_text(encoding="utf-8")
        self.assertIn("14", recovered)
        self.assertNotIn("[[SMU_", recovered)

    def test_process_chunk_marks_ambiguous_transport_failure_uncertain_without_output(self) -> None:
        class FakeClient:
            calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                self.calls += 1
                raise RUNNER.AmbiguousTransportError("connection reset before response body completed")

        client = FakeClient()
        result = RUNNER.process_chunk(self.task, client, [])
        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]

        self.assertEqual(client.calls, 1)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(translate["status"], "uncertain")
        self.assertFalse((self.article_dir / "stage1_chunk0001.md").exists())

    def test_process_chunk_reserves_budget_before_calling_model(self) -> None:
        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                raise AssertionError("model must not be called after budget rejection")

        guard = RUNNER.BudgetGuard(0.001)
        with self.assertRaises(RUNNER.BudgetExceededError):
            RUNNER.process_chunk(self.task, NoCallClient(), [], budget_guard=guard)

        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        self.assertEqual(status["stages"]["translate"]["status"], "failed")
        self.assertEqual(guard.snapshot()["active_reservations"], 0)

    def test_process_chunk_commits_reservation_after_ambiguous_transport(self) -> None:
        class AmbiguousClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                raise RUNNER.AmbiguousTransportError("response may have been generated")

        guard = RUNNER.BudgetGuard(1.0)
        result = RUNNER.process_chunk(self.task, AmbiguousClient(), [], budget_guard=guard)

        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(guard.snapshot()["active_reservations"], 0)
        self.assertGreater(guard.snapshot()["spent_rmb"], 0)

    def test_process_chunk_persists_raw_response_before_validation_failure(self) -> None:
        class FakeClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                return {
                    "id": "resp_bad",
                    "status": "incomplete",
                    "model": RUNNER.MODEL,
                    "incomplete_details": {"reason": "max_output_tokens"},
                }, 0.2

        with self.assertRaises(RUNNER.IncompleteResponseError):
            RUNNER.process_chunk(self.task, FakeClient(), [])

        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]

        self.assertEqual(translate["status"], "failed")
        self.assertEqual(translate["raw_response"]["status"], "incomplete")
        self.assertEqual(translate["response_id"], "resp_bad")

    def test_process_chunk_persists_coarse_metadata_for_malformed_usage_details(self) -> None:
        malformed = completed_response()
        malformed["id"] = "resp_malformed_usage"
        malformed["usage"]["input_tokens_details"] = ["not-an-object"]

        class FakeClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                return malformed, 0.2

        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.process_chunk(self.task, FakeClient(), [])

        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]

        self.assertEqual(translate["status"], "failed")
        self.assertEqual(translate["raw_response"]["id"], "resp_malformed_usage")
        self.assertEqual(translate["raw_response"]["status"], "completed")
        self.assertEqual(translate["raw_response"]["usage"]["input_tokens"], 10)
        self.assertIsNone(translate["raw_response"]["usage"]["cached_tokens"])
        self.assertIn("input_tokens_details", translate["error"])


class DeepSeekClientRetryTests(unittest.TestCase):
    def test_client_retries_tls_handshake_eof_before_request(self) -> None:
        client = RUNNER.DeepSeekClient("test-key", max_retries=2)
        tls_eof = RUNNER.urllib.error.URLError(
            ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING")
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            completed_response("ok")
        ).encode()
        with (
            mock.patch.object(
                RUNNER.urllib.request,
                "urlopen",
                side_effect=[tls_eof, response],
            ) as urlopen,
            mock.patch.object(RUNNER.time, "sleep") as sleep,
            mock.patch.object(RUNNER.random, "random", return_value=0.0),
        ):
            result, _latency = client.complete("instructions", "input", 2048)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    def test_client_does_not_retry_ambiguous_transport_failures(self) -> None:
        client = RUNNER.DeepSeekClient("test-key", max_retries=3)

        with (
            mock.patch.object(
                RUNNER.urllib.request,
                "urlopen",
                side_effect=RUNNER.urllib.error.URLError("connection reset"),
            ) as urlopen,
            mock.patch.object(RUNNER.time, "sleep") as sleep,
        ):
            with self.assertRaises(RUNNER.AmbiguousTransportError):
                client.complete("instructions", "input", 2048)

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_client_retries_retryable_http_errors_only_up_to_the_bound(self) -> None:
        client = RUNNER.DeepSeekClient("test-key", max_retries=2)
        errors = [
            RUNNER.urllib.error.HTTPError(
                RUNNER.API_URL,
                429,
                "Too Many Requests",
                hdrs=None,
                fp=io.BytesIO(b'{"error":"rate limited"}'),
            )
            for _ in range(3)
        ]
        self.addCleanup(lambda: [error.close() for error in errors])

        with (
            mock.patch.object(RUNNER.urllib.request, "urlopen", side_effect=errors) as urlopen,
            mock.patch.object(RUNNER.time, "sleep") as sleep,
            mock.patch.object(RUNNER.random, "random", return_value=0.0),
        ):
            with self.assertRaises(RuntimeError):
                client.complete("instructions", "input", 2048)

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
