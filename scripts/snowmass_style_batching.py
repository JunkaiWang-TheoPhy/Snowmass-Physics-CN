#!/usr/bin/env python3
"""Pure planning and response protocol for Snowmass style batches."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable

from scripts.run_snowmass_translation import text_hash


STYLE_BATCH_PROTOCOL = "snowmass-style-batch-v1"
NORMAL_BATCH_CHUNKS = 24
NORMAL_BATCH_CHARACTERS = 18_000
RECOVERY_BATCH_CHUNKS = 8


@dataclass(frozen=True)
class StyleBatchItem:
    chunk_id: str
    protected_text: str
    source_hash: str
    prior_hash: str
    glossary_text: str
    context: str
    item_key: str


@dataclass(frozen=True)
class StyleBatch:
    items: tuple[StyleBatchItem, ...]
    recovery: bool = False


class StyleBatchProtocolError(ValueError):
    """Raised when a style-batch response violates the exact-ID protocol."""


def plan_style_batches(
    items: Iterable[StyleBatchItem],
    *,
    recovery: bool = False,
) -> tuple[StyleBatch, ...]:
    """Partition items in order, closing before either normal limit is exceeded."""

    item_list = tuple(items)
    ids = [item.chunk_id for item in item_list]
    if len(ids) != len(set(ids)):
        raise ValueError("style batch items must have unique chunk IDs")

    max_chunks = RECOVERY_BATCH_CHUNKS if recovery else NORMAL_BATCH_CHUNKS
    batches: list[StyleBatch] = []
    current: list[StyleBatchItem] = []
    current_characters = 0

    for batch_item in item_list:
        item_characters = len(batch_item.protected_text)
        oversized = item_characters > NORMAL_BATCH_CHARACTERS
        would_overflow = current and (
            len(current) >= max_chunks
            or current_characters + item_characters > NORMAL_BATCH_CHARACTERS
        )
        if would_overflow:
            batches.append(StyleBatch(tuple(current), recovery=recovery))
            current = []
            current_characters = 0

        if oversized:
            batches.append(StyleBatch((batch_item,), recovery=recovery))
            continue

        current.append(batch_item)
        current_characters += item_characters

    if current:
        batches.append(StyleBatch(tuple(current), recovery=recovery))
    return tuple(batches)


def build_style_batch_payload(
    batch: StyleBatch,
    *,
    stage: str = "anti_ai",
) -> dict[str, object]:
    """Build the stable JSON object sent for one style batch."""

    if not stage:
        raise ValueError("style batch stage must be non-empty")
    return {
        "protocol": STYLE_BATCH_PROTOCOL,
        "stage": stage,
        "chunks": [
            {
                "id": item.chunk_id,
                "text": item.protected_text,
                "locked_terminology": item.glossary_text,
                "read_only_context": item.context,
            }
            for item in batch.items
        ],
    }


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StyleBatchProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_style_batch_response(
    response_text: str,
    expected_ids: Iterable[str],
) -> dict[str, str]:
    """Parse a response whose translations map must match expected IDs exactly."""

    expected = tuple(expected_ids)
    if len(expected) != len(set(expected)):
        raise StyleBatchProtocolError("expected IDs must be unique")
    try:
        response = json.loads(
            response_text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON constant: {value}")
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StyleBatchProtocolError("response must be standard JSON") from exc
    if not isinstance(response, dict) or not isinstance(response.get("translations"), dict):
        raise StyleBatchProtocolError("response must contain a translations object")

    translations = response["translations"]
    if set(translations) != set(expected):
        raise StyleBatchProtocolError("translations must contain exactly the expected IDs")
    if any(not isinstance(value, str) or not value.strip() for value in translations.values()):
        raise StyleBatchProtocolError("translations must contain nonblank strings")
    return dict(translations)


def style_batch_request_key(
    *,
    batch: StyleBatch,
    stage: str,
    model: str,
    instructions: str,
    max_output_tokens: int,
) -> str:
    """Return a deterministic identity for the complete style-batch request."""

    payload = build_style_batch_payload(batch, stage=stage)
    serialized_payload = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity = {
        "protocol": STYLE_BATCH_PROTOCOL,
        "stage": stage,
        "model": model,
        "item_keys": [item.item_key for item in batch.items],
        "instructions": instructions,
        "payload": serialized_payload,
        "max_output_tokens": max_output_tokens,
    }
    serialized_identity = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text_hash(serialized_identity)
