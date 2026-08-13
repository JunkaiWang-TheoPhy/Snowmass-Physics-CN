#!/usr/bin/env python3
"""Pure planning and response protocol for Snowmass style batches."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from scripts import run_snowmass_translation as runner
from scripts.snowmass_batch_budget import RequestLimitExceededError
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


@dataclass(frozen=True)
class StyleStageResult:
    planned_chunks: int
    completed_chunks: int
    reused_chunks: int
    local_chunks: int
    failed_chunks: int
    normal_requests: int
    recovery_requests: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    total_tokens: int
    total_cost_rmb: float


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


def _batch_status_path(article_dir: Path) -> Path:
    return article_dir / "style_batch_status.json"


def _load_batch_status(article_dir: Path, stage: str) -> dict[str, Any]:
    path = _batch_status_path(article_dir)
    if not path.exists():
        return {"schema_version": 1, "stage": stage, "requests": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {"schema_version": 1, "stage": stage, "requests": []}
    if not isinstance(payload, dict):
        payload = {"schema_version": 1, "stage": stage, "requests": []}
    payload.setdefault("schema_version", 1)
    payload["stage"] = stage
    requests = payload.get("requests")
    if not isinstance(requests, list):
        payload["requests"] = []
    return payload


def _persist_batch_request(article_dir: Path, stage: str, request_status: dict[str, Any]) -> None:
    payload = _load_batch_status(article_dir, stage)
    requests = [
        entry
        for entry in payload["requests"]
        if not (
            isinstance(entry, dict)
            and entry.get("request_key") == request_status.get("request_key")
        )
    ]
    requests.append(request_status)
    payload["requests"] = requests
    runner.atomic_json(_batch_status_path(article_dir), payload)


def _response_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    return runner.extract_output(response)


def _token_count(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _update_stage_status(
    *,
    article_dir: Path,
    task: dict[str, Any],
    chunk: dict[str, Any],
    source_hash: str,
    stage: str,
    mutate: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    status_path, status = _load_status(article_dir, task, chunk, source_hash)
    stage_status = status.setdefault("stages", {}).setdefault(stage, {})
    mutate(stage_status)
    runner.atomic_json(status_path, status)
    return stage_status


def execute_style_stage(
    *,
    article_dir: Path,
    chunks: Iterable[dict[str, Any]],
    task_factory: Callable[[dict[str, Any]], dict[str, Any]],
    terms: list[dict[str, Any]],
    stage: str,
    plan: StyleStagePlan,
    client: Any,
    instructions: str,
    max_output_tokens: int,
    budget_guard: Any | None = None,
    run_id: str | None = None,
    model: str = "snowmass-style-batch",
) -> StyleStageResult:
    if budget_guard is not None and hasattr(budget_guard, "snapshot"):
        snapshot = budget_guard.snapshot()
        if isinstance(snapshot, dict):
            remaining = snapshot.get("stage_remaining_api_calls")
            if isinstance(remaining, (int, float)) and remaining < plan.worst_case_requests:
                raise RequestLimitExceededError(
                    "stage request cap would be exceeded before style batching starts"
                )

    chunk_map = {str(chunk["id"]): dict(chunk) for chunk in chunks}
    item_map = {item.chunk_id: item for item in plan.model_items}
    totals = {
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_rmb": 0.0,
    }
    failed_after_recovery: list[str] = []
    normal_requests = 0
    recovery_requests = 0

    def process_batch(batch: StyleBatch) -> tuple[str, ...]:
        nonlocal normal_requests, recovery_requests

        if batch.recovery:
            recovery_requests += 1
        else:
            normal_requests += 1
        request_key = style_batch_request_key(
            batch=batch,
            stage=stage,
            model=model,
            instructions=instructions,
            max_output_tokens=max_output_tokens,
        )
        uncertainty_key = f"{stage}:{request_key}"
        payload = build_style_batch_payload(batch, stage=stage)
        input_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        request_status = {
            "request_key": request_key,
            "recovery": batch.recovery,
            "chunk_ids": [item.chunk_id for item in batch.items],
            "started_at": runner.now(),
            "status": "running",
        }
        _persist_batch_request(article_dir, stage, request_status)

        for item in batch.items:
            chunk = chunk_map[item.chunk_id]
            task = dict(task_factory(chunk))
            _update_stage_status(
                article_dir=article_dir,
                task=task,
                chunk=chunk,
                source_hash=item.source_hash,
                stage=stage,
                mutate=lambda stage_status, item=item, request_key=request_key: stage_status.update(
                    {
                        "status": "running",
                        "started_at": runner.now(),
                        "request_key": request_key,
                        "execution_policy": "model_pipeline",
                        "max_output_tokens": max_output_tokens,
                        **({"run_id": run_id} if run_id is not None else {}),
                    }
                ),
            )

        reservation: str | None = None
        try:
            if budget_guard is not None:
                reservation = budget_guard.reserve(
                    instructions + "\n" + input_text,
                    max_output_tokens,
                    uncertainty_key=uncertainty_key,
                )
            response, _latency = client.complete(instructions, input_text, max_output_tokens)
        except runner.AmbiguousTransportError as exc:
            conservative_cost_rmb = (
                float(budget_guard.commit_estimate(reservation))
                if reservation is not None
                else 0.0
            )
            request_status.update(
                {
                    "status": "uncertain",
                    "finished_at": runner.now(),
                    "error": str(exc),
                    "conservative_cost_rmb": conservative_cost_rmb,
                    "uncertainty_key": uncertainty_key,
                    "uncertainty_reservation_id": reservation,
                }
            )
            _persist_batch_request(article_dir, stage, request_status)
            if conservative_cost_rmb > 0:
                runner.append_cost_ledger(
                    article_dir,
                    {
                        "event_id": request_key,
                        "kind": "style_batch_ambiguous_transport_reservation",
                        "stage": stage,
                        "request_key": request_key,
                        "chunk_ids": request_status["chunk_ids"],
                        "cost_rmb": conservative_cost_rmb,
                    },
                )
            for item in batch.items:
                chunk = chunk_map[item.chunk_id]
                task = dict(task_factory(chunk))
                _update_stage_status(
                    article_dir=article_dir,
                    task=task,
                    chunk=chunk,
                    source_hash=item.source_hash,
                    stage=stage,
                    mutate=lambda stage_status, exc=exc, conservative_cost_rmb=conservative_cost_rmb: stage_status.update(
                        {
                            "status": "uncertain",
                            "finished_at": runner.now(),
                            "error": str(exc),
                            "conservative_cost_rmb": conservative_cost_rmb,
                            "uncertainty_key": uncertainty_key,
                            "uncertainty_reservation_id": reservation,
                        }
                    ),
                )
            raise
        except Exception:
            if reservation is not None and budget_guard is not None:
                budget_guard.commit_estimate(reservation)
            raise

        billed_usage = runner.coarse_response_usage(response)
        if reservation is not None and budget_guard is not None:
            budget_guard.settle(reservation, billed_usage)
            if hasattr(budget_guard, "resolve_uncertain"):
                budget_guard.resolve_uncertain(uncertainty_key)
        cost_rmb = runner.estimate_cost_rmb(
            billed_usage,
            budget_guard.usd_cny_rate
            if budget_guard is not None and hasattr(budget_guard, "usd_cny_rate")
            else runner.DEFAULT_USD_CNY_RATE,
        )
        totals["input_tokens"] += _token_count(billed_usage.get("input_tokens"))
        totals["cached_tokens"] += _token_count(billed_usage.get("cached_tokens"))
        totals["output_tokens"] += _token_count(billed_usage.get("output_tokens"))
        totals["total_tokens"] += _token_count(billed_usage.get("total_tokens"))
        totals["cost_rmb"] += cost_rmb
        request_status.update(
            {
                "status": "settled",
                "finished_at": runner.now(),
                "response_id": str(response.get("id") or request_key),
                "usage": billed_usage,
                "cost_rmb": cost_rmb,
                "response": runner.response_metadata(response),
            }
        )
        _persist_batch_request(article_dir, stage, request_status)
        runner.append_cost_ledger(
            article_dir,
            {
                "event_id": request_status["response_id"],
                "kind": "style_batch_settled_response",
                "stage": stage,
                "request_key": request_key,
                "chunk_ids": request_status["chunk_ids"],
                "usage": billed_usage,
                "cost_rmb": cost_rmb,
            },
        )

        try:
            parsed = parse_style_batch_response(
                _response_text(response),
                (item.chunk_id for item in batch.items),
            )
        except StyleBatchProtocolError as exc:
            request_status.update(
                {
                    "status": "protocol_failed",
                    "finished_at": runner.now(),
                    "error": str(exc),
                }
            )
            _persist_batch_request(article_dir, stage, request_status)
            for item in batch.items:
                chunk = chunk_map[item.chunk_id]
                task = dict(task_factory(chunk))
                _update_stage_status(
                    article_dir=article_dir,
                    task=task,
                    chunk=chunk,
                    source_hash=item.source_hash,
                    stage=stage,
                    mutate=lambda stage_status, exc=exc: stage_status.update(
                        {
                            "status": "failed",
                            "finished_at": runner.now(),
                            "error": str(exc),
                        }
                    ),
                )
            return tuple(item.chunk_id for item in batch.items)

        failed_ids: list[str] = []
        for item in batch.items:
            chunk = chunk_map[item.chunk_id]
            task = dict(task_factory(chunk))
            source_path = runner.article_artifact_path(article_dir, str(chunk["source_file"]))
            source = source_path.read_text(encoding="utf-8")
            selected_terms = runner.compile_glossary_terms(source, terms)
            output_path = runner.stage_output_path(
                article_dir,
                item.chunk_id,
                str(chunk["output_file"]),
                stage,
            )
            protected_text = parsed[item.chunk_id]
            try:
                mapping, typed_nodes = plan.restoration_data[item.chunk_id]
                restored = runner.restore_stage_text(protected_text, mapping, typed_nodes)
                qc = runner.validate_chunk(source, restored, {}, selected_terms)
            except Exception as exc:
                candidate_metadata = runner.persist_rejected_candidate(
                    article_dir,
                    item.chunk_id,
                    stage,
                    request_key,
                    protected_text,
                    protected=True,
                )
                failed_ids.append(item.chunk_id)
                _update_stage_status(
                    article_dir=article_dir,
                    task=task,
                    chunk=chunk,
                    source_hash=item.source_hash,
                    stage=stage,
                    mutate=lambda stage_status, exc=exc, candidate_metadata=candidate_metadata: stage_status.update(
                        {
                            "status": "failed",
                            "finished_at": runner.now(),
                            "error": str(exc),
                            "request_key": request_key,
                            "response_id": request_status["response_id"],
                            "item_key": item.item_key,
                            "execution_policy": "model_pipeline",
                            **candidate_metadata,
                        }
                    ),
                )
                continue

            if not qc.ok:
                candidate_metadata = runner.persist_rejected_candidate(
                    article_dir,
                    item.chunk_id,
                    stage,
                    request_key,
                    restored,
                    protected=False,
                )
                failed_ids.append(item.chunk_id)
                _update_stage_status(
                    article_dir=article_dir,
                    task=task,
                    chunk=chunk,
                    source_hash=item.source_hash,
                    stage=stage,
                    mutate=lambda stage_status, qc=qc, candidate_metadata=candidate_metadata: stage_status.update(
                        {
                            "status": "failed",
                            "finished_at": runner.now(),
                            "error": "QC failed: " + ", ".join(qc.failures),
                            "request_key": request_key,
                            "response_id": request_status["response_id"],
                            "item_key": item.item_key,
                            "execution_policy": "model_pipeline",
                            "qc": qc.to_dict(),
                            **candidate_metadata,
                        }
                    ),
                )
                continue

            runner.atomic_text(output_path, restored)
            _update_stage_status(
                article_dir=article_dir,
                task=task,
                chunk=chunk,
                source_hash=item.source_hash,
                stage=stage,
                mutate=lambda stage_status, restored=restored, qc=qc, output_path=output_path: (
                    [stage_status.pop(field_name, None) for field_name in _LOCAL_STAGE_STALE_FIELDS],
                    stage_status.update(
                        {
                            "status": "complete",
                            "finished_at": runner.now(),
                            "request_key": request_key,
                            "response_id": request_status["response_id"],
                            "item_key": item.item_key,
                            "execution_policy": "model_pipeline",
                            "output_file": output_path.name,
                            "output_hash": text_hash(restored),
                            "qc": qc.to_dict(),
                        }
                    ),
                ),
            )
        return tuple(failed_ids)

    recovery_queue: list[str] = []
    for batch in plan.normal_batches:
        recovery_queue.extend(process_batch(batch))

    if recovery_queue:
        recovery_items = [item_map[chunk_id] for chunk_id in recovery_queue]
        for batch in plan_style_batches(recovery_items, recovery=True):
            failed_after_recovery.extend(process_batch(batch))

    if failed_after_recovery:
        failed = ", ".join(failed_after_recovery)
        raise RuntimeError(f"style stage failed after one recovery pass: {failed}")

    planned_chunks = len(plan.reused) + len(plan.local) + len(plan.model_items)
    completed_chunks = planned_chunks
    return StyleStageResult(
        planned_chunks=planned_chunks,
        completed_chunks=completed_chunks,
        reused_chunks=len(plan.reused),
        local_chunks=len(plan.local),
        failed_chunks=0,
        normal_requests=normal_requests,
        recovery_requests=recovery_requests,
        input_tokens=totals["input_tokens"],
        cached_tokens=totals["cached_tokens"],
        output_tokens=totals["output_tokens"],
        total_tokens=totals["total_tokens"],
        total_cost_rmb=totals["cost_rmb"],
    )
