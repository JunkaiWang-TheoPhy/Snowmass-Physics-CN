#!/usr/bin/env python3
"""Regression tests for the rights gate in the Snowmass translator."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import ssl
import subprocess
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


def chat_completion_response(
    text: str = "译文\n",
    *,
    model: str = RUNNER.MODEL,
    finish_reason: str = "stop",
) -> dict[str, object]:
    return {
        "id": "chatcmpl_123",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "prompt_cache_hit_tokens": 1,
            "prompt_cache_miss_tokens": 9,
            "completion_tokens": 20,
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
                            "record_id": record_id,
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
                (article / "chunk0001.md").write_text("Figure text\n", encoding="utf-8")
                constraints = RUNNER.constraint_compiler.load_constraints(
                    article, record_id, RUNNER.TRACKED_HARD_CONSTRAINTS
                )
                RUNNER.constraint_compiler.write_constraint_plan(
                    article,
                    RUNNER.constraint_compiler.compile_constraint_plan(
                        article,
                        json.loads((article / "manifest.json").read_text(encoding="utf-8")),
                        constraints,
                    ),
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
                        "record_id": "arxiv:allowed",
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
            manifest = json.loads((article / "manifest.json").read_text(encoding="utf-8"))
            constraints = RUNNER.constraint_compiler.load_constraints(
                article, "arxiv:allowed", RUNNER.TRACKED_HARD_CONSTRAINTS
            )
            RUNNER.constraint_compiler.write_constraint_plan(
                article,
                RUNNER.constraint_compiler.compile_constraint_plan(
                    article, manifest, constraints
                ),
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

    def test_neighbor_context_does_not_spawn_one_python_process_per_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            article = Path(temporary)
            (article / "chunk0001.md").write_text("Previous evidence.\n", encoding="utf-8")
            (article / "chunk0002.md").write_text("Current claim.\n", encoding="utf-8")
            (article / "chunk0003.md").write_text("Next qualification.\n", encoding="utf-8")

            with mock.patch.object(
                RUNNER.subprocess,
                "run",
                side_effect=AssertionError("neighbor context must run in-process"),
            ):
                context = RUNNER.load_neighbor_context(article, "chunk0002.md")

            self.assertIn("Previous evidence.", context)
            self.assertIn("Next qualification.", context)

    def test_neighbor_context_reloads_when_translate_book_helper_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "chunk_context.py"
            article = root / "article"
            article.mkdir()
            (article / "chunk0001.md").write_text("Current.\n", encoding="utf-8")
            helper.write_text(
                "def get_neighbor_context(*args): return {'value': 'v1'}\n"
                "def format_for_prompt(context): return context['value']\n",
                encoding="utf-8",
            )

            with mock.patch.object(RUNNER, "TRANSLATE_BOOK_CONTEXT", helper):
                RUNNER._TRANSLATE_BOOK_CONTEXT_MODULE = None
                RUNNER._TRANSLATE_BOOK_CONTEXT_SHA256 = None
                self.assertEqual(RUNNER.load_neighbor_context(article, "chunk0001.md"), "v1")
                helper.write_text(
                    "def get_neighbor_context(*args): return {'value': 'v2'}\n"
                    "def format_for_prompt(context): return context['value']\n",
                    encoding="utf-8",
                )
                self.assertEqual(RUNNER.load_neighbor_context(article, "chunk0001.md"), "v2")

    def test_neighbor_context_normalizes_helper_load_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "chunk_context.py"
            helper.write_text("this is not valid python !!!\n", encoding="utf-8")
            article = root / "article"
            article.mkdir()
            (article / "chunk0001.md").write_text("Current.\n", encoding="utf-8")

            with mock.patch.object(RUNNER, "TRANSLATE_BOOK_CONTEXT", helper):
                RUNNER._TRANSLATE_BOOK_CONTEXT_MODULE = None
                RUNNER._TRANSLATE_BOOK_CONTEXT_SHA256 = None
                with self.assertRaisesRegex(
                    RuntimeError,
                    "translate-book neighbor context failed for chunk0001.md",
                ):
                    RUNNER.load_neighbor_context(article, "chunk0001.md")


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

    def test_select_glossary_terms_ignores_configured_proper_name_phrase(self) -> None:
        selected = RUNNER.select_glossary_terms(
            "Université Catholique, Chemin du Cyclotron, Belgium.",
            [
                {
                    "source": "cyclotron",
                    "target": "回旋加速器",
                    "exclude_phrases": ["Chemin du Cyclotron"],
                }
            ],
        )

        self.assertEqual(selected, [])

    def test_select_glossary_terms_does_not_match_inside_a_longer_english_word(self) -> None:
        selected = RUNNER.select_glossary_terms(
            "Education initiatives and collaborations with industry.",
            [{"source": "collaboration", "target": "合作组"}],
        )

        self.assertEqual(selected, [])

    def test_compile_glossary_terms_uses_contextual_target_for_current_source(self) -> None:
        compiled = RUNNER.compile_glossary_terms(
            "Many parameters have no observable effect.",
            [
                {
                    "source": "observable",
                    "target": "可观测量",
                    "contextual_targets": [
                        {
                            "source_regex": r"\bobservable\s+(?:effect|effects)\b",
                            "target": "可观测",
                        }
                    ],
                }
            ],
        )

        self.assertEqual(compiled[0]["target"], "可观测")
        self.assertEqual(compiled[0]["canonical_target"], "可观测量")

    def test_compile_glossary_terms_treats_collaboration_models_as_ordinary_cooperation(self) -> None:
        terms = json.loads(
            (Path(__file__).parents[1] / "translations" / "snowmass-global-glossary.json").read_text(
                encoding="utf-8"
            )
        )["terms"]

        compiled = RUNNER.compile_glossary_terms(
            "Business collaboration models and recommendations are discussed.",
            terms,
        )

        collaboration = next(term for term in compiled if term["source"] == "collaboration")
        self.assertEqual(collaboration["target"], "合作")
        self.assertNotIn("contextual_targets", compiled[0])

    def test_tracked_glossary_treats_observable_scales_as_adjectival(self) -> None:
        terms = RUNNER.load_glossary(RUNNER.TRACKED_GLOBAL_GLOSSARY)

        compiled = RUNNER.compile_glossary_terms(
            "The interaction washes out structure on observable scales.",
            terms,
        )

        observable = next(term for term in compiled if term["source"] == "observable")
        self.assertEqual(observable["target"], "可观测")
        self.assertEqual(observable["canonical_target"], "可观测量")


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

    def test_request_key_is_bound_to_execution_lock(self) -> None:
        prior = RUNNER.ACTIVE_EXECUTION_LOCK_SHA256
        try:
            RUNNER.ACTIVE_EXECUTION_LOCK_SHA256 = "execution-a"
            first = RUNNER.request_key(
                stage="translate",
                model="model",
                instructions="instructions",
                input_text="input",
                max_output_tokens=1024,
            )
            RUNNER.ACTIVE_EXECUTION_LOCK_SHA256 = "execution-b"
            second = RUNNER.request_key(
                stage="translate",
                model="model",
                instructions="instructions",
                input_text="input",
                max_output_tokens=1024,
            )
        finally:
            RUNNER.ACTIVE_EXECUTION_LOCK_SHA256 = prior
        self.assertNotEqual(first, second)

    def test_qc_contract_version_requires_v5_checkpoint_revalidation(self) -> None:
        self.assertEqual(RUNNER.QC_CONTRACT_VERSION, 5)

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

    def test_passthrough_checkpoint_cannot_be_reused_after_policy_changes_to_model(self) -> None:
        status = {
            "status": "complete",
            "decision": {
                "action": "copy_prior_text",
                "reason": "reference_section_passthrough",
            },
        }

        self.assertFalse(RUNNER.checkpoint_policy_matches(status, "model_pipeline"))
        self.assertTrue(
            RUNNER.checkpoint_policy_matches(
                status,
                "passthrough:reference_section_passthrough",
            )
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

    def test_plain_refinement_input_does_not_reexpose_original_source_literals(self) -> None:
        request = RUNNER.stage_input(
            "revision",
            "Original source contains 2021 and 1.4PB.",
            "当前译文 [[SMU_0001_NUMBER_1bea20e1df]] 与 "
            "[[SMU_0002_UNIT_2cf342a8e5]]。",
            "",
        )

        self.assertNotIn("ORIGINAL SOURCE:", request)
        self.assertNotIn("2021", request)
        self.assertNotIn("1.4PB", request)
        self.assertIn("[[SMU_0001_NUMBER_1bea20e1df]]", request)

    def test_refinement_context_redacts_literals_but_preserves_critique_wording(self) -> None:
        context = (
            "chunk0022: move 2021 after the phrase; "
            "chunk0140: place 1.4PB after SoCal Repo."
        )

        sanitized = RUNNER.sanitize_refinement_context(context)

        self.assertNotIn("2021", sanitized)
        self.assertNotIn("1.4PB", sanitized)
        self.assertIn("move", sanitized)
        self.assertIn("SoCal Repo", sanitized)
        self.assertGreaterEqual(sanitized.count("<PROTECTED_NUMBER>"), 1)
        self.assertEqual(sanitized.count("<PROTECTED_UNIT>"), 1)

    def test_translation_omission_is_not_blocked_as_literal_rebinding(self) -> None:
        context = 'chunk0001: 中文稿漏译“March 30th”，应译为“3月30日”。'

        self.assertTrue(RUNNER.refinement_context_contains_factual_literals(context))
        self.assertTrue(RUNNER.refinement_context_is_translation_omission(context))

    def test_placeholder_indices_are_not_treated_as_factual_critique_literals(self) -> None:
        context = "chunk0136: replace the punctuation after {v15}."

        self.assertFalse(RUNNER.refinement_context_contains_factual_literals(context))
        self.assertIn("{v15}", RUNNER.sanitize_refinement_context(context))

    def test_unique_quoted_critique_replacement_is_applied_without_model(self) -> None:
        source = "Section 2.4 describes the result.\n"
        prior = "第节所述，2.4该结果。\n"
        context = 'chunk0001: “第节所述，2.4”语序错误，应为“第2.4节所述”。'

        repaired = RUNNER.deterministic_critique_revision(
            source=source,
            prior_text=prior,
            paper_context=context,
            qc_terms=[],
        )

        self.assertEqual(repaired, "第2.4节所述该结果。\n")

    def test_quoted_translation_omission_is_repaired_without_model(self) -> None:
        source = "Additional contributors can subscribe until March 30th.\n"
        prior = "Additional contributors can subscribe until March 30th.\n"
        context = (
            'chunk0001: 中文稿未翻译“Additional contributors can subscribe until '
            'March 30th.”，应译为“其他贡献者可在3月30日前订阅。”'
        )

        repaired = RUNNER.deterministic_critique_revision(
            source=source,
            prior_text=prior,
            paper_context=context,
            qc_terms=[],
        )

        self.assertEqual(repaired, "其他贡献者可在3月30日前订阅。\n")

    def test_critique_replacement_that_changes_numeric_literals_is_rejected(self) -> None:
        repaired = RUNNER.deterministic_critique_revision(
            source="The demand was reduced by a factor of 2.\n",
            prior_text="需求平均降低了2倍。\n",
            paper_context='chunk0001: “平均降低了2倍”应为“平均减少了一半”。',
            qc_terms=[],
        )

        self.assertIsNone(repaired)

    def test_process_chunk_uses_deterministic_critique_revision_without_api(self) -> None:
        source = "Section 2.4 describes the result.\n"
        prior = "第节所述，2.4该结果。\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")
        initial = self.article_dir / "stage2_chunk0001.md"
        initial.write_text(prior, encoding="utf-8")

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("deterministic critique repair must not call the API")

        result = RUNNER.process_chunk(
            self.task,
            NoCallClient(),
            [],
            stages=("revision",),
            initial_text_path=initial,
            paper_context='chunk0001: “第节所述，2.4”语序错误，应为“第2.4节所述”。',
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            (self.article_dir / "stage_revision_chunk0001.md").read_text(encoding="utf-8"),
            "第2.4节所述该结果。\n",
        )
        status = json.loads(
            (self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8")
        )["stages"]["revision"]
        self.assertEqual(
            status["decision"]["reason"],
            "revision_deterministic_critique_replacement",
        )

    def test_revision_with_literal_bearing_critique_preserves_prior_text_without_model(self) -> None:
        source = "The repository has 24 nodes and 2.5PB of storage.\n"
        prior = "该仓库有24个节点，存储容量为2.5PB。\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")
        initial = self.article_dir / "stage2_chunk0001.md"
        initial.write_text(prior, encoding="utf-8")

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("literal-bearing critique must not reach the model")

        result = RUNNER.process_chunk(
            self.task,
            NoCallClient(),
            [],
            stages=("revision",),
            initial_text_path=initial,
            paper_context="chunk0001: move 24 after the year and place 2.5PB before nodes.",
        )

        self.assertEqual(result["status"], "complete")
        output = self.article_dir / "stage_revision_chunk0001.md"
        self.assertEqual(output.read_text(encoding="utf-8"), prior)
        status = json.loads(
            (self.article_dir / "chunk_status" / "chunk0001.json").read_text(
                encoding="utf-8"
            )
        )["stages"]["revision"]
        self.assertEqual(
            status["decision"]["reason"],
            "revision_literal_rebinding_requires_manual_review",
        )

    def test_literal_review_policy_invalidates_a_prior_model_revision_checkpoint(self) -> None:
        source = "The repository has 24 nodes and 2.5PB of storage.\n"
        prior = "该仓库有24个节点，存储容量为2.5PB。\n"
        unsafe = "该仓库于24年提供2.5PB个节点。\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")
        initial = self.article_dir / "stage2_chunk0001.md"
        initial.write_text(prior, encoding="utf-8")
        output = self.article_dir / "stage_revision_chunk0001.md"
        output.write_text(unsafe, encoding="utf-8")
        status_dir = self.article_dir / "chunk_status"
        status_dir.mkdir()
        (status_dir / "chunk0001.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "chunk_id": "chunk0001",
                    "source_file": "chunk0001.md",
                    "source_hash": "source-hash",
                    "stages": {
                        "revision": {
                            "status": "complete",
                            "request_key": "legacy-key",
                            "output_file": output.name,
                            "output_hash": RUNNER.text_hash(unsafe),
                            "qc": {"ok": True, "failures": []},
                            "decision": {"action": "call_model", "reason": "stage_requires_model"},
                            "execution_policy": "model_pipeline",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("unsafe revision checkpoint must downgrade without a call")

        RUNNER.process_chunk(
            self.task,
            NoCallClient(),
            [],
            stages=("revision",),
            initial_text_path=initial,
            paper_context="chunk0001: move 24 and 2.5PB to different clauses.",
        )

        self.assertEqual(output.read_text(encoding="utf-8"), prior)

    def test_structure_slot_requests_enable_official_json_output_mode(self) -> None:
        structured = RUNNER.build_request_payload(
            "Return JSON. STRUCTURE-SLOT PROTOCOL", "{}", 128
        )
        fallback = RUNNER.build_request_payload(
            "Return JSON. STRUCTURE-ANCHOR FALLBACK PROTOCOL", "{}", 128
        )
        style_batch = RUNNER.build_request_payload(
            "Return JSON. STYLE-BATCH JSON PROTOCOL", "{}", 128
        )
        plain = RUNNER.build_request_payload("Translate", "text", 128)

        self.assertEqual(structured["response_format"], {"type": "json_object"})
        self.assertEqual(fallback["response_format"], {"type": "json_object"})
        self.assertEqual(style_batch["response_format"], {"type": "json_object"})
        self.assertEqual(plain["response_format"], {"type": "text"})
        self.assertEqual(plain["thinking"], {"type": "disabled"})
        self.assertEqual(
            plain["messages"],
            [
                {"role": "system", "content": "Translate"},
                {"role": "user", "content": "text"},
            ],
        )

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

    def test_stage_protection_keeps_reference_parentheses_as_one_typed_node(self) -> None:
        protected, mapping, nodes = RUNNER.protect_stage_text(
            "Defined below Eq. (2.6) and Eq. (B.63)."
        )

        self.assertEqual(list(mapping.values()), ["(2.6)", "(B.63)"])
        self.assertEqual(nodes, ())
        self.assertNotIn("(2.6)", protected)
        self.assertEqual(
            RUNNER.restore_stage_text(protected, mapping, nodes),
            "Defined below Eq. (2.6) and Eq. (B.63).",
        )

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

    def test_structure_slots_allow_short_connector_to_move_for_chinese_syntax(self) -> None:
        _, anchors, parts = RUNNER.build_structure_slot_input(
            "Production cross sections [[SM_0001_0000000001]]"
            " in BPs 4 and 5 [[SM_0002_0000000002]], respectively, "
        )
        response = json.dumps(
            {
                "translations": {
                    "T0000": "产生截面",
                    "T0001": "在基准点4和5中分别为",
                    "T0002": "，",
                }
            },
            ensure_ascii=False,
        )

        restored = RUNNER.restore_structure_slot_output(response, anchors, parts)

        self.assertIn("分别", restored)
        self.assertEqual(RUNNER._MODEL_SENTINEL_RE.findall(restored), list(anchors))

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
        self.assertTrue(
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
        self.assertTrue(
            RUNNER.should_use_structure_anchor_fallback(
                "translate",
                {"error": "Structure-slot response is suspiciously short for the complete segment"},
                "",
            )
        )

    def test_structure_failure_keeps_bounded_multi_anchor_segments(self) -> None:
        self.assertEqual(RUNNER.structure_segment_limit({}), 24)
        for error in (
            "Anchor-template response changed anchor identity or count",
            "Structure-slot response changed text-slot identities",
            "Invalid structure-slot value for T0001",
        ):
            with self.subTest(error=error):
                self.assertEqual(
                    RUNNER.structure_segment_limit({"error": error}),
                    8,
                )

    def test_fidelity_qc_retry_keeps_bounded_multi_anchor_segments(self) -> None:
        for failure in (
            "numbers_mismatch",
            "units_mismatch",
            "parentheses_mismatch",
            "locked_terms_mismatch",
        ):
            with self.subTest(failure=failure):
                self.assertEqual(
                    RUNNER.structure_segment_limit(
                        {"error": f"QC failed: {failure}"}
                    ),
                    24,
                )

    def test_default_structure_density_uses_three_requests_for_sixty_six_nodes(self) -> None:
        protected = " ".join(
            f"part{i} [[SM_{i:04d}_{i:010x}]]" for i in range(66)
        )

        segments = RUNNER.split_protected_model_input(
            protected, RUNNER.structure_segment_limit({})
        )

        self.assertEqual("".join(segments), protected)
        self.assertEqual(len(segments), 3)
        self.assertTrue(
            all(len(RUNNER._MODEL_SENTINEL_RE.findall(item)) <= 24 for item in segments)
        )

    def test_structure_dense_input_prefers_clause_boundary_before_hard_split(self) -> None:
        protected = (
            "first [[SM_0001_0000000001]] second [[SM_0002_0000000002]], "
            "third [[SM_0003_0000000003]] fourth [[SM_0004_0000000004]] "
            "fifth [[SM_0005_0000000005]]"
        )

        segments = RUNNER.split_protected_model_input(protected, 4)

        self.assertEqual("".join(segments), protected)
        self.assertEqual(len(segments), 2)
        self.assertTrue(segments[0].endswith(", "))
        self.assertTrue(all(len(RUNNER._MODEL_SENTINEL_RE.findall(x)) <= 4 for x in segments))

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

    def test_hyphenated_source_range_does_not_become_a_negative_endpoint(self) -> None:
        source = (
            "The gain is 2-to-3 orders of magnitude, while the limit is "
            "2-to-3 times lower."
        )
        candidate = "增益高出2到-3个数量级，而极限低2至-3倍。"

        normalized = RUNNER.normalize_hyphenated_numeric_ranges(source, candidate)

        self.assertEqual(normalized, "增益高出2到3个数量级，而极限低2至3倍。")
        self.assertTrue(RUNNER.validate_chunk(source, normalized, {}, []).ok)

    def test_numeric_range_normalization_preserves_a_real_negative_endpoint(self) -> None:
        source = "The interval runs from 2 to -3."
        candidate = "区间从2到-3。"

        normalized = RUNNER.normalize_hyphenated_numeric_ranges(source, candidate)

        self.assertEqual(normalized, candidate)
        self.assertTrue(RUNNER.validate_chunk(source, normalized, {}, []).ok)

    def test_source_month_years_are_localized_before_model_protection(self) -> None:
        source = "In January 2020 and June 2021, the council met."

        localized = RUNNER.localize_source_month_years(source)

        self.assertEqual(localized, "In 2020年一月 and 2021年六月, the council met.")
        protected, _mapping, nodes = RUNNER.protect_stage_text(localized)
        self.assertNotIn("January", protected)
        self.assertNotIn("June", protected)
        self.assertEqual([node.value for node in nodes], ["2020", "2021"])

    def test_candidate_month_numbers_are_normalized_for_abbreviated_source_months(self) -> None:
        source = "Data from Julyto Dec. 2021, with nodes added since Sep. 2021."
        candidate = "数据来自7月至12月2021，自9月2021起增加节点。"

        normalized = RUNNER.normalize_source_month_names(source, candidate)

        self.assertEqual(normalized, "数据来自七月至十二月2021，自九月2021起增加节点。")
        self.assertTrue(RUNNER.validate_chunk(source, normalized, {}, []).ok)

    def test_numeric_dimension_normalization_removes_spurious_negative_separator(self) -> None:
        source = "Each module uses a 3-by-3 crystals matrix."
        candidate = "每个模块使用3乘-3晶体矩阵。"

        normalized = RUNNER.normalize_hyphenated_numeric_ranges(source, candidate)

        self.assertEqual(normalized, "每个模块使用3乘3晶体矩阵。")
        self.assertTrue(RUNNER.validate_chunk(source, normalized, {}, []).ok)

    def test_numeric_dimension_normalization_repairs_spurious_negative_after_multiplication_sign(self) -> None:
        source = "Each module uses a 3-by-3 crystals matrix."
        candidate = "每个子模块由一块3×-3晶体矩阵构成。"

        normalized = RUNNER.normalize_hyphenated_numeric_ranges(source, candidate)

        self.assertEqual(normalized, "每个子模块由一块3×3晶体矩阵构成。")
        self.assertTrue(RUNNER.validate_chunk(source, normalized, {}, []).ok)

    def test_tier_label_normalization_repairs_spurious_negative_level(self) -> None:
        source = "The ATLAS and CMS Tier-1 sites provide the data."
        candidate = "ATLAS和CMS以及-1站点提供数据。"

        normalized = RUNNER.normalize_source_evidenced_candidate(source, candidate)

        self.assertEqual(normalized, "ATLAS和CMS以及1级站点提供数据。")
        self.assertTrue(RUNNER.validate_chunk(source, normalized, {}, []).ok)

    def test_anchor_json_accepts_one_unmatched_quote_before_closing_brace(self) -> None:
        protected, _mapping, _nodes = RUNNER.protect_stage_text("Bandwidth is 10 Gb/s.")
        _payload, anchors, markers = RUNNER.build_structure_anchor_input(protected)
        malformed = json.dumps(
            {"translation": f"带宽为{markers[0]}。"},
            ensure_ascii=False,
        ) + '"'

        restored = RUNNER.restore_structure_anchor_output(malformed, anchors, markers)

        self.assertEqual(restored, f"带宽为{anchors[0]}。")

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

    def test_style_fallback_resolves_discarded_uncertain_request_without_refund(self) -> None:
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
                            "status": "running",
                            "subrequests": [
                                {
                                    "status": "uncertain",
                                    "request_key": "stale-request",
                                    "uncertainty_key": (
                                        "arxiv:allowed:chunk0001:academic:segment0001"
                                    ),
                                    "conservative_cost_rmb": 0.125,
                                }
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        class RecordingGuard:
            resolved: list[str] = []

            def resolve_uncertain(self, uncertainty_key: str) -> bool:
                self.resolved.append(uncertainty_key)
                return True

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("verified style fallback must not call the model")

        guard = RecordingGuard()
        result = RUNNER.process_chunk(
            self.task,
            NoCallClient(),
            [],
            stages=("academic",),
            initial_text_path=initial,
            budget_guard=guard,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            guard.resolved,
            ["arxiv:allowed:chunk0001:academic:segment0001"],
        )
        status = json.loads((status_dir / "chunk0001.json").read_text(encoding="utf-8"))
        fallback = status["stages"]["academic"]
        self.assertTrue(fallback["fallback_to_prior_stage"])
        self.assertEqual(
            fallback["resolved_discarded_uncertainties"],
            ["arxiv:allowed:chunk0001:academic:segment0001"],
        )

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

    def test_revision_retry_qc_failure_falls_back_to_valid_terminology_text(self) -> None:
        source = "The observed traffic saving was 1.4PB.\n"
        prior = "观测到的流量节省量为1.4PB。\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")
        initial = self.article_dir / "stage2_chunk0001.md"
        initial.write_text(prior, encoding="utf-8")

        class DuplicatingClient:
            def complete(
                self, instructions: str, input_text: str, max_output_tokens: int
            ) -> tuple[dict[str, object], float]:
                slot_ids = list(dict.fromkeys(re.findall(r'"(T\d{4})"', input_text)))
                translations = {
                    slot_id: ("1.4PB观测到的流量节省量为" if index == 0 else "。\n")
                    for index, slot_id in enumerate(slot_ids)
                }
                return completed_response(
                    json.dumps({"translations": translations}, ensure_ascii=False)
                ), 0.1

        result = RUNNER.process_chunk(
            self.task,
            DuplicatingClient(),
            [],
            stages=("revision",),
            initial_text_path=initial,
            paper_context="# QC-CORRECTION RETRY 1\n修正语序。",
            paper_context_identity="chunk0001: 修正语序。",
        )

        self.assertEqual(result["status"], "complete")
        output = self.article_dir / "stage_revision_chunk0001.md"
        self.assertEqual(output.read_text(encoding="utf-8"), prior)
        status = json.loads(
            (self.article_dir / "chunk_status" / "chunk0001.json").read_text(
                encoding="utf-8"
            )
        )["stages"]["revision"]
        self.assertTrue(status["fallback_to_prior_stage"])
        self.assertEqual(status["fallback_policy"], "revision_qc_retry_exhausted")

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
        self.assertNotIn("ORIGINAL SOURCE:", observed_inputs[0])
        self.assertNotIn("Neighbor-only evidence", observed_inputs[0])
        self.assertNotIn("999", observed_inputs[0])

    def test_successful_qc_retry_is_reused_under_its_stable_base_context(self) -> None:
        (self.article_dir / "chunk0001.md").write_text(
            "Original source contains unique retry contaminant ZEPHYRWORD.\n",
            encoding="utf-8",
        )
        initial = self.article_dir / "stage2_chunk0001.md"
        initial.write_text("当前译文。\n", encoding="utf-8")
        base_context = "# Actionable critique for this chunk only\nchunk0001: 修正语序。"
        observed_inputs: list[str] = []

        class RetryClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                observed_inputs.append(input_text)
                return completed_response("修订后的译文。"), 0.1

        RUNNER.process_chunk(
            self.task,
            RetryClient(),
            [],
            stages=("revision",),
            initial_text_path=initial,
            paper_context="# QC-CORRECTION RETRY 1\n修正上一候选。",
            paper_context_identity=base_context,
        )

        self.assertEqual(len(observed_inputs), 1)
        self.assertNotIn("ORIGINAL SOURCE:", observed_inputs[0])
        self.assertNotIn("ZEPHYRWORD", observed_inputs[0])

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                raise AssertionError("stable retry output must be reused without an API call")

        result = RUNNER.process_chunk(
            self.task,
            NoCallClient(),
            [],
            stages=("revision",),
            initial_text_path=initial,
            paper_context=base_context,
            paper_context_identity=base_context,
        )

        self.assertEqual(result["status"], "complete")
        status = json.loads(
            (self.article_dir / "chunk_status" / "chunk0001.json").read_text(
                encoding="utf-8"
            )
        )["stages"]["revision"]
        self.assertTrue(status["reused_after_request_contract_change"])
        self.assertEqual(
            status["paper_context_hash"],
            RUNNER.text_hash(base_context),
        )

    def test_failed_segmented_refinement_retry_omits_whole_source_context(self) -> None:
        (self.article_dir / "chunk0001.md").write_text(
            "Whole original source contains trailing 999 evidence.\n", encoding="utf-8"
        )
        initial = self.article_dir / "stage_revision_chunk0001.md"
        initial.write_text(
            "前导句。\n后文"
            + "".join(f"{{v{index}}}正文{index}。" for index in range(1, 26))
            + "\n",
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
                        "terminology": {
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
                stages=("terminology",),
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
            "前导句。\n后文"
            + "".join(f"{{v{index}}}正文{index}。" for index in range(1, 26))
            + "\n",
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
                        "terminology": {
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
                stages=("terminology",),
                initial_text_path=initial,
            )

        self.assertEqual(len(observed_inputs), 1)
        self.assertTrue(
            all("Whole original source remains valid context" in item for item in observed_inputs)
        )

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
        self.assertEqual(client.calls, 3)
        self.assertTrue(client.assert_no_anchor)
        status = json.loads(
            (self.article_dir / "chunk_status" / "chunk0001.json").read_text(
                encoding="utf-8"
            )
        )
        translate = status["stages"]["translate"]
        self.assertEqual(translate["structure_segment_count"], 3)
        self.assertEqual(len(translate["subrequests"]), 3)
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

    def test_translate_qc_retry_omits_paper_analysis_and_neighbor_context(self) -> None:
        (self.article_dir / "chunk0002.md").write_text(
            "Neighbor-only evidence contains 999 detectors.\n",
            encoding="utf-8",
        )
        observed_inputs: list[str] = []

        class FakeClient:
            def complete(self, _instructions: str, input_text: str, _maximum: int):
                observed_inputs.append(input_text)
                return completed_response("原始源段落。\n"), 0.1

        RUNNER.process_chunk(
            self.task,
            FakeClient(),
            [],
            stages=("translate",),
            paper_context=(
                "PAPER ANALYSIS CONTEXT WITH 12345\n\n"
                "# QC-CORRECTION RETRY 1\n"
                "Correct the reported structural defect without adding content."
            ),
        )

        self.assertEqual(len(observed_inputs), 1)
        self.assertNotIn("PAPER ANALYSIS CONTEXT", observed_inputs[0])
        self.assertNotIn("12345", observed_inputs[0])
        self.assertNotIn("Neighbor-only evidence", observed_inputs[0])
        self.assertNotIn("999", observed_inputs[0])

    def test_revision_checkpoint_is_not_reused_after_critique_context_changes(self) -> None:
        source = "Original source paragraph.\n"
        (self.article_dir / "chunk0001.md").write_text(source, encoding="utf-8")
        initial = self.article_dir / "stage2_chunk0001.md"
        initial.write_text("现有中文初稿。\n", encoding="utf-8")
        output = self.article_dir / "stage_revision_chunk0001.md"
        output.write_text("旧批评下的修订。\n", encoding="utf-8")
        status_dir = self.article_dir / "chunk_status"
        status_dir.mkdir()
        (status_dir / "chunk0001.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_id": "arxiv:allowed",
                    "chunk_id": "chunk0001",
                    "stages": {
                        "revision": {
                            "status": "complete",
                            "request_key": "old-key",
                            "output_hash": RUNNER.text_hash("旧批评下的修订。\n"),
                            "qc": {"ok": True, "failures": []},
                            "paper_context_hash": RUNNER.text_hash("old critique"),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        calls = 0

        class Client:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int):
                nonlocal calls
                calls += 1
                return completed_response("新批评下的修订。"), 0.1

        RUNNER.process_chunk(
            self.task,
            Client(),
            [],
            stages=("revision",),
            paper_context="new critique",
            initial_text_path=initial,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "新批评下的修订。\n")

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
        self.assertIn("citation marker", instructions)
        self.assertIn("exactly once and in the same order", instructions)

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

    def test_locked_term_contract_change_recovers_now_valid_rejected_candidate(self) -> None:
        source = "The observable cluster properties constrain mass.\n"
        candidate = "可观测星系团性质约束质量。\n"
        output = self.article_dir / "stage1_chunk0001.md"
        metadata = RUNNER.persist_rejected_candidate(
            self.article_dir,
            "chunk0001",
            "translate",
            "old-request-key",
            candidate,
            protected=False,
        )
        status = {
            "status": "failed",
            "request_key": "old-request-key",
            "error": "QC failed: locked_terms_mismatch",
            "qc": {"ok": False, "failures": ["locked_terms_mismatch"]},
            **metadata,
        }
        terms = [
            {
                "source": "observable",
                "target": "可观测量",
                "contextual_targets": [
                    {
                        "source_regex": r"\bobservable\s+(?:cluster\s+)?properties\b",
                        "target": "可观测",
                    }
                ],
            }
        ]

        recovered = RUNNER.recover_rejected_candidate(
            self.article_dir,
            source,
            output,
            status,
            "new-request-key",
            terms,
        )

        self.assertEqual(recovered, candidate)
        self.assertEqual(status["request_key"], "new-request-key")
        self.assertEqual(status["previous_request_key"], "old-request-key")
        self.assertTrue(status["recovered_after_locked_term_contract_change"])

    def test_rejected_numeric_range_candidate_is_repaired_without_an_api_call(self) -> None:
        source = "The gain is 2-to-3 orders of magnitude.\n"
        candidate = "增益高出2到-3个数量级。\n"
        output = self.article_dir / "stage1_chunk0001.md"
        metadata = RUNNER.persist_rejected_candidate(
            self.article_dir,
            "chunk0001",
            "translate",
            "same-request-key",
            candidate,
            protected=False,
        )
        status = {
            "status": "failed",
            "request_key": "same-request-key",
            "error": "QC failed: numbers_mismatch",
            "qc": {"ok": False, "failures": ["numbers_mismatch"]},
            **metadata,
        }

        recovered = RUNNER.recover_rejected_candidate(
            self.article_dir,
            source,
            output,
            status,
            "same-request-key",
            [],
        )

        self.assertEqual(recovered, "增益高出2到3个数量级。\n")
        self.assertEqual(output.read_text(encoding="utf-8"), recovered)
        self.assertTrue(status["recovered_from_rejected_candidate"])
        self.assertTrue(status["qc"]["ok"])

    def test_numeric_qc_candidate_recovers_after_request_contract_change(self) -> None:
        source = "The Phase-1 detector is ready.\n"
        candidate = "该Phase-1探测器已就绪。\n"
        output = self.article_dir / "stage1_chunk0001.md"
        metadata = RUNNER.persist_rejected_candidate(
            self.article_dir,
            "chunk0001",
            "translate",
            "old-request-key",
            candidate,
            protected=False,
        )
        status = {
            "status": "failed",
            "request_key": "old-request-key",
            "error": "QC failed: numbers_mismatch",
            "qc": {"ok": False, "failures": ["numbers_mismatch"]},
            **metadata,
        }

        recovered = RUNNER.recover_rejected_candidate(
            self.article_dir,
            source,
            output,
            status,
            "new-request-key",
            [],
        )

        self.assertEqual(recovered, candidate)
        self.assertEqual(output.read_text(encoding="utf-8"), candidate)
        self.assertEqual(status["previous_request_key"], "old-request-key")
        self.assertTrue(status["recovered_after_qc_contract_change"])

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

    def test_authorized_uncertain_replay_closes_persistent_risk_after_success(self) -> None:
        class RecordingGuard:
            usd_cny_rate = RUNNER.DEFAULT_USD_CNY_RATE

            def __init__(self) -> None:
                self.reservations = 0
                self.resolved: list[str] = []

            def reserve(self, _input: str, _maximum: int, *, uncertainty_key=None):
                self.reservations += 1
                self.last_key = uncertainty_key
                return f"reservation-{self.reservations}"

            def commit_estimate(self, _reservation: str) -> float:
                return 0.125

            def settle(self, _reservation: str, _usage: dict[str, object]) -> None:
                return None

            def unresolved_uncertain_cost(self, _uncertainty_key: str) -> float:
                return 0.125

            def resolve_uncertain(self, uncertainty_key: str) -> bool:
                self.resolved.append(uncertainty_key)
                return True

        class AmbiguousClient:
            def complete(self, _instructions: str, _input: str, _maximum: int):
                raise RUNNER.AmbiguousTransportError("response may have been generated")

        guard = RecordingGuard()
        first = RUNNER.process_chunk(
            self.task,
            AmbiguousClient(),
            [],
            stages=("translate",),
            budget_guard=guard,
        )
        self.assertEqual(first["status"], "uncertain")

        self.task["retry_uncertain"] = True

        class SuccessClient:
            def complete(self, _instructions: str, _input: str, _maximum: int):
                return completed_response("原文段落。\n"), 0.1

        second = RUNNER.process_chunk(
            self.task,
            SuccessClient(),
            [],
            stages=("translate",),
            budget_guard=guard,
        )

        self.assertEqual(second["status"], "complete")
        self.assertEqual(guard.reservations, 2)
        self.assertEqual(
            guard.resolved,
            ["arxiv:allowed:chunk0001:translate:segment0001"],
        )

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
    def test_client_curl_transport_has_process_level_timeout(self) -> None:
        client = RUNNER.DeepSeekClient("test-key", max_retries=0, transport="curl")
        completed = subprocess.CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout=json.dumps(chat_completion_response("ok")) + "\n200",
            stderr="",
        )
        with mock.patch.object(RUNNER.subprocess, "run", return_value=completed) as run:
            result, _latency = client.complete("instructions", "input", 2048)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(run.call_args.kwargs["timeout"], 180)
        self.assertIn("--max-time", run.call_args.args[0])

    def test_client_uses_bounded_request_timeout(self) -> None:
        client = RUNNER.DeepSeekClient("test-key", max_retries=0)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            chat_completion_response("ok")
        ).encode()
        with mock.patch.object(RUNNER.urllib.request, "urlopen", return_value=response) as urlopen:
            client.complete("instructions", "input", 2048)

        self.assertEqual(urlopen.call_args.kwargs["timeout"], 180)

    def test_client_serializes_style_batch_requests_in_json_object_mode(self) -> None:
        client = RUNNER.DeepSeekClient("test-key", max_retries=0)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            chat_completion_response('{"translations":{"chunk0001":"译文"}}')
        ).encode()
        with mock.patch.object(RUNNER.urllib.request, "urlopen", return_value=response) as urlopen:
            client.complete(
                "base\nSTYLE-BATCH JSON PROTOCOL",
                '{"protocol":"snowmass-style-batch-v1"}',
                2048,
            )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertIn("STYLE-BATCH JSON PROTOCOL", payload["messages"][0]["content"])
        self.assertEqual(RUNNER.API_URL, "https://api.deepseek.com/chat/completions")

    def test_client_retries_tls_handshake_eof_before_request(self) -> None:
        client = RUNNER.DeepSeekClient("test-key", max_retries=2)
        tls_eof = RUNNER.urllib.error.URLError(
            ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING")
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            chat_completion_response("ok")
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
        self.assertEqual(result["usage"]["input_tokens"], 10)
        self.assertEqual(result["usage"]["input_tokens_details"]["cached_tokens"], 1)
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
