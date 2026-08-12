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
from snowmass_document_units import compare_numeric_literals
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
    _merge_sharded_critiques([output])
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
    instructions = f"""Review one aligned English/Chinese shard of an academic paper.
Return exactly these Markdown sections: ## Accuracy, ## Native Voice, ## Notes & Adaptation, and ## Summary.
Report only high-impact actionable defects. Every actionable line must start with its exact chunk ID, for example `- chunk0001:`. Return at most {CRITIQUE_SHARD_MAX_FINDINGS} actionable lines total, ranked highest risk first. Each actionable line must contain at most {CRITIQUE_SHARD_MAX_FINDING_CHARACTERS} characters including its chunk ID. If none exist, write `- NO_ACTIONABLE_FINDINGS`. In every other empty section write only `- NO_ACTIONABLE_FINDINGS`. Do not quote passages, enumerate correct chunks, explain methods, add prose summaries, or rewrite the draft."""
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
            repair_instructions = f"""STRUCTURE-REPAIR: Rewrite the supplied critique without adding, deleting, or strengthening findings.
Return exactly these Markdown sections: ## Accuracy, ## Native Voice, ## Notes & Adaptation, and ## Summary.
Every actionable line must use exactly one of these allowed chunk IDs followed immediately by a colon: {', '.join(sorted(allowed_chunk_ids))}.
Ranges, lists, invented chunk IDs, quotations, explanations, and prose outside the four sections are forbidden. Preserve at most {CRITIQUE_SHARD_VALIDATION_MAX_FINDINGS} actionable lines, each at most {CRITIQUE_SHARD_MAX_FINDING_CHARACTERS} characters. If a range finding cannot be assigned safely to one exact allowed chunk, omit it. Empty sections must contain only `- NO_ACTIONABLE_FINDINGS`."""
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
    """Return the bibliography heading and following units for verbatim passthrough."""

    for index, chunk in enumerate(chunks):
        text = runner.article_artifact_path(
            article_dir, str(chunk["source_file"])
        ).read_text(encoding="utf-8")
        heading = " ".join(text.split()).rstrip(":").casefold()
        if heading in {"references", "bibliography"}:
            return {str(item["id"]) for item in chunks[index:]}
    return set()


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
        for rule in rules:
            source = " ".join(str(rule.get("source", "")).split()).casefold()
            target = str(rule.get("target", "")).strip()
            if not source or not target:
                raise RuntimeError(f"Incomplete exact translation rule: {path}")
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
    max_qc_retries: int = 4,
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
                        terminal_failures[chunk_id] = f"{type(exc).__name__}:{exc}"
                    except Exception as exc:
                        if attempt == max_qc_retries and stop_event is not None:
                            stop_event.set()
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
    if failures:
        if stop_event is not None:
            stop_event.set()
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


def _style_batch_projection(
    article_dir: Path,
    chunks: list[dict[str, Any]],
    chunk_task: Any,
    *,
    max_group_size: int = 4,
    max_group_characters: int = 6000,
) -> dict[str, Any]:
    """Estimate safe final-style grouping without changing execution behavior."""

    if max_group_size <= 0 or max_group_characters <= 0:
        raise ValueError("style batch projection limits must be positive")
    eligible = 0
    groupable = 0
    groups: list[list[str]] = []
    current_group: list[str] = []
    current_characters = 0

    def flush() -> None:
        nonlocal current_group, current_characters
        if current_group:
            groups.append(current_group)
            current_group = []
            current_characters = 0

    for chunk in chunks:
        task = chunk_task(chunk)
        if task.get("passthrough") or task.get("fixed_translation") is not None:
            flush()
            continue
        eligible += 1
        chunk_id = str(chunk["id"])
        input_path = runner.stage_output_path(
            article_dir,
            chunk_id,
            str(chunk["output_file"]),
            "revision",
        )
        if not runner.nonempty(input_path):
            flush()
            continue
        text = input_path.read_text(encoding="utf-8")
        protected, _mapping, _typed = runner.protect_stage_text(text)
        segments = runner.split_protected_model_input(
            protected,
            runner.MODEL_STRUCTURE_SEGMENT_LIMIT,
        )
        safe = len(segments) == 1 and runner._MODEL_SENTINEL_RE.search(segments[0]) is None
        if not safe:
            flush()
            continue
        groupable += 1
        if (
            current_group
            and (
                len(current_group) >= max_group_size
                or current_characters + len(text) > max_group_characters
            )
        ):
            flush()
        current_group.append(chunk_id)
        current_characters += len(text)
    flush()
    non_groupable = eligible - groupable
    per_stage_groups = len(groups) + non_groupable
    current_style_requests = eligible * 2
    projected_style_requests = per_stage_groups * 2
    reduction = (
        (current_style_requests - projected_style_requests) / current_style_requests
        if current_style_requests
        else 0.0
    )
    return {
        "schema_version": 1,
        "execution_mode": "observational_projection_only",
        "eligible_chunks": eligible,
        "groupable_chunks": groupable,
        "non_groupable_chunks": non_groupable,
        "projected_groups": per_stage_groups,
        "max_group_size": max_group_size,
        "max_group_characters": max_group_characters,
        "current_style_requests": current_style_requests,
        "projected_style_requests": projected_style_requests,
        "projected_request_reduction_fraction": reduction,
        "groups": groups,
    }


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
    findings = [line.strip() for line in critique.splitlines() if chunk_id in line]
    if not findings:
        return f"{NO_ACTIONABLE_CRITIQUE}: {chunk_id}"
    return "# Actionable critique for this chunk only\n" + "\n".join(findings)


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
    status_path = article_dir / "chunk_status" / f"{chunk_id}.json"
    if status_path.is_file():
        stage = _load_json(status_path).get("stages", {}).get("revision", {})
        if isinstance(stage, dict) and stage.get("status") == "complete":
            if stage.get("paper_context_scope") in {"chunk_local", "no_actionable"}:
                return _critique_context_for_chunk(critique, chunk_id)
            return critique
    return _critique_context_for_chunk(critique, chunk_id)


def run_refined_article(
    article_dir: Path,
    *,
    client: Any,
    terms: list[dict[str, Any]],
    run_id: str | None = None,
    budget_guard: runner.BudgetGuard | None = None,
    concurrency: int = 8,
    retry_uncertain: bool = False,
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
    analysis_instructions = """Perform a compact paper-level content analysis for an English-to-Simplified-Chinese academic translation.
Return exactly these Markdown sections: ## Content Summary, ## Terminology, ## Tone & Style, and ## Translation Challenges.
Identify the paper's argument, domain-specific meanings, preferred terminology, register, and concrete translation risks. Be selective: use at most 40 concise bullets total, do not inventory chunks, and do not reproduce source passages. Do not translate the paper yet."""
    analysis = _run_paper_model_phase(
        phase_name="analysis",
        output_path=article_dir / ANALYSIS_FILE,
        instructions=analysis_instructions,
        input_text=source,
        max_output_tokens=4000,
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

    _run_chunk_barrier(
        chunks,
        concurrency=concurrency,
        phase="draft",
        record_id=record_id,
        progress_callback=_print_barrier_progress,
        stop_event=getattr(budget_guard, "stop_event", None),
        invoke=lambda chunk, attempt: runner.process_chunk(
            chunk_task(chunk),
            client,
            terms,
            run_id,
            budget_guard,
            stages=("translate", "terminology"),
            paper_context=_qc_retry_context(
                article_dir, chunk, prompt, attempt, terms=terms
            ),
        ),
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

    critique_instructions = """Perform a paper-level critical review of the tagged Chinese draft against the tagged English source.
Return exactly these Markdown sections: ## Accuracy, ## Native Voice, ## Notes & Adaptation, and ## Summary.
Every actionable finding must start with its chunk ID (for example, `chunk0001:`). Check omissions, additions, factual drift, modality, terminology, syntax, academic register, and translationese. Report only high-impact actionable defects, at most 30 one-line findings total. If more exist, select the 30 highest-risk defects. Do not enumerate correct chunks, reproduce passages, explain your method, or rewrite the draft."""
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
            max_output_tokens=4000,
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

    style_projection = _style_batch_projection(article_dir, chunks, chunk_task)
    projection_text = json.dumps(
        style_projection,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    _deterministic_phase(
        name="style_batch_projection",
        path=article_dir / "style_batch_projection.json",
        text=projection_text,
        input_hash=runner.text_hash(projection_text),
        status=status,
        status_path=status_path,
    )

    _run_chunk_barrier(
        chunks,
        concurrency=concurrency,
        phase="final",
        record_id=record_id,
        progress_callback=_print_barrier_progress,
        stop_event=getattr(budget_guard, "stop_event", None),
        invoke=lambda chunk, attempt: runner.process_chunk(
            chunk_task(chunk),
            client,
            terms,
            run_id,
            budget_guard,
            stages=("anti_ai", "academic"),
            paper_context=_qc_retry_context(
                article_dir,
                chunk,
                _critique_context_for_chunk(critique, str(chunk["id"])),
                attempt,
                terms=terms,
            ),
            initial_text_path=runner.stage_output_path(
                article_dir,
                str(chunk["id"]),
                str(chunk["output_file"]),
                "revision",
            ),
        ),
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
    args = parser.parse_args(argv)
    if not math.isfinite(args.max_cost_rmb) or args.max_cost_rmb <= 0:
        parser.error("--max-cost-rmb must be finite and greater than zero")
    if not math.isfinite(args.usd_cny_rate) or args.usd_cny_rate <= 0:
        parser.error("--usd-cny-rate must be finite and greater than zero")
    if args.concurrency < 1 or args.concurrency > 64:
        parser.error("--concurrency must be between 1 and 64")

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
    )
    result.update({"run_id": run_id, "budget": budget.snapshot()})
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
