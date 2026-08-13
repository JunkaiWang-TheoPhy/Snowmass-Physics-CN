#!/usr/bin/env python3
"""TDD tests for the Snowmass style batch planner and protocol."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).with_name("snowmass_style_batching.py")


def load_batching():
    if not MODULE_PATH.exists():
        raise AssertionError("style batching planner is not implemented")
    spec = importlib.util.spec_from_file_location("snowmass_style_batching", MODULE_PATH)
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
