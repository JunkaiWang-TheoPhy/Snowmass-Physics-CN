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
