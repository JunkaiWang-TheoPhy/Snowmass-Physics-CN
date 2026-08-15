#!/usr/bin/env python3
"""Paper-level refined orchestration for resumable Snowmass translation."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
from pathlib import Path
import re
import sys
from typing import Any
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_snowmass_translation as runner
import snowmass_constraint_compiler as constraint_compiler
import snowmass_style_batching as style_batching
from snowmass_document_units import compare_numeric_literals
from snowmass_reference_boundaries import reference_boundary
from snowmass_translation_qc import _extract_unit_values, _parenthesis_residue


SCHEMA_VERSION = 1
ANALYSIS_FILE = "01-analysis.md"
PROMPT_FILE = "02-prompt.md"
DRAFT_FILE = "03-draft.md"
CRITIQUE_FILE = "04-critique.md"
REVISION_FILE = "05-revision.md"
FINAL_FILE = "translation.md"
NO_ACTIONABLE_CRITIQUE = "NO_ACTIONABLE_CHUNK_CRITIQUE"
MANUAL_CORRECTIONS_FILE = "manual_corrections.json"
TRACKED_HARD_CONSTRAINTS = SCRIPT_DIR.parent / "translations/snowmass-hard-constraints.json"
CRITIQUE_SHARD_CHAR_LIMIT = 16_000
CRITIQUE_SHARD_MAX_FINDINGS = 4
CRITIQUE_SHARD_VALIDATION_MAX_FINDINGS = CRITIQUE_SHARD_MAX_FINDINGS * 3
CRITIQUE_SHARD_MAX_FINDING_CHARACTERS = 160
CRITIQUE_SHARD_MAX_OUTPUT_TOKENS = 900
CRITIQUE_GLOBAL_MAX_FINDINGS = 30
CRITIQUE_DRAFT_MAX_SOURCE_RATIO = 3
CRITIQUE_DRAFT_MAX_EXTRA_CHARACTERS = 512
ANALYSIS_MAX_OUTPUT_TOKENS = 4000
CRITIQUE_MAX_OUTPUT_TOKENS = 4000
STRUCTURE_PLACEHOLDER_RE = re.compile(r"\{v\d+\}")


def _analysis_instructions() -> str:
    return """Perform a compact paper-level content analysis for an English-to-Simplified-Chinese academic translation.
Return exactly these Markdown sections: ## Content Summary, ## Terminology, ## Tone & Style, and ## Translation Challenges.
Identify the paper's argument, domain-specific meanings, preferred terminology, register, and concrete translation risks. Be selective: use at most 40 concise bullets total, do not inventory chunks, and do not reproduce source passages. Do not translate the paper yet."""


def _critique_instructions() -> str:
    return """Perform a paper-level critical review of the tagged Chinese draft against the tagged English source.
Return exactly these Markdown sections: ## Accuracy, ## Native Voice, ## Notes & Adaptation, and ## Summary.
Every actionable finding must start with its chunk ID (for example, `chunk0001:`). Check omissions, additions, factual drift, modality, terminology, syntax, academic register, and translationese. Report only high-impact actionable defects, at most 30 one-line findings total. If more exist, select the 30 highest-risk defects. Do not enumerate correct chunks, reproduce passages, explain your method, or rewrite the draft."""


def _critique_shard_instructions() -> str:
    return f"""Review one aligned English/Chinese shard of an academic paper.
Return exactly these Markdown sections: ## Accuracy, ## Native Voice, ## Notes & Adaptation, and ## Summary.
Report only high-impact actionable defects. Every actionable line must start with its exact chunk ID, for example `- chunk0001:`. Return at most {CRITIQUE_SHARD_MAX_FINDINGS} actionable lines total, ranked highest risk first. Each actionable line must contain at most {CRITIQUE_SHARD_MAX_FINDING_CHARACTERS} characters including its chunk ID. If none exist, write `- NO_ACTIONABLE_FINDINGS`. In every other empty section write only `- NO_ACTIONABLE_FINDINGS`. Do not quote passages, enumerate correct chunks, explain methods, add prose summaries, or rewrite the draft."""


def _critique_shard_repair_instructions(allowed_chunk_ids: set[str]) -> str:
    return f"""STRUCTURE-REPAIR: Rewrite the supplied critique without adding, deleting, or strengthening findings.
Return exactly these Markdown sections: ## Accuracy, ## Native Voice, ## Notes & Adaptation, and ## Summary.
Every actionable line must use exactly one of these allowed chunk IDs followed immediately by a colon: {', '.join(sorted(allowed_chunk_ids))}.
Ranges, lists, invented chunk IDs, quotations, explanations, and prose outside the four sections are forbidden. Preserve at most {CRITIQUE_SHARD_VALIDATION_MAX_FINDINGS} actionable lines, each at most {CRITIQUE_SHARD_MAX_FINDING_CHARACTERS} characters. If a range finding cannot be assigned safely to one exact allowed chunk, omit it. Empty sections must contain only `- NO_ACTIONABLE_FINDINGS`."""


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return value


def _write_text(path: Path, text: str) -> str:
    normalized = text.rstrip() + "\n"
    runner.atomic_text(path, normalized)
    return normalized


def _phase_valid(phase: dict[str, Any], path: Path, input_hash: str) -> bool:
    return (
        phase.get("status") == "complete"
        and phase.get("input_hash") == input_hash
        and isinstance(phase.get("output_hash"), str)
        and runner.nonempty(path)
        and runner.text_hash(path.read_text(encoding="utf-8")) == phase["output_hash"]
    )


def _paper_phase_input_hash(
    instructions: str,
    input_text: str,
    max_output_tokens: int,
) -> str:
    return runner.text_hash(
        json.dumps(
            {
                "model": runner.MODEL,
                "provider": runner.ACTIVE_PROVIDER,
                "execution_lock_sha256": runner.ACTIVE_EXECUTION_LOCK_SHA256,
                "instructions": instructions,
                "input": input_text,
                "max_output_tokens": max_output_tokens,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _persist_status(path: Path, status: dict[str, Any]) -> None:
    status["updated_at"] = runner.now()
    runner.atomic_json(path, status)


def _run_paper_model_phase(
    *,
    phase_name: str,
    output_path: Path,
    instructions: str,
    input_text: str,
    max_output_tokens: int,
    client: Any,
    status: dict[str, Any],
    status_path: Path,
    run_id: str | None,
    budget_guard: runner.BudgetGuard | None,
    retry_uncertain: bool = False,
) -> str:
    article_dir = status_path.parent
    input_hash = _paper_phase_input_hash(
        instructions,
        input_text,
        max_output_tokens,
    )
    phase = status.setdefault("phases", {}).setdefault(phase_name, {})
    uncertainty_key = f"{status.get('record_id')}:{phase_name}:paper"
    if _phase_valid(phase, output_path, input_hash):
        return output_path.read_text(encoding="utf-8")
    prior_uncertain_cost = 0.0
    if (
        phase.get("status") in {"running", "uncertain"}
        and phase.get("input_hash") == input_hash
    ):
        if not retry_uncertain:
            raise runner.AmbiguousTransportError(
                f"Paper phase {phase_name} is uncertain; inspect it before replaying the paid request"
            )
        prior_uncertain_cost = float(phase.get("conservative_cost_rmb") or 0)
        if budget_guard is not None and prior_uncertain_cost <= 0:
            prior_uncertain_cost = budget_guard.unresolved_uncertain_cost(uncertainty_key)
        if prior_uncertain_cost <= 0:
            raise runner.AmbiguousTransportError(
                f"Paper phase {phase_name} is uncertain without a recorded budget contract; "
                "refusing replay"
            )

    phase.clear()
    phase.update(
        {
            "status": "running",
            "started_at": runner.now(),
            "input_hash": input_hash,
            "output_file": output_path.name,
            "max_output_tokens": max_output_tokens,
        }
    )
    if run_id is not None:
        phase["run_id"] = run_id
    if prior_uncertain_cost > 0:
        phase["uncertain_replays"] = [
            {
                "authorized_at": runner.now(),
                "uncertainty_key": uncertainty_key,
                "conservative_cost_rmb": prior_uncertain_cost,
            }
        ]
    _persist_status(status_path, status)

    def paid_request(
        request_instructions: str,
        *,
        request_uncertainty_key: str,
        request_kind: str,
    ) -> tuple[dict[str, Any], float]:
        reservation: str | None = None
        try:
            if budget_guard is not None:
                reservation = budget_guard.reserve(
                    request_instructions + "\n" + input_text,
                    max_output_tokens,
                    uncertainty_key=request_uncertainty_key,
                )
            response, latency = client.complete(
                request_instructions, input_text, max_output_tokens
            )
        except runner.AmbiguousTransportError as exc:
            conservative_cost_rmb = (
                budget_guard.commit_estimate(reservation)
                if reservation is not None
                else 0.0
            )
            if conservative_cost_rmb > 0:
                runner.append_cost_ledger(
                    article_dir,
                    {
                        "event_id": uuid.uuid4().hex,
                        "kind": "paper_ambiguous_transport_reservation",
                        "phase": phase_name,
                        "input_hash": input_hash,
                        "request_kind": request_kind,
                        "cost_rmb": conservative_cost_rmb,
                    },
                )
            phase.update(
                {
                    "status": "uncertain",
                    "finished_at": runner.now(),
                    "error": str(exc),
                    "conservative_cost_rmb": conservative_cost_rmb,
                    "uncertainty_key": request_uncertainty_key,
                    "uncertainty_reservation_id": reservation,
                }
            )
            _persist_status(status_path, status)
            raise
        except Exception as exc:
            conservative_cost_rmb = (
                budget_guard.commit_estimate(reservation)
                if reservation is not None
                else 0.0
            )
            if conservative_cost_rmb > 0:
                runner.append_cost_ledger(
                    article_dir,
                    {
                        "event_id": uuid.uuid4().hex,
                        "kind": "paper_failed_transport_reservation",
                        "phase": phase_name,
                        "input_hash": input_hash,
                        "request_kind": request_kind,
                        "cost_rmb": conservative_cost_rmb,
                    },
                )
            phase.update(
                {
                    "status": "failed",
                    "finished_at": runner.now(),
                    "error": repr(exc),
                    "conservative_cost_rmb": conservative_cost_rmb,
                }
            )
            _persist_status(status_path, status)
            raise

        billed_usage = runner.coarse_response_usage(response)
        if reservation is not None:
            budget_guard.settle(reservation, billed_usage)
            budget_guard.resolve_uncertain(request_uncertainty_key)
        runner.append_cost_ledger(
            article_dir,
            {
                "event_id": str(response.get("id") or uuid.uuid4().hex),
                "kind": "paper_settled_response",
                "phase": phase_name,
                "input_hash": input_hash,
                "request_kind": request_kind,
                "usage": billed_usage,
                "cost_rmb": runner.estimate_cost_rmb(
                    billed_usage,
                    budget_guard.usd_cny_rate
                    if budget_guard is not None
                    else runner.DEFAULT_USD_CNY_RATE,
                ),
            },
        )
        return response, latency

    response, latency = paid_request(
        instructions,
        request_uncertainty_key=uncertainty_key,
        request_kind="primary",
    )
    responses = [response]
    total_latency = latency
    phase["raw_response"] = runner.response_metadata(response)
    _persist_status(status_path, status)
    try:
        parsed = runner.validate_response(response, runner.MODEL)
    except runner.IncompleteResponseError as exc:
        if "max_output_tokens" not in str(exc):
            phase.update({"status": "failed", "finished_at": runner.now(), "error": str(exc)})
            _persist_status(status_path, status)
            raise
        retry_instructions = (
            instructions
            + "\n\nOUTPUT-COMPRESSION RETRY: The previous answer reached the output-token "
            "ceiling. Return only the exact required section headings and obey the "
            "original instructions' stricter count and per-line length limits. Omit method, praise, "
            "examples, quotations, and commentary. Do not relax any accuracy check."
        )
        phase.setdefault("output_retries", []).append(
            {
                "attempt": 1,
                "authorized_at": runner.now(),
                "reason": "max_output_tokens",
                "previous_response_id": str(response.get("id") or ""),
            }
        )
        _persist_status(status_path, status)
        response, retry_latency = paid_request(
            retry_instructions,
            request_uncertainty_key=uncertainty_key + ":compression-1",
            request_kind="output_compression_retry",
        )
        responses.append(response)
        total_latency += retry_latency
        phase["raw_response"] = runner.response_metadata(response)
        _persist_status(status_path, status)
        try:
            parsed = runner.validate_response(response, runner.MODEL)
        except Exception as retry_exc:
            phase.update(
                {
                    "status": "failed",
                    "finished_at": runner.now(),
                    "error": str(retry_exc),
                }
            )
            _persist_status(status_path, status)
            raise
    except Exception as exc:
        phase.update({"status": "failed", "finished_at": runner.now(), "error": str(exc)})
        _persist_status(status_path, status)
        raise

    text = _write_text(output_path, parsed.text)
    usage = {
        key: sum(
            int(runner.coarse_response_usage(item).get(key) or 0)
            for item in responses
        )
        for key in (
            "input_tokens",
            "cached_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
        )
    }
    usage["latency_seconds"] = total_latency
    phase.update(
        {
            "status": "complete",
            "finished_at": runner.now(),
            "response_id": parsed.response_id,
            "usage": usage,
            "output_hash": runner.text_hash(text),
        }
    )
    _persist_status(status_path, status)
    return text


def _deterministic_phase(
    *,
    name: str,
    path: Path,
    text: str,
    input_hash: str,
    status: dict[str, Any],
    status_path: Path,
) -> str:
    phase = status.setdefault("phases", {}).setdefault(name, {})
    if _phase_valid(phase, path, input_hash):
        return path.read_text(encoding="utf-8")
    written = _write_text(path, text)
    phase.clear()
    phase.update(
        {
            "status": "complete",
            "finished_at": runner.now(),
            "input_hash": input_hash,
            "output_file": path.name,
            "output_hash": runner.text_hash(written),
        }
    )
    _persist_status(status_path, status)
    return written


def _tagged_source(article_dir: Path, chunks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for chunk in chunks:
        source_path = runner.article_artifact_path(article_dir, str(chunk["source_file"]))
        source = source_path.read_text(encoding="utf-8").rstrip()
        unit_id = str(chunk.get("babeldoc_unit_id", ""))
        parts.append(f"<!-- {chunk['id']} {unit_id} -->\n{source}\n")
    return "".join(parts)


def _merge_tagged_outputs(
    chunks: list[dict[str, Any]],
    texts: dict[str, str],
) -> str:
    parts: list[str] = []
    for chunk in chunks:
        chunk_id = str(chunk["id"])
        text = texts[chunk_id]
        unit_id = str(chunk.get("babeldoc_unit_id", ""))
        parts.append(f"<!-- {chunk_id} {unit_id} -->\n{text.rstrip()}\n")
    return "".join(parts)


def _critique_shard_count(
    chunks: list[dict[str, Any]],
    *,
    source_texts: dict[str, str],
    draft_texts: dict[str, str],
    shard_char_limit: int,
) -> int:
    if shard_char_limit <= 0:
        raise ValueError("shard_char_limit must be positive")
    count = 0
    shard_size = 0
    has_items = False
    for chunk in chunks:
        chunk_id = str(chunk["id"])
        unit_id = str(chunk.get("babeldoc_unit_id", ""))
        source_part = f"<!-- {chunk_id} {unit_id} -->\n{source_texts[chunk_id].rstrip()}\n"
        draft_part = f"<!-- {chunk_id} {unit_id} -->\n{draft_texts[chunk_id].rstrip()}\n"
        pair_size = len(source_part) + len(draft_part)
        if has_items and shard_size + pair_size > shard_char_limit:
            count += 1
            shard_size = 0
            has_items = False
        shard_size += pair_size
        has_items = True
    if has_items:
        count += 1
    return count


def _critique_draft_character_bound(source_text: str) -> int:
    source_length = len(source_text.rstrip())
    return max(
        source_length * CRITIQUE_DRAFT_MAX_SOURCE_RATIO,
        source_length + CRITIQUE_DRAFT_MAX_EXTRA_CHARACTERS,
    )


def _projected_unknown_critique_shard_count(
    chunks: list[dict[str, Any]],
    *,
    source_texts: dict[str, str],
    shard_char_limit: int = CRITIQUE_SHARD_CHAR_LIMIT,
) -> int:
    """Bound critique shards before translated drafts exist.

    Execution enforces the same per-chunk bound before critique, so this is a
    launch-budget invariant rather than a heuristic request estimate.
    """

    bounded_drafts = {
        str(chunk["id"]): "中" * _critique_draft_character_bound(
            source_texts[str(chunk["id"])]
        )
        for chunk in chunks
    }
    return _critique_shard_count(
        chunks,
        source_texts=source_texts,
        draft_texts=bounded_drafts,
        shard_char_limit=shard_char_limit,
    )


def _require_critique_drafts_within_projection_bound(
    chunks: list[dict[str, Any]],
    *,
    source_texts: dict[str, str],
    draft_texts: dict[str, str],
) -> None:
    oversized = []
    for chunk in chunks:
        chunk_id = str(chunk["id"])
        actual = len(draft_texts[chunk_id].rstrip())
        allowed = _critique_draft_character_bound(source_texts[chunk_id])
        if actual > allowed:
            oversized.append(f"{chunk_id} ({actual}>{allowed})")
    if oversized:
        raise RuntimeError(
            "Critique draft exceeds the preflight projection bound: "
            + ", ".join(oversized)
        )


def _critique_shards(
    article_dir: Path,
    chunks: list[dict[str, Any]],
    *,
    shard_char_limit: int,
) -> list[tuple[str, str]]:
    """Build aligned source/draft shards without splitting a semantic chunk."""

    if shard_char_limit <= 0:
        raise ValueError("shard_char_limit must be positive")
    shards: list[tuple[str, str]] = []
    source_parts: list[str] = []
    draft_parts: list[str] = []
    shard_size = 0
    for chunk in chunks:
        chunk_id = str(chunk["id"])
        unit_id = str(chunk.get("babeldoc_unit_id", ""))
        source = runner.article_artifact_path(
            article_dir, str(chunk["source_file"])
        ).read_text(encoding="utf-8").rstrip()
        draft_path = runner.stage_output_path(
            article_dir,
            chunk_id,
            str(chunk["output_file"]),
            "terminology",
        )
        if not runner.nonempty(draft_path):
            raise RuntimeError(f"Missing terminology draft for critique shard: {chunk_id}")
        draft = draft_path.read_text(encoding="utf-8").rstrip()
        source_part = f"<!-- {chunk_id} {unit_id} -->\n{source}\n"
        draft_part = f"<!-- {chunk_id} {unit_id} -->\n{draft}\n"
        pair_size = len(source_part) + len(draft_part)
        if source_parts and shard_size + pair_size > shard_char_limit:
            shards.append(("".join(source_parts), "".join(draft_parts)))
            source_parts = []
            draft_parts = []
            shard_size = 0
        source_parts.append(source_part)
        draft_parts.append(draft_part)
        shard_size += pair_size
    if source_parts:
        shards.append(("".join(source_parts), "".join(draft_parts)))
    return shards


def _merge_sharded_critiques(
    shard_outputs: list[str],
    *,
    max_findings: int = CRITIQUE_GLOBAL_MAX_FINDINGS,
    max_findings_per_shard: int = CRITIQUE_SHARD_VALIDATION_MAX_FINDINGS,
    max_finding_characters: int = CRITIQUE_SHARD_MAX_FINDING_CHARACTERS,
) -> str:
    """Round-robin actionable findings so late shards retain review coverage."""

    def split_finding(value: str) -> list[str]:
        if len(value) <= max_finding_characters:
            return [value]
        label = re.match(r"^(chunk\d{4}:\s*)", value, flags=re.I)
        if label is None:
            raise RuntimeError("critique finding has no routable chunk label")
        prefix = label.group(1)
        body = value[label.end() :]
        body_limit = max_finding_characters - len(prefix)
        if body_limit <= 0:
            raise RuntimeError("critique finding character limit cannot contain its chunk label")
        parts: list[str] = []
        while len(body) > body_limit:
            window = body[:body_limit]
            boundaries = [
                match.end()
                for match in re.finditer(r"[。；;！？!?，,]\s*|\s+", window)
            ]
            cut = boundaries[-1] if boundaries else body_limit
            parts.append(prefix + body[:cut])
            body = body[cut:]
        if body:
            parts.append(prefix + body)
        return parts

    if max_findings <= 0:
        raise ValueError("max_findings must be positive")
    if max_findings_per_shard <= 0 or max_finding_characters <= 0:
        raise ValueError("per-shard critique limits must be positive")
    per_shard: list[list[str]] = []
    for shard_index, output in enumerate(shard_outputs, 1):
        findings: list[str] = []
        seen: set[str] = set()
        original_finding_count = 0
        section = ""
        for line in output.splitlines():
            normalized = line.strip()
            heading = re.match(r"^##\s+(.+?)\s*$", normalized)
            if heading:
                section = heading.group(1)
                continue
            if section not in {"Accuracy", "Native Voice", "Notes & Adaptation"}:
                continue
            if not normalized or "NO_ACTIONABLE_FINDINGS" in normalized:
                continue
            match = re.match(
                r"^(?:[-*]\s*|\d+[.)]\s*)?(chunk\d{4}:\s*.+)$",
                normalized,
                flags=re.I,
            )
            if match is None:
                raise RuntimeError(
                    f"critique shard {shard_index} contains unparseable actionable content: "
                    f"{normalized[:160]}"
                )
            original_finding_count += 1
            for fragment in split_finding(match.group(1)):
                finding = "- " + fragment
                if finding not in seen:
                    findings.append(finding)
                    seen.add(finding)
        if original_finding_count > max_findings_per_shard:
            raise RuntimeError(
                f"critique shard {shard_index} exceeds "
                f"{max_findings_per_shard} actionable findings"
            )
        per_shard.append(findings)
    selected: list[str] = []
    index = 0
    while len(selected) < max_findings:
        added = False
        for findings in per_shard:
            if index < len(findings):
                selected.append(findings[index])
                added = True
                if len(selected) == max_findings:
                    break
        if not added:
            break
        index += 1
    body = "\n".join(selected) if selected else "- NO_ACTIONABLE_FINDINGS"
    return (
        "## Accuracy\n"
        + body
        + "\n\n## Native Voice\n- Findings are included above by chunk ID.\n\n"
        "## Notes & Adaptation\n- Preserve all locked terminology and immutable structures.\n\n"
        "## Summary\n- Deterministically merged from aligned critique shards.\n"
    )


def _bound_shard_critique(
    output: str,
    *,
    maximum: int = CRITIQUE_SHARD_VALIDATION_MAX_FINDINGS,
) -> str:
    """Deterministically cap already-routable findings in model-ranked order."""

    if maximum <= 0:
        raise ValueError("maximum must be positive")
    section = ""
    findings: list[tuple[str, str]] = []
    for line in output.splitlines():
        normalized = line.strip()
        heading = re.match(r"^##\s+(.+?)\s*$", normalized)
        if heading:
            section = heading.group(1)
            continue
        if section not in {"Accuracy", "Native Voice", "Notes & Adaptation"}:
            continue
        match = re.match(
            r"^(?:[-*]\s*|\d+[.)]\s*)?(chunk\d{4}:\s*.+)$",
            normalized,
            flags=re.I,
        )
        if match:
            findings.append((section, "- " + match.group(1)))
    selected = findings[:maximum]
    blocks: list[str] = []
    for name in ("Accuracy", "Native Voice", "Notes & Adaptation"):
        lines = [line for section_name, line in selected if section_name == name]
        blocks.append(f"## {name}\n" + ("\n".join(lines) if lines else "- NO_ACTIONABLE_FINDINGS"))
    blocks.append("## Summary\n- Deterministically bounded from model-ranked findings.")
    return "\n\n".join(blocks) + "\n"


def _validate_shard_critique(
    output: str,
    *,
    shard_index: int,
    allowed_chunk_ids: set[str],
) -> None:
    """Reject findings that cannot be routed to one exact chunk in this shard."""

    _require_sections(
        output,
        ("Accuracy", "Native Voice", "Notes & Adaptation", "Summary"),
        f"critique shard {shard_index}",
    )
    try:
        _merge_sharded_critiques([output])
    except RuntimeError as error:
        raise RuntimeError(f"critique shard {shard_index}: {error}") from error
    referenced = set(re.findall(r"\bchunk\d{4}\b", output, flags=re.I))
    unexpected = sorted(item.lower() for item in referenced if item.lower() not in allowed_chunk_ids)
    if unexpected:
        raise RuntimeError(
            f"critique shard {shard_index} references chunks outside its aligned input: "
            + ", ".join(unexpected)
        )


def _run_sharded_critique(
    *,
    article_dir: Path,
    chunks: list[dict[str, Any]],
    client: Any,
    status: dict[str, Any],
    status_path: Path,
    run_id: str | None,
    budget_guard: runner.BudgetGuard | None,
    retry_uncertain: bool,
    shard_char_limit: int = CRITIQUE_SHARD_CHAR_LIMIT,
) -> str:
    shards = _critique_shards(
        article_dir,
        chunks,
        shard_char_limit=shard_char_limit,
    )
    shard_dir = article_dir / "critique_shards"
    shard_outputs: list[str] = []
    instructions = _critique_shard_instructions()
    for index, (source_shard, draft_shard) in enumerate(shards, 1):
        allowed_chunk_ids = {
            item.lower() for item in re.findall(r"\bchunk\d{4}\b", source_shard, flags=re.I)
        }
        output = _run_paper_model_phase(
            phase_name=f"critique_shard_{index:04d}",
            output_path=shard_dir / f"shard{index:04d}.md",
            instructions=instructions,
            input_text=(
                f"ENGLISH SOURCE SHARD {index}/{len(shards)}:\n{source_shard}\n\n"
                f"CHINESE DRAFT SHARD {index}/{len(shards)}:\n{draft_shard}"
            ),
            max_output_tokens=CRITIQUE_SHARD_MAX_OUTPUT_TOKENS,
            client=client,
            status=status,
            status_path=status_path,
            run_id=run_id,
            budget_guard=budget_guard,
            retry_uncertain=retry_uncertain,
        )
        try:
            _validate_shard_critique(
                output,
                shard_index=index,
                allowed_chunk_ids=allowed_chunk_ids,
            )
        except RuntimeError as validation_error:
            repair_instructions = _critique_shard_repair_instructions(allowed_chunk_ids)
            output = _run_paper_model_phase(
                phase_name=f"critique_shard_repair_{index:04d}",
                output_path=article_dir / "critique_shard_repairs" / f"shard{index:04d}.md",
                instructions=repair_instructions,
                input_text=(
                    f"VALIDATION ERROR:\n{validation_error}\n\n"
                    f"CRITIQUE TO REPAIR:\n{output}"
                ),
                max_output_tokens=CRITIQUE_SHARD_MAX_OUTPUT_TOKENS,
                client=client,
                status=status,
                status_path=status_path,
                run_id=run_id,
                budget_guard=budget_guard,
                retry_uncertain=retry_uncertain,
            )
            try:
                _validate_shard_critique(
                    output,
                    shard_index=index,
                    allowed_chunk_ids=allowed_chunk_ids,
                )
            except RuntimeError as repair_error:
                if "exceeds" not in str(repair_error):
                    raise
                referenced = {
                    item.lower()
                    for item in re.findall(r"\bchunk\d{4}\b", output, flags=re.I)
                }
                unexpected = sorted(referenced - allowed_chunk_ids)
                if unexpected:
                    raise RuntimeError(
                        f"critique shard {index} references chunks outside its aligned input: "
                        + ", ".join(unexpected)
                    ) from repair_error
                output = _bound_shard_critique(output)
                repair_path = article_dir / "critique_shard_repairs" / f"shard{index:04d}.md"
                output = _write_text(repair_path, output)
                repair_phase = status.setdefault("phases", {}).setdefault(
                    f"critique_shard_repair_{index:04d}", {}
                )
                repair_phase["output_hash"] = runner.text_hash(output)
                repair_phase["deterministically_bounded"] = True
                _persist_status(status_path, status)
                _validate_shard_critique(
                    output,
                    shard_index=index,
                    allowed_chunk_ids=allowed_chunk_ids,
                )
        shard_outputs.append(output)
    merged = _merge_sharded_critiques(shard_outputs)
    signature = runner.text_hash(
        json.dumps(
            {
                "schema_version": 1,
                "shard_hashes": [runner.text_hash(item) for item in shard_outputs],
                "max_findings": CRITIQUE_GLOBAL_MAX_FINDINGS,
            },
            sort_keys=True,
        )
    )
    return _deterministic_phase(
        name="critique_merge",
        path=article_dir / CRITIQUE_FILE,
        text=merged,
        input_hash=signature,
        status=status,
        status_path=status_path,
    )


def _valid_legacy_critique(
    *,
    article_dir: Path,
    status: dict[str, Any],
    instructions: str,
    source: str,
    draft: str,
) -> str | None:
    """Reuse a pre-sharding critique only when its exact aligned input still matches."""

    path = article_dir / CRITIQUE_FILE
    phase = status.get("phases", {}).get("critique", {})
    if not isinstance(phase, dict):
        return None
    input_text = f"ENGLISH SOURCE:\n{source}\n\nCHINESE DRAFT:\n{draft}"
    input_hash = _paper_phase_input_hash(instructions, input_text, 4000)
    if _phase_valid(phase, path, input_hash):
        return path.read_text(encoding="utf-8")
    return None


def _reference_chunk_ids(article_dir: Path, chunks: list[dict[str, Any]]) -> set[str]:
    """Return the real bibliography tail, excluding a table-of-contents label."""

    resolved = [
        {
            **chunk,
            "source_file": str(
                runner.article_artifact_path(article_dir, str(chunk["source_file"]))
            ),
        }
        for chunk in chunks
    ]
    return set(reference_boundary(Path("/"), resolved)["chunk_ids"])


def _chunk_passthrough_reason(
    chunk: dict[str, Any],
    *,
    reference_ids: set[str],
    fragile_fragment_ids: set[str],
    figure_text_ids: set[str],
    table_text_ids: set[str] | None = None,
) -> str | None:
    chunk_id = str(chunk["id"])
    source_text = str(chunk.get("source_text") or "")
    if chunk_id in figure_text_ids:
        return "figure_internal_text_passthrough"
    if chunk_id in set(table_text_ids or ()):
        return "table_internal_text_passthrough"
    if chunk_id in reference_ids:
        return "reference_section_passthrough"
    if source_text.strip() and not re.search(r"[A-Za-z\u3400-\u9fff]", source_text):
        return "nonlinguistic_symbol_passthrough"
    if chunk_id in fragile_fragment_ids:
        return "fragile_layout_fragment_passthrough"
    return None


def _hard_exact_translations(
    article_dir: Path,
    record_id: str,
    *,
    policy_path: Path = TRACKED_HARD_CONSTRAINTS,
) -> dict[str, str]:
    """Load document-level exact translations used for repeated PDF artifacts."""

    local_path = article_dir / "hard_constraints.json"
    rule_sets: list[tuple[Path, list[dict[str, Any]]]] = []
    if policy_path.is_file():
        policy = _load_json(policy_path)
        if policy.get("schema_version") != 1 or not isinstance(
            policy.get("records"), dict
        ):
            raise RuntimeError(f"Invalid tracked hard constraint policy: {policy_path}")
        record_rules = policy["records"].get(record_id, {})
        if not isinstance(record_rules, dict):
            raise RuntimeError(f"Invalid tracked hard constraints for {record_id}")
        rule_sets.append((policy_path, record_rules.get("exact_translations", [])))
    if local_path.is_file():
        value = _load_json(local_path)
        if value.get("schema_version") != 1:
            raise RuntimeError(f"Unsupported hard constraint schema: {local_path}")
        if value.get("record_id") not in {None, record_id}:
            raise RuntimeError(f"Hard constraint record mismatch: {local_path}")
        rule_sets.append((local_path, value.get("exact_translations", [])))
    if not rule_sets:
        return {}
    mapping: dict[str, str] = {}
    for path, rules in rule_sets:
        if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
            raise RuntimeError(f"Exact translations must be a list of objects: {path}")
        for rule in constraint_compiler.body_exact_translation_rules(
            {"exact_translations": rules}
        ):
            source_text = " ".join(str(rule.get("source", "")).split())
            source = source_text.casefold()
            target = str(rule.get("target", "")).strip()
            if not source or not target:
                raise RuntimeError(f"Incomplete exact translation rule: {path}")
            if STRUCTURE_PLACEHOLDER_RE.findall(source_text) != STRUCTURE_PLACEHOLDER_RE.findall(target):
                raise RuntimeError(
                    "Exact translation changed structure placeholder order "
                    f"for {record_id} in {path}"
                )
            report = runner.validate_chunk(source_text, target, {}, [])
            if not report.ok:
                raise RuntimeError(
                    "Exact translation failed deterministic QC "
                    f"for {record_id} in {path}: {', '.join(report.failures)}"
                )
            mapping[source] = target
    return mapping


def _apply_manual_corrections(
    article_dir: Path,
    record_id: str,
    chunks: list[dict[str, Any]],
) -> int:
    """Apply explicit final-text corrections pinned to exact source hashes."""

    path = article_dir / MANUAL_CORRECTIONS_FILE
    if not path.exists():
        return 0
    manifest = _load_json(path)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported manual correction schema: {path}")
    if manifest.get("record_id") != record_id:
        raise RuntimeError(f"Manual correction record mismatch: {path}")
    corrections = manifest.get("corrections")
    if not isinstance(corrections, list):
        raise RuntimeError(f"Manual corrections must be a list: {path}")
    chunks_by_id = {str(chunk["id"]): chunk for chunk in chunks}
    seen: set[str] = set()
    applied = 0
    for correction in corrections:
        if not isinstance(correction, dict):
            raise RuntimeError(f"Manual correction entry is not an object: {path}")
        chunk_id = correction.get("chunk_id")
        source_hash = correction.get("source_hash")
        replacement = correction.get("replacement")
        reason = correction.get("reason")
        if not all(isinstance(value, str) and value for value in (
            chunk_id, source_hash, replacement, reason
        )):
            raise RuntimeError(f"Manual correction entry is incomplete: {path}")
        if chunk_id in seen:
            raise RuntimeError(f"Duplicate manual correction: {record_id}/{chunk_id}")
        seen.add(chunk_id)
        chunk = chunks_by_id.get(chunk_id)
        if chunk is None:
            raise RuntimeError(f"Unknown manual correction chunk: {record_id}/{chunk_id}")
        source_path = runner.article_artifact_path(article_dir, str(chunk["source_file"]))
        source = source_path.read_text(encoding="utf-8")
        live_source_hash = runner.text_hash(source)
        if live_source_hash != chunk.get("source_hash") or live_source_hash != source_hash:
            raise RuntimeError(
                f"Manual correction source hash mismatch: {record_id}/{chunk_id}"
            )
        qc_report = runner.validate_chunk(source, replacement, {}, [])
        numeric_localization = correction.get("allow_numeric_localization") is True
        if numeric_localization and "numbers_mismatch" in qc_report.failures:
            remaining_failures = tuple(
                failure
                for failure in qc_report.failures
                if failure != "numbers_mismatch"
            )
            qc_report = type(qc_report)(
                ok=not remaining_failures,
                failures=remaining_failures,
            )
        if not qc_report.ok:
            raise RuntimeError(
                f"Manual correction failed QC: {record_id}/{chunk_id}: "
                f"{', '.join(qc_report.failures)}"
            )
        output_path = runner.article_artifact_path(article_dir, str(chunk["output_file"]))
        previous_hash = (
            runner.text_hash(output_path.read_text(encoding="utf-8"))
            if output_path.exists()
            else None
        )
        runner.atomic_text(output_path, replacement)
        status_path = article_dir / "chunk_status" / f"{chunk_id}.json"
        status = _load_json(status_path)
        stage_status = status.setdefault("stages", {}).setdefault("academic", {})
        stage_status.update(
            {
                "status": "complete",
                "finished_at": runner.now(),
                "output_hash": runner.text_hash(replacement),
                "qc": qc_report.to_dict(),
                "manual_correction_applied": True,
                "manual_correction_reason": reason,
                "manual_correction_source_hash": source_hash,
                "manual_correction_previous_output_hash": previous_hash,
                "manual_correction_numeric_localization": numeric_localization,
            }
        )
        status["status"] = "complete"
        status["updated_at"] = runner.now()
        runner.atomic_json(status_path, status)
        applied += 1
    return applied


def _verified_merge(
    article_dir: Path,
    record_id: str,
    chunks: list[dict[str, Any]],
    stage: str,
) -> tuple[str, str]:
    parts: list[str] = []
    fingerprints: list[dict[str, str]] = []
    for chunk in chunks:
        chunk_id = str(chunk["id"])
        source_path = runner.article_artifact_path(article_dir, str(chunk["source_file"]))
        source_hash = runner.text_hash(source_path.read_text(encoding="utf-8"))
        if source_hash != chunk.get("source_hash"):
            raise RuntimeError(f"Source hash mismatch before merge: {record_id}/{chunk_id}")
        status_path = article_dir / "chunk_status" / f"{chunk_id}.json"
        chunk_status = _load_json(status_path)
        stage_status = chunk_status.get("stages", {}).get(stage, {})
        output_path = runner.stage_output_path(
            article_dir, chunk_id, str(chunk["output_file"]), stage
        )
        if stage_status.get("status") != "complete" or not runner.nonempty(output_path):
            raise RuntimeError(f"Incomplete {stage} checkpoint: {record_id}/{chunk_id}")
        output = output_path.read_text(encoding="utf-8")
        output_hash = runner.text_hash(output)
        if output_hash != stage_status.get("output_hash"):
            raise RuntimeError(f"Output hash mismatch before merge: {record_id}/{chunk_id}/{stage}")
        unit_id = str(chunk.get("babeldoc_unit_id", ""))
        parts.append(f"<!-- {chunk_id} {unit_id} -->\n{output.rstrip()}\n")
        fingerprints.append(
            {"chunk_id": chunk_id, "source_hash": source_hash, "output_hash": output_hash}
        )
    signature = runner.text_hash(json.dumps(fingerprints, sort_keys=True))
    return "".join(parts), signature


def _prompt_text(analysis: str, terms: list[dict[str, Any]]) -> str:
    return f"""# Paper Translation Brief

{analysis.rstrip()}

## Locked Terminology
{runner.glossary_text(terms)}

## Non-negotiable Constraints
- Faithfully preserve facts, modality, names, citations, numbers, units, formulas, URLs, and structure.
- Apply the analysis as read-only context; do not reproduce it in the translation.
- Use restrained, idiomatic Simplified Chinese academic prose.
"""


def _require_sections(text: str, sections: tuple[str, ...], artifact: str) -> None:
    missing = [section for section in sections if f"## {section}" not in text]
    if missing:
        raise RuntimeError(f"{artifact} is missing required sections: {', '.join(missing)}")


def existing_article_cost_rmb(article_dir: Path, usd_cny_rate: float) -> float:
    """Recover settled API cost from durable paper/chunk checkpoints without double count."""

    ledger_path = article_dir / "api_cost_ledger.jsonl"
    baseline_path = article_dir / "cost_ledger_baseline.json"
    if ledger_path.is_file() or baseline_path.is_file():
        baseline = 0.0
        if baseline_path.is_file():
            baseline = float(_load_json(baseline_path).get("cost_rmb") or 0)
        events: dict[str, float] = {}
        if ledger_path.is_file():
            for line_number, line in enumerate(
                ledger_path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not line.strip():
                    continue
                event = json.loads(line)
                event_id = str(event.get("event_id") or f"line-{line_number}")
                events.setdefault(event_id, float(event.get("cost_rmb") or 0))
        return baseline + sum(events.values())

    usages: list[dict[str, Any]] = []
    paper_status_path = article_dir / "paper_status.json"
    if paper_status_path.is_file():
        paper_status = _load_json(paper_status_path)
        for phase in paper_status.get("phases", {}).values():
            if isinstance(phase, dict) and isinstance(phase.get("usage"), dict):
                usages.append(phase["usage"])
    for status_path in sorted((article_dir / "chunk_status").glob("*.json")):
        chunk_status = _load_json(status_path)
        for stage in chunk_status.get("stages", {}).values():
            if not isinstance(stage, dict):
                continue
            if isinstance(stage.get("usage"), dict):
                usages.append(stage["usage"])
            else:
                for subrequest in stage.get("subrequests", []):
                    if isinstance(subrequest, dict) and isinstance(subrequest.get("usage"), dict):
                        usages.append(subrequest["usage"])
    settled = sum(runner.estimate_cost_rmb(usage, usd_cny_rate) for usage in usages)
    conservative = 0.0
    if paper_status_path.is_file():
        paper_status = _load_json(paper_status_path)
        for phase in paper_status.get("phases", {}).values():
            if not isinstance(phase, dict):
                continue
            conservative += float(phase.get("conservative_cost_rmb") or 0)
            conservative += sum(
                float(item.get("conservative_cost_rmb") or 0)
                for item in phase.get("uncertain_replays", [])
                if isinstance(item, dict)
            )
    for status_path in sorted((article_dir / "chunk_status").glob("*.json")):
        chunk_status = _load_json(status_path)
        for stage in chunk_status.get("stages", {}).values():
            if isinstance(stage, dict):
                conservative += float(stage.get("conservative_cost_rmb") or 0)
                conservative += sum(
                    float(item.get("conservative_cost_rmb") or 0)
                    for item in stage.get("subrequests", [])
                    if isinstance(item, dict)
                )
                conservative += sum(
                    float(item.get("conservative_cost_rmb") or 0)
                    for item in stage.get("uncertain_replays", [])
                    if isinstance(item, dict)
                )
    return settled + conservative


def _run_chunk_barrier(
    chunks: list[dict[str, Any]],
    *,
    concurrency: int,
    invoke: Any,
    phase: str,
    record_id: str,
    max_qc_retries: int = 1,
    stop_event: Any = None,
    progress_callback: Any = None,
    heartbeat_every: int = 50,
) -> None:
    """Run independent chunks concurrently and stop before the next paper phase on failure."""

    if heartbeat_every <= 0:
        raise ValueError("heartbeat_every must be positive")
    pending = list(chunks)
    total = len(chunks)
    failures: dict[str, str] = {}
    terminal_failures: dict[str, str] = {}
    budget_failure: runner.BudgetExceededError | None = None
    for attempt in range(max_qc_retries + 1):
        retryable: list[dict[str, Any]] = []
        completed_this_attempt = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            iterator = iter(pending)
            future_map: dict[concurrent.futures.Future[Any], dict[str, Any]] = {}
            for _ in range(concurrency):
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
                future_map[executor.submit(invoke, chunk, attempt)] = chunk
            while future_map:
                done, _remaining = concurrent.futures.wait(
                    future_map,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    chunk = future_map.pop(future)
                    chunk_id = str(chunk["id"])
                    try:
                        result = future.result()
                        result_status = result.get("status")
                        if result_status == "complete":
                            failures.pop(chunk_id, None)
                        elif result_status == "uncertain":
                            if stop_event is not None:
                                stop_event.set()
                            terminal_failures[chunk_id] = "uncertain"
                        else:
                            failures[chunk_id] = str(result_status)
                            retryable.append(chunk)
                    except runner.BudgetExceededError as exc:
                        if stop_event is not None:
                            stop_event.set()
                        if budget_failure is None:
                            budget_failure = exc
                        terminal_failures[chunk_id] = f"{type(exc).__name__}:{exc}"
                    except Exception as exc:
                        failures[chunk_id] = f"{type(exc).__name__}:{exc}"
                        retryable.append(chunk)
                    completed_this_attempt += 1
                    if (
                        progress_callback is not None
                        and (
                            completed_this_attempt % heartbeat_every == 0
                            or completed_this_attempt == len(pending)
                        )
                    ):
                        progress_callback(
                            {
                                "record_id": record_id,
                                "phase": phase,
                                "attempt": attempt,
                                "completed": completed_this_attempt,
                                "total": len(pending),
                                "paper_total": total,
                                "retryable": len(retryable),
                                "terminal_failures": len(terminal_failures),
                            }
                        )
                if terminal_failures or (stop_event is not None and stop_event.is_set()):
                    for queued in future_map:
                        queued.cancel()
                    continue
                try:
                    next_chunk = next(iterator)
                except StopIteration:
                    continue
                future_map[executor.submit(invoke, next_chunk, attempt)] = next_chunk
        if not retryable:
            break
        if attempt == max_qc_retries:
            break
        pending = retryable
    failures.update(terminal_failures)
    if budget_failure is not None:
        raise budget_failure
    if failures:
        raise RuntimeError(
            f"{phase} barrier failed for {record_id}: "
            + "; ".join(f"{key}:{value}" for key, value in sorted(failures.items())[:20])
        )


def _print_barrier_progress(event: dict[str, Any]) -> None:
    print(
        "PAPER_PROGRESS "
        f"{event['record_id']} phase={event['phase']} attempt={event['attempt']} "
        f"{event['completed']}/{event['total']} retryable={event['retryable']} "
        f"terminal={event['terminal_failures']}",
        flush=True,
    )


def _style_batch_max_output_tokens(plan: style_batching.StyleStagePlan) -> int:
    batch_characters = max(
        (
            sum(len(item.protected_text) for item in batch.items)
            for batch in plan.normal_batches
        ),
        default=0,
    )
    return max(4096, min(20000, int(max(batch_characters, 4000) * 0.8)))


def _require_style_request_capacity(
    budget_guard: Any,
    *,
    stage: str,
    worst_case_requests: int,
) -> None:
    """Fail before a style stage when its exact plan exceeds the durable call cap."""

    if budget_guard is None or not hasattr(budget_guard, "snapshot"):
        return
    snapshot = budget_guard.snapshot()
    remaining = snapshot.get("stage_remaining_api_calls")
    if remaining is None:
        return
    remaining = max(0, int(remaining))
    if worst_case_requests > remaining:
        raise runner.BudgetExceededError(
            f"{stage} style projection worst case would exceed the remaining stage request cap: "
            f"{worst_case_requests} > {remaining}"
        )


def _legacy_style_batch_projection(planned: dict[str, dict[str, Any]]) -> dict[str, Any]:
    model_chunks: list[str] = []
    seen_chunk_ids: set[str] = set()
    groups: list[list[str]] = []
    seen_groups: set[tuple[str, ...]] = set()
    groupable_chunk_ids: set[str] = set()
    max_group_characters = 0
    current_style_requests = 0
    projected_style_requests = 0

    for stage_name in ("anti_ai", "academic"):
        plan = planned.get(stage_name)
        if not isinstance(plan, dict):
            continue
        stage_model_chunks = plan.get("model_chunks")
        if isinstance(stage_model_chunks, list):
            for chunk_id in stage_model_chunks:
                if isinstance(chunk_id, str) and chunk_id not in seen_chunk_ids:
                    seen_chunk_ids.add(chunk_id)
                    model_chunks.append(chunk_id)
            current_style_requests += len(stage_model_chunks)
        stage_batches = plan.get("estimated_normal_batches") or plan.get("normal_batches")
        stage_batch_characters = (
            plan.get("estimated_normal_batch_characters") or plan.get("normal_batch_characters")
        )
        if isinstance(stage_batches, list):
            projected_style_requests += int(
                plan.get("estimated_normal_requests")
                or plan.get("normal_requests")
                or len(stage_batches)
            )
            for index, batch in enumerate(stage_batches):
                if not isinstance(batch, list) or not all(isinstance(chunk_id, str) for chunk_id in batch):
                    continue
                batch_key = tuple(batch)
                if batch_key not in seen_groups:
                    seen_groups.add(batch_key)
                    groups.append(list(batch))
                if len(batch) > 1:
                    groupable_chunk_ids.update(batch)
                if isinstance(stage_batch_characters, list) and index < len(stage_batch_characters):
                    max_group_characters = max(
                        max_group_characters,
                        int(stage_batch_characters[index] or 0),
                    )

    eligible_chunks = len(model_chunks)
    projected_groups = len(groups)
    groupable_chunks = len(groupable_chunk_ids)
    return {
        "eligible_chunks": eligible_chunks,
        "groupable_chunks": groupable_chunks,
        "non_groupable_chunks": max(0, eligible_chunks - groupable_chunks),
        "current_style_requests": current_style_requests,
        "projected_style_requests": projected_style_requests,
        "projected_request_reduction_fraction": (
            (current_style_requests - projected_style_requests) / current_style_requests
            if current_style_requests
            else 0.0
        ),
        "projected_groups": projected_groups,
        "max_group_size": max((len(group) for group in groups), default=0),
        "max_group_characters": max_group_characters,
        "groups": groups,
    }


def _persist_style_batch_projection(
    article_dir: Path,
    *,
    status: dict[str, Any],
    status_path: Path,
    planned_updates: dict[str, dict[str, Any]] | None = None,
    actual_updates: dict[str, dict[str, Any]] | None = None,
) -> None:
    projection_path = article_dir / "style_batch_projection.json"
    payload = {
        "schema_version": 1,
        "execution_mode": "exact_id_batching",
        "planned": {},
        "actual": {},
    }
    if projection_path.exists():
        try:
            existing = json.loads(projection_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if isinstance(existing, dict):
            payload.update(
                {
                    "planned": existing.get("planned", {}) if isinstance(existing.get("planned"), dict) else {},
                    "actual": existing.get("actual", {}) if isinstance(existing.get("actual"), dict) else {},
                }
            )
    if planned_updates:
        payload["planned"].update(planned_updates)
    if actual_updates:
        payload["actual"].update(actual_updates)
    payload.update(_legacy_style_batch_projection(payload["planned"]))
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    runner.atomic_text(projection_path, text)
    phase = status.setdefault("phases", {}).setdefault("style_batch_projection", {})
    phase.update(
        {
            "status": "complete",
            "output_file": projection_path.name,
            "output_hash": runner.text_hash(text),
            "execution_mode": "exact_id_batching",
            "finished_at": runner.now(),
        }
    )
    _persist_status(status_path, status)


def run_batched_final_style_passes(
    article_dir: Path,
    *,
    chunks: list[dict[str, Any]],
    chunk_task: Any,
    terms: list[dict[str, Any]],
    critique: str,
    client: Any,
    status: dict[str, Any],
    status_path: Path,
    budget_guard: runner.BudgetGuard | None,
    run_id: str | None,
) -> dict[str, style_batching.StyleStageResult]:
    context_factory = lambda chunk: _critique_context_for_chunk(critique, str(chunk["id"]))

    anti_ai_plan = style_batching.prepare_style_items(
        article_dir=article_dir,
        chunks=chunks,
        task_factory=chunk_task,
        terms=terms,
        stage="anti_ai",
        input_stage="revision",
        context_factory=context_factory,
    )
    _persist_style_batch_projection(
        article_dir,
        status=status,
        status_path=status_path,
        planned_updates={"anti_ai": style_batching.stage_plan_projection(anti_ai_plan)},
    )
    _require_style_request_capacity(
        budget_guard,
        stage="anti_ai",
        worst_case_requests=anti_ai_plan.worst_case_requests,
    )
    anti_ai_result = style_batching.execute_style_stage(
        article_dir=article_dir,
        chunks=chunks,
        task_factory=chunk_task,
        terms=terms,
        stage="anti_ai",
        plan=anti_ai_plan,
        client=client,
        instructions=runner.stage_instructions("anti_ai", ""),
        max_output_tokens=_style_batch_max_output_tokens(anti_ai_plan),
        budget_guard=budget_guard,
        run_id=run_id,
        model=runner.MODEL,
    )
    _persist_style_batch_projection(
        article_dir,
        status=status,
        status_path=status_path,
        actual_updates={"anti_ai": style_batching.stage_result_projection(anti_ai_result)},
    )

    academic_plan = style_batching.prepare_style_items(
        article_dir=article_dir,
        chunks=chunks,
        task_factory=chunk_task,
        terms=terms,
        stage="academic",
        input_stage="anti_ai",
        context_factory=context_factory,
    )
    _persist_style_batch_projection(
        article_dir,
        status=status,
        status_path=status_path,
        planned_updates={"academic": style_batching.stage_plan_projection(academic_plan)},
    )
    _require_style_request_capacity(
        budget_guard,
        stage="academic",
        worst_case_requests=academic_plan.worst_case_requests,
    )
    academic_result = style_batching.execute_style_stage(
        article_dir=article_dir,
        chunks=chunks,
        task_factory=chunk_task,
        terms=terms,
        stage="academic",
        plan=academic_plan,
        client=client,
        instructions=runner.stage_instructions("academic", ""),
        max_output_tokens=_style_batch_max_output_tokens(academic_plan),
        budget_guard=budget_guard,
        run_id=run_id,
        model=runner.MODEL,
    )
    _persist_style_batch_projection(
        article_dir,
        status=status,
        status_path=status_path,
        actual_updates={"academic": style_batching.stage_result_projection(academic_result)},
    )

    return {"anti_ai": anti_ai_result, "academic": academic_result}


def run_batched_draft_passes(
    article_dir: Path,
    *,
    chunks: list[dict[str, Any]],
    chunk_task: Any,
    terms: list[dict[str, Any]],
    prompt: str,
    client: Any,
    budget_guard: runner.BudgetGuard | None,
    run_id: str | None,
) -> dict[str, style_batching.StyleStageResult]:
    """Batch plain draft chunks and retain the legacy structure-dense path."""

    def split_batch_safe(
        stage: str,
        candidates: list[dict[str, Any]],
        *,
        input_stage: str,
        context: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        safe: list[dict[str, Any]] = []
        legacy: list[dict[str, Any]] = []
        for chunk in candidates:
            task = chunk_task(chunk)
            if task.get("passthrough") or task.get("fixed_translation") is not None:
                safe.append(chunk)
                continue
            source = runner.article_artifact_path(
                article_dir, str(chunk["source_file"])
            ).read_text(encoding="utf-8")
            current_path = (
                runner.article_artifact_path(article_dir, str(chunk["source_file"]))
                if input_stage == "source"
                else runner.stage_output_path(
                    article_dir, str(chunk["id"]), str(chunk["output_file"]), input_stage
                )
            )
            current = current_path.read_text(encoding="utf-8")
            status_path = article_dir / "chunk_status" / f"{chunk['id']}.json"
            chunk_status = _load_json(status_path) if status_path.is_file() else {}
            stage_status = chunk_status.get("stages", {}).get(stage, {})
            plan = _planned_stage_model_subrequests(
                article_dir=article_dir,
                chunk=chunk,
                stage=stage,
                current=current,
                terms=terms,
                paper_context=context,
                stage_status=stage_status if isinstance(stage_status, dict) else {},
            )
            if not plan["decision"].should_call_model or (
                plan["model_subrequest_count"] == 1
            ):
                safe.append(chunk)
            else:
                legacy.append(chunk)
        return safe, legacy

    translate_chunks, translate_legacy = split_batch_safe(
        "translate", chunks, input_stage="source", context=prompt
    )
    translate_plan = style_batching.prepare_style_items(
        article_dir=article_dir,
        chunks=translate_chunks,
        task_factory=chunk_task,
        terms=terms,
        stage="translate",
        input_stage="source",
        context_factory=lambda _chunk: prompt,
        mandatory=True,
    )
    _require_style_request_capacity(
        budget_guard,
        stage="translate",
        worst_case_requests=translate_plan.worst_case_requests,
    )
    if len(translate_plan.model_items) < 2:
        translate_legacy.extend(
            chunk for chunk in translate_chunks
            if str(chunk["id"]) in {item.chunk_id for item in translate_plan.model_items}
        )
        translate_result = style_batching.StyleStageResult(
            planned_chunks=len(translate_plan.reused) + len(translate_plan.local),
            completed_chunks=len(translate_plan.reused) + len(translate_plan.local),
            reused_chunks=len(translate_plan.reused), local_chunks=len(translate_plan.local),
            failed_chunks=0, normal_requests=0, recovery_requests=0,
            input_tokens=0, cached_tokens=0, output_tokens=0, total_tokens=0,
            total_cost_rmb=0.0,
        )
    else:
        translate_result = style_batching.execute_style_stage(
        article_dir=article_dir,
        chunks=translate_chunks,
        task_factory=chunk_task,
        terms=terms,
        stage="translate",
        plan=translate_plan,
        client=client,
        instructions=runner.stage_instructions("translate", ""),
        max_output_tokens=_style_batch_max_output_tokens(translate_plan),
        budget_guard=budget_guard,
        run_id=run_id,
            model=runner.MODEL,
        )
    translate_fallback_ids = set(translate_result.failed_chunk_ids)
    translate_legacy.extend(
        chunk for chunk in translate_chunks if str(chunk["id"]) in translate_fallback_ids
    )
    _run_chunk_barrier(
        translate_legacy,
        concurrency=min(8, max(1, len(translate_legacy))),
        phase="draft_translate_legacy",
        record_id=str(chunk_task(chunks[0])["record_id"]),
        progress_callback=_print_barrier_progress,
        stop_event=getattr(budget_guard, "stop_event", None),
        invoke=lambda chunk, attempt: runner.process_chunk(
            chunk_task(chunk), client, terms, run_id, budget_guard,
            stages=("translate",),
            paper_context=_qc_retry_context(article_dir, chunk, prompt, attempt, terms=terms),
        ),
    )

    terminology_chunks, terminology_legacy = split_batch_safe(
        "terminology", chunks, input_stage="translate", context=""
    )
    terminology_plan = style_batching.prepare_style_items(
        article_dir=article_dir,
        chunks=terminology_chunks,
        task_factory=chunk_task,
        terms=terms,
        stage="terminology",
        input_stage="translate",
        context_factory=lambda _chunk: "",
        mandatory=True,
    )
    _require_style_request_capacity(
        budget_guard,
        stage="terminology",
        worst_case_requests=terminology_plan.worst_case_requests,
    )
    if len(terminology_plan.model_items) < 2:
        terminology_legacy.extend(
            chunk for chunk in terminology_chunks
            if str(chunk["id"]) in {item.chunk_id for item in terminology_plan.model_items}
        )
        terminology_result = style_batching.StyleStageResult(
            planned_chunks=len(terminology_plan.reused) + len(terminology_plan.local),
            completed_chunks=len(terminology_plan.reused) + len(terminology_plan.local),
            reused_chunks=len(terminology_plan.reused), local_chunks=len(terminology_plan.local),
            failed_chunks=0, normal_requests=0, recovery_requests=0,
            input_tokens=0, cached_tokens=0, output_tokens=0, total_tokens=0,
            total_cost_rmb=0.0,
        )
    else:
        terminology_result = style_batching.execute_style_stage(
        article_dir=article_dir,
        chunks=terminology_chunks,
        task_factory=chunk_task,
        terms=terms,
        stage="terminology",
        plan=terminology_plan,
        client=client,
        instructions=runner.stage_instructions("terminology", ""),
        max_output_tokens=_style_batch_max_output_tokens(terminology_plan),
        budget_guard=budget_guard,
        run_id=run_id,
            model=runner.MODEL,
        )
    terminology_fallback_ids = set(terminology_result.failed_chunk_ids)
    terminology_legacy.extend(
        chunk for chunk in terminology_chunks
        if str(chunk["id"]) in terminology_fallback_ids
    )
    _run_chunk_barrier(
        terminology_legacy,
        concurrency=min(8, max(1, len(terminology_legacy))),
        phase="draft_terminology_legacy",
        record_id=str(chunk_task(chunks[0])["record_id"]),
        progress_callback=_print_barrier_progress,
        stop_event=getattr(budget_guard, "stop_event", None),
        invoke=lambda chunk, attempt: runner.process_chunk(
            chunk_task(chunk), client, terms, run_id, budget_guard,
            stages=("terminology",),
            paper_context=_qc_retry_context(article_dir, chunk, "", attempt, terms=terms),
        ),
    )
    return {"translate": translate_result, "terminology": terminology_result}


def _qc_retry_context(
    article_dir: Path,
    chunk: dict[str, Any],
    base_context: str,
    attempt: int,
    *,
    terms: list[dict[str, Any]] | None = None,
) -> str:
    if attempt == 0:
        return base_context
    status_path = article_dir / "chunk_status" / f"{chunk['id']}.json"
    errors: list[str] = []
    rejected_candidate: Path | None = None
    if status_path.is_file():
        chunk_status = _load_json(status_path)
        for stage_name, stage in chunk_status.get("stages", {}).items():
            if isinstance(stage, dict) and stage.get("status") == "failed" and stage.get("error"):
                errors.append(f"{stage_name}: {stage['error']}")
                rejected_relative = stage.get("rejected_candidate_file")
                if isinstance(rejected_relative, str) and rejected_relative:
                    rejected_candidate = article_dir / rejected_relative
    failure = "; ".join(errors[-2:]) or "deterministic structure or fidelity QC failed"
    diagnostics: list[str] = []
    source_path = runner.article_artifact_path(article_dir, str(chunk["source_file"]))
    source = source_path.read_text(encoding="utf-8")
    candidate = (
        rejected_candidate.read_text(encoding="utf-8")
        if rejected_candidate is not None and rejected_candidate.is_file()
        else ""
    )
    if candidate and "numbers_mismatch" in failure:
        numeric = compare_numeric_literals(source, candidate)
        if numeric.missing_values:
            diagnostics.append("missing numeric literals: " + ", ".join(numeric.missing_values))
        if numeric.added_values:
            diagnostics.append("added numeric literals: " + ", ".join(numeric.added_values))
    if "units_mismatch" in failure:
        units = _extract_unit_values(source)
        diagnostics.append("source unit literals: " + (", ".join(units) or "none"))
    if "parentheses_mismatch" in failure:
        opening, closing = _parenthesis_residue(source)
        diagnostics.append(f"source parenthesis residue: open={opening}, closing={closing}")
    if "locked_terms_mismatch" in failure and terms:
        required = [
            f"{term.get('source')} => {term.get('target')}"
            for term in runner.select_glossary_terms(source, terms)
            if term.get("source") and term.get("target")
        ]
        if required:
            diagnostics.append("required locked terms: " + "; ".join(required))
    detail = ("\n" + "\n".join(diagnostics)) if diagnostics else ""
    return (
        f"# QC-CORRECTION RETRY {attempt}\n"
        + f"The previous output for {chunk['id']} was rejected ({failure})."
        + detail
        + "\nTranslate only the target segment again. Correct the reported defect; copy every "
        + "placeholder, Arabic numeral, unit, citation, URL, proper name, and locked term exactly "
        + "as required. Never import a number or phrase from read-only context."
    )


def _critique_context_for_chunk(critique: str, chunk_id: str) -> str:
    finding_prefix = re.compile(
        rf"^\s*(?:[-*+]\s*)?{re.escape(chunk_id)}\s*:",
        flags=re.I,
    )
    findings = [
        line.strip() for line in critique.splitlines() if finding_prefix.match(line)
    ]
    if not findings:
        return f"{NO_ACTIONABLE_CRITIQUE}: {chunk_id}"
    return "# Actionable critique for this chunk only\n" + "\n".join(findings)


def _require_critique_revision_targets_within_bound(
    critique: str,
    chunks: list[dict[str, Any]],
) -> None:
    known_ids = {str(chunk["id"]) for chunk in chunks}
    target_ids = set()
    for line in critique.splitlines():
        match = re.match(r"^\s*(?:[-*+]\s*)?(chunk\d{4})\s*:", line, flags=re.I)
        if match and match.group(1).lower() in known_ids:
            target_ids.add(match.group(1).lower())
    if len(target_ids) > CRITIQUE_GLOBAL_MAX_FINDINGS:
        raise RuntimeError(
            "Critique exceeds the revision target cap: "
            f"{len(target_ids)}>{CRITIQUE_GLOBAL_MAX_FINDINGS}"
        )


def _revision_context_signature(chunk_id: str, context: str) -> str:
    return runner.text_hash(
        json.dumps(
            {"chunk_id": chunk_id, "context": context},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _revision_context_preserving_completed_checkpoint(
    article_dir: Path,
    chunk_id: str,
    critique: str,
) -> str:
    return _critique_context_for_chunk(critique, chunk_id)


def _chunk_stage_complete(
    article_dir: Path,
    chunk: dict[str, Any],
    stage: str,
) -> bool:
    status_path = article_dir / "chunk_status" / f"{chunk['id']}.json"
    if not status_path.is_file():
        return False
    status = _load_json(status_path)
    stage_status = status.get("stages", {}).get(stage, {})
    if not isinstance(stage_status, dict) or stage_status.get("status") != "complete":
        return False
    output_path = runner.stage_output_path(
        article_dir,
        str(chunk["id"]),
        str(chunk["output_file"]),
        stage,
    )
    if not runner.nonempty(output_path):
        return False
    output_hash = stage_status.get("output_hash")
    if isinstance(output_hash, str) and output_hash:
        return runner.text_hash(output_path.read_text(encoding="utf-8")) == output_hash
    return True


def _missing_stage_chunk_ids(
    article_dir: Path,
    chunks: list[dict[str, Any]],
    stage: str,
) -> list[str]:
    return [
        str(chunk["id"])
        for chunk in chunks
        if not _chunk_stage_complete(article_dir, chunk, stage)
    ]


def _projection_terms(article_dir: Path) -> list[dict[str, Any]]:
    root = article_dir.parent.parent if len(article_dir.parents) >= 2 else article_dir.parent
    return runner.merge_glossary_terms(
        runner.load_glossary(runner.resolve_glossary_path(root, None)),
        runner.load_article_glossary(article_dir),
    )


def _valid_chunk_stage_checkpoint(
    article_dir: Path,
    chunk: dict[str, Any],
    stage_status: dict[str, Any],
    stage: str,
) -> bool:
    if stage_status.get("status") != "complete":
        return False
    qc = stage_status.get("qc")
    if not isinstance(qc, dict) or qc.get("ok") is not True:
        return False
    output_path = runner.stage_output_path(
        article_dir,
        str(chunk["id"]),
        str(chunk["output_file"]),
        stage,
    )
    output_hash = stage_status.get("output_hash")
    if not isinstance(output_hash, str) or not output_hash or not runner.nonempty(output_path):
        return False
    return runner.text_hash(output_path.read_text(encoding="utf-8")) == output_hash


def _valid_sharded_critique_checkpoint(
    article_dir: Path,
    *,
    chunks: list[dict[str, Any]],
    source_texts: dict[str, str],
    draft_texts: dict[str, str],
    paper_status: dict[str, Any],
) -> bool:
    shards = _critique_shards(article_dir, chunks, shard_char_limit=CRITIQUE_SHARD_CHAR_LIMIT)
    instructions = _critique_shard_instructions()
    shard_outputs: list[str] = []
    phases = paper_status.get("phases", {})
    for index, (source_shard, draft_shard) in enumerate(shards, 1):
        allowed_chunk_ids = {
            item.lower() for item in re.findall(r"\bchunk\d{4}\b", source_shard, flags=re.I)
        }
        input_text = (
            f"ENGLISH SOURCE SHARD {index}/{len(shards)}:\n{source_shard}\n\n"
            f"CHINESE DRAFT SHARD {index}/{len(shards)}:\n{draft_shard}"
        )
        primary_path = article_dir / "critique_shards" / f"shard{index:04d}.md"
        primary_phase = phases.get(f"critique_shard_{index:04d}", {})
        primary_hash = _paper_phase_input_hash(
            instructions,
            input_text,
            CRITIQUE_SHARD_MAX_OUTPUT_TOKENS,
        )
        if not _phase_valid(primary_phase, primary_path, primary_hash):
            return False
        primary_output = primary_path.read_text(encoding="utf-8")
        try:
            _validate_shard_critique(
                primary_output,
                shard_index=index,
                allowed_chunk_ids=allowed_chunk_ids,
            )
            shard_outputs.append(primary_output)
            continue
        except RuntimeError as validation_error:
            repair_path = article_dir / "critique_shard_repairs" / f"shard{index:04d}.md"
            repair_phase = phases.get(f"critique_shard_repair_{index:04d}", {})
            repair_instructions = _critique_shard_repair_instructions(allowed_chunk_ids)
            repair_hash = _paper_phase_input_hash(
                repair_instructions,
                f"VALIDATION ERROR:\n{validation_error}\n\nCRITIQUE TO REPAIR:\n{primary_output}",
                CRITIQUE_SHARD_MAX_OUTPUT_TOKENS,
            )
            if not _phase_valid(repair_phase, repair_path, repair_hash):
                return False
            repair_output = repair_path.read_text(encoding="utf-8")
            try:
                _validate_shard_critique(
                    repair_output,
                    shard_index=index,
                    allowed_chunk_ids=allowed_chunk_ids,
                )
            except RuntimeError:
                return False
            shard_outputs.append(repair_output)
    signature = runner.text_hash(
        json.dumps(
            {
                "schema_version": 1,
                "shard_hashes": [runner.text_hash(item) for item in shard_outputs],
                "max_findings": CRITIQUE_GLOBAL_MAX_FINDINGS,
            },
            sort_keys=True,
        )
    )
    critique_merge_phase = phases.get("critique_merge", {})
    return _phase_valid(
        critique_merge_phase,
        article_dir / CRITIQUE_FILE,
        signature,
    )


def _planned_stage_model_subrequests(
    *,
    article_dir: Path,
    chunk: dict[str, Any],
    stage: str,
    current: str,
    terms: list[dict[str, Any]],
    paper_context: str,
    stage_status: dict[str, Any],
) -> dict[str, Any]:
    """Mirror process_chunk request planning to count model-bearing subrequests."""

    article_dir = Path(article_dir)
    source = runner.article_artifact_path(
        article_dir, str(chunk["source_file"])
    ).read_text(encoding="utf-8")
    neighbor_context = runner.load_neighbor_context(article_dir, str(chunk["source_file"]))
    selected_terms = runner.compile_glossary_terms(source, terms)
    glossary = runner.glossary_text(selected_terms)
    instructions = runner.stage_instructions(stage, glossary)
    model_current = runner.localize_source_month_years(current) if stage == "translate" else current
    protected_current, _mapping, _typed_nodes = runner.protect_stage_text(model_current)
    protected_segments = runner.split_protected_model_input(
        protected_current,
        runner.structure_segment_limit(stage_status),
    )
    use_anchor_fallback = runner.should_use_structure_anchor_fallback(
        stage,
        stage_status,
        paper_context,
    )
    bounded_segmented_retry = (
        len(protected_segments) > 1
        and stage_status.get("status") in {"failed", "uncertain", "running"}
        and (
            bool(stage_status.get("bounded_segmented_retry"))
            or bool(stage_status.get("error"))
        )
    )
    compact_source = len(source.strip()) <= 12
    retry_marker = "# QC-CORRECTION RETRY"
    is_retry_request = retry_marker in paper_context
    context_for_request = (
        paper_context[paper_context.index(retry_marker) :]
        if is_retry_request
        else paper_context
    )
    if stage == "revision":
        context_for_request = runner.sanitize_refinement_context(context_for_request)
    if compact_source and not is_retry_request:
        context_for_request = ""

    segments: list[dict[str, Any]] = []
    for segment_index, protected_segment in enumerate(protected_segments, 1):
        segment_instructions = instructions
        segment_passthrough = False
        if runner._MODEL_SENTINEL_RE.search(protected_segment):
            _payload, _anchors, structure_parts = runner.build_structure_slot_input(
                protected_segment
            )
            has_translatable_slots = any(
                runner._TRANSLATABLE_SLOT_RE.search(part) for part in structure_parts
            )
            if not has_translatable_slots:
                model_segment = protected_segment
                segment_instructions = "STRUCTURE-ONLY PASSTHROUGH"
                segment_passthrough = True
            elif use_anchor_fallback or stage in runner.OPTIONAL_STYLE_STAGES:
                model_segment, _anchors, _markers = runner.build_structure_anchor_input(
                    protected_segment
                )
                segment_instructions += (
                    "\n\nSTRUCTURE-ANCHOR FALLBACK PROTOCOL: The input JSON contains a "
                    "source_template with synthetic <ANCHOR_0000> markers replacing protected "
                    "structures. Translate the complete template faithfully and return exactly "
                    'one JSON object {"translation":"..."}. Every supplied anchor marker must '
                    "appear exactly once. They may be reordered within the sentence as Chinese "
                    "syntax requires. Do not output commentary or any "
                    "real formula, number, URL, or protected node."
                )
            else:
                model_segment, _anchors, _source_parts = runner.build_structure_slot_input(
                    protected_segment
                )
                segment_instructions += (
                    "\n\nSTRUCTURE-SLOT PROTOCOL: The input is a JSON object whose slots are "
                    "independent text islands surrounding protected document structures. "
                    "Use source_context to understand complete sentence syntax; its synthetic "
                    "<ANCHOR_0000> markers show immutable structure boundaries and must not "
                    "appear in output. Translate/revise every supplied slot completely without "
                    "summarizing, merging, or reordering IDs. "
                    "Return exactly one JSON object of the form "
                    '{"translations":{"T0000":"..."}} with exactly the supplied IDs. '
                    "Do not output Markdown fences, commentary, source fields, or any protected "
                    "structure."
                )
        else:
            model_segment = protected_segment
        input_text = (
            model_segment
            if stage == "translate"
            else runner.stage_input(
                stage,
                source,
                model_segment,
                glossary,
                include_source=not bounded_segmented_retry,
            )
        )
        if len(protected_segments) > 1:
            input_text += (
                f"\n\nSTRUCTURE-DENSITY SEGMENT {segment_index}/{len(protected_segments)}. "
                "Output only the complete translation/revision of this segment. "
                "Do not reproduce source or context from another segment."
            )
        if context_for_request:
            if is_retry_request:
                input_text += "\n\nRETRY INSTRUCTIONS:\n" + context_for_request
            else:
                input_text += (
                    "\n\nREAD-ONLY PAPER ANALYSIS CONTEXT — apply it to this paragraph; "
                    "do not reproduce it:\n" + context_for_request
                )
        if (
            not segment_passthrough
            and "STRUCTURE-ANCHOR FALLBACK PROTOCOL" not in segment_instructions
            and not bounded_segmented_retry
            and neighbor_context
            and not compact_source
            and not is_retry_request
        ):
            input_text += (
                "\n\nREAD-ONLY NEIGHBOR CONTEXT — use only for disambiguation; "
                "do not translate or reproduce it:\n"
                + neighbor_context
            )
        max_output_tokens = max(4096, min(20000, int(max(len(protected_segment), 4000) * 0.8)))
        segments.append(
            {
                "segment_index": segment_index,
                "request_key": runner.request_key(
                    stage=stage,
                    model=runner.MODEL,
                    instructions=segment_instructions,
                    input_text=input_text,
                    max_output_tokens=max_output_tokens,
                ),
                "segment_passthrough": segment_passthrough,
                "instructions": segment_instructions,
                "input_text": input_text,
                "max_output_tokens": max_output_tokens,
            }
        )

    decision = runner.stage_decision(stage, current, selected_terms)
    deterministic_revision = (
        runner.deterministic_critique_revision(
            source=source,
            prior_text=current,
            paper_context=paper_context,
            qc_terms=selected_terms,
        )
        if stage == "revision"
        else None
    )
    if stage == "revision" and NO_ACTIONABLE_CRITIQUE in paper_context:
        decision = runner.StageDecision(False, "revision_no_actionable_chunk_critique")
    elif deterministic_revision is not None:
        decision = runner.StageDecision(False, "revision_deterministic_critique_replacement")
    elif stage == "revision" and runner.refinement_context_contains_factual_literals(
        paper_context
    ):
        decision = runner.StageDecision(
            False,
            "revision_literal_rebinding_requires_manual_review",
        )

    return {
        "decision": decision,
        "selected_terms": selected_terms,
        "segments": segments,
        "structure_segment_count": len(protected_segments),
        "model_subrequest_count": (
            sum(0 if item["segment_passthrough"] else 1 for item in segments)
            if decision.should_call_model
            else 0
        ),
    }


def revision_ready_projection(
    article_dir: Path,
    *,
    retry_uncertain: bool = False,
) -> dict[str, Any]:
    article_dir = Path(article_dir)
    diagnostics = {
        "record_identity_mismatches": [],
        "invalid_checkpoint_hashes": [],
        "blocking_uncertain_checkpoints": [],
    }
    report = {
        "record_id": "",
        "projection_ready": False,
        "projected_worst_case_api_calls": 0,
        "missing_stage_api_calls": {
            "analysis": 0,
            "translate": 0,
            "terminology": 0,
            "critique": 0,
            "revision": 0,
        },
        "identity_diagnostics": diagnostics,
    }

    manifest = _load_json(article_dir / "manifest.json")
    chunking_status = _load_json(article_dir / "chunking_status.json")
    record_id = str(manifest.get("record_id") or chunking_status.get("record_id") or "")
    report["record_id"] = record_id
    if not record_id or record_id != str(chunking_status.get("record_id")):
        diagnostics["record_identity_mismatches"].append(
            f"{article_dir}: manifest/chunking_status"
        )
        return report
    chunks = sorted(manifest.get("chunks", []), key=lambda item: item.get("order", 0))
    if not chunks:
        diagnostics["record_identity_mismatches"].append(f"{record_id}: no prepared chunks")
        return report

    constraints = runner.constraint_compiler.load_constraints(
        article_dir,
        record_id,
        TRACKED_HARD_CONSTRAINTS,
    )
    try:
        constraint_plan = runner.constraint_compiler.load_constraint_plan(
            article_dir,
            manifest,
            constraints,
        )
    except RuntimeError:
        diagnostics["invalid_checkpoint_hashes"].append("constraint_plan")
        return report

    terms = _projection_terms(article_dir)
    source_texts = {
        str(chunk["id"]): runner.article_artifact_path(
            article_dir, str(chunk["source_file"])
        ).read_text(encoding="utf-8")
        for chunk in chunks
    }
    source = _tagged_source(article_dir, chunks)
    reference_ids = _reference_chunk_ids(article_dir, chunks)
    figure_text_ids = runner.figure_text_chunk_ids(article_dir, manifest)
    table_text_ids = runner.table_text_chunk_ids(article_dir, manifest)
    hard_exact_translations = _hard_exact_translations(article_dir, record_id)
    fragile_fragment_ids = {
        str(chunk["id"])
        for chunk in chunks
        if str(chunk.get("layout_label", "")) == "fallback_line"
        and len(source_texts[str(chunk["id"])].strip()) <= 12
    }

    paper_status_path = article_dir / "paper_status.json"
    paper_status = (
        _load_json(paper_status_path)
        if paper_status_path.is_file()
        else {"record_id": record_id, "phases": {}}
    )
    if paper_status.get("record_id") != record_id:
        diagnostics["record_identity_mismatches"].append(str(paper_status_path))
        return report
    paper_phases = paper_status.get("phases", {})
    for phase_name, phase in paper_phases.items():
        expected_analysis_hash = _paper_phase_input_hash(
            _analysis_instructions(),
            source,
            ANALYSIS_MAX_OUTPUT_TOKENS,
        )
        recorded_input_hash = phase.get("input_hash") if isinstance(phase, dict) else None
        superseded_analysis_identity = (
            phase_name == "analysis"
            and isinstance(recorded_input_hash, str)
            and bool(recorded_input_hash)
            and recorded_input_hash != expected_analysis_hash
        )
        authorized_analysis_replay = (
            retry_uncertain
            and phase_name == "analysis"
            and isinstance(phase, dict)
            and recorded_input_hash == expected_analysis_hash
            and float(phase.get("conservative_cost_rmb") or 0) > 0
        )
        if (
            isinstance(phase, dict)
            and (
                phase_name == "analysis"
                or phase_name == "critique"
                or phase_name == "critique_merge"
                or phase_name.startswith("critique_shard_")
            )
            and phase.get("status") in {"running", "uncertain"}
            and not authorized_analysis_replay
            and not superseded_analysis_identity
        ):
            diagnostics["blocking_uncertain_checkpoints"].append(f"paper:{phase_name}")

    chunk_statuses: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        chunk_id = str(chunk["id"])
        status_path = article_dir / "chunk_status" / f"{chunk_id}.json"
        if not status_path.is_file():
            continue
        chunk_status = _load_json(status_path)
        chunk_statuses[chunk_id] = chunk_status
        if chunk_status.get("record_id") != record_id:
            diagnostics["record_identity_mismatches"].append(str(status_path))
            continue
        if str(chunk_status.get("chunk_id") or "") != chunk_id:
            diagnostics["record_identity_mismatches"].append(str(status_path))
            continue
        if str(chunk_status.get("source_file") or "") != str(chunk.get("source_file")):
            diagnostics["record_identity_mismatches"].append(str(status_path))
            continue
        manifest_source_hash = str(chunk.get("source_hash") or "")
        recorded_source_hash = str(chunk_status.get("source_hash") or "")
        if manifest_source_hash and recorded_source_hash and recorded_source_hash != manifest_source_hash:
            diagnostics["record_identity_mismatches"].append(str(status_path))
            continue
        for stage_name in ("translate", "terminology", "revision"):
            stage_status = chunk_status.get("stages", {}).get(stage_name, {})
            if (
                isinstance(stage_status, dict)
                and stage_status.get("status") in {"running", "uncertain"}
            ):
                diagnostics["blocking_uncertain_checkpoints"].append(f"{chunk_id}/{stage_name}")

    if diagnostics["record_identity_mismatches"] or diagnostics["blocking_uncertain_checkpoints"]:
        return report

    analysis_phase = paper_phases.get("analysis", {})
    analysis_valid = isinstance(analysis_phase, dict) and _phase_valid(
        analysis_phase,
        article_dir / ANALYSIS_FILE,
        _paper_phase_input_hash(
            _analysis_instructions(),
            source,
            ANALYSIS_MAX_OUTPUT_TOKENS,
        ),
    )
    if (
        isinstance(analysis_phase, dict)
        and analysis_phase.get("status") == "complete"
        and not analysis_valid
    ):
        diagnostics["invalid_checkpoint_hashes"].append("paper:analysis")
    report["missing_stage_api_calls"]["analysis"] = 0 if analysis_valid else 1

    chunk_runtime: dict[str, dict[str, Any]] = {}
    predicted_terminology_texts: dict[str, str] = {}
    actual_terminology_complete = True
    unknown_critique_inputs = False

    for chunk in chunks:
        chunk_id = str(chunk["id"])
        chunk_status = chunk_statuses.get(chunk_id, {})
        stages = chunk_status.get("stages", {}) if isinstance(chunk_status, dict) else {}
        source_text = source_texts[chunk_id]
        normalized_source = " ".join(source_text.split()).casefold()
        fixed_translation = hard_exact_translations.get(normalized_source)
        if fixed_translation is not None and source_text.endswith("\n"):
            fixed_translation += "\n"
        passthrough_reason = _chunk_passthrough_reason(
            {**chunk, "source_text": source_text},
            reference_ids=reference_ids,
            fragile_fragment_ids=fragile_fragment_ids,
            figure_text_ids=figure_text_ids,
            table_text_ids=table_text_ids,
        )
        passthrough = passthrough_reason is not None
        expected_base_policy = (
            f"passthrough:{passthrough_reason}"
            if passthrough
            else (
                "fixed_translation:"
                + runner.text_hash(str(fixed_translation))
                + ":"
                + str(constraint_plan.get("plan_sha256") or "legacy")
                if fixed_translation is not None
                else "model_pipeline"
            )
        )
        translate_stage = stages.get("translate", {}) if isinstance(stages, dict) else {}
        translate_valid = (
            isinstance(translate_stage, dict)
            and runner.checkpoint_policy_matches(translate_stage, expected_base_policy)
            and _valid_chunk_stage_checkpoint(
                article_dir, chunk, translate_stage, "translate"
            )
        )
        if (
            isinstance(translate_stage, dict)
            and translate_stage.get("status") == "complete"
            and not translate_valid
        ):
            diagnostics["invalid_checkpoint_hashes"].append(f"{chunk_id}/translate")
        translate_requires_model = not passthrough and fixed_translation is None
        translate_reusable = translate_valid or not translate_requires_model
        if translate_valid:
            translate_text = runner.stage_output_path(
                article_dir,
                chunk_id,
                str(chunk["output_file"]),
                "translate",
            ).read_text(encoding="utf-8")
        elif fixed_translation is not None:
            translate_text = fixed_translation
        else:
            translate_text = source_text
        translate_plan = _planned_stage_model_subrequests(
            article_dir=article_dir,
            chunk=chunk,
            stage="translate",
            current=source_text,
            terms=terms,
            paper_context="",
            stage_status=translate_stage if isinstance(translate_stage, dict) else {},
        )
        report["missing_stage_api_calls"]["translate"] += (
            translate_plan["model_subrequest_count"]
            if translate_requires_model and not translate_valid
            else 0
        )

        terminology_stage = stages.get("terminology", {}) if isinstance(stages, dict) else {}
        terminology_valid = (
            isinstance(terminology_stage, dict)
            and runner.checkpoint_policy_matches(terminology_stage, expected_base_policy)
            and _valid_chunk_stage_checkpoint(
                article_dir, chunk, terminology_stage, "terminology"
            )
        )
        if (
            isinstance(terminology_stage, dict)
            and terminology_stage.get("status") == "complete"
            and not terminology_valid
        ):
            diagnostics["invalid_checkpoint_hashes"].append(f"{chunk_id}/terminology")
        terminology_current = translate_text
        if not translate_reusable:
            terminology_current = source_text
            unknown_critique_inputs = True
        terminology_requires_model = (
            not passthrough
            and fixed_translation is None
            and _planned_stage_model_subrequests(
                article_dir=article_dir,
                chunk=chunk,
                stage="terminology",
                current=terminology_current,
                terms=terms,
                paper_context="",
                stage_status=(
                    terminology_stage if isinstance(terminology_stage, dict) else {}
                ),
            )["decision"].should_call_model
        )
        terminology_reusable = translate_reusable and terminology_valid
        terminology_plan = _planned_stage_model_subrequests(
            article_dir=article_dir,
            chunk=chunk,
            stage="terminology",
            current=terminology_current,
            terms=terms,
            paper_context="",
            stage_status=(
                terminology_stage if isinstance(terminology_stage, dict) else {}
            ),
        )
        report["missing_stage_api_calls"]["terminology"] += (
            terminology_plan["model_subrequest_count"]
            if terminology_requires_model and not terminology_reusable
            else 0
        )
        if terminology_reusable:
            terminology_text = runner.stage_output_path(
                article_dir,
                chunk_id,
                str(chunk["output_file"]),
                "terminology",
            ).read_text(encoding="utf-8")
            predicted_terminology_texts[chunk_id] = terminology_text
        elif translate_reusable and not terminology_requires_model:
            predicted_terminology_texts[chunk_id] = translate_text
            actual_terminology_complete = False
        else:
            actual_terminology_complete = False
            predicted_terminology_texts[chunk_id] = terminology_current

        chunk_runtime[chunk_id] = {
            "passthrough": passthrough,
            "fixed_translation": fixed_translation,
            "translate_reusable": translate_reusable,
            "translate_text": translate_text,
            "revision_surrogate_text": terminology_current,
            "terminology_requires_model": terminology_requires_model,
            "terminology_reusable": terminology_reusable,
            "stages": stages,
        }

    critique_valid = False
    critique_text = ""
    if actual_terminology_complete:
        draft, _draft_signature = _verified_merge(article_dir, record_id, chunks, "terminology")
        critique_text = (
            (article_dir / CRITIQUE_FILE).read_text(encoding="utf-8")
            if runner.nonempty(article_dir / CRITIQUE_FILE)
            else ""
        )
        legacy_critique = _valid_legacy_critique(
            article_dir=article_dir,
            status=paper_status,
            instructions=_critique_instructions(),
            source=source,
            draft=draft,
        )
        if legacy_critique is not None:
            critique_valid = True
            critique_text = legacy_critique
        elif len(source) + len(draft) > CRITIQUE_SHARD_CHAR_LIMIT:
            critique_valid = _valid_sharded_critique_checkpoint(
                article_dir,
                chunks=chunks,
                source_texts=source_texts,
                draft_texts={
                    str(chunk["id"]): runner.stage_output_path(
                        article_dir,
                        str(chunk["id"]),
                        str(chunk["output_file"]),
                        "terminology",
                    ).read_text(encoding="utf-8")
                    for chunk in chunks
                },
                paper_status=paper_status,
            )
        else:
            critique_phase = paper_phases.get("critique", {})
            critique_valid = isinstance(critique_phase, dict) and _phase_valid(
                critique_phase,
                article_dir / CRITIQUE_FILE,
                _paper_phase_input_hash(
                    _critique_instructions(),
                    f"ENGLISH SOURCE:\n{source}\n\nCHINESE DRAFT:\n{draft}",
                    CRITIQUE_MAX_OUTPUT_TOKENS,
                ),
            )
        if (
            not critique_valid
            and critique_text
            and isinstance(paper_phases.get("critique"), dict)
            and paper_phases.get("critique", {}).get("status") == "complete"
        ):
            diagnostics["invalid_checkpoint_hashes"].append("paper:critique")

    if critique_valid:
        report["missing_stage_api_calls"]["critique"] = 0
    elif unknown_critique_inputs:
        report["missing_stage_api_calls"]["critique"] = 2 * (
            _projected_unknown_critique_shard_count(
                chunks,
                source_texts=source_texts,
                shard_char_limit=CRITIQUE_SHARD_CHAR_LIMIT,
            )
        )
    else:
        projected_draft = _merge_tagged_outputs(chunks, predicted_terminology_texts)
        if len(source) + len(projected_draft) <= CRITIQUE_SHARD_CHAR_LIMIT:
            report["missing_stage_api_calls"]["critique"] = 1
        else:
            report["missing_stage_api_calls"]["critique"] = 2 * _critique_shard_count(
                chunks,
                source_texts=source_texts,
                draft_texts=predicted_terminology_texts,
                shard_char_limit=CRITIQUE_SHARD_CHAR_LIMIT,
            )

    if not critique_valid:
        critique_text = ""

    projected_unknown_revision_calls: list[int] = []
    for chunk in chunks:
        chunk_id = str(chunk["id"])
        state = chunk_runtime[chunk_id]
        if state["passthrough"] or state["fixed_translation"] is not None:
            continue
        revision_stage = state["stages"].get("revision", {}) if isinstance(state["stages"], dict) else {}
        revision_context = _critique_context_for_chunk(critique_text, chunk_id)
        expected_context_hash = runner.text_hash(revision_context)
        revision_valid = (
            critique_valid
            and state["terminology_reusable"]
            and isinstance(revision_stage, dict)
            and _valid_chunk_stage_checkpoint(article_dir, chunk, revision_stage, "revision")
            and revision_stage.get("paper_context_hash") == expected_context_hash
        )
        if (
            isinstance(revision_stage, dict)
            and revision_stage.get("status") == "complete"
            and not revision_valid
        ):
            diagnostics["invalid_checkpoint_hashes"].append(f"{chunk_id}/revision")
        revision_projection_context = (
            revision_context
            if critique_valid
            else f"# Actionable critique for this chunk only\n- {chunk_id}: critique pending"
        )
        revision_plan = _planned_stage_model_subrequests(
            article_dir=article_dir,
            chunk=chunk,
            stage="revision",
            current=str(state["revision_surrogate_text"]),
            terms=terms,
            paper_context=revision_projection_context,
            stage_status=revision_stage if isinstance(revision_stage, dict) else {},
        )
        if not revision_valid:
            if critique_valid:
                report["missing_stage_api_calls"]["revision"] += revision_plan[
                    "model_subrequest_count"
                ]
            else:
                projected_unknown_revision_calls.append(
                    revision_plan["model_subrequest_count"]
                )

    if not critique_valid:
        # The deterministic critique merge keeps at most one routable finding
        # per selected line and caps the paper globally. The conservative
        # revision reserve therefore takes the costliest possible distinct
        # chunk targets, rather than pretending every chunk will be revised.
        report["missing_stage_api_calls"]["revision"] = sum(
            sorted(projected_unknown_revision_calls, reverse=True)[
                :CRITIQUE_GLOBAL_MAX_FINDINGS
            ]
        )

    chunk_model_calls = sum(
        report["missing_stage_api_calls"][stage_name]
        for stage_name in ("translate", "terminology", "revision")
    )
    report["qc_retry_reserve_api_calls"] = chunk_model_calls
    report["projected_worst_case_api_calls"] = (
        sum(report["missing_stage_api_calls"].values()) + chunk_model_calls
    )
    report["projection_ready"] = True
    return report


def style_projection_report(
    article_dir: Path,
    *,
    terms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    article_dir = Path(article_dir)
    manifest = _load_json(article_dir / "manifest.json")
    chunking_status = _load_json(article_dir / "chunking_status.json")
    record_id = str(manifest.get("record_id") or chunking_status.get("record_id") or "")
    if not record_id or record_id != str(chunking_status.get("record_id")):
        raise RuntimeError(f"Record identity mismatch in {article_dir}")
    chunks = sorted(manifest.get("chunks", []), key=lambda item: item.get("order", 0))
    if not chunks:
        raise RuntimeError(f"No prepared chunks for {record_id}")
    missing_revision_chunk_ids = _missing_stage_chunk_ids(article_dir, chunks, "revision")
    report: dict[str, Any] = {
        "record_id": record_id,
        "projection_ready": not missing_revision_chunk_ids,
    }
    if missing_revision_chunk_ids:
        report["missing_revision_chunk_ids"] = missing_revision_chunk_ids
        return report

    terms = list(terms or [])
    reference_ids = _reference_chunk_ids(article_dir, chunks)
    figure_text_ids = runner.figure_text_chunk_ids(article_dir, manifest)
    table_text_ids = runner.table_text_chunk_ids(article_dir, manifest)
    hard_exact_translations = _hard_exact_translations(article_dir, record_id)
    constraints = runner.constraint_compiler.load_constraints(
        article_dir, record_id, TRACKED_HARD_CONSTRAINTS
    )
    try:
        constraint_plan = runner.constraint_compiler.load_constraint_plan(
            article_dir, manifest, constraints
        )
    except RuntimeError:
        constraint_plan = {"plan_sha256": "legacy"}
    fragile_fragment_ids = {
        str(chunk["id"])
        for chunk in chunks
        if str(chunk.get("layout_label", "")) == "fallback_line"
        and len(
            runner.article_artifact_path(
                article_dir, str(chunk["source_file"])
            ).read_text(encoding="utf-8").strip()
        )
        <= 12
    }

    def chunk_task(chunk: dict[str, Any]) -> dict[str, Any]:
        chunk_id = str(chunk["id"])
        source_text = runner.article_artifact_path(
            article_dir, str(chunk["source_file"])
        ).read_text(encoding="utf-8")
        normalized_source = " ".join(source_text.split()).casefold()
        fixed_translation = hard_exact_translations.get(normalized_source)
        if fixed_translation is not None and source_text.endswith("\n"):
            fixed_translation += "\n"
        passthrough_reason = _chunk_passthrough_reason(
            {**chunk, "source_text": source_text},
            reference_ids=reference_ids,
            fragile_fragment_ids=fragile_fragment_ids,
            figure_text_ids=figure_text_ids,
            table_text_ids=table_text_ids,
        )
        return {
            "article_dir": article_dir,
            "record_id": record_id,
            "chunk": chunk,
            "passthrough": passthrough_reason is not None,
            "passthrough_reason": passthrough_reason,
            "fixed_translation": fixed_translation,
            "fixed_translation_reason": (
                "hard_exact_translation" if fixed_translation is not None else None
            ),
            "retry_uncertain": False,
        }

    critique_path = article_dir / CRITIQUE_FILE
    critique = (
        critique_path.read_text(encoding="utf-8")
        if runner.nonempty(critique_path)
        else ""
    )
    context_factory = lambda chunk: _critique_context_for_chunk(critique, str(chunk["id"]))
    anti_ai_plan = style_batching.prepare_style_items(
        article_dir=article_dir,
        chunks=chunks,
        task_factory=chunk_task,
        terms=terms,
        stage="anti_ai",
        input_stage="revision",
        context_factory=context_factory,
    )
    anti_ai_complete = not _missing_stage_chunk_ids(article_dir, chunks, "anti_ai")
    academic_input_stage = "anti_ai" if anti_ai_complete else "revision"
    academic_plan = style_batching.prepare_style_items(
        article_dir=article_dir,
        chunks=chunks,
        task_factory=chunk_task,
        terms=terms,
        stage="academic",
        input_stage=academic_input_stage,
        context_factory=context_factory,
    )
    academic_projection = (
        style_batching.stage_plan_projection(academic_plan)
        if anti_ai_complete
        else style_batching.conservative_stage_plan_projection(academic_plan)
    )
    style_projection = {
        "schema_version": 1,
        "execution_mode": "exact_id_batching",
        "planned": {
            "anti_ai": style_batching.stage_plan_projection(anti_ai_plan),
            "academic": academic_projection,
        },
        "actual": {},
    }
    style_projection.update(_legacy_style_batch_projection(style_projection["planned"]))
    projected_normal_api_calls = sum(
        int(
            style_projection["planned"][stage_name].get("estimated_normal_requests")
            or style_projection["planned"][stage_name]["normal_requests"]
            or 0
        )
        for stage_name in ("anti_ai", "academic")
    )
    projected_worst_case_api_calls = sum(
        int(style_projection["planned"][stage_name]["worst_case_requests"])
        for stage_name in ("anti_ai", "academic")
    )
    report.update(
        {
            "style_projection": style_projection,
            "projected_normal_api_calls": projected_normal_api_calls,
            "projected_worst_case_api_calls": projected_worst_case_api_calls,
            "launch_worst_case_api_calls": (
                projected_worst_case_api_calls
                if anti_ai_complete
                else int(style_projection["planned"]["anti_ai"]["worst_case_requests"])
            ),
        }
    )
    return report


def run_refined_article(
    article_dir: Path,
    *,
    client: Any,
    terms: list[dict[str, Any]],
    run_id: str | None = None,
    budget_guard: runner.BudgetGuard | None = None,
    concurrency: int = 8,
    retry_uncertain: bool = False,
    stop_after_revision: bool = False,
) -> dict[str, Any]:
    """Run all causally ordered paper and chunk passes for one prepared article."""

    article_dir = Path(article_dir)
    if concurrency < 1 or concurrency > 64:
        raise ValueError("concurrency must be between 1 and 64")
    manifest = _load_json(article_dir / "manifest.json")
    chunking_status = _load_json(article_dir / "chunking_status.json")
    record_id = str(manifest.get("record_id") or chunking_status.get("record_id") or "")
    if not record_id or record_id != str(chunking_status.get("record_id")):
        raise RuntimeError(f"Record identity mismatch in {article_dir}")
    chunks = sorted(manifest.get("chunks", []), key=lambda item: item.get("order", 0))
    if not chunks:
        raise RuntimeError(f"No prepared chunks for {record_id}")
    reference_ids = _reference_chunk_ids(article_dir, chunks)
    figure_text_ids = runner.figure_text_chunk_ids(article_dir, manifest)
    table_text_ids = runner.table_text_chunk_ids(article_dir, manifest)
    hard_exact_translations = _hard_exact_translations(article_dir, record_id)
    constraints = runner.constraint_compiler.load_constraints(
        article_dir, record_id, TRACKED_HARD_CONSTRAINTS
    )
    constraint_plan = runner.constraint_compiler.load_constraint_plan(
        article_dir, manifest, constraints
    )
    fragile_fragment_ids = {
        str(chunk["id"])
        for chunk in chunks
        if str(chunk.get("layout_label", "")) == "fallback_line"
        and len(
            runner.article_artifact_path(
                article_dir, str(chunk["source_file"])
            ).read_text(encoding="utf-8").strip()
        )
        <= 12
    }

    def chunk_task(chunk: dict[str, Any]) -> dict[str, Any]:
        chunk_id = str(chunk["id"])
        source_text = runner.article_artifact_path(
            article_dir, str(chunk["source_file"])
        ).read_text(encoding="utf-8")
        normalized_source = " ".join(source_text.split()).casefold()
        fixed_translation = hard_exact_translations.get(normalized_source)
        if fixed_translation is not None and source_text.endswith("\n"):
            fixed_translation += "\n"
        passthrough_reason = _chunk_passthrough_reason(
            {**chunk, "source_text": source_text},
            reference_ids=reference_ids,
            fragile_fragment_ids=fragile_fragment_ids,
            figure_text_ids=figure_text_ids,
            table_text_ids=table_text_ids,
        )
        return {
            "article_dir": article_dir,
            "record_id": record_id,
            "chunk": chunk,
            "passthrough": passthrough_reason is not None,
            "passthrough_reason": passthrough_reason,
            "fixed_translation": fixed_translation,
            "fixed_translation_reason": (
                "hard_exact_translation" if fixed_translation is not None else None
            ),
            "constraint_plan_sha256": constraint_plan.get("plan_sha256"),
            "retry_uncertain": retry_uncertain,
        }

    status_path = article_dir / "paper_status.json"
    if status_path.exists():
        status = _load_json(status_path)
        if status.get("record_id") != record_id:
            raise RuntimeError(f"Paper checkpoint belongs to another record: {status_path}")
    else:
        status = {
            "schema_version": SCHEMA_VERSION,
            "record_id": record_id,
            "phases": {},
            "created_at": runner.now(),
        }
    if status.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported paper checkpoint schema: {status_path}")

    source = _tagged_source(article_dir, chunks)
    analysis_instructions = _analysis_instructions()
    analysis = _run_paper_model_phase(
        phase_name="analysis",
        output_path=article_dir / ANALYSIS_FILE,
        instructions=analysis_instructions,
        input_text=source,
        max_output_tokens=ANALYSIS_MAX_OUTPUT_TOKENS,
        client=client,
        status=status,
        status_path=status_path,
        run_id=run_id,
        budget_guard=budget_guard,
        retry_uncertain=retry_uncertain,
    )
    _require_sections(
        analysis,
        ("Content Summary", "Terminology", "Tone & Style", "Translation Challenges"),
        ANALYSIS_FILE,
    )

    prompt = _prompt_text(analysis, terms)
    prompt_signature = runner.text_hash(analysis + "\n" + runner.glossary_text(terms))
    prompt = _deterministic_phase(
        name="prompt",
        path=article_dir / PROMPT_FILE,
        text=prompt,
        input_hash=prompt_signature,
        status=status,
        status_path=status_path,
    )

    run_batched_draft_passes(
        article_dir,
        chunks=chunks,
        chunk_task=chunk_task,
        terms=terms,
        prompt=prompt,
        client=client,
        budget_guard=budget_guard,
        run_id=run_id,
    )
    _print_barrier_progress(
        {
            "phase": "draft",
            "record_id": record_id,
            "attempt": 0,
            "completed": len(chunks),
            "total": len(chunks),
            "retryable": 0,
            "terminal_failures": 0,
        }
    )
    draft, draft_signature = _verified_merge(article_dir, record_id, chunks, "terminology")
    draft = _deterministic_phase(
        name="draft_merge",
        path=article_dir / DRAFT_FILE,
        text=draft,
        input_hash=draft_signature,
        status=status,
        status_path=status_path,
    )

    critique_instructions = _critique_instructions()
    legacy_critique = _valid_legacy_critique(
        article_dir=article_dir,
        status=status,
        instructions=critique_instructions,
        source=source,
        draft=draft,
    )
    if legacy_critique is not None:
        critique = legacy_critique
    elif len(source) + len(draft) > CRITIQUE_SHARD_CHAR_LIMIT:
        _require_critique_drafts_within_projection_bound(
            chunks,
            source_texts={
                str(chunk["id"]): runner.article_artifact_path(
                    article_dir, str(chunk["source_file"])
                ).read_text(encoding="utf-8")
                for chunk in chunks
            },
            draft_texts={
                str(chunk["id"]): runner.stage_output_path(
                    article_dir,
                    str(chunk["id"]),
                    str(chunk["output_file"]),
                    "terminology",
                ).read_text(encoding="utf-8")
                for chunk in chunks
            },
        )
        critique = _run_sharded_critique(
            article_dir=article_dir,
            chunks=chunks,
            client=client,
            status=status,
            status_path=status_path,
            run_id=run_id,
            budget_guard=budget_guard,
            retry_uncertain=retry_uncertain,
        )
    else:
        critique = _run_paper_model_phase(
            phase_name="critique",
            output_path=article_dir / CRITIQUE_FILE,
            instructions=critique_instructions,
            input_text=f"ENGLISH SOURCE:\n{source}\n\nCHINESE DRAFT:\n{draft}",
            max_output_tokens=CRITIQUE_MAX_OUTPUT_TOKENS,
            client=client,
            status=status,
            status_path=status_path,
            run_id=run_id,
            budget_guard=budget_guard,
            retry_uncertain=retry_uncertain,
        )
    _require_sections(
        critique,
        ("Accuracy", "Native Voice", "Notes & Adaptation", "Summary"),
        CRITIQUE_FILE,
    )
    if not any(str(chunk["id"]) in critique for chunk in chunks if str(chunk["id"]) not in reference_ids):
        raise RuntimeError(
            f"{CRITIQUE_FILE} contains no body chunk tags"
        )
    _require_critique_revision_targets_within_bound(critique, chunks)

    revision_contexts = {
        str(chunk["id"]): _revision_context_preserving_completed_checkpoint(
            article_dir, str(chunk["id"]), critique
        )
        for chunk in chunks
    }
    _run_chunk_barrier(
        chunks,
        concurrency=concurrency,
        phase="revision",
        record_id=record_id,
        progress_callback=_print_barrier_progress,
        stop_event=getattr(budget_guard, "stop_event", None),
        invoke=lambda chunk, attempt: runner.process_chunk(
            chunk_task(chunk),
            client,
            terms,
            run_id,
            budget_guard,
            stages=("revision",),
            paper_context=_qc_retry_context(
                article_dir,
                chunk,
                revision_contexts[str(chunk["id"])],
                attempt,
                terms=terms,
            ),
            paper_context_identity=revision_contexts[str(chunk["id"])],
            initial_text_path=runner.stage_output_path(
                article_dir,
                str(chunk["id"]),
                str(chunk["output_file"]),
                "terminology",
            ),
        ),
    )
    revision, revision_signature = _verified_merge(article_dir, record_id, chunks, "revision")
    revision = _deterministic_phase(
        name="revision_merge",
        path=article_dir / REVISION_FILE,
        text=revision,
        input_hash=revision_signature,
        status=status,
        status_path=status_path,
    )
    if stop_after_revision:
        final_merge_phase = status.get("phases", {}).get("final_merge", {})
        try:
            _final_text, final_signature = _verified_merge(
                article_dir, record_id, chunks, "academic"
            )
        except RuntimeError:
            final_signature = None
        if final_signature is not None and _phase_valid(
            final_merge_phase,
            article_dir / FINAL_FILE,
            final_signature,
        ):
            status["status"] = "complete"
            status["finished_at"] = runner.now()
            _persist_status(status_path, status)
            return {"record_id": record_id, "status": "complete", "chunks": len(chunks)}
        status["status"] = "revision_ready"
        status["finished_at"] = runner.now()
        _persist_status(status_path, status)
        return {"record_id": record_id, "status": "revision_ready", "chunks": len(chunks)}

    run_batched_final_style_passes(
        article_dir,
        chunks=chunks,
        chunk_task=chunk_task,
        terms=terms,
        critique=critique,
        client=client,
        status=status,
        status_path=status_path,
        budget_guard=budget_guard,
        run_id=run_id,
    )
    _apply_manual_corrections(article_dir, record_id, chunks)
    final, final_signature = _verified_merge(article_dir, record_id, chunks, "academic")
    _deterministic_phase(
        name="final_merge",
        path=article_dir / FINAL_FILE,
        text=final,
        input_hash=final_signature,
        status=status,
        status_path=status_path,
    )
    status["status"] = "complete"
    status["finished_at"] = runner.now()
    _persist_status(status_path, status)
    return {"record_id": record_id, "status": "complete", "chunks": len(chunks)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--article-dir", type=Path, required=True)
    parser.add_argument("--rights-manifest", type=Path, default=runner.RIGHTS_MANIFEST)
    parser.add_argument("--glossary", type=Path)
    parser.add_argument("--max-cost-rmb", type=float, required=True)
    parser.add_argument("--usd-cny-rate", type=float, default=runner.DEFAULT_USD_CNY_RATE)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--retry-uncertain",
        action="store_true",
        help="Conservatively charge and replay stale in-flight requests after operator review",
    )
    parser.add_argument("--style-projection-only", action="store_true")
    parser.add_argument("--stop-after-revision", action="store_true")
    args = parser.parse_args(argv)
    if not math.isfinite(args.max_cost_rmb) or args.max_cost_rmb <= 0:
        parser.error("--max-cost-rmb must be finite and greater than zero")
    if not math.isfinite(args.usd_cny_rate) or args.usd_cny_rate <= 0:
        parser.error("--usd-cny-rate must be finite and greater than zero")
    if args.concurrency < 1 or args.concurrency > 64:
        parser.error("--concurrency must be between 1 and 64")
    if args.stop_after_revision and args.style_projection_only:
        parser.error("--stop-after-revision cannot be combined with --style-projection-only")

    manifest = _load_json(args.article_dir / "manifest.json")
    record_id = manifest.get("record_id")
    allowed = runner.load_allowed_record_ids(args.rights_manifest)
    if record_id not in allowed:
        print(f"Refusing record outside the publication rights gate: {record_id}", file=sys.stderr)
        return 2

    if args.glossary is not None:
        global_terms = runner.load_glossary(args.glossary)
    else:
        default_glossary = runner.resolve_glossary_path(args.article_dir.parents[1], None)
        global_terms = runner.load_glossary(default_glossary)
    terms = runner.merge_glossary_terms(
        global_terms,
        runner.load_article_glossary(args.article_dir),
    )
    if args.style_projection_only:
        report = style_projection_report(args.article_dir, terms=terms)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0
    prior_cost = existing_article_cost_rmb(args.article_dir, args.usd_cny_rate)
    budget = runner.BudgetGuard(
        args.max_cost_rmb,
        args.usd_cny_rate,
        initial_spent_rmb=prior_cost,
    )
    run_id = uuid.uuid4().hex
    result = run_refined_article(
        args.article_dir,
        client=runner.DeepSeekClient(runner.load_api_key()),
        terms=terms,
        run_id=run_id,
        budget_guard=budget,
        concurrency=args.concurrency,
        retry_uncertain=args.retry_uncertain,
        stop_after_revision=args.stop_after_revision,
    )
    result.update({"run_id": run_id, "budget": budget.snapshot()})
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
