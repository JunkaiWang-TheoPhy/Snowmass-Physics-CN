#!/usr/bin/env python3
"""Pure planning and response protocol for Snowmass style batches."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from scripts import run_snowmass_translation as runner
    from scripts.snowmass_batch_budget import RequestLimitExceededError
    from scripts.run_snowmass_translation import text_hash
except ModuleNotFoundError as error:
    if error.name != "scripts":
        raise
    import run_snowmass_translation as runner
    from snowmass_batch_budget import RequestLimitExceededError
    from run_snowmass_translation import text_hash


STYLE_BATCH_PROTOCOL = "snowmass-style-batch-v1"
NORMAL_BATCH_CHUNKS = 24
NORMAL_BATCH_CHARACTERS = 18_000
RECOVERY_BATCH_CHUNKS = 8


def style_batch_instructions(base_instructions: str) -> str:
    """Append the output contract that governs multi-chunk style requests."""

    return base_instructions.rstrip() + """

STYLE-BATCH JSON PROTOCOL
The input is one JSON object whose `chunks` array contains exact chunk IDs.
Return exactly one standard JSON object of the form
{"translations":{"chunk0001":"完整译文","chunk0002":"完整译文"}}.
The `translations` keys must be exactly the input chunk IDs, each exactly once.
Every value must be the complete revised text for that ID. Return no Markdown code fence,
explanation, prefix, suffix, or analysis.
This JSON requirement overrides any earlier plain-text output instruction.
"""
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
    input_stage: str = ""


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


def stage_plan_projection(plan: StyleStagePlan) -> dict[str, Any]:
    return {
        "reused_chunks": list(plan.reused),
        "local_chunks": list(plan.local),
        "model_chunks": [item.chunk_id for item in plan.model_items],
        "normal_batches": [
            [item.chunk_id for item in batch.items]
            for batch in plan.normal_batches
        ],
        "normal_batch_characters": [
            sum(len(item.protected_text) for item in batch.items)
            for batch in plan.normal_batches
        ],
        "normal_requests": len(plan.normal_batches),
        "worst_case_requests": plan.worst_case_requests,
        "semantics": "exact",
    }


def conservative_stage_plan_projection(plan: StyleStagePlan) -> dict[str, Any]:
    estimated = stage_plan_projection(plan)
    conservative_model_batches = [[item.chunk_id] for item in plan.model_items]
    conservative_batch_characters = [len(item.protected_text) for item in plan.model_items]
    conservative_normal_requests = len(conservative_model_batches)
    conservative_worst_case_requests = conservative_normal_requests + len(conservative_model_batches)
    return {
        **estimated,
        "semantics": "conservative",
        "estimated_normal_requests": estimated["normal_requests"],
        "estimated_worst_case_requests": estimated["worst_case_requests"],
        "estimated_normal_batches": estimated["normal_batches"],
        "estimated_normal_batch_characters": estimated["normal_batch_characters"],
        "normal_batches": conservative_model_batches,
        "normal_batch_characters": conservative_batch_characters,
        "normal_requests": conservative_normal_requests,
        "worst_case_requests": conservative_worst_case_requests,
    }


def stage_result_projection(result: StyleStageResult) -> dict[str, Any]:
    return {
        "planned_chunks": result.planned_chunks,
        "completed_chunks": result.completed_chunks,
        "reused_chunks": result.reused_chunks,
        "local_chunks": result.local_chunks,
        "failed_chunks": result.failed_chunks,
        "normal_requests": result.normal_requests,
        "recovery_requests": result.recovery_requests,
        "input_tokens": result.input_tokens,
        "cached_tokens": result.cached_tokens,
        "output_tokens": result.output_tokens,
        "total_tokens": result.total_tokens,
        "total_cost_rmb": result.total_cost_rmb,
    }


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
    recorded_policy = stage_status.get("execution_policy")
    audited_fallback = (
        recorded_policy == "verified_prior_style_fallback"
        and policy == "model_pipeline"
        and stage_status.get("decision")
        == {"action": "copy_prior_text", "reason": "style_recovery_exhausted"}
    )
    if not audited_fallback and not runner.checkpoint_policy_matches(stage_status, policy):
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
    candidate = response_text.strip()
    try:
        response = json.loads(
            candidate,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON constant: {value}")
            ),
        )
    except json.JSONDecodeError as exc:
        if candidate.endswith("}") and exc.msg == "Extra data" and candidate[exc.pos:] == "}":
            try:
                response = json.loads(
                    candidate[:exc.pos],
                    object_pairs_hook=_reject_duplicate_json_keys,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"non-standard JSON constant: {value}")
                    ),
                )
            except (TypeError, ValueError, json.JSONDecodeError) as retry_exc:
                raise StyleBatchProtocolError("response must be standard JSON") from retry_exc
            if not isinstance(response, dict) or not isinstance(response.get("translations"), dict):
                raise StyleBatchProtocolError("response must contain a translations object")
        else:
            raise StyleBatchProtocolError("response must be standard JSON") from exc
    except (TypeError, ValueError) as exc:
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
    recovery_batches = plan_style_batches(planned_items, recovery=True)
    worst_case_requests = len(normal_batches) + len(recovery_batches)
    return StyleStagePlan(
        reused=tuple(reused),
        local=tuple(local),
        model_items=planned_items,
        normal_batches=normal_batches,
        worst_case_requests=worst_case_requests,
        restoration_data=restoration_data,
        input_stage=input_stage,
    )


def _batch_status_path(article_dir: Path) -> Path:
    return article_dir / "style_batch_status.json"


def _normalize_stage_requests(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    stages: dict[str, dict[str, Any]] = {}
    raw_stages = payload.get("stages")
    if isinstance(raw_stages, dict):
        for stage_name, stage_payload in raw_stages.items():
            if not isinstance(stage_name, str) or not stage_name:
                continue
            requests = (
                stage_payload.get("requests")
                if isinstance(stage_payload, dict)
                else None
            )
            stages[stage_name] = {
                "requests": list(requests) if isinstance(requests, list) else []
            }
    legacy_stage = payload.get("stage")
    if isinstance(legacy_stage, str) and legacy_stage and legacy_stage not in stages:
        legacy_requests = payload.get("requests")
        stages[legacy_stage] = {
            "requests": list(legacy_requests) if isinstance(legacy_requests, list) else []
        }
    return stages


def _load_batch_status(article_dir: Path) -> dict[str, Any]:
    path = _batch_status_path(article_dir)
    if not path.exists():
        return {"schema_version": 2, "stages": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "schema_version": 2,
        "stages": _normalize_stage_requests(payload),
    }


def _persist_batch_request(article_dir: Path, stage: str, request_status: dict[str, Any]) -> None:
    payload = _load_batch_status(article_dir)
    stage_payload = payload["stages"].setdefault(stage, {"requests": []})
    requests = [
        entry
        for entry in stage_payload["requests"]
        if not (
            isinstance(entry, dict)
            and entry.get("attempt_id") == request_status.get("attempt_id")
        )
    ]
    requests.append(request_status)
    stage_payload["requests"] = requests
    runner.atomic_json(_batch_status_path(article_dir), payload)


def _response_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    return runner.extract_output(response)


def _persist_rejected_batch_response(
    article_dir: Path,
    stage: str,
    attempt_id: str,
    response_text: str,
) -> dict[str, str]:
    safe_stage = re.sub(r"[^a-z0-9_]+", "_", stage.casefold()).strip("_") or "stage"
    relative_path = Path("rejected_style_responses") / f"{safe_stage}_{text_hash(attempt_id)[:12]}.txt"
    root = (article_dir / "rejected_style_responses").resolve()
    path = (article_dir / relative_path).resolve()
    if path.parent != root:
        raise RuntimeError("rejected style response path escapes its artifact directory")
    runner.atomic_text(path, response_text)
    return {
        "rejected_response_file": relative_path.as_posix(),
        "rejected_response_hash": text_hash(response_text),
    }


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


def _batch_attempt_history(article_dir: Path, stage: str, request_key: str) -> list[dict[str, Any]]:
    payload = _load_batch_status(article_dir)
    return [
        entry
        for entry in payload.get("stages", {}).get(stage, {}).get("requests", [])
        if isinstance(entry, dict) and entry.get("request_key") == request_key
    ]


def _next_attempt_identity(
    *,
    article_dir: Path,
    stage: str,
    request_key: str,
    recovery: bool,
) -> tuple[str, int]:
    attempt_ordinal = len(_batch_attempt_history(article_dir, stage, request_key)) + 1
    attempt_kind = "recovery" if recovery else "normal"
    return f"{request_key}:{attempt_kind}:{attempt_ordinal}", attempt_ordinal


def _has_unresolved_uncertain_attempt(article_dir: Path, stage: str, request_key: str) -> bool:
    return any(
        entry.get("status") == "uncertain"
        for entry in _batch_attempt_history(article_dir, stage, request_key)
    )


def _matching_recovery_was_already_paid(
    article_dir: Path,
    stage: str,
    request_key: str,
    batch: StyleBatch,
    chunk_map: dict[str, dict[str, Any]],
    task_factory: Callable[[dict[str, Any]], dict[str, Any]],
) -> bool:
    expected_ids = tuple(item.chunk_id for item in batch.items)
    attempts = _batch_attempt_history(article_dir, stage, request_key)
    paid_recovery = any(
        attempt.get("recovery") is True
        and attempt.get("status") in {"settled", "protocol_failed", "recovered_offline"}
        and tuple(attempt.get("chunk_ids") or ()) == expected_ids
        for attempt in attempts
    )
    if not paid_recovery:
        return False
    for item in batch.items:
        chunk = chunk_map[item.chunk_id]
        task = dict(task_factory(chunk))
        _status_path_value, status = _load_status(
            article_dir, task, chunk, item.source_hash
        )
        stage_status = status.get("stages", {}).get(stage, {})
        if not (
            status.get("chunk_id") == item.chunk_id
            and status.get("source_hash") == item.source_hash
            and isinstance(stage_status, dict)
            and stage_status.get("status") == "failed"
            and stage_status.get("item_key") == item.item_key
            and stage_status.get("request_key") == request_key
        ):
            return False
    return True


def _recover_paid_protocol_response(
    article_dir: Path,
    stage: str,
    request_key: str,
    expected_ids: Iterable[str],
) -> tuple[dict[str, str], dict[str, Any]] | None:
    expected = tuple(expected_ids)
    for attempt in reversed(_batch_attempt_history(article_dir, stage, request_key)):
        if attempt.get("status") not in {"protocol_failed", "recovered_offline"}:
            continue
        if tuple(attempt.get("chunk_ids") or ()) != expected:
            continue
        relative_name = attempt.get("rejected_response_file")
        expected_hash = attempt.get("rejected_response_hash")
        if not isinstance(relative_name, str) or not isinstance(expected_hash, str):
            continue
        relative_path = Path(relative_name)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            continue
        response_path = (article_dir / relative_path).resolve()
        rejected_root = (article_dir / "rejected_style_responses").resolve()
        if response_path.parent != rejected_root or not response_path.is_file():
            continue
        response_text = response_path.read_text(encoding="utf-8")
        if text_hash(response_text) != expected_hash:
            continue
        try:
            parsed = parse_style_batch_response(response_text, expected)
        except StyleBatchProtocolError:
            continue
        return parsed, attempt
    return None


def _stage_record_id(
    article_dir: Path,
    chunks: Iterable[dict[str, Any]],
    task_factory: Callable[[dict[str, Any]], dict[str, Any]],
) -> str:
    for chunk in chunks:
        task = dict(task_factory(chunk))
        record_id = task.get("record_id")
        if isinstance(record_id, str) and record_id:
            return record_id
        break
    for path in (article_dir / "paper_status.json", article_dir / "manifest.json"):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            record_id = payload.get("record_id")
            if isinstance(record_id, str) and record_id:
                return record_id
    return str(article_dir.name)


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
    instructions = style_batch_instructions(instructions)
    chunk_list = tuple(chunks)
    record_id = _stage_record_id(article_dir, chunk_list, task_factory)
    if budget_guard is not None and hasattr(budget_guard, "snapshot"):
        snapshot = budget_guard.snapshot()
        if isinstance(snapshot, dict):
            remaining = snapshot.get("stage_remaining_api_calls")
            if isinstance(remaining, (int, float)) and remaining < plan.worst_case_requests:
                raise RequestLimitExceededError(
                    "stage request cap would be exceeded before style batching starts"
                )

    chunk_map = {str(chunk["id"]): dict(chunk) for chunk in chunk_list}
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
        request_key = style_batch_request_key(
            batch=batch,
            stage=stage,
            model=model,
            instructions=instructions,
            max_output_tokens=max_output_tokens,
        )
        if _has_unresolved_uncertain_attempt(article_dir, stage, request_key):
            raise RuntimeError(
                f"style stage {stage} has unresolved uncertain paid request for {record_id}; "
                "explicit retry authorization required"
            )
        if _matching_recovery_was_already_paid(
            article_dir,
            stage,
            request_key,
            batch,
            chunk_map,
            task_factory,
        ):
            return tuple(item.chunk_id for item in batch.items)
        offline_recovery = _recover_paid_protocol_response(
            article_dir,
            stage,
            request_key,
            (item.chunk_id for item in batch.items),
        )
        if offline_recovery is not None:
            parsed, request_status = offline_recovery
            request_status.update(
                {
                    "status": "recovered_offline",
                    "finished_at": runner.now(),
                    "offline_recovery_protocol": STYLE_BATCH_PROTOCOL,
                }
            )
            _persist_batch_request(article_dir, stage, request_status)
        else:
            parsed = None

        if parsed is None:
            if batch.recovery:
                recovery_requests += 1
            else:
                normal_requests += 1
        attempt_id, attempt_ordinal = _next_attempt_identity(
            article_dir=article_dir,
            stage=stage,
            request_key=request_key,
            recovery=batch.recovery,
        )
        uncertainty_key = f"{stage}:{request_key}"
        payload = build_style_batch_payload(batch, stage=stage)
        input_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if parsed is None:
            request_status = {
                "attempt_id": attempt_id,
                "attempt_ordinal": attempt_ordinal,
                "stage": stage,
                "request_key": request_key,
                "recovery": batch.recovery,
                "chunk_ids": [item.chunk_id for item in batch.items],
                "started_at": runner.now(),
                "status": "running",
            }
            _persist_batch_request(article_dir, stage, request_status)

        for item in batch.items if parsed is None else ():
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
            if parsed is not None:
                raise StopIteration
            if budget_guard is not None:
                reservation = budget_guard.reserve(
                    instructions + "\n" + input_text,
                    max_output_tokens,
                    uncertainty_key=uncertainty_key,
                )
            response, _latency = client.complete(instructions, input_text, max_output_tokens)
        except StopIteration:
            response = {}
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
                        "event_id": attempt_id,
                        "kind": "style_batch_ambiguous_transport_reservation",
                        "stage": stage,
                        "request_key": request_key,
                        "attempt_id": attempt_id,
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
        except Exception as exc:
            conservative_cost_rmb = (
                float(budget_guard.commit_estimate(reservation))
                if reservation is not None and budget_guard is not None
                else 0.0
            )
            request_status.update(
                {
                    "status": "failed",
                    "finished_at": runner.now(),
                    "error": repr(exc),
                    "conservative_cost_rmb": conservative_cost_rmb,
                }
            )
            _persist_batch_request(article_dir, stage, request_status)
            if conservative_cost_rmb > 0:
                runner.append_cost_ledger(
                    article_dir,
                    {
                        "event_id": attempt_id,
                        "kind": "style_batch_failed_transport_reservation",
                        "stage": stage,
                        "request_key": request_key,
                        "attempt_id": attempt_id,
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
                    mutate=lambda stage_status, exc=exc: stage_status.update(
                        {
                            "status": "failed",
                            "finished_at": runner.now(),
                            "error": repr(exc),
                        }
                    ),
                )
            raise

        if parsed is not None:
            billed_usage = {}
        else:
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
        if parsed is None:
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
                    "attempt_id": attempt_id,
                    "chunk_ids": request_status["chunk_ids"],
                    "usage": billed_usage,
                    "cost_rmb": cost_rmb,
                },
            )
            response_text = _response_text(response)
            try:
                parsed = parse_style_batch_response(
                    response_text,
                    (item.chunk_id for item in batch.items),
                )
            except StyleBatchProtocolError as exc:
                request_status.update(
                    {
                        "status": "protocol_failed",
                        "finished_at": runner.now(),
                        "error": str(exc),
                        **_persist_rejected_batch_response(
                            article_dir, stage, attempt_id, response_text
                        ),
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
        if not plan.input_stage:
            raise RuntimeError("style fallback requires an explicit planned input stage")
        for chunk_id in failed_after_recovery:
            item = item_map[chunk_id]
            chunk = chunk_map[chunk_id]
            task = dict(task_factory(chunk))
            source_path = runner.article_artifact_path(
                article_dir, str(chunk["source_file"])
            )
            source = source_path.read_text(encoding="utf-8")
            selected_terms = runner.compile_glossary_terms(source, terms)
            status_path, status = _load_status(
                article_dir, task, chunk, item.source_hash
            )
            prior_path = runner.stage_output_path(
                article_dir,
                chunk_id,
                str(chunk["output_file"]),
                plan.input_stage,
            )
            prior_status = status.get("stages", {}).get(plan.input_stage, {})
            prior_checkpoint_verified = (
                status.get("chunk_id") == chunk_id
                and status.get("source_hash") == item.source_hash
                and isinstance(prior_status, dict)
                and runner.checkpoint_is_reusable_after_contract_change(
                    prior_status, prior_path
                )
                and runner.nonempty(prior_path)
                and text_hash(prior_path.read_text(encoding="utf-8"))
                == item.prior_hash
            )
            if not prior_checkpoint_verified:
                raise RuntimeError(
                    f"style prior checkpoint is not verified for {record_id} {chunk_id}"
                )
            prior = prior_path.read_text(encoding="utf-8")
            qc = runner.validate_chunk(source, prior, {}, selected_terms)
            if not qc.ok:
                failures = ", ".join(qc.failures)
                raise RuntimeError(
                    f"verified prior style fallback failed QC for {record_id} {chunk_id}: {failures}"
                )
            output_path = runner.stage_output_path(
                article_dir,
                chunk_id,
                str(chunk["output_file"]),
                stage,
            )
            runner.atomic_text(output_path, prior)
            last_request_key = str(status.get("stages", {}).get(stage, {}).get("request_key", ""))
            _update_stage_status(
                article_dir=article_dir,
                task=task,
                chunk=chunk,
                source_hash=item.source_hash,
                stage=stage,
                mutate=lambda stage_status, prior=prior, qc=qc, output_path=output_path, last_request_key=last_request_key: (
                    [stage_status.pop(field_name, None) for field_name in _LOCAL_STAGE_STALE_FIELDS],
                    stage_status.update(
                        {
                            "status": "complete",
                            "finished_at": runner.now(),
                            "item_key": item.item_key,
                            "execution_policy": "verified_prior_style_fallback",
                            "decision": {
                                "action": "copy_prior_text",
                                "reason": "style_recovery_exhausted",
                            },
                            "last_failed_model_request_key": last_request_key,
                            "output_file": output_path.name,
                            "output_hash": text_hash(prior),
                            "qc": qc.to_dict(),
                        }
                    ),
                ),
            )

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
