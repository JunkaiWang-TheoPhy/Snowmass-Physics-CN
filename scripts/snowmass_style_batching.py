#!/usr/bin/env python3
"""Pure planning and response protocol for Snowmass style batches."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from scripts import run_snowmass_translation as runner
from scripts.run_snowmass_translation import text_hash


STYLE_BATCH_PROTOCOL = "snowmass-style-batch-v1"
NORMAL_BATCH_CHUNKS = 24
NORMAL_BATCH_CHARACTERS = 18_000
RECOVERY_BATCH_CHUNKS = 8
_LOCAL_STAGE_STALE_FIELDS = (
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
    "output_hash",
    "qc",
)


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


@dataclass(frozen=True)
class StyleStagePlan:
    reused: tuple[str, ...]
    local: tuple[str, ...]
    model_items: tuple[StyleBatchItem, ...]
    normal_batches: tuple[StyleBatch, ...]
    worst_case_requests: int
    restoration_data: dict[str, tuple[dict[str, str], Any]] = field(default_factory=dict)


class StyleBatchProtocolError(ValueError):
    """Raised when a style-batch response violates the exact-ID protocol."""


_CHUNK_ID_RE = re.compile(r"^chunk\d{4}$")


def _validate_chunk_id(chunk_id: object) -> str:
    if not isinstance(chunk_id, str) or _CHUNK_ID_RE.fullmatch(chunk_id) is None:
        raise StyleBatchProtocolError(f"invalid chunk_id: {chunk_id!r}")
    return chunk_id


def _status_path(article_dir: Path, chunk_id: str) -> Path:
    return article_dir / "chunk_status" / f"{chunk_id}.json"


def _default_status(task: dict[str, Any], chunk: dict[str, Any], source_hash: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_id": str(task.get("record_id") or ""),
        "chunk_id": str(chunk["id"]),
        "source_file": str(chunk["source_file"]),
        "source_hash": source_hash,
        "stages": {},
    }


def _load_status(
    article_dir: Path,
    task: dict[str, Any],
    chunk: dict[str, Any],
    source_hash: str,
) -> tuple[Path, dict[str, Any]]:
    status_path = _status_path(article_dir, str(chunk["id"]))
    try:
        status = (
            json.loads(status_path.read_text(encoding="utf-8"))
            if status_path.exists()
            else _default_status(task, chunk, source_hash)
        )
    except json.JSONDecodeError:
        status = _default_status(task, chunk, source_hash)
    if not isinstance(status, dict):
        status = _default_status(task, chunk, source_hash)
    status.setdefault("stages", {})
    return status_path, status


def _execution_policy(task: dict[str, Any], fixed_translation: str | None) -> tuple[str, str]:
    if bool(task.get("passthrough")):
        reason = str(task.get("passthrough_reason") or "reference_section_passthrough")
        return f"passthrough:{reason}", reason
    if fixed_translation is not None:
        reason = str(task.get("fixed_translation_reason") or "hard_exact_translation")
        return (
            "fixed_translation:"
            + text_hash(fixed_translation)
            + ":"
            + str(task.get("constraint_plan_sha256") or "legacy"),
            reason,
        )
    return "model_pipeline", "style_batch_model_pipeline"


def _item_key(
    *,
    stage: str,
    chunk_id: str,
    source: str,
    prior: str,
    glossary_terms: list[dict[str, Any]],
    context_identity: str,
    policy: str,
) -> str:
    return text_hash(
        json.dumps(
            {
                "protocol": STYLE_BATCH_PROTOCOL,
                "stage": stage,
                "chunk_id": chunk_id,
                "source_hash": text_hash(source),
                "prior_hash": text_hash(prior),
                "glossary": glossary_terms,
                "context_identity": context_identity,
                "policy": policy,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _checkpoint_matches(
    stage_status: dict[str, Any],
    output_path: Path,
    *,
    item_key: str,
    policy: str,
) -> bool:
    if stage_status.get("status") != "complete":
        return False
    if stage_status.get("item_key") != item_key:
        return False
    if not runner.checkpoint_policy_matches(stage_status, policy):
        return False
    qc = stage_status.get("qc")
    if not isinstance(qc, dict) or qc.get("ok") is not True:
        return False
    output_hash = stage_status.get("output_hash")
    if not isinstance(output_hash, str) or not output_hash or not runner.nonempty(output_path):
        return False
    return text_hash(output_path.read_text(encoding="utf-8")) == output_hash


def _complete_local_stage(
    *,
    article_dir: Path,
    stage: str,
    task: dict[str, Any],
    chunk: dict[str, Any],
    source: str,
    output_text: str,
    glossary_terms: list[dict[str, Any]],
    item_key: str,
    policy: str,
    decision_reason: str,
    status_path: Path,
    status: dict[str, Any],
) -> None:
    qc_terms = [] if bool(task.get("passthrough")) else glossary_terms
    qc = runner.validate_chunk(source, output_text, {}, qc_terms)
    if not qc.ok:
        failures = ", ".join(qc.failures)
        raise RuntimeError(
            f"local style policy failed QC for {chunk['id']} at {stage}: {failures}"
        )
    output_path = runner.stage_output_path(
        article_dir,
        str(chunk["id"]),
        str(chunk["output_file"]),
        stage,
    )
    runner.atomic_text(output_path, output_text)
    stage_status = status.setdefault("stages", {}).setdefault(stage, {})
    for field_name in _LOCAL_STAGE_STALE_FIELDS:
        stage_status.pop(field_name, None)
    stage_status.update(
        {
            "status": "complete",
            "item_key": item_key,
            "execution_policy": policy,
            "decision": {"action": "copy_prior_text", "reason": decision_reason},
            "output_file": output_path.name,
            "output_hash": text_hash(output_text),
            "qc": qc.to_dict(),
        }
    )
    runner.atomic_json(status_path, status)


def plan_style_batches(
    items: Iterable[StyleBatchItem],
    *,
    recovery: bool = False,
) -> tuple[StyleBatch, ...]:
    """Partition items in order, closing before either normal limit is exceeded."""

    item_list = tuple(items)
    ids = [_validate_chunk_id(item.chunk_id) for item in item_list]
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
                "id": _validate_chunk_id(item.chunk_id),
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

    expected = tuple(_validate_chunk_id(chunk_id) for chunk_id in expected_ids)
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
    translation_ids = tuple(_validate_chunk_id(chunk_id) for chunk_id in translations)
    if len(translation_ids) != len(set(translation_ids)):
        raise StyleBatchProtocolError("translations must have unique IDs")
    if set(translation_ids) != set(expected):
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


def prepare_style_items(
    *,
    article_dir: Path,
    chunks: Iterable[dict[str, Any]],
    task_factory: Callable[[dict[str, Any]], dict[str, Any]],
    terms: list[dict[str, Any]],
    stage: str,
    input_stage: str,
    context_factory: Callable[[dict[str, Any]], str],
) -> StyleStagePlan:
    reused: list[str] = []
    local: list[str] = []
    model_items: list[StyleBatchItem] = []
    restoration_data: dict[str, tuple[dict[str, str], Any]] = {}

    for chunk in chunks:
        task = dict(task_factory(chunk))
        chunk_id = _validate_chunk_id(chunk["id"])
        source_path = runner.article_artifact_path(article_dir, str(chunk["source_file"]))
        source = source_path.read_text(encoding="utf-8")
        prior_path = runner.stage_output_path(article_dir, chunk_id, str(chunk["output_file"]), input_stage)
        if not runner.nonempty(prior_path):
            raise RuntimeError(f"style stage input is missing or blank: {prior_path}")
        prior = prior_path.read_text(encoding="utf-8")
        selected_terms = runner.compile_glossary_terms(source, terms)
        context_identity = str(context_factory(chunk) or "")
        fixed_translation = task.get("fixed_translation")
        if fixed_translation is not None:
            fixed_translation = str(fixed_translation)
            if not fixed_translation.strip():
                raise RuntimeError(f"Fixed translation is blank for {chunk_id}")
        policy, decision_reason = _execution_policy(task, fixed_translation)
        item_key = _item_key(
            stage=stage,
            chunk_id=chunk_id,
            source=source,
            prior=prior,
            glossary_terms=selected_terms,
            context_identity=context_identity,
            policy=policy,
        )
        status_path, status = _load_status(article_dir, task, chunk, text_hash(source))
        stage_status = status.setdefault("stages", {}).setdefault(stage, {})
        output_path = runner.stage_output_path(article_dir, chunk_id, str(chunk["output_file"]), stage)

        if _checkpoint_matches(stage_status, output_path, item_key=item_key, policy=policy):
            reused.append(chunk_id)
            continue

        if bool(task.get("passthrough")):
            _complete_local_stage(
                article_dir=article_dir,
                stage=stage,
                task=task,
                chunk=chunk,
                source=source,
                output_text=source,
                glossary_terms=selected_terms,
                item_key=item_key,
                policy=policy,
                decision_reason=decision_reason,
                status_path=status_path,
                status=status,
            )
            local.append(chunk_id)
            continue

        if fixed_translation is not None:
            _complete_local_stage(
                article_dir=article_dir,
                stage=stage,
                task=task,
                chunk=chunk,
                source=source,
                output_text=fixed_translation,
                glossary_terms=selected_terms,
                item_key=item_key,
                policy=policy,
                decision_reason=decision_reason,
                status_path=status_path,
                status=status,
            )
            local.append(chunk_id)
            continue

        protected_text, mapping, typed_nodes = runner.protect_stage_text(prior)
        restoration_data[chunk_id] = (mapping, typed_nodes)
        model_items.append(
            StyleBatchItem(
                chunk_id=chunk_id,
                protected_text=protected_text,
                source_hash=text_hash(source),
                prior_hash=text_hash(prior),
                glossary_text=runner.glossary_text(selected_terms),
                context=context_identity,
                item_key=item_key,
            )
        )

    planned_items = tuple(model_items)
    normal_batches = plan_style_batches(planned_items)
    recovery_batches = (len(planned_items) + RECOVERY_BATCH_CHUNKS - 1) // RECOVERY_BATCH_CHUNKS
    worst_case_requests = len(normal_batches) + recovery_batches
    return StyleStagePlan(
        reused=tuple(reused),
        local=tuple(local),
        model_items=planned_items,
        normal_batches=normal_batches,
        worst_case_requests=worst_case_requests,
        restoration_data=restoration_data,
    )
