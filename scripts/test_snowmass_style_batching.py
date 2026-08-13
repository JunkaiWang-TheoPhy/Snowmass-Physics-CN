#!/usr/bin/env python3
"""TDD tests for the Snowmass style batch planner and protocol."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("snowmass_style_batching.py")
RUNNER_MODULE_PATH = Path(__file__).with_name("run_snowmass_translation.py")


def load_batching():
    if not MODULE_PATH.exists():
        raise AssertionError("style batching planner is not implemented")
    spec = importlib.util.spec_from_file_location("snowmass_style_batching", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_runner():
    spec = importlib.util.spec_from_file_location("run_snowmass_translation", RUNNER_MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def item(chunk_id: str, protected_text: str):
    batching = load_batching()
    return batching.StyleBatchItem(
        chunk_id=chunk_id,
        protected_text=protected_text,
        source_hash=f"source-{chunk_id}",
        prior_hash=f"prior-{chunk_id}",
        glossary_text="source => target",
        context="chunk-local critique",
        item_key=f"item-{chunk_id}",
    )


class StyleBatchPlanningTests(unittest.TestCase):
    def test_parser_accepts_exactly_one_redundant_trailing_closing_brace(self) -> None:
        batching = load_batching()
        text = '{"translations":{"chunk0001":"译文"}}}'
        self.assertEqual(
            batching.parse_style_batch_response(text, ["chunk0001"]),
            {"chunk0001": "译文"},
        )

    def test_parser_rejects_other_trailing_content_and_multiple_braces(self) -> None:
        batching = load_batching()
        for suffix in ("explanation", "}}", "```", " extra"):
            with self.subTest(suffix=suffix), self.assertRaises(
                batching.StyleBatchProtocolError
            ):
                batching.parse_style_batch_response(
                    '{"translations":{"chunk0001":"译文"}}' + suffix,
                    ["chunk0001"],
                )

    def test_trailing_brace_salvage_rejects_duplicate_keys_and_nonobjects(self) -> None:
        batching = load_batching()
        candidates = (
            '{"translations":{"chunk0001":"甲","chunk0001":"乙"}}}',
            "1}",
            "[]}",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(
                batching.StyleBatchProtocolError
            ):
                batching.parse_style_batch_response(candidate, ["chunk0001"])

    def test_style_batch_instructions_override_plain_text_output_contract(self) -> None:
        batching = load_batching()
        instructions = batching.style_batch_instructions(
            "Output only the complete revised Chinese text, with no preface."
        )
        self.assertIn("STYLE-BATCH JSON PROTOCOL", instructions)
        self.assertIn('"translations"', instructions)
        self.assertIn("no Markdown code fence", instructions)
        self.assertTrue(instructions.rstrip().endswith("This JSON requirement overrides any earlier plain-text output instruction."))

    def test_rejected_response_path_cannot_escape_artifact_directory(self) -> None:
        batching = load_batching()
        with tempfile.TemporaryDirectory() as temporary:
            article_dir = Path(temporary) / "paper"
            metadata = batching._persist_rejected_batch_response(
                article_dir,
                "../../outside",
                "attempt-1",
                "not-json",
            )
            path = (article_dir / metadata["rejected_response_file"]).resolve()
            self.assertEqual(path.parent, (article_dir / "rejected_style_responses").resolve())
            self.assertNotIn("..", Path(metadata["rejected_response_file"]).parts)

    def test_twenty_five_items_plan_as_two_normal_batches(self) -> None:
        batching = load_batching()
        items = tuple(item(f"chunk{i:04d}", "甲" * 100) for i in range(25))
        batches = batching.plan_style_batches(items)
        self.assertEqual([len(batch.items) for batch in batches], [24, 1])

    def test_character_limit_closes_batch_before_overflow(self) -> None:
        batching = load_batching()
        items = (item("chunk0001", "a" * 10_000), item("chunk0002", "b" * 9_000))
        self.assertEqual([len(x.items) for x in batching.plan_style_batches(items)], [1, 1])

    def test_blank_or_malformed_chunk_id_is_rejected_at_all_batch_boundaries(self) -> None:
        batching = load_batching()
        for chunk_id in ("", " ", "chunk1", "chunk00001", "Chunk0001", "chunk000a"):
            with self.subTest(chunk_id=chunk_id):
                batch = batching.StyleBatch((item(chunk_id, "text"),))
                calls = {
                    "planner": lambda: batching.plan_style_batches(batch.items),
                    "payload": lambda: batching.build_style_batch_payload(batch),
                    "request_key": lambda: batching.style_batch_request_key(
                        batch=batch,
                        stage="anti_ai",
                        model="model-a",
                        instructions="clean",
                        max_output_tokens=128,
                    ),
                }
                for boundary, call in calls.items():
                    with self.subTest(boundary=boundary), self.assertRaises(ValueError):
                        call()

    def test_recovery_batches_have_at_most_eight_items(self) -> None:
        batching = load_batching()
        items = tuple(item(f"chunk{i:04d}", "text") for i in range(9))
        batches = batching.plan_style_batches(items, recovery=True)
        self.assertEqual([len(batch.items) for batch in batches], [8, 1])
        self.assertTrue(all(batch.recovery for batch in batches))

    def test_oversized_item_is_always_alone(self) -> None:
        batching = load_batching()
        items = (item("chunk0001", "a"), item("chunk0002", "b" * 18_001), item("chunk0003", "c"))
        batches = batching.plan_style_batches(items)
        self.assertEqual([len(batch.items) for batch in batches], [1, 1, 1])
        self.assertEqual(batches[1].items[0].chunk_id, "chunk0002")

    def test_stage_plan_projection_reports_exact_batches_and_worst_case_requests(self) -> None:
        batching = load_batching()
        plan = batching.StyleStagePlan(
            reused=("chunk0004",),
            local=("chunk0005",),
            model_items=(item("chunk0001", "甲"), item("chunk0002", "乙"), item("chunk0003", "丙")),
            normal_batches=(
                batching.StyleBatch((item("chunk0001", "甲"), item("chunk0002", "乙"))),
                batching.StyleBatch((item("chunk0003", "丙"),)),
            ),
            worst_case_requests=3,
        )

        self.assertEqual(
            batching.stage_plan_projection(plan),
            {
                "reused_chunks": ["chunk0004"],
                "local_chunks": ["chunk0005"],
                "model_chunks": ["chunk0001", "chunk0002", "chunk0003"],
                "normal_batches": [["chunk0001", "chunk0002"], ["chunk0003"]],
                "normal_batch_characters": [2, 1],
                "normal_requests": 2,
                "worst_case_requests": 3,
                "semantics": "exact",
            },
        )

    def test_stage_result_projection_reports_actual_request_counts(self) -> None:
        batching = load_batching()
        result = batching.StyleStageResult(
            planned_chunks=3,
            completed_chunks=3,
            reused_chunks=1,
            local_chunks=1,
            failed_chunks=0,
            normal_requests=1,
            recovery_requests=1,
            input_tokens=100,
            cached_tokens=10,
            output_tokens=50,
            total_tokens=150,
            total_cost_rmb=0.75,
        )

        self.assertEqual(
            batching.stage_result_projection(result),
            {
                "planned_chunks": 3,
                "completed_chunks": 3,
                "reused_chunks": 1,
                "local_chunks": 1,
                "failed_chunks": 0,
                "normal_requests": 1,
                "recovery_requests": 1,
                "input_tokens": 100,
                "cached_tokens": 10,
                "output_tokens": 50,
                "total_tokens": 150,
                "total_cost_rmb": 0.75,
            },
        )


class StyleBatchProtocolTests(unittest.TestCase):
    def test_response_requires_exact_nonblank_id_mapping(self) -> None:
        batching = load_batching()
        expected = ("chunk0001", "chunk0002")
        good = '{"translations":{"chunk0001":"甲","chunk0002":"乙"}}'
        self.assertEqual(set(batching.parse_style_batch_response(good, expected)), set(expected))
        for bad in (
            '{"translations":{"chunk0001":"甲"}}',
            '{"translations":{"chunk0001":"甲","chunk0002":"乙","chunk9999":"丙"}}',
            '{"translations":{"chunk0001":"甲","chunk0002":""}}',
        ):
            with self.assertRaises(batching.StyleBatchProtocolError):
                batching.parse_style_batch_response(bad, expected)

    def test_payload_uses_the_exact_style_batch_shape(self) -> None:
        batching = load_batching()
        batch = batching.StyleBatch(
            (
                item("chunk0001", "protected text"),
            )
        )
        self.assertEqual(
            batching.build_style_batch_payload(batch),
            {
                "protocol": "snowmass-style-batch-v1",
                "stage": "anti_ai",
                "chunks": [
                    {
                        "id": "chunk0001",
                        "text": "protected text",
                        "locked_terminology": "source => target",
                        "read_only_context": "chunk-local critique",
                    }
                ],
            },
        )

    def test_request_key_is_deterministic_and_covers_request_identity(self) -> None:
        batching = load_batching()
        first_batch = batching.StyleBatch((item("chunk0001", "甲"), item("chunk0002", "乙")))
        second_batch = batching.StyleBatch((item("chunk0002", "乙"), item("chunk0001", "甲")))

        def request_key(batch, **changes):
            values = {
                "batch": batch,
                "stage": "anti_ai",
                "model": "model-a",
                "instructions": "clean the prose",
                "max_output_tokens": 4096,
            }
            values.update(changes)
            return batching.style_batch_request_key(**values)

        self.assertEqual(request_key(first_batch), request_key(first_batch))
        self.assertNotEqual(request_key(first_batch), request_key(second_batch))
        self.assertNotEqual(request_key(first_batch), request_key(first_batch, stage="academic"))
        self.assertNotEqual(request_key(first_batch), request_key(first_batch, model="model-b"))
        self.assertNotEqual(
            request_key(first_batch), request_key(first_batch, instructions="different")
        )
        self.assertNotEqual(
            request_key(first_batch), request_key(first_batch, max_output_tokens=4097)
        )

    def test_malformed_expected_ids_are_rejected_even_when_the_mapping_matches(self) -> None:
        batching = load_batching()
        for chunk_id in ("", "chunk1", "chunk00001", "chunk000a"):
            with self.subTest(chunk_id=chunk_id), self.assertRaises(ValueError):
                batching.parse_style_batch_response(
                    json.dumps({"translations": {chunk_id: "甲"}}, ensure_ascii=False),
                    (chunk_id,),
                )

    def test_malformed_response_translation_keys_are_rejected(self) -> None:
        batching = load_batching()
        for chunk_id in ("", "chunk1", "chunk00001", "chunk000a"):
            with self.subTest(chunk_id=chunk_id), self.assertRaises(ValueError):
                batching.parse_style_batch_response(
                    json.dumps({"translations": {chunk_id: "甲"}}, ensure_ascii=False),
                    ("chunk0001",),
                )


class StylePreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.batching = load_batching()
        self.runner = load_runner()
        self.temporary = tempfile.TemporaryDirectory()
        self.article_dir = Path(self.temporary.name)
        self.chunk = {
            "id": "chunk0001",
            "source_file": "chunk0001.md",
            "output_file": "output_chunk0001.md",
        }
        (self.article_dir / self.chunk["source_file"]).write_text(
            "Original source paragraph.\n",
            encoding="utf-8",
        )
        self._write_stage_text("revision", "Prior refined paragraph.\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _status_path(self) -> Path:
        return self.article_dir / "chunk_status" / "chunk0001.json"

    def _chunk(self, chunk_id: str) -> dict[str, str]:
        return {
            "id": chunk_id,
            "source_file": f"{chunk_id}.md",
            "output_file": f"output_{chunk_id}.md",
        }

    def _write_stage_text(self, stage: str, text: str) -> Path:
        path = self.runner.stage_output_path(
            self.article_dir,
            self.chunk["id"],
            self.chunk["output_file"],
            stage,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _stage_status(self) -> dict[str, object]:
        return json.loads(self._status_path().read_text(encoding="utf-8"))["stages"]["anti_ai"]

    def _item_key(
        self,
        *,
        source: str,
        prior: str,
        glossary: list[dict[str, object]] | None = None,
        context: str = "",
        policy: str = "model_pipeline",
    ) -> str:
        glossary = glossary or []
        return self.runner.text_hash(
            json.dumps(
                {
                    "protocol": self.batching.STYLE_BATCH_PROTOCOL,
                    "stage": "anti_ai",
                    "chunk_id": self.chunk["id"],
                    "source_hash": self.runner.text_hash(source),
                    "prior_hash": self.runner.text_hash(prior),
                    "glossary": glossary,
                    "context_identity": context,
                    "policy": policy,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    def _write_checkpoint(
        self,
        *,
        item_key: str,
        policy: str,
        output_text: str,
    ) -> None:
        output_path = self._write_stage_text("anti_ai", output_text)
        status = {
            "schema_version": 1,
            "record_id": "arxiv:test",
            "chunk_id": self.chunk["id"],
            "source_file": self.chunk["source_file"],
            "stages": {
                "anti_ai": {
                    "status": "complete",
                    "item_key": item_key,
                    "execution_policy": policy,
                    "output_file": output_path.name,
                    "output_hash": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
                    "qc": {"ok": True, "failures": []},
                }
            },
        }
        self._status_path().parent.mkdir(parents=True, exist_ok=True)
        self._status_path().write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_stage_status(self, stage_status: dict[str, object]) -> None:
        status = {
            "schema_version": 1,
            "record_id": "arxiv:test",
            "chunk_id": self.chunk["id"],
            "source_file": self.chunk["source_file"],
            "stages": {
                "anti_ai": stage_status,
            },
        }
        self._status_path().parent.mkdir(parents=True, exist_ok=True)
        self._status_path().write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _prepare(
        self,
        *,
        task_factory=None,
        terms=None,
        context="chunk-local critique",
    ):
        return self.batching.prepare_style_items(
            article_dir=self.article_dir,
            chunks=(self.chunk,),
            task_factory=task_factory or (lambda _chunk: {"record_id": "arxiv:test"}),
            terms=terms or [],
            stage="anti_ai",
            input_stage="revision",
            context_factory=lambda _chunk: context,
        )

    def test_stale_prior_hash_schedules_a_model_item_and_keeps_checkpoint_metadata(self) -> None:
        old_prior = "Prior refined paragraph.\n"
        current_prior = "Changed refined paragraph.\n"
        source = "Original source paragraph.\n"
        self._write_stage_text("revision", current_prior)
        self._write_checkpoint(
            item_key=self._item_key(
                source=source,
                prior=old_prior,
                context="chunk-local critique",
            ),
            policy="model_pipeline",
            output_text="Old style output.\n",
        )

        plan = self._prepare()

        self.assertEqual(plan.reused, ())
        self.assertEqual(plan.local, ())
        self.assertEqual([item.chunk_id for item in plan.model_items], ["chunk0001"])
        self.assertEqual([len(batch.items) for batch in plan.normal_batches], [1])
        self.assertEqual(plan.worst_case_requests, 2)
        self.assertEqual(plan.model_items[0].prior_hash, self.runner.text_hash(current_prior))
        stage = self._stage_status()
        self.assertEqual(stage["execution_policy"], "model_pipeline")
        self.assertEqual(
            stage["output_hash"],
            hashlib.sha256("Old style output.\n".encode("utf-8")).hexdigest(),
        )

    def test_valid_batch_item_checkpoint_is_reused_without_paid_batches(self) -> None:
        source = "Original source paragraph.\n"
        prior = "Prior refined paragraph.\n"
        output_text = "Checkpoint style output.\n"
        self._write_checkpoint(
            item_key=self._item_key(
                source=source,
                prior=prior,
                context="chunk-local critique",
            ),
            policy="model_pipeline",
            output_text=output_text,
        )

        plan = self._prepare()

        self.assertEqual(plan.reused, ("chunk0001",))
        self.assertEqual(plan.local, ())
        self.assertEqual(plan.model_items, ())
        self.assertEqual(plan.normal_batches, ())
        self.assertEqual(plan.worst_case_requests, 0)
        stage = self._stage_status()
        self.assertEqual(stage["execution_policy"], "model_pipeline")
        self.assertEqual(
            stage["output_hash"],
            hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
        )

    def test_hard_exact_translation_writes_local_output_with_fixed_policy(self) -> None:
        fixed_translation = "统一运行页眉\n"

        plan = self._prepare(
            task_factory=lambda _chunk: {
                "record_id": "arxiv:test",
                "fixed_translation": fixed_translation,
                "fixed_translation_reason": "hard_exact_translation",
                "constraint_plan_sha256": "constraint-plan",
            }
        )

        self.assertEqual(plan.reused, ())
        self.assertEqual(plan.local, ("chunk0001",))
        self.assertEqual(plan.model_items, ())
        self.assertEqual(plan.worst_case_requests, 0)
        output_path = self.runner.stage_output_path(
            self.article_dir,
            self.chunk["id"],
            self.chunk["output_file"],
            "anti_ai",
        )
        self.assertEqual(output_path.read_text(encoding="utf-8"), fixed_translation)
        stage = self._stage_status()
        self.assertEqual(
            stage["execution_policy"],
            "fixed_translation:"
            + self.runner.text_hash(fixed_translation)
            + ":constraint-plan",
        )
        self.assertEqual(stage["decision"]["reason"], "hard_exact_translation")
        self.assertEqual(stage["output_hash"], self.runner.text_hash(fixed_translation))

    def test_reference_passthrough_writes_local_output_with_passthrough_policy(self) -> None:
        plan = self._prepare(
            task_factory=lambda _chunk: {
                "record_id": "arxiv:test",
                "passthrough": True,
                "passthrough_reason": "reference_section_passthrough",
            }
        )

        self.assertEqual(plan.reused, ())
        self.assertEqual(plan.local, ("chunk0001",))
        self.assertEqual(plan.model_items, ())
        self.assertEqual(plan.worst_case_requests, 0)
        output_path = self.runner.stage_output_path(
            self.article_dir,
            self.chunk["id"],
            self.chunk["output_file"],
            "anti_ai",
        )
        self.assertEqual(
            output_path.read_text(encoding="utf-8"),
            "Original source paragraph.\n",
        )
        stage = self._stage_status()
        self.assertEqual(
            stage["execution_policy"],
            "passthrough:reference_section_passthrough",
        )
        self.assertEqual(stage["decision"]["reason"], "reference_section_passthrough")
        self.assertEqual(
            stage["output_hash"],
            self.runner.text_hash("Original source paragraph.\n"),
        )

    def test_worst_case_requests_uses_eight_item_recovery_chunks_not_character_limited_recovery(self) -> None:
        chunks = tuple(self._chunk(f"chunk{i:04d}") for i in range(1, 10))
        for chunk in chunks:
            (self.article_dir / chunk["source_file"]).write_text(
                f"Source for {chunk['id']}.\n",
                encoding="utf-8",
            )
            self.runner.stage_output_path(
                self.article_dir,
                chunk["id"],
                chunk["output_file"],
                "revision",
            ).write_text("甲" * 10_000, encoding="utf-8")

        plan = self.batching.prepare_style_items(
            article_dir=self.article_dir,
            chunks=chunks,
            task_factory=lambda _chunk: {"record_id": "arxiv:test"},
            terms=[],
            stage="anti_ai",
            input_stage="revision",
            context_factory=lambda _chunk: "",
        )

        self.assertEqual(len(plan.model_items), 9)
        self.assertEqual(len(plan.normal_batches), 9)
        self.assertEqual(plan.worst_case_requests, 18)

    def test_worst_case_requests_tracks_recovery_character_caps_for_near_limit_chunks(self) -> None:
        chunks = tuple(self._chunk(f"chunk{i:04d}") for i in range(1, 4))
        for index, chunk in enumerate(chunks, 1):
            source_text = f"{index}-" + ("a" * 10_000) + "\n"
            (self.article_dir / chunk["source_file"]).write_text(
                source_text,
                encoding="utf-8",
            )
            self.runner.stage_output_path(
                self.article_dir,
                chunk["id"],
                chunk["output_file"],
                "revision",
            ).write_text(source_text, encoding="utf-8")

        plan = self.batching.prepare_style_items(
            article_dir=self.article_dir,
            chunks=chunks,
            task_factory=lambda _chunk: {"record_id": "arxiv:test"},
            terms=[],
            stage="anti_ai",
            input_stage="revision",
            context_factory=lambda _chunk: "",
        )

        self.assertEqual([len(batch.items) for batch in plan.normal_batches], [1, 1, 1])
        self.assertEqual(plan.worst_case_requests, 6)

    def test_passthrough_local_completion_clears_old_failed_stage_metadata(self) -> None:
        stale_fields = {
            "status": "failed",
            "request_key": "old-request",
            "error": "old error",
            "subrequests": [{"id": "sub-1"}],
            "response_output_hash": "response-hash",
            "structure_diagnostics": {"segments": 3},
            "max_output_tokens": 4096,
            "run_id": "run-123",
            "rejected_candidate_file": "rejected.md",
            "rejected_candidate_hash": "hash-a",
            "rejected_candidate_protected": True,
            "recovered_from_rejected_candidate": True,
            "recovery_previous_error": "previous failure",
            "invalid_structure_slot_file": "invalid-slot.md",
            "invalid_structure_slot_hash": "invalid-slot-hash",
            "response_id": "resp_old",
            "raw_response": {"id": "raw"},
            "usage": {"total_tokens": 100},
            "conservative_cost_rmb": "1.23",
            "uncertainty_key": "uncertain-key",
            "uncertainty_reservation_id": "reservation-id",
            "uncertain_replays": 2,
            "finished_at": "2026-08-13T00:00:00+00:00",
            "output_hash": "old-output-hash",
            "qc": {"ok": False, "failures": ["numbers_mismatch"]},
        }
        self._write_stage_status(stale_fields)

        plan = self._prepare(
            task_factory=lambda _chunk: {
                "record_id": "arxiv:test",
                "passthrough": True,
                "passthrough_reason": "reference_section_passthrough",
            }
        )

        self.assertEqual(plan.local, ("chunk0001",))
        stage = self._stage_status()
        self.assertEqual(stage["status"], "complete")
        for key in (
            "request_key",
            "error",
            "subrequests",
            "response_output_hash",
            "structure_diagnostics",
            "max_output_tokens",
            "run_id",
            "rejected_candidate_file",
            "rejected_candidate_hash",
            "rejected_candidate_protected",
            "recovered_from_rejected_candidate",
            "recovery_previous_error",
            "invalid_structure_slot_file",
            "invalid_structure_slot_hash",
            "response_id",
            "raw_response",
            "usage",
            "conservative_cost_rmb",
            "uncertainty_key",
            "uncertainty_reservation_id",
            "uncertain_replays",
            "finished_at",
        ):
            self.assertNotIn(key, stage)
        self.assertEqual(
            stage["output_hash"],
            self.runner.text_hash("Original source paragraph.\n"),
        )
        self.assertEqual(stage["qc"], {"ok": True, "failures": []})

    def test_fixed_translation_local_completion_clears_old_running_stage_metadata(self) -> None:
        stale_fields = {
            "status": "running",
            "request_key": "old-request",
            "error": "old error",
            "subrequests": [{"id": "sub-1"}],
            "response_output_hash": "response-hash",
            "structure_diagnostics": {"segments": 3},
            "max_output_tokens": 4096,
            "run_id": "run-123",
            "rejected_candidate_file": "rejected.md",
            "rejected_candidate_hash": "hash-a",
            "rejected_candidate_protected": False,
            "recovered_from_rejected_candidate": True,
            "recovery_previous_error": "previous failure",
            "invalid_structure_slot_file": "invalid-slot.md",
            "invalid_structure_slot_hash": "invalid-slot-hash",
            "response_id": "resp_old",
            "raw_response": {"id": "raw"},
            "usage": {"total_tokens": 100},
            "conservative_cost_rmb": "1.23",
            "uncertainty_key": "uncertain-key",
            "uncertainty_reservation_id": "reservation-id",
            "uncertain_replays": 2,
            "finished_at": "2026-08-13T00:00:00+00:00",
            "output_hash": "old-output-hash",
            "qc": {"ok": False, "failures": ["numbers_mismatch"]},
        }
        self._write_stage_status(stale_fields)
        fixed_translation = "统一运行页眉\n"

        plan = self._prepare(
            task_factory=lambda _chunk: {
                "record_id": "arxiv:test",
                "fixed_translation": fixed_translation,
                "fixed_translation_reason": "hard_exact_translation",
                "constraint_plan_sha256": "constraint-plan",
            }
        )

        self.assertEqual(plan.local, ("chunk0001",))
        stage = self._stage_status()
        self.assertEqual(stage["status"], "complete")
        for key in (
            "request_key",
            "error",
            "subrequests",
            "response_output_hash",
            "structure_diagnostics",
            "max_output_tokens",
            "run_id",
            "rejected_candidate_file",
            "rejected_candidate_hash",
            "rejected_candidate_protected",
            "recovered_from_rejected_candidate",
            "recovery_previous_error",
            "invalid_structure_slot_file",
            "invalid_structure_slot_hash",
            "response_id",
            "raw_response",
            "usage",
            "conservative_cost_rmb",
            "uncertainty_key",
            "uncertainty_reservation_id",
            "uncertain_replays",
            "finished_at",
        ):
            self.assertNotIn(key, stage)
        self.assertEqual(stage["output_hash"], self.runner.text_hash(fixed_translation))
        self.assertEqual(stage["qc"], {"ok": True, "failures": []})


class StyleExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.batching = load_batching()
        self.runner = load_runner()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.article_dir = Path(self.temporary.name)
        self.chunks = (
            {
                "id": "chunk0001",
                "source_file": "chunk0001.md",
                "output_file": "output_chunk0001.md",
            },
            {
                "id": "chunk0002",
                "source_file": "chunk0002.md",
                "output_file": "output_chunk0002.md",
            },
        )
        fixtures = {
            "chunk0001": "The yield is 14.\n",
            "chunk0002": "The count (final) is 7.\n",
        }
        for chunk in self.chunks:
            text = fixtures[chunk["id"]]
            (self.article_dir / chunk["source_file"]).write_text(text, encoding="utf-8")
            self.runner.stage_output_path(
                self.article_dir,
                chunk["id"],
                chunk["output_file"],
                "revision",
            ).write_text(text, encoding="utf-8")

    def _task(self, chunk: dict[str, str]) -> dict[str, str]:
        return {"record_id": f"arxiv:test:{chunk['id']}"}

    def _prepare(self, chunks=None):
        return self.batching.prepare_style_items(
            article_dir=self.article_dir,
            chunks=self.chunks if chunks is None else chunks,
            task_factory=self._task,
            terms=[],
            stage="anti_ai",
            input_stage="revision",
            context_factory=lambda _chunk: "chunk-local critique",
        )

    def _status(self, chunk_id: str) -> dict[str, object]:
        path = self.article_dir / "chunk_status" / f"{chunk_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))["stages"]["anti_ai"]

    def _output_path(self, chunk_id: str) -> Path:
        chunk = next(item for item in self.chunks if item["id"] == chunk_id)
        return self.runner.stage_output_path(
            self.article_dir,
            chunk_id,
            chunk["output_file"],
            "anti_ai",
        )

    def _batch_status(self) -> dict[str, object]:
        return json.loads((self.article_dir / "style_batch_status.json").read_text(encoding="utf-8"))

    def _batch_requests(self, stage: str = "anti_ai") -> list[dict[str, object]]:
        payload = self._batch_status()
        if "stages" in payload:
            return list(payload["stages"][stage]["requests"])
        return list(payload["requests"])

    def _cost_events(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (self.article_dir / "api_cost_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    @staticmethod
    def _response(response_id: str, translations: dict[str, str], *, input_tokens: int, output_tokens: int):
        return {
            "id": response_id,
            "status": "completed",
            "model": "fake-style-model",
            "output_text": json.dumps({"translations": translations}, ensure_ascii=False),
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }

    def test_mixed_qc_batch_commits_good_sibling_then_recovers_only_failed_id(self) -> None:
        class RecordingBudgetGuard:
            usd_cny_rate = self.runner.DEFAULT_USD_CNY_RATE

            def __init__(self) -> None:
                self.reservations: list[tuple[str, int, str | None]] = []
                self.settled: list[tuple[str, dict[str, object]]] = []
                self.committed: list[str] = []

            def reserve(self, input_text: str, maximum: int, *, uncertainty_key=None):
                reservation = f"reservation-{len(self.reservations) + 1}"
                self.reservations.append((input_text, maximum, uncertainty_key))
                return reservation

            def settle(self, reservation: str, usage: dict[str, object]) -> None:
                self.settled.append((reservation, usage))

            def commit_estimate(self, reservation: str) -> float:
                self.committed.append(reservation)
                return 0.25

            def resolve_uncertain(self, _uncertainty_key: str) -> bool:
                return True

            def snapshot(self) -> dict[str, object]:
                return {"stage_remaining_api_calls": 8}

        test_case = self

        plan = self._prepare()
        protected = {item.chunk_id: item.protected_text for item in plan.model_items}

        class FakeClient:
            def __init__(self) -> None:
                self.requested_ids: list[tuple[str, ...]] = []
                self.instructions: list[str] = []
                self.calls = 0

            def complete(self, instructions: str, input_text: str, _maximum: int):
                self.instructions.append(instructions)
                payload = json.loads(input_text)
                ids = tuple(chunk["id"] for chunk in payload["chunks"])
                self.requested_ids.append(ids)
                self.calls += 1
                if self.calls == 2:
                    test_case.assertTrue(test_case._output_path("chunk0001").exists())
                    test_case.assertFalse(test_case._output_path("chunk0002").exists())
                    test_case.assertEqual(test_case._status("chunk0001")["status"], "complete")
                    test_case.assertIn(test_case._status("chunk0002")["status"], {"failed", "running"})
                    rejected = Path(str(test_case._status("chunk0002")["rejected_candidate_file"]))
                    test_case.assertTrue((test_case.article_dir / rejected).exists())
                    return (
                        test_case._response(
                            "resp-recovery",
                            {"chunk0002": protected["chunk0002"]},
                            input_tokens=15,
                            output_tokens=5,
                        ),
                        0.2,
                    )
                return (
                    test_case._response(
                        "resp-normal",
                        {
                            "chunk0001": protected["chunk0001"],
                            "chunk0002": "The count final is [[SMU_0001_NUMBER_7902699be4]].\n",
                        },
                        input_tokens=30,
                        output_tokens=10,
                    ),
                    0.1,
                )

        guard = RecordingBudgetGuard()
        client = FakeClient()

        result = self.batching.execute_style_stage(
            article_dir=self.article_dir,
            chunks=self.chunks,
            task_factory=self._task,
            terms=[],
            stage="anti_ai",
            plan=plan,
            client=client,
            instructions="clean the prose",
            max_output_tokens=256,
            budget_guard=guard,
            run_id="run-mixed",
        )

        self.assertEqual(client.requested_ids, [("chunk0001", "chunk0002"), ("chunk0002",)])
        self.assertEqual(result.planned_chunks, 2)
        self.assertEqual(result.completed_chunks, 2)
        self.assertEqual(result.failed_chunks, 0)
        self.assertEqual(result.normal_requests, 1)
        self.assertEqual(result.recovery_requests, 1)
        self.assertTrue(all("STYLE-BATCH JSON PROTOCOL" in value for value in client.instructions))
        self.assertTrue(all(value.rstrip().endswith("This JSON requirement overrides any earlier plain-text output instruction.") for value in client.instructions))
        self.assertEqual(result.total_tokens, 60)
        self.assertEqual(self._status("chunk0001")["status"], "complete")
        self.assertEqual(self._status("chunk0002")["status"], "complete")
        self.assertNotIn("usage", self._status("chunk0001"))
        self.assertNotIn("usage", self._status("chunk0002"))
        batch_status = self._batch_status()
        self.assertEqual(
            [request["response_id"] for request in batch_status["stages"]["anti_ai"]["requests"]],
            ["resp-normal", "resp-recovery"],
        )
        self.assertEqual([event["event_id"] for event in self._cost_events()], ["resp-normal", "resp-recovery"])
        self.assertEqual(len(guard.settled), 2)
        self.assertEqual(guard.committed, [])

    def test_batch_status_groups_requests_by_stage_and_migrates_legacy_shape(self) -> None:
        path = self.article_dir / "style_batch_status.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "stage": "anti_ai",
                    "requests": [
                        {
                            "attempt_id": "legacy-1",
                            "request_key": "legacy-request",
                            "status": "settled",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        self.batching._persist_batch_request(
            self.article_dir,
            "academic",
            {
                "attempt_id": "academic-1",
                "request_key": "academic-request",
                "status": "running",
            },
        )

        batch_status = self._batch_status()
        self.assertEqual(batch_status["schema_version"], 2)
        self.assertNotIn("stage", batch_status)
        self.assertNotIn("requests", batch_status)
        self.assertEqual(
            [request["attempt_id"] for request in batch_status["stages"]["anti_ai"]["requests"]],
            ["legacy-1"],
        )
        self.assertEqual(
            [request["attempt_id"] for request in batch_status["stages"]["academic"]["requests"]],
            ["academic-1"],
        )

    def test_protocol_failure_commits_nothing_before_retrying_the_whole_failed_request(self) -> None:
        class RecordingBudgetGuard:
            usd_cny_rate = self.runner.DEFAULT_USD_CNY_RATE

            def reserve(self, _input: str, _maximum: int, *, uncertainty_key=None):
                return "reservation"

            def settle(self, _reservation: str, _usage: dict[str, object]) -> None:
                return None

            def commit_estimate(self, _reservation: str) -> float:
                return 0.25

            def resolve_uncertain(self, _uncertainty_key: str) -> bool:
                return True

            def snapshot(self) -> dict[str, object]:
                return {"stage_remaining_api_calls": 8}

        test_case = self

        plan = self._prepare()
        protected = {item.chunk_id: item.protected_text for item in plan.model_items}

        class FakeClient:
            def __init__(self) -> None:
                self.requested_ids: list[tuple[str, ...]] = []
                self.calls = 0

            def complete(self, _instructions: str, input_text: str, _maximum: int):
                payload = json.loads(input_text)
                ids = tuple(chunk["id"] for chunk in payload["chunks"])
                self.requested_ids.append(ids)
                self.calls += 1
                if self.calls == 2:
                    test_case.assertFalse(test_case._output_path("chunk0001").exists())
                    test_case.assertFalse(test_case._output_path("chunk0002").exists())
                    return (
                        test_case._response(
                            "resp-recovery",
                            {
                                "chunk0001": protected["chunk0001"],
                                "chunk0002": protected["chunk0002"],
                            },
                            input_tokens=25,
                            output_tokens=10,
                        ),
                        0.2,
                    )
                return (
                    test_case._response(
                        "resp-malformed",
                        {"chunk0001": protected["chunk0001"]},
                        input_tokens=25,
                        output_tokens=10,
                    ),
                    0.1,
                )

        client = FakeClient()
        result = self.batching.execute_style_stage(
            article_dir=self.article_dir,
            chunks=self.chunks,
            task_factory=self._task,
            terms=[],
            stage="anti_ai",
            plan=plan,
            client=client,
            instructions="clean the prose",
            max_output_tokens=256,
            budget_guard=RecordingBudgetGuard(),
            run_id="run-protocol",
        )

        self.assertEqual(result.completed_chunks, 2)
        self.assertEqual(result.recovery_requests, 1)
        self.assertEqual(client.requested_ids, [("chunk0001", "chunk0002"), ("chunk0001", "chunk0002")])
        requests = self._batch_requests()
        self.assertEqual(len(requests), 2)
        self.assertEqual([request["request_key"] for request in requests], [requests[0]["request_key"]] * 2)
        self.assertNotEqual(requests[0]["attempt_id"], requests[1]["attempt_id"])
        self.assertEqual(
            [(request["status"], request["recovery"], request["attempt_ordinal"]) for request in requests],
            [("protocol_failed", False, 1), ("settled", True, 2)],
        )
        rejected_path = self.article_dir / requests[0]["rejected_response_file"]
        self.assertTrue(rejected_path.is_file())
        self.assertEqual(requests[0]["rejected_response_hash"], self.runner.text_hash(rejected_path.read_text(encoding="utf-8")))

    def test_replays_matching_paid_rejected_response_offline_before_client_call(self) -> None:
        plan = self._prepare()
        protected = {item.chunk_id: item.protected_text for item in plan.model_items}
        instructions = self.batching.style_batch_instructions("clean the prose")
        batch = plan.normal_batches[0]
        request_key = self.batching.style_batch_request_key(
            batch=batch,
            stage="anti_ai",
            model="fake-style-model",
            instructions=instructions,
            max_output_tokens=256,
        )
        attempt_id = f"{request_key}:normal:1"
        response_text = json.dumps(
            {"translations": protected}, ensure_ascii=False
        ) + "}"
        rejected = self.batching._persist_rejected_batch_response(
            self.article_dir,
            "anti_ai",
            attempt_id,
            response_text,
        )
        self.batching._persist_batch_request(
            self.article_dir,
            "anti_ai",
            {
                "attempt_id": attempt_id,
                "attempt_ordinal": 1,
                "stage": "anti_ai",
                "request_key": request_key,
                "recovery": False,
                "chunk_ids": [item.chunk_id for item in batch.items],
                "status": "protocol_failed",
                "response_id": "resp-already-paid",
                **rejected,
            },
        )

        class NoCallClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, _instructions: str, _input_text: str, _maximum: int):
                self.calls += 1
                raise AssertionError("a verified paid response must be replayed offline")

        client = NoCallClient()
        result = self.batching.execute_style_stage(
            article_dir=self.article_dir,
            chunks=self.chunks,
            task_factory=self._task,
            terms=[],
            stage="anti_ai",
            plan=plan,
            client=client,
            instructions="clean the prose",
            max_output_tokens=256,
            budget_guard=None,
            run_id="run-offline-replay",
            model="fake-style-model",
        )

        self.assertEqual(client.calls, 0)
        self.assertEqual(result.normal_requests, 0)
        self.assertEqual(result.recovery_requests, 0)
        self.assertEqual(result.total_tokens, 0)
        self.assertEqual(result.total_cost_rmb, 0.0)
        self.assertTrue(self._output_path("chunk0001").is_file())
        self.assertTrue(self._output_path("chunk0002").is_file())
        request = self._batch_requests()[0]
        self.assertEqual(request["status"], "recovered_offline")
        self.assertEqual(request["offline_recovery_protocol"], self.batching.STYLE_BATCH_PROTOCOL)

        for chunk in self.chunks:
            self._output_path(chunk["id"]).unlink()
        rebuilt_plan = self._prepare()
        rebuilt = self.batching.execute_style_stage(
            article_dir=self.article_dir,
            chunks=self.chunks,
            task_factory=self._task,
            terms=[],
            stage="anti_ai",
            plan=rebuilt_plan,
            client=client,
            instructions="clean the prose",
            max_output_tokens=256,
            budget_guard=None,
            run_id="run-offline-replay-again",
            model="fake-style-model",
        )
        self.assertEqual(client.calls, 0)
        self.assertEqual(rebuilt.normal_requests, 0)
        self.assertTrue(self._output_path("chunk0001").is_file())
        self.assertTrue(self._output_path("chunk0002").is_file())

    def test_offline_replay_rejects_response_file_outside_dedicated_store(self) -> None:
        plan = self._prepare()
        protected = {item.chunk_id: item.protected_text for item in plan.model_items}
        instructions = self.batching.style_batch_instructions("clean the prose")
        batch = plan.normal_batches[0]
        request_key = self.batching.style_batch_request_key(
            batch=batch,
            stage="anti_ai",
            model="fake-style-model",
            instructions=instructions,
            max_output_tokens=256,
        )
        response_text = json.dumps({"translations": protected}, ensure_ascii=False) + "}"
        sibling_path = self.article_dir / "untrusted-response.txt"
        sibling_path.write_text(response_text, encoding="utf-8")
        self.batching._persist_batch_request(
            self.article_dir,
            "anti_ai",
            {
                "attempt_id": f"{request_key}:normal:1",
                "attempt_ordinal": 1,
                "stage": "anti_ai",
                "request_key": request_key,
                "recovery": False,
                "chunk_ids": [item.chunk_id for item in batch.items],
                "status": "protocol_failed",
                "response_id": "resp-untrusted",
                "rejected_response_file": sibling_path.name,
                "rejected_response_hash": self.runner.text_hash(response_text),
            },
        )

        test_case = self

        class ValidClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, _instructions: str, _input_text: str, _maximum: int):
                self.calls += 1
                return (
                    test_case._response(
                        "resp-new",
                        protected,
                        input_tokens=10,
                        output_tokens=5,
                    ),
                    0.1,
                )

        client = ValidClient()
        result = self.batching.execute_style_stage(
            article_dir=self.article_dir,
            chunks=self.chunks,
            task_factory=self._task,
            terms=[],
            stage="anti_ai",
            plan=plan,
            client=client,
            instructions="clean the prose",
            max_output_tokens=256,
            budget_guard=None,
            run_id="run-untrusted-path",
            model="fake-style-model",
        )
        self.assertEqual(client.calls, 1)
        self.assertEqual(result.normal_requests, 1)

    def test_rejects_before_first_client_call_when_remaining_calls_cannot_cover_worst_case(self) -> None:
        from scripts import snowmass_batch_budget as budget_module

        class SnapshotBudgetGuard:
            def __init__(self) -> None:
                self.reserve_calls = 0

            def snapshot(self) -> dict[str, object]:
                return {"stage_remaining_api_calls": 1}

            def reserve(self, _input: str, _maximum: int, *, uncertainty_key=None):
                self.reserve_calls += 1
                raise AssertionError("reserve must not run after preflight rejection")

        class NoCallClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, _instructions: str, _input_text: str, _maximum: int):
                self.calls += 1
                raise AssertionError("client.complete must not run after preflight rejection")

        plan = self._prepare()
        budget_guard = SnapshotBudgetGuard()
        client = NoCallClient()

        with self.assertRaises(budget_module.RequestLimitExceededError):
            self.batching.execute_style_stage(
                article_dir=self.article_dir,
                chunks=self.chunks,
                task_factory=self._task,
                terms=[],
                stage="anti_ai",
                plan=plan,
                client=client,
                instructions="clean the prose",
                max_output_tokens=256,
                budget_guard=budget_guard,
                run_id="run-preflight",
            )

        self.assertEqual(client.calls, 0)
        self.assertEqual(budget_guard.reserve_calls, 0)

    def test_ambiguous_transport_commits_estimate_once_and_persists_uncertain_batch(self) -> None:
        single_chunk = self.chunks[:1]
        test_case = self

        class RecordingBudgetGuard:
            usd_cny_rate = self.runner.DEFAULT_USD_CNY_RATE

            def __init__(self) -> None:
                self.reservations: list[str] = []
                self.committed: list[str] = []
                self.settled: list[str] = []

            def reserve(self, _input: str, _maximum: int, *, uncertainty_key=None):
                reservation = f"reservation-{len(self.reservations) + 1}"
                self.reservations.append(reservation)
                return reservation

            def commit_estimate(self, reservation: str) -> float:
                self.committed.append(reservation)
                return 0.25

            def settle(self, reservation: str, _usage: dict[str, object]) -> None:
                self.settled.append(reservation)

            def snapshot(self) -> dict[str, object]:
                return {"stage_remaining_api_calls": 8}

        class AmbiguousClient:
            def __init__(self) -> None:
                self.requested_ids: list[tuple[str, ...]] = []

            def complete(self, _instructions: str, input_text: str, _maximum: int):
                payload = json.loads(input_text)
                self.requested_ids.append(tuple(chunk["id"] for chunk in payload["chunks"]))
                raise test_case.batching.runner.AmbiguousTransportError(
                    "response may have been generated"
                )

        plan = self._prepare(single_chunk)
        guard = RecordingBudgetGuard()
        client = AmbiguousClient()

        with self.assertRaises(self.batching.runner.AmbiguousTransportError):
            self.batching.execute_style_stage(
                article_dir=self.article_dir,
                chunks=single_chunk,
                task_factory=self._task,
                terms=[],
                stage="anti_ai",
                plan=plan,
                client=client,
                instructions="clean the prose",
                max_output_tokens=256,
                budget_guard=guard,
                run_id="run-ambiguous",
            )

        self.assertEqual(client.requested_ids, [("chunk0001",)])
        self.assertEqual(guard.committed, ["reservation-1"])
        self.assertEqual(guard.settled, [])
        self.assertEqual(self._status("chunk0001")["status"], "uncertain")
        self.assertEqual(
            self._batch_status()["stages"]["anti_ai"]["requests"][0]["status"],
            "uncertain",
        )
        events = self._cost_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "style_batch_ambiguous_transport_reservation")

    def test_failed_transport_commits_estimate_once_marks_batch_failed_and_clears_running_chunks(self) -> None:
        class RecordingBudgetGuard:
            usd_cny_rate = self.runner.DEFAULT_USD_CNY_RATE

            def __init__(self) -> None:
                self.reservations: list[str] = []
                self.committed: list[str] = []
                self.settled: list[str] = []

            def reserve(self, _input: str, _maximum: int, *, uncertainty_key=None):
                reservation = f"reservation-{len(self.reservations) + 1}"
                self.reservations.append(reservation)
                return reservation

            def commit_estimate(self, reservation: str) -> float:
                self.committed.append(reservation)
                return 0.25

            def settle(self, reservation: str, _usage: dict[str, object]) -> None:
                self.settled.append(reservation)

            def snapshot(self) -> dict[str, object]:
                return {"stage_remaining_api_calls": 8}

        class ExplodingClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, _instructions: str, _input_text: str, _maximum: int):
                self.calls += 1
                raise RuntimeError("socket closed")

        plan = self._prepare()
        guard = RecordingBudgetGuard()
        client = ExplodingClient()

        with self.assertRaisesRegex(RuntimeError, "socket closed"):
            self.batching.execute_style_stage(
                article_dir=self.article_dir,
                chunks=self.chunks,
                task_factory=self._task,
                terms=[],
                stage="anti_ai",
                plan=plan,
                client=client,
                instructions="clean the prose",
                max_output_tokens=256,
                budget_guard=guard,
                run_id="run-transport-failure",
            )

        self.assertEqual(client.calls, 1)
        self.assertEqual(guard.committed, ["reservation-1"])
        self.assertEqual(guard.settled, [])
        requests = self._batch_requests()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["status"], "failed")
        self.assertEqual(requests[0]["conservative_cost_rmb"], 0.25)
        self.assertEqual(self._status("chunk0001")["status"], "failed")
        self.assertEqual(self._status("chunk0002")["status"], "failed")
        self.assertEqual(self._status("chunk0001")["error"], "RuntimeError('socket closed')")
        self.assertEqual(self._status("chunk0002")["error"], "RuntimeError('socket closed')")
        events = self._cost_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "style_batch_failed_transport_reservation")

    def test_reserve_failure_does_not_commit_missing_reservation(self) -> None:
        class ReserveFailingBudgetGuard:
            def __init__(self) -> None:
                self.commit_calls = 0

            def reserve(self, _input: str, _maximum: int, *, uncertainty_key=None):
                raise RuntimeError("reserve gate closed")

            def commit_estimate(self, reservation: str) -> float:
                self.commit_calls += 1
                raise AssertionError(f"commit_estimate must not run for {reservation!r}")

            def snapshot(self) -> dict[str, object]:
                return {"stage_remaining_api_calls": 8}

        class NoCallClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, _instructions: str, _input_text: str, _maximum: int):
                self.calls += 1
                raise AssertionError("client.complete must not run when reserve fails")

        guard = ReserveFailingBudgetGuard()
        client = NoCallClient()

        with self.assertRaisesRegex(RuntimeError, "reserve gate closed"):
            self.batching.execute_style_stage(
                article_dir=self.article_dir,
                chunks=self.chunks,
                task_factory=self._task,
                terms=[],
                stage="anti_ai",
                plan=self._prepare(),
                client=client,
                instructions="clean the prose",
                max_output_tokens=256,
                budget_guard=guard,
                run_id="run-reserve-failure",
            )

        self.assertEqual(client.calls, 0)
        self.assertEqual(guard.commit_calls, 0)
        self.assertFalse((self.article_dir / "api_cost_ledger.jsonl").exists())

    def test_unresolved_ambiguous_request_blocks_replay_before_second_client_call(self) -> None:
        single_chunk = self.chunks[:1]
        test_case = self

        class RecordingBudgetGuard:
            usd_cny_rate = self.runner.DEFAULT_USD_CNY_RATE

            def __init__(self) -> None:
                self.reservations = 0
                self.commits = 0

            def reserve(self, _input: str, _maximum: int, *, uncertainty_key=None):
                self.reservations += 1
                return f"reservation-{self.reservations}"

            def commit_estimate(self, reservation: str) -> float:
                self.commits += 1
                return 0.25

            def snapshot(self) -> dict[str, object]:
                return {"stage_remaining_api_calls": 8}

        class AmbiguousOnceClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, _instructions: str, input_text: str, _maximum: int):
                payload = json.loads(input_text)
                self.calls += 1
                test_case.assertEqual(tuple(chunk["id"] for chunk in payload["chunks"]), ("chunk0001",))
                raise test_case.batching.runner.AmbiguousTransportError("response may have been generated")

        plan = self._prepare(single_chunk)
        guard = RecordingBudgetGuard()
        client = AmbiguousOnceClient()

        with self.assertRaises(self.batching.runner.AmbiguousTransportError):
            self.batching.execute_style_stage(
                article_dir=self.article_dir,
                chunks=single_chunk,
                task_factory=self._task,
                terms=[],
                stage="anti_ai",
                plan=plan,
                client=client,
                instructions="clean the prose",
                max_output_tokens=256,
                budget_guard=guard,
                run_id="run-uncertain-1",
            )

        with self.assertRaisesRegex(
            RuntimeError,
            r"style stage anti_ai has unresolved uncertain paid request for arxiv:test:chunk0001; explicit retry authorization required",
        ):
            self.batching.execute_style_stage(
                article_dir=self.article_dir,
                chunks=single_chunk,
                task_factory=self._task,
                terms=[],
                stage="anti_ai",
                plan=plan,
                client=client,
                instructions="clean the prose",
                max_output_tokens=256,
                budget_guard=guard,
                run_id="run-uncertain-2",
            )

        self.assertEqual(client.calls, 1)
        self.assertEqual(guard.reservations, 1)

    def test_recovery_exhaustion_error_mentions_stage_and_record_id(self) -> None:
        single_chunk = self.chunks[:1]
        plan = self._prepare(single_chunk)

        class MalformedClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, _instructions: str, _input_text: str, _maximum: int):
                self.calls += 1
                return (
                    {
                        "id": f"resp-{self.calls}",
                        "status": "completed",
                        "model": "fake-style-model",
                        "output_text": json.dumps({"translations": {}}, ensure_ascii=False),
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "total_tokens": 15,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens_details": {"reasoning_tokens": 0},
                        },
                    },
                    0.1,
                )

        client = MalformedClient()
        with self.assertRaisesRegex(
            RuntimeError,
            r"style stage anti_ai failed after one recovery pass for arxiv:test:chunk0001: chunk0001",
        ):
            self.batching.execute_style_stage(
                article_dir=self.article_dir,
                chunks=single_chunk,
                task_factory=self._task,
                terms=[],
                stage="anti_ai",
                plan=plan,
                client=client,
                instructions="clean the prose",
                max_output_tokens=256,
                budget_guard=None,
                run_id="run-recovery-exhausted",
            )

        self.assertEqual(client.calls, 2)

    def test_recovery_requests_stay_within_the_planned_near_limit_ceiling(self) -> None:
        chunks = tuple(
            {
                "id": f"chunk{i:04d}",
                "source_file": f"chunk{i:04d}.md",
                "output_file": f"output_chunk{i:04d}.md",
            }
            for i in range(1, 4)
        )
        for chunk in chunks:
            source_text = f"{chunk['id']} " + ("a" * 10_000) + "\n"
            (self.article_dir / chunk["source_file"]).write_text(
                source_text,
                encoding="utf-8",
            )
            self.runner.stage_output_path(
                self.article_dir,
                chunk["id"],
                chunk["output_file"],
                "revision",
            ).write_text(source_text, encoding="utf-8")

        plan = self.batching.prepare_style_items(
            article_dir=self.article_dir,
            chunks=chunks,
            task_factory=self._task,
            terms=[],
            stage="anti_ai",
            input_stage="revision",
            context_factory=lambda _chunk: "chunk-local critique",
        )

        class CeilingBudgetGuard:
            usd_cny_rate = self.runner.DEFAULT_USD_CNY_RATE

            def reserve(self, _input: str, _maximum: int, *, uncertainty_key=None):
                return "reservation"

            def settle(self, _reservation: str, _usage: dict[str, object]) -> None:
                return None

            def commit_estimate(self, _reservation: str) -> float:
                return 0.25

            def resolve_uncertain(self, _uncertainty_key: str) -> bool:
                return True

            def snapshot(self) -> dict[str, object]:
                return {"stage_remaining_api_calls": 16}

        class SplitFailureThenRecoveryClient:
            def __init__(self, plan):
                self.plan = plan
                self.calls = 0
                self.requested_ids: list[tuple[str, ...]] = []

            def complete(self, _instructions: str, input_text: str, _maximum: int):
                payload = json.loads(input_text)
                ids = tuple(chunk["id"] for chunk in payload["chunks"])
                self.requested_ids.append(ids)
                self.calls += 1
                if self.calls <= 3:
                    return (
                        {
                            "id": f"resp-{self.calls}",
                            "status": "completed",
                            "model": "fake-style-model",
                            "output_text": json.dumps({"translations": {}}, ensure_ascii=False),
                            "usage": {
                                "input_tokens": 10,
                                "input_tokens_details": {"cached_tokens": 0},
                                "output_tokens": 5,
                                "output_tokens_details": {"reasoning_tokens": 0},
                                "total_tokens": 15,
                            },
                        },
                        0.1,
                    )
                item = next(item for item in self.plan.model_items if item.chunk_id == ids[0])
                return (
                    {
                        "id": f"resp-{self.calls}",
                        "status": "completed",
                        "model": "fake-style-model",
                        "output_text": json.dumps(
                            {"translations": {ids[0]: item.protected_text}},
                            ensure_ascii=False,
                        ),
                        "usage": {
                            "input_tokens": 10,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 5,
                            "output_tokens_details": {"reasoning_tokens": 0},
                            "total_tokens": 15,
                        },
                    },
                    0.1,
                )

        client = SplitFailureThenRecoveryClient(plan)
        result = self.batching.execute_style_stage(
            article_dir=self.article_dir,
            chunks=chunks,
            task_factory=self._task,
            terms=[],
            stage="anti_ai",
            plan=plan,
            client=client,
            instructions="clean the prose",
            max_output_tokens=256,
            budget_guard=CeilingBudgetGuard(),
            run_id="run-near-limit",
        )

        self.assertEqual(result.normal_requests, 3)
        self.assertEqual(result.recovery_requests, 3)
        self.assertLessEqual(result.normal_requests + result.recovery_requests, plan.worst_case_requests)
