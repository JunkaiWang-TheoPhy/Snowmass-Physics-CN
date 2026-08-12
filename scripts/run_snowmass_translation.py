#!/usr/bin/env python3
"""Checkpointed DeepSeek V4 Flash translation pipeline for Snowmass chunks."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import math
import os
import random
import re
import ssl
import subprocess
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from snowmass_translation_qc import StageDecision, stage_decision, validate_chunk
import snowmass_babeldoc_bridge as babeldoc_bridge
from snowmass_document_units import (
    StructureMismatchError as TypedStructureMismatchError,
    protect_translation_unit,
    restore_translation_unit,
)
from snowmass_pipeline import (
    StructureMismatchError,
    protect_structures,
    sentinel_sequence,
    validate_and_restore,
)


TRANSLATION_ROOT = Path("output/snowmass2021_translation")
RIGHTS_MANIFEST = Path("site/data/papers.json")
API_URL = "https://api.deepseek.com/responses"
MODEL = "deepseek-v4-flash"
STAGES = ("translate", "terminology", "anti_ai", "academic")
OPTIONAL_STYLE_STAGES = frozenset({"anti_ai", "academic"})
QC_CONTRACT_VERSION = 5
MODEL_STRUCTURE_SEGMENT_LIMIT = 4
STRUCTURE_SLOT_PROTOCOL = "snowmass-text-slots-v1"
STRUCTURE_ANCHOR_PROTOCOL = "snowmass-anchor-template-v1"
_TRANSLATABLE_SLOT_RE = re.compile(r"[A-Za-z\u3400-\u9fff]")
_SOURCE_LEXICAL_RE = re.compile(r"[A-Za-z]")
_TARGET_LEXICAL_RE = re.compile(r"[A-Za-z\u3400-\u9fff]")
_CONTEXT_ANCHOR_RE = re.compile(r"<ANCHOR_[0-9]{4}>")
_ACADEMIC_PUNCTUATION_TABLE = str.maketrans({".": "。", ",": "，", ";": "；", ":": "："})
_MONTH_NAMES_ZH = {
    "january": "一月",
    "february": "二月",
    "march": "三月",
    "april": "四月",
    "may": "五月",
    "june": "六月",
    "july": "七月",
    "august": "八月",
    "september": "九月",
    "october": "十月",
    "november": "十一月",
    "december": "十二月",
}
_MODEL_SENTINEL_RE = re.compile(
    r"\[\[(?:SM_[0-9]{4}_[0-9a-f]{5,10}|SMU_[0-9]{4}_[A-Z_]+_[0-9a-f]{10})\]\]"
)
_SAFE_SEGMENT_BOUNDARY_RE = re.compile(
    r"(?:\n+|(?<=[.!?;:,。！？；：，])\s+|(?<=[,，;；:：]))"
)
_USAGE_LEDGER_LOCK = threading.Lock()
RETRYABLE_HTTP_CODES = {408, 409, 429, 500, 502, 503, 504}
# Official DeepSeek V4 Flash rates, USD per million tokens:
# https://api-docs.deepseek.com/quick_start/pricing/
INPUT_CACHE_HIT_USD_PER_MILLION = 0.0028
INPUT_CACHE_MISS_USD_PER_MILLION = 0.14
OUTPUT_USD_PER_MILLION = 0.28
# Conservative operator-pinned conversion for RMB budget enforcement. Each run
# records the actual value, and callers may override it without changing code.
DEFAULT_USD_CNY_RATE = 7.20
PRICING_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
PRICING_VERIFIED_AT = "2026-08-10"
TRANSLATE_BOOK_CONTEXT = Path(
    "/Users/Zhuanz/.agents/skills/translate-book/scripts/chunk_context.py"
)


class ResponseValidationError(RuntimeError):
    """The API returned a structurally invalid or unacceptable response."""


class IncompleteResponseError(ResponseValidationError):
    """The API reported an incomplete response."""


class FailedResponseError(ResponseValidationError):
    """The API reported a failed response."""


class AmbiguousTransportError(RuntimeError):
    """Transport failed before a trustworthy API response could be validated."""


class BudgetExceededError(RuntimeError):
    """A request could not be reserved within the configured RMB budget."""


class UnsafeArticlePathError(RuntimeError):
    """A manifest artifact path escaped its article workspace."""


def article_artifact_path(article_dir: Path, relative: str) -> Path:
    candidate = Path(relative)
    if not relative or candidate.is_absolute():
        raise UnsafeArticlePathError(f"article artifact must be a relative path: {relative}")
    root = Path(article_dir).resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise UnsafeArticlePathError(f"article artifact escapes workspace: {relative}") from error
    return resolved


class ParsedResponse:
    def __init__(
        self,
        *,
        text: str,
        response_id: str,
        model: str,
        status: str,
        usage: dict[str, Any],
        output_hash: str,
    ) -> None:
        self.text = text
        self.response_id = response_id
        self.model = model
        self.status = status
        self.usage = usage
        self.output_hash = output_hash


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def append_cost_ledger(article_dir: Path, event: dict[str, Any]) -> None:
    """Append one immutable billing event using one locked O_APPEND write."""

    record = {"schema_version": 1, "recorded_at": now(), **event}
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    ledger = article_dir / "api_cost_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with _USAGE_LEDGER_LOCK:
        descriptor = os.open(ledger, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def load_api_key() -> str:
    supplied = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if supplied:
        return supplied
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "codex_0805", "-s", "codex.deepseek.api", "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    key = result.stdout.strip()
    if result.returncode != 0 or not key:
        raise RuntimeError("DeepSeek Keychain credential is unavailable")
    return key


def load_glossary(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("terms", [])


def load_article_glossary(article_dir: Path) -> list[dict[str, Any]]:
    path = article_dir / "glossary.json"
    return load_glossary(path) if path.exists() else []


TRACKED_GLOBAL_GLOSSARY = Path(__file__).resolve().parents[1] / "translations" / "snowmass-global-glossary.json"


def merge_glossary_terms(
    global_terms: list[dict[str, Any]],
    article_terms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = [dict(term) for term in global_terms]
    positions: dict[str, int] = {}
    for index, term in enumerate(merged):
        source = str(term.get("source", "")).strip().casefold()
        if source:
            positions[source] = index
    for term in article_terms:
        replacement = dict(term)
        source = str(replacement.get("source", "")).strip().casefold()
        if source and source in positions:
            merged[positions[source]] = replacement
        else:
            if source:
                positions[source] = len(merged)
            merged.append(replacement)
    return merged


def select_glossary_terms(source_text: str, terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    folded = source_text.casefold()
    selected: list[dict[str, Any]] = []
    for term in terms:
        excluded = term.get("exclude_phrases")
        if isinstance(excluded, list) and any(
            isinstance(phrase, str)
            and phrase.strip()
            and phrase.strip().casefold() in folded
            for phrase in excluded
        ):
            continue
        surfaces = [term.get("source", ""), *term.get("aliases", [])]
        if any(str(surface).strip().casefold() in folded for surface in surfaces if str(surface).strip()):
            selected.append(term)
    return selected


def resolve_glossary_path(root: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    root_glossary = root / "global_glossary.json"
    return root_glossary if root_glossary.is_file() else TRACKED_GLOBAL_GLOSSARY


def _token_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def estimate_cost_usd(usage: dict[str, Any]) -> float:
    input_tokens = _token_count(usage.get("input_tokens"))
    cached_tokens = min(_token_count(usage.get("cached_tokens")), input_tokens)
    uncached_tokens = input_tokens - cached_tokens
    output_tokens = _token_count(usage.get("output_tokens"))
    return (
        uncached_tokens * INPUT_CACHE_MISS_USD_PER_MILLION
        + cached_tokens * INPUT_CACHE_HIT_USD_PER_MILLION
        + output_tokens * OUTPUT_USD_PER_MILLION
    ) / 1_000_000


def estimate_cost_rmb(
    usage: dict[str, Any],
    usd_cny_rate: float = DEFAULT_USD_CNY_RATE,
) -> float:
    if not math.isfinite(usd_cny_rate) or usd_cny_rate <= 0:
        raise ValueError("usd_cny_rate must be finite and positive")
    return estimate_cost_usd(usage) * usd_cny_rate


class BudgetGuard:
    """Thread-safe reservation accounting for paid DeepSeek requests."""

    def __init__(
        self,
        max_cost_rmb: float,
        usd_cny_rate: float = DEFAULT_USD_CNY_RATE,
        initial_spent_rmb: float = 0.0,
    ) -> None:
        if not math.isfinite(max_cost_rmb) or max_cost_rmb <= 0:
            raise ValueError("max_cost_rmb must be finite and greater than zero")
        if not math.isfinite(usd_cny_rate) or usd_cny_rate <= 0:
            raise ValueError("usd_cny_rate must be finite and positive")
        if not math.isfinite(initial_spent_rmb) or initial_spent_rmb < 0:
            raise ValueError("initial_spent_rmb must be finite and non-negative")
        self.max_cost_rmb = float(max_cost_rmb)
        self.usd_cny_rate = float(usd_cny_rate)
        self._spent_rmb = float(initial_spent_rmb)
        self._reservations: dict[str, float] = {}
        self._lock = threading.Lock()

    def _conservative_request_cost(self, input_text: str, max_output_tokens: int) -> float:
        # A UTF-8 byte count is a conservative token ceiling for the submitted
        # text. The fixed allowance covers request framing and tokenizer edge
        # cases; output is reserved at the full configured maximum.
        input_token_ceiling = len(input_text.encode("utf-8")) + 4096
        output_token_ceiling = max(0, int(max_output_tokens))
        cost_usd = (
            input_token_ceiling * INPUT_CACHE_MISS_USD_PER_MILLION
            + output_token_ceiling * OUTPUT_USD_PER_MILLION
        ) / 1_000_000
        return cost_usd * self.usd_cny_rate

    def reserve(
        self,
        input_text: str,
        max_output_tokens: int,
        *,
        uncertainty_key: str | None = None,
    ) -> str:
        estimate = self._conservative_request_cost(input_text, max_output_tokens)
        with self._lock:
            reserved = sum(self._reservations.values())
            projected = self._spent_rmb + reserved + estimate
            if projected > self.max_cost_rmb:
                raise BudgetExceededError(
                    f"DeepSeek budget cap would be exceeded: "
                    f"projected ¥{projected:.6f} > cap ¥{self.max_cost_rmb:.6f}"
                )
            reservation = uuid.uuid4().hex
            self._reservations[reservation] = estimate
            return reservation

    def settle(self, reservation: str, usage: dict[str, Any]) -> None:
        actual = estimate_cost_rmb(usage, self.usd_cny_rate)
        with self._lock:
            estimate = self._reservations.pop(reservation)
            self._spent_rmb += actual if actual > 0 else estimate

    def commit_estimate(self, reservation: str) -> float:
        with self._lock:
            estimate = self._reservations.pop(reservation)
            self._spent_rmb += estimate
            return estimate

    def resolve_uncertain(
        self,
        uncertainty_key: str,
        *,
        reservation_id: str | None = None,
    ) -> bool:
        return False

    def unresolved_uncertain_cost(self, uncertainty_key: str) -> float:
        return 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            reserved = sum(self._reservations.values())
            remaining = max(0.0, self.max_cost_rmb - self._spent_rmb - reserved)
            return {
                "max_cost_rmb": self.max_cost_rmb,
                "usd_cny_rate": self.usd_cny_rate,
                "spent_rmb": self._spent_rmb,
                "reserved_rmb": reserved,
                "remaining_rmb": remaining,
                "active_reservations": len(self._reservations),
            }


def collect_run_usage(
    tasks: list[dict[str, Any]],
    run_id: str,
    usd_cny_rate: float = DEFAULT_USD_CNY_RATE,
) -> dict[str, Any]:
    totals = {
        "api_calls": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    seen: set[tuple[Path, str]] = set()
    for task in tasks:
        article_dir = Path(task["article_dir"])
        chunk_id = str(task["chunk"]["id"])
        identity = (article_dir.resolve(), chunk_id)
        if identity in seen:
            continue
        seen.add(identity)
        status_path = article_dir / "chunk_status" / f"{chunk_id}.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        stages = status.get("stages")
        if not isinstance(stages, dict):
            continue
        for stage in stages.values():
            if (
                not isinstance(stage, dict)
                or stage.get("run_id") != run_id
            ):
                continue
            usage = stage.get("usage")
            if not isinstance(usage, dict):
                continue
            totals["api_calls"] += 1
            for key in ("input_tokens", "cached_tokens", "output_tokens", "total_tokens"):
                totals[key] += _token_count(usage.get(key))
    totals["estimated_cost_usd"] = estimate_cost_usd(totals)
    totals["estimated_cost_rmb"] = estimate_cost_rmb(totals, usd_cny_rate)
    return totals


def summary_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "article_dir"}


def write_run_summary(root: Path, summary: dict[str, Any]) -> None:
    run_id = summary.get("run_id")
    if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id:
        raise ValueError("summary must contain a safe, non-empty run_id")
    run_path = root / "runs" / run_id / "run.json"
    if run_path.exists():
        existing = json.loads(run_path.read_text(encoding="utf-8"))
        if existing != summary:
            raise RuntimeError(f"Refusing to overwrite immutable run record: {run_path}")
    else:
        atomic_json(run_path, summary)
    atomic_json(root / "translation_summary.json", summary)


def glossary_text(terms: list[dict[str, Any]]) -> str:
    lines = ["| Source term | Canonical Chinese | Note |", "|---|---|---|"]
    for term in terms:
        source = str(term.get("source", "")).replace("|", "\\|")
        target = str(term.get("target", "")).replace("|", "\\|")
        note = str(term.get("note", "")).replace("|", "\\|")
        lines.append(f"| {source} | {target} | {note} |")
    return "\n".join(lines)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_protected_model_input(
    text: str,
    max_structure_tokens: int = MODEL_STRUCTURE_SEGMENT_LIMIT,
) -> tuple[str, ...]:
    """Split without changing bytes so each model request has bounded structure density."""

    if max_structure_tokens < 1:
        raise ValueError("max_structure_tokens must be positive")
    remaining = text
    segments: list[str] = []
    while True:
        tokens = list(_MODEL_SENTINEL_RE.finditer(remaining))
        if len(tokens) <= max_structure_tokens:
            if remaining:
                segments.append(remaining)
            break
        hard_split = tokens[max_structure_tokens].start()
        safe_splits = [
            match.end()
            for match in _SAFE_SEGMENT_BOUNDARY_RE.finditer(remaining, 0, hard_split)
            if match.end() > 0
        ]
        split_at = safe_splits[-1] if safe_splits else hard_split
        if split_at <= 0:
            raise RuntimeError("Unable to split structure-dense protected text safely")
        segments.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if "".join(segments) != text:
        raise AssertionError("Structure segmentation changed protected text")
    if any(
        len(_MODEL_SENTINEL_RE.findall(segment)) > max_structure_tokens
        for segment in segments
    ):
        raise AssertionError("Structure segmentation exceeded its per-request limit")
    return tuple(segments)


def build_structure_slot_input(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Remove protected anchors from model input and expose only keyed text islands."""

    anchors = tuple(_MODEL_SENTINEL_RE.findall(text))
    parts = tuple(_MODEL_SENTINEL_RE.split(text))
    if len(parts) != len(anchors) + 1:
        raise AssertionError("Structure slot split is not lossless")
    slots = [
        {"id": f"T{index:04d}", "text": part}
        for index, part in enumerate(parts)
        if _TRANSLATABLE_SLOT_RE.search(part)
    ]
    context: list[str] = []
    for index, part in enumerate(parts):
        context.append(part)
        if index < len(anchors):
            context.append(f"<ANCHOR_{index:04d}>")
    payload = json.dumps(
        {
            "protocol": STRUCTURE_SLOT_PROTOCOL,
            "source_context": "".join(context),
            "slots": slots,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if _MODEL_SENTINEL_RE.search(payload):
        raise AssertionError("Protected anchors leaked into model input")
    return payload, anchors, parts


def restore_structure_slot_output(
    response_text: str,
    anchors: tuple[str, ...],
    source_parts: tuple[str, ...],
) -> str:
    """Validate keyed translations and deterministically restore protected anchors."""

    candidate = response_text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        try:
            parsed, parsed_end = json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError:
            raise StructureMismatchError("Structure-slot response is not valid JSON") from exc
        # V4 Flash occasionally appends one or more unmatched closing braces even
        # in JSON mode. Accept only that narrow, content-free suffix.
        if candidate[parsed_end:].strip().strip("}"):
            raise StructureMismatchError("Structure-slot response is not valid JSON") from exc
    translations = parsed.get("translations") if isinstance(parsed, dict) else None
    if not isinstance(translations, dict):
        raise StructureMismatchError("Structure-slot response lacks a translations object")
    expected_ids = {
        f"T{index:04d}"
        for index, part in enumerate(source_parts)
        if _TRANSLATABLE_SLOT_RE.search(part)
    }
    if set(translations) != expected_ids:
        raise StructureMismatchError("Structure-slot response changed text-slot identities")
    for slot_id, value in translations.items():
        if (
            not isinstance(value, str)
            or _MODEL_SENTINEL_RE.search(value)
            or _CONTEXT_ANCHOR_RE.search(value)
        ):
            raise StructureMismatchError(f"Invalid structure-slot value for {slot_id}")
    source_lexical = sum(
        len(_SOURCE_LEXICAL_RE.findall(part)) for part in source_parts
    )
    target_lexical = sum(
        len(_TARGET_LEXICAL_RE.findall(value)) for value in translations.values()
    )
    for index, source_part in enumerate(source_parts):
        slot_id = f"T{index:04d}"
        if (
            len(_SOURCE_LEXICAL_RE.findall(source_part)) >= 12
            and not _TARGET_LEXICAL_RE.search(translations.get(slot_id, ""))
        ):
            raise StructureMismatchError(
                f"Structure-slot response omitted substantive slot {slot_id}"
            )
    if source_lexical >= 20 and target_lexical < math.ceil(source_lexical * 0.22):
        raise StructureMismatchError(
            "Structure-slot response is suspiciously short for the complete segment"
        )
    translated_parts = [
        translations.get(
            f"T{index:04d}",
            part.translate(_ACADEMIC_PUNCTUATION_TABLE),
        )
        for index, part in enumerate(source_parts)
    ]
    rebuilt: list[str] = []
    for index, part in enumerate(translated_parts):
        rebuilt.append(part)
        if index < len(anchors):
            rebuilt.append(anchors[index])
    result = "".join(rebuilt)
    if tuple(_MODEL_SENTINEL_RE.findall(result)) != anchors:
        raise AssertionError("Deterministic structure-slot restoration changed anchors")
    return result


def build_structure_anchor_input(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Replace real protected nodes with abstract anchors for syntax-flexible fallback."""

    anchors = tuple(_MODEL_SENTINEL_RE.findall(text))
    markers = tuple(f"<ANCHOR_{index:04d}>" for index in range(len(anchors)))
    pieces = _MODEL_SENTINEL_RE.split(text)
    template: list[str] = []
    for index, piece in enumerate(pieces):
        template.append(piece)
        if index < len(markers):
            template.append(markers[index])
    payload = json.dumps(
        {"protocol": STRUCTURE_ANCHOR_PROTOCOL, "source_template": "".join(template)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if _MODEL_SENTINEL_RE.search(payload):
        raise AssertionError("Protected anchors leaked into anchor-template input")
    return payload, anchors, markers


def restore_structure_anchor_output(
    response_text: str,
    anchors: tuple[str, ...],
    markers: tuple[str, ...],
) -> str:
    candidate = response_text.strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        try:
            parsed, parsed_end = json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError:
            raise StructureMismatchError("Anchor-template response is not valid JSON") from exc
        if candidate[parsed_end:].strip().strip("}"):
            raise StructureMismatchError("Anchor-template response is not valid JSON") from exc
    translation = parsed.get("translation") if isinstance(parsed, dict) else None
    if not isinstance(translation, str):
        raise StructureMismatchError("Anchor-template response lacks translation text")
    observed = tuple(_CONTEXT_ANCHOR_RE.findall(translation))
    if Counter(observed) != Counter(markers):
        raise StructureMismatchError("Anchor-template response changed anchor identity or count")
    if _MODEL_SENTINEL_RE.search(translation):
        raise StructureMismatchError("Anchor-template response exposed a protected node")
    restored = translation
    for marker, anchor in zip(markers, anchors):
        restored = restored.replace(marker, anchor, 1)
    return restored


def should_use_structure_anchor_fallback(
    stage: str,
    stage_status: dict[str, Any],
    paper_context: str,
) -> bool:
    """Use the flexible protocol once, then return to strict slots if it fails."""

    error = str(stage_status.get("error") or "")
    if stage == "translate" and (
        error.startswith("Invalid structure-slot value")
        or error.startswith("Structure-slot response")
    ):
        return True
    if stage not in {"revision", *OPTIONAL_STYLE_STAGES}:
        return False
    if "Anchor-template response" in error:
        return False
    return "suspiciously short" in error or "# QC-CORRECTION RETRY 2" in paper_context


def structure_segment_limit(stage_status: dict[str, Any]) -> int:
    return MODEL_STRUCTURE_SEGMENT_LIMIT


def normalize_source_month_names(source: str, translated: str) -> str:
    """Keep translated English month names from inventing Arabic numerals."""

    normalized = translated
    for month_number, (english, chinese) in enumerate(_MONTH_NAMES_ZH.items(), 1):
        if not re.search(rf"\b{english}\b", source, flags=re.I):
            continue
        normalized = re.sub(
            rf"(?<!\d){month_number}\s*月份?",
            chinese,
            normalized,
        )
        for year in re.findall(rf"\b{english}\s+([12]\d{{3}})\b", source, flags=re.I):
            normalized = re.sub(
                rf"({re.escape(year)}年{chinese})\s*{re.escape(year)}(?=\D|$)",
                r"\1",
                normalized,
            )
    return normalized


def localize_source_month_years(text: str) -> str:
    """Replace unambiguous English month-year pairs before model submission."""

    localized = text
    for english, chinese in _MONTH_NAMES_ZH.items():
        localized = re.sub(
            rf"\b{english}\s+([12]\d{{3}})\b",
            lambda match: f"{match.group(1)}年{chinese}",
            localized,
            flags=re.I,
        )
    return localized


def build_request_payload(instructions: str, input_text: str, max_output_tokens: int) -> dict[str, Any]:
    output_format = (
        {"type": "json_object"}
        if (
            "STRUCTURE-SLOT PROTOCOL" in instructions
            or "STRUCTURE-ANCHOR FALLBACK PROTOCOL" in instructions
        )
        else {"type": "text"}
    )
    return {
        "model": MODEL,
        "instructions": instructions,
        "input": input_text,
        "reasoning": {"effort": "none"},
        "max_output_tokens": max_output_tokens,
        "temperature": 0.15,
        "text": {"format": output_format},
    }


def request_key(
    *,
    stage: str,
    model: str,
    instructions: str,
    input_text: str,
    max_output_tokens: int,
) -> str:
    payload = {
        "stage": stage,
        "qc_contract_version": QC_CONTRACT_VERSION,
        **build_request_payload(instructions, input_text, max_output_tokens),
    }
    payload["model"] = model
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text_hash(serialized)


def response_usage(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage", {})
    if not isinstance(usage, dict):
        raise ResponseValidationError("DeepSeek response usage must be an object")
    input_details = usage.get("input_tokens_details")
    if input_details is not None and not isinstance(input_details, dict):
        raise ResponseValidationError("DeepSeek response usage.input_tokens_details must be an object")
    output_details = usage.get("output_tokens_details")
    if output_details is not None and not isinstance(output_details, dict):
        raise ResponseValidationError("DeepSeek response usage.output_tokens_details must be an object")
    return {
        "input_tokens": usage.get("input_tokens"),
        "cached_tokens": (input_details or {}).get("cached_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": (output_details or {}).get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def coarse_response_usage(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    return {
        "input_tokens": usage.get("input_tokens"),
        "cached_tokens": input_details.get("cached_tokens") if isinstance(input_details, dict) else None,
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None,
        "total_tokens": usage.get("total_tokens"),
    }


def response_metadata(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {"response_type": type(response).__name__}
    metadata: dict[str, Any] = {
        "id": str(response.get("id", "")),
        "status": response.get("status"),
        "model": response.get("model"),
        "incomplete_details": response.get("incomplete_details"),
        "error": response.get("error"),
        "usage": coarse_response_usage(response),
    }
    output = response.get("output")
    if isinstance(output, list):
        metadata["output_items"] = len(output)
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        metadata["output_text_length"] = len(output_text)
    return metadata


def extract_output(response: dict[str, Any]) -> str:
    texts: list[str] = []
    output = response.get("output", [])
    if output is None:
        output = []
    if not isinstance(output, list):
        raise ResponseValidationError("DeepSeek response output must be an array")
    for item in output:
        if not isinstance(item, dict):
            raise ResponseValidationError("DeepSeek response output items must be objects")
        if item.get("type") != "message":
            continue
        contents = item.get("content", [])
        if contents is None:
            contents = []
        if not isinstance(contents, list):
            raise ResponseValidationError("DeepSeek response message content must be an array")
        for content in contents:
            if not isinstance(content, dict):
                raise ResponseValidationError("DeepSeek response content items must be objects")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                texts.append(content["text"])
    if not texts and isinstance(response.get("output_text"), str):
        texts.append(response["output_text"])
    text = "\n".join(texts).strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1]).strip()
    if not text:
        raise ResponseValidationError("DeepSeek response contained no output_text")
    return text + "\n"


def validate_response(response: Any, expected_model: str) -> ParsedResponse:
    if not isinstance(response, dict):
        raise ResponseValidationError("DeepSeek response must be an object")
    status = response.get("status")
    model = str(response.get("model", ""))
    response_id = str(response.get("id", ""))

    if model != expected_model:
        raise ResponseValidationError(f"DeepSeek response used unexpected model: {model or '<missing>'}")
    if status == "incomplete":
        details = response.get("incomplete_details") or {}
        reason = details.get("reason", "unknown") if isinstance(details, dict) else "unknown"
        raise IncompleteResponseError(f"DeepSeek response incomplete: {reason}")
    if status == "failed":
        error = response.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("code") or "unknown")
        else:
            message = str(error or "unknown")
        raise FailedResponseError(f"DeepSeek response failed: {message}")
    if status != "completed":
        raise ResponseValidationError(f"DeepSeek response had unexpected status: {status!r}")
    text = extract_output(response)
    return ParsedResponse(
        text=text,
        response_id=response_id,
        model=model,
        status="completed",
        usage=response_usage(response),
        output_hash=text_hash(text),
    )


def checkpoint_is_valid(status: dict[str, Any], output_path: Path, expected_key: str) -> bool:
    if status.get("status") != "complete":
        return False
    if status.get("request_key") != expected_key:
        return False
    qc = status.get("qc")
    if not isinstance(qc, dict) or qc.get("ok") is not True:
        return False
    output_hash = status.get("output_hash")
    if not isinstance(output_hash, str) or not output_hash:
        return False
    if not nonempty(output_path):
        return False
    return text_hash(output_path.read_text(encoding="utf-8")) == output_hash


def checkpoint_is_reusable_after_contract_change(
    status: dict[str, Any], output_path: Path
) -> bool:
    """Recognize an intact, QC-accepted artifact independently of prompt hashing."""

    if status.get("status") != "complete":
        return False
    qc = status.get("qc")
    if not isinstance(qc, dict) or qc.get("ok") is not True:
        return False
    output_hash = status.get("output_hash")
    if not isinstance(output_hash, str) or not output_hash or not nonempty(output_path):
        return False
    return text_hash(output_path.read_text(encoding="utf-8")) == output_hash


def stage_output_path(article_dir: Path, chunk_id: str, final_output_file: str, stage: str) -> Path:
    if stage == "translate":
        return article_dir / f"stage1_{chunk_id}.md"
    if stage == "terminology":
        return article_dir / f"stage2_{chunk_id}.md"
    if stage == "revision":
        return article_dir / f"stage_revision_{chunk_id}.md"
    if stage == "anti_ai":
        return article_dir / f"stage3_{chunk_id}.md"
    return article_artifact_path(article_dir, final_output_file)


def persist_rejected_candidate(
    article_dir: Path,
    chunk_id: str,
    stage: str,
    request_key_value: str,
    text: str,
    *,
    protected: bool,
) -> dict[str, Any]:
    candidate_hash = text_hash(text)
    suffix = "_protected" if protected else ""
    filename = (
        f"{chunk_id}_{stage}_{request_key_value[:12]}_{candidate_hash[:12]}{suffix}.md"
    )
    path = article_dir / "rejected_candidates" / filename
    if path.exists() and path.read_text(encoding="utf-8") != text:
        raise RuntimeError(f"Rejected candidate hash collision: {path}")
    if not path.exists():
        atomic_text(path, text)
    return {
        "rejected_candidate_file": path.relative_to(article_dir).as_posix(),
        "rejected_candidate_hash": candidate_hash,
        "rejected_candidate_protected": protected,
    }


def recover_rejected_candidate(
    article_dir: Path,
    source: str,
    output_path: Path,
    stage_status: dict[str, Any],
    expected_key: str,
    qc_terms: list[dict[str, Any]],
) -> str | None:
    if stage_status.get("request_key") != expected_key:
        return None
    if stage_status.get("rejected_candidate_protected") is not False:
        return None
    relative_value = stage_status.get("rejected_candidate_file")
    if not isinstance(relative_value, str) or not relative_value:
        return None
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate_path = article_dir / relative
    try:
        candidate = candidate_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if text_hash(candidate) != stage_status.get("rejected_candidate_hash"):
        return None
    qc_report = validate_chunk(source, candidate, {}, qc_terms)
    if not qc_report.ok:
        return None
    prior_error = stage_status.pop("error", None)
    atomic_text(output_path, candidate)
    stage_status.update(
        {
            "status": "complete",
            "finished_at": now(),
            "output_hash": text_hash(candidate),
            "qc": qc_report.to_dict(),
            "recovered_from_rejected_candidate": True,
        }
    )
    if prior_error:
        stage_status["recovery_previous_error"] = prior_error
    return candidate


def recover_prior_valid_output(
    source: str,
    output_path: Path,
    stage_status: dict[str, Any],
    expected_key: str,
    qc_terms: list[dict[str, Any]],
) -> str | None:
    """Keep the last accepted artifact when a later retry candidate failed QC."""

    if stage_status.get("status") != "failed":
        return None
    if not stage_status.get("rejected_candidate_file") or not nonempty(output_path):
        return None
    candidate = output_path.read_text(encoding="utf-8")
    qc_report = validate_chunk(source, candidate, {}, qc_terms)
    if not qc_report.ok:
        return None
    prior_error = stage_status.pop("error", None)
    stage_status.update(
        {
            "status": "complete",
            "request_key": expected_key,
            "finished_at": now(),
            "output_hash": text_hash(candidate),
            "qc": qc_report.to_dict(),
            "recovered_prior_valid_output": True,
        }
    )
    if prior_error:
        stage_status["recovery_previous_error"] = prior_error
    return candidate


def complete_style_fallback(
    *,
    source: str,
    prior_text: str,
    output_path: Path,
    stage_status: dict[str, Any],
    qc_terms: list[dict[str, Any]],
    reason: str,
) -> bool:
    """Complete an optional style pass with its QC-valid input after a bad candidate."""

    qc_report = validate_chunk(source, prior_text, {}, qc_terms)
    if not qc_report.ok:
        return False
    previous_status = stage_status.get("status")
    previous_error = stage_status.get("error")
    atomic_text(output_path, prior_text)
    stage_status.update(
        {
            "status": "complete",
            "finished_at": now(),
            "output_hash": text_hash(prior_text),
            "qc": qc_report.to_dict(),
            "fallback_to_prior_stage": True,
            "fallback_reason": reason,
            "fallback_previous_status": previous_status,
        }
    )
    stage_status.pop("error", None)
    if previous_error:
        stage_status["fallback_previous_error"] = previous_error
    return True


class DeepSeekClient:
    def __init__(self, api_key: str, max_retries: int = 5) -> None:
        self.api_key = api_key
        self.max_retries = max_retries

    def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, Any], float]:
        payload = build_request_payload(instructions, input_text, max_output_tokens)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            request = urllib.request.Request(
                API_URL,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Snowmass2021-local-translation/1.0",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=900) as response_stream:
                    response = json.loads(response_stream.read().decode("utf-8"))
                return response, round(time.monotonic() - started, 3)
            except urllib.error.HTTPError as error:
                try:
                    error_body = error.read().decode("utf-8", errors="replace")
                finally:
                    error.close()
                last_error = f"HTTP {error.code}: {' '.join(error_body.split())[:500]}"
                if error.code not in RETRYABLE_HTTP_CODES or attempt >= self.max_retries:
                    raise RuntimeError(last_error) from error
            except urllib.error.URLError as error:
                if isinstance(error.reason, ssl.SSLError) and attempt < self.max_retries:
                    last_error = f"TLS handshake failed before response: {error.reason}"
                else:
                    raise AmbiguousTransportError(f"{type(error).__name__}: {error}") from error
            except (http.client.IncompleteRead, TimeoutError, OSError, json.JSONDecodeError) as error:
                raise AmbiguousTransportError(f"{type(error).__name__}: {error}") from error
            time.sleep(min(60, (2**attempt) + random.random() * 2))
        raise RuntimeError(last_error)


def stage_instructions(stage: str, glossary: str) -> str:
    common = """You are translating a high-energy physics or cosmology academic paper from English to Simplified Chinese.
The result is for scholarly readers. Preserve meaning exactly. Never add facts, explanations, examples, claims, citations, links, names, numbers, units, equations, symbols, or section order.
Preserve Markdown, LaTeX, inline math, citation markers, URLs, and line/block boundaries whenever possible.
Tokens matching [[SM_0000_...]] or [[SMU_0000_TYPE_...]] are immutable structure or typed-literal placeholders. Copy every such token exactly once, character for character, and in the same order. Never translate, rename, omit, duplicate, or move one.
Never replace a placeholder with a pronoun such as "it", "its", "其", or "该值", even when the referenced expression was just mentioned.
Copy every Arabic numeral exactly as written. Never spell a form such as 2D with Chinese numerals, introduce an Arabic numeral for a word such as "unity", or remove TeX escaping such as \\%.
Output only the complete revised Chinese text, with no preface, commentary, analysis, or quotation fences.
"""
    if stage == "translate":
        return common + f"""
Perform the first faithful translation pass. Use the following locked terminology table whenever a listed source term appears:
{glossary}
"""
    if stage == "terminology":
        return common + f"""
This is the terminology-unification pass. Revise the draft only where terminology, proper names, abbreviations, or recurring technical phrases are inconsistent with the locked table below. Keep already-correct sentences unchanged and do not stylistically rewrite the text.
Locked terminology:
{glossary}
"""
    if stage == "anti_ai":
        return common + """
This is the AI-mannerism cleanup pass. Remove formulaic AI phrasing, empty transitions, repetitive summaries, canned conclusions, excessive parallelism, and unnatural connective words. Keep the author's technical register and sentence logic. Do not shorten, summarize, embellish, or change terminology.
"""
    if stage == "revision":
        return common + """
This is the refined revision pass. Apply every relevant issue from the supplied paper critique to this paragraph. Correct accuracy, terminology, sentence structure, and native academic expression. Do not apply critique tagged to another chunk and do not add unrequested content.
"""
    return common + """
This is the final Chinese naturalization and academic-polish pass. Make the Chinese read like careful human academic prose: precise, restrained, coherent, and idiomatic. Preserve the source's level of certainty and all technical content. Do not add interpretation or omit detail.
"""


def stage_input(
    stage: str,
    source: str,
    current: str,
    glossary: str,
    *,
    include_source: bool = True,
) -> str:
    if stage == "translate":
        return source
    label = {
        "terminology": "DRAFT TRANSLATION",
        "revision": "TERMINOLOGY-CORRECTED DRAFT",
        "anti_ai": "TERMINOLOGY-CORRECTED TRANSLATION",
        "academic": "AI-MANNERISM-CLEANED TRANSLATION",
    }[stage]
    if not include_source or STRUCTURE_ANCHOR_PROTOCOL in current:
        # Anchor fallback is a narrowly scoped repair request. Re-supplying the
        # whole raw source can expose protected literals and invite the model to
        # translate text outside the current structure segment.
        return f"{label}:\n---\n{current}\n---\n\nLOCKED TERMINOLOGY:\n{glossary}\n"
    return f"ORIGINAL SOURCE:\n---\n{source}\n---\n\n{label}:\n---\n{current}\n---\n\nLOCKED TERMINOLOGY:\n{glossary}\n"


def load_neighbor_context(article_dir: Path, source_file: str, chars: int = 300) -> str:
    """Reuse translate-book's read-only adjacent-unit context renderer."""

    if not TRANSLATE_BOOK_CONTEXT.is_file():
        raise RuntimeError(f"translate-book chunk_context.py is unavailable: {TRANSLATE_BOOK_CONTEXT}")
    result = subprocess.run(
        [
            sys.executable,
            str(TRANSLATE_BOOK_CONTEXT),
            str(article_dir),
            source_file,
            "--chars",
            str(chars),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"translate-book neighbor context failed for {source_file}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def nonempty(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0 and bool(path.read_text(encoding="utf-8").strip())


def protect_stage_text(text: str):
    """Compose legacy TeX protection with typed paragraph literal protection."""

    structural = protect_structures(text)
    typed = protect_translation_unit(structural.text, max_nodes=512)
    return typed.text, structural.mapping, typed.nodes


def restore_stage_text(text: str, mapping: dict[str, str], nodes) -> str:
    try:
        typed_restored = restore_translation_unit(text, nodes)
    except TypedStructureMismatchError as error:
        raise StructureMismatchError(str(error)) from error
    return validate_and_restore(typed_restored, mapping)


def load_allowed_record_ids(path: Path) -> set[str]:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise RuntimeError(f"Rights manifest must be a JSON list: {path}")
    seen: set[str] = set()
    allowed: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RuntimeError(f"Rights manifest record {index} is not an object")
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise RuntimeError(f"Rights manifest record {index} has no record_id")
        if record_id in seen:
            raise RuntimeError(f"Duplicate record_id in rights manifest: {record_id}")
        seen.add(record_id)
        if record.get("publication_allowed") is True:
            allowed.add(record_id)
    if not allowed:
        raise RuntimeError(f"Rights manifest contains no explicitly allowed records: {path}")
    return allowed


def process_chunk(
    task: dict[str, Any],
    client: DeepSeekClient,
    terms: list[dict[str, Any]],
    run_id: str | None = None,
    budget_guard: BudgetGuard | None = None,
    stages: tuple[str, ...] | None = None,
    paper_context: str = "",
    initial_text_path: Path | None = None,
) -> dict[str, Any]:
    article_dir = task["article_dir"]
    chunk = task["chunk"]
    chunk_id = chunk["id"]
    source_path = article_artifact_path(article_dir, str(chunk["source_file"]))
    source = source_path.read_text(encoding="utf-8")
    neighbor_context = load_neighbor_context(article_dir, chunk["source_file"])
    selected_terms = select_glossary_terms(source, terms)
    glossary = glossary_text(selected_terms)
    status_path = article_dir / "chunk_status" / f"{chunk_id}.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {
            "schema_version": 1,
            "record_id": task["record_id"],
            "chunk_id": chunk_id,
            "source_file": chunk["source_file"],
            "source_hash": chunk.get("source_hash", ""),
            "stages": {},
        }
    except json.JSONDecodeError:
        status = {"schema_version": 1, "record_id": task["record_id"], "chunk_id": chunk_id, "stages": {}}

    if initial_text_path is not None:
        initial_text_path = Path(initial_text_path)
        if not nonempty(initial_text_path):
            raise RuntimeError(f"Initial refined-stage input is missing or blank: {initial_text_path}")
        current = initial_text_path.read_text(encoding="utf-8")
    else:
        current = source
    fixed_translation = task.get("fixed_translation")
    if fixed_translation is not None:
        if not isinstance(fixed_translation, str) or not fixed_translation.strip():
            raise RuntimeError(f"Fixed translation is blank for {chunk_id}")
        current = fixed_translation
    stage_sequence = stages if stages is not None else STAGES
    for stage in stage_sequence:
        output_path = stage_output_path(article_dir, chunk_id, chunk["output_file"], stage)
        stage_status = status.setdefault("stages", {}).setdefault(stage, {})
        instructions = stage_instructions(stage, glossary)
        model_current = localize_source_month_years(current) if stage == "translate" else current
        protected_current, mapping, typed_nodes = protect_stage_text(model_current)
        protected_segments = split_protected_model_input(
            protected_current,
            structure_segment_limit(stage_status),
        )
        input_texts: list[str] = []
        request_instructions: list[str] = []
        slot_protocols: list[tuple[tuple[str, ...], tuple[str, ...]] | None] = []
        anchor_protocols: list[tuple[tuple[str, ...], tuple[str, ...]] | None] = []
        segment_passthroughs: list[bool] = []
        max_outputs: list[int] = []
        use_anchor_fallback = should_use_structure_anchor_fallback(
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
        if bounded_segmented_retry:
            stage_status["bounded_segmented_retry"] = True
        for segment_index, protected_segment in enumerate(protected_segments, 1):
            segment_instructions = instructions
            slot_protocol: tuple[tuple[str, ...], tuple[str, ...]] | None = None
            anchor_protocol: tuple[tuple[str, ...], tuple[str, ...]] | None = None
            segment_passthrough = False
            if _MODEL_SENTINEL_RE.search(protected_segment):
                _, structure_anchors, structure_parts = build_structure_slot_input(
                    protected_segment
                )
                has_translatable_slots = any(
                    _TRANSLATABLE_SLOT_RE.search(part) for part in structure_parts
                )
                if not has_translatable_slots:
                    model_segment = protected_segment
                    segment_instructions = "STRUCTURE-ONLY PASSTHROUGH"
                    segment_passthrough = True
                elif use_anchor_fallback or stage in OPTIONAL_STYLE_STAGES:
                    model_segment, anchors, markers = build_structure_anchor_input(
                        protected_segment
                    )
                    anchor_protocol = (anchors, markers)
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
                    model_segment, anchors, source_parts = build_structure_slot_input(
                        protected_segment
                    )
                    slot_protocol = (anchors, source_parts)
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
                else stage_input(
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
            compact_source = len(source.strip()) <= 12
            retry_marker = "# QC-CORRECTION RETRY"
            is_retry_request = retry_marker in paper_context
            context_for_request = (
                paper_context[paper_context.index(retry_marker) :]
                if is_retry_request
                else paper_context
            )
            if compact_source and not is_retry_request:
                context_for_request = ""
            if context_for_request:
                if is_retry_request:
                    input_text += "\n\nRETRY INSTRUCTIONS:\n" + context_for_request
                else:
                    input_text += (
                        "\n\nREAD-ONLY PAPER ANALYSIS CONTEXT — apply it to this paragraph; "
                        "do not reproduce it:\n" + context_for_request
                    )
            if (
                anchor_protocol is None
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
            input_texts.append(input_text)
            request_instructions.append(segment_instructions)
            slot_protocols.append(slot_protocol)
            anchor_protocols.append(anchor_protocol)
            segment_passthroughs.append(segment_passthrough)
            # Keep long non-streaming responses below the local proxy's practical
            # response size while leaving enough headroom for Chinese output.
            max_outputs.append(
                max(4096, min(20000, int(max(len(protected_segment), 4000) * 0.8)))
            )
        segment_request_keys = [
            request_key(
                stage=stage,
                model=MODEL,
                instructions=segment_instructions,
                input_text=input_text,
                max_output_tokens=max_output,
            )
            for segment_instructions, input_text, max_output in zip(
                request_instructions, input_texts, max_outputs
            )
        ]
        expected_key = (
            segment_request_keys[0]
            if len(segment_request_keys) == 1
            else text_hash(
                json.dumps(
                    {
                        "segmentation_schema_version": 1,
                        "stage": stage,
                        "segment_request_keys": segment_request_keys,
                    },
                    sort_keys=True,
                )
            )
        )
        input_text = input_texts[0]
        max_output = max_outputs[0]
        passthrough = bool(task.get("passthrough"))
        qc_terms = [] if passthrough else selected_terms
        if checkpoint_is_valid(stage_status, output_path, expected_key):
            current = output_path.read_text(encoding="utf-8")
            continue
        context_hash = text_hash(paper_context) if stage == "revision" else None
        context_allows_reuse = (
            stage != "revision"
            or stage_status.get("paper_context_hash") == context_hash
        )
        if (
            context_allows_reuse
            and checkpoint_is_reusable_after_contract_change(stage_status, output_path)
        ):
            candidate = output_path.read_text(encoding="utf-8")
            live_qc = validate_chunk(source, candidate, {}, qc_terms)
            if live_qc.ok:
                previous_key = stage_status.get("request_key")
                stage_status.update(
                    {
                        "request_key": expected_key,
                        "qc": live_qc.to_dict(),
                        "reused_after_request_contract_change": True,
                    }
                )
                if previous_key:
                    stage_status["previous_request_key"] = previous_key
                current = candidate
                atomic_json(status_path, status)
                continue
        if (
            stage in OPTIONAL_STYLE_STAGES
            and stage_status.get("status") in {"failed", "uncertain", "running"}
            and complete_style_fallback(
                source=source,
                prior_text=current,
                output_path=output_path,
                stage_status=stage_status,
                qc_terms=qc_terms,
                reason="prior attempt did not produce a QC-valid optional style candidate",
            )
        ):
            atomic_json(status_path, status)
            current = output_path.read_text(encoding="utf-8")
            continue

        recovered = (
            recover_prior_valid_output(
                source,
                output_path,
                stage_status,
                expected_key,
                qc_terms,
            )
            if context_allows_reuse
            else None
        )
        if recovered is not None:
            current = recovered
            atomic_json(status_path, status)
            continue
        recovered = (
            recover_rejected_candidate(
                article_dir,
                source,
                output_path,
                stage_status,
                expected_key,
                qc_terms,
            )
            if context_allows_reuse
            else None
        )
        if recovered is not None:
            current = recovered
            atomic_json(status_path, status)
            continue

        decision = stage_decision(stage, current, selected_terms)
        if stage == "revision" and "NO_ACTIONABLE_CHUNK_CRITIQUE" in paper_context:
            decision = StageDecision(False, "revision_no_actionable_chunk_critique")
        if fixed_translation is not None:
            decision = StageDecision(
                False,
                str(task.get("fixed_translation_reason") or "hard_exact_translation"),
            )
        if passthrough:
            decision = StageDecision(
                False,
                str(task.get("passthrough_reason") or "reference_section_passthrough"),
            )
        started = now()
        allow_subrequest_resume = (
            stage_status.get("status") in {"running", "uncertain", "failed"}
            and stage_status.get("request_key") == expected_key
        )
        stage_status.update(
            {
                "started_at": started,
                "request_key": expected_key,
                "output_file": output_path.name,
                "decision": decision.to_dict(),
            }
        )
        if stage == "revision":
            stage_status["paper_context_hash"] = context_hash
            stage_status["paper_context_scope"] = (
                "no_actionable"
                if "NO_ACTIONABLE_CHUNK_CRITIQUE" in paper_context
                else (
                    "chunk_local"
                    if "# Actionable critique for this chunk only" in paper_context
                    else "paper_full"
                )
            )
        for stale_field in (
            "finished_at",
            "error",
            "output_hash",
            "response_output_hash",
            "raw_response",
            "response_id",
            "usage",
            "qc",
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
        ):
            stage_status.pop(stale_field, None)
        if not allow_subrequest_resume:
            stage_status.pop("subrequests", None)

        if not decision.should_call_model:
            qc_report = validate_chunk(source, current, {}, qc_terms)
            stage_status["qc"] = qc_report.to_dict()
            if not qc_report.ok:
                stage_status.update(
                    {
                        "status": "failed",
                        "finished_at": now(),
                        "error": f"QC failed: {', '.join(qc_report.failures)}",
                    }
                )
                status["status"] = "failed"
                status["updated_at"] = now()
                atomic_json(status_path, status)
                raise RuntimeError(stage_status["error"])

            atomic_text(output_path, current)
            stage_status.update(
                {
                    "status": "complete",
                    "finished_at": now(),
                    "output_hash": text_hash(current),
                }
            )
            atomic_json(status_path, status)
            continue

        stage_status.update(
            {
                "status": "running",
                "max_output_tokens": max_output if len(max_outputs) == 1 else max_outputs,
                "structure_segment_count": len(protected_segments),
            }
        )
        if run_id is not None:
            stage_status["run_id"] = run_id
        atomic_json(status_path, status)
        parsed_text = ""
        parsed_usage = {
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }
        parsed_response_ids: list[str] = []
        latency_seconds = 0.0
        subrequests = stage_status.get("subrequests")
        if not isinstance(subrequests, list) or len(subrequests) != len(input_texts):
            subrequests = [{} for _ in input_texts]
            stage_status["subrequests"] = subrequests
        protected_parts: list[str] = []
        style_fallback = False
        for segment_index, (
            protected_segment,
            segment_instructions,
            segment_input,
            segment_max_output,
            segment_key,
            slot_protocol,
            anchor_protocol,
            segment_passthrough,
        ) in enumerate(
            zip(
                protected_segments,
                request_instructions,
                input_texts,
                max_outputs,
                segment_request_keys,
                slot_protocols,
                anchor_protocols,
                segment_passthroughs,
            ),
            1,
        ):
            sub_status = subrequests[segment_index - 1]
            uncertainty_key = (
                f"{task['record_id']}:{chunk_id}:{stage}:segment{segment_index:04d}"
            )
            sub_output = (
                article_dir
                / "stage_subrequests"
                / f"{chunk_id}_{stage}_{segment_index:04d}.protected.md"
            )
            if segment_passthrough:
                atomic_text(sub_output, protected_segment)
                sub_status.clear()
                sub_status.update(
                    {
                        "status": "complete",
                        "started_at": now(),
                        "finished_at": now(),
                        "request_key": segment_key,
                        "output_file": sub_output.relative_to(article_dir).as_posix(),
                        "output_hash": text_hash(protected_segment),
                        "decision": {
                            "action": "copy_prior_text",
                            "reason": "structure_only_segment_passthrough",
                        },
                    }
                )
                atomic_json(status_path, status)
                protected_parts.append(protected_segment)
                continue
            invalid_relative = sub_status.get("invalid_structure_slot_file")
            if (
                (slot_protocol is not None or anchor_protocol is not None)
                and sub_status.get("status") == "failed"
                and sub_status.get("request_key") == segment_key
                and isinstance(invalid_relative, str)
                and invalid_relative
            ):
                invalid_path = article_dir / invalid_relative
                try:
                    invalid_text = invalid_path.read_text(encoding="utf-8")
                    invalid_hash_ok = text_hash(invalid_text) == sub_status.get(
                        "invalid_structure_slot_hash"
                    )
                    if not invalid_hash_ok:
                        raise StructureMismatchError("Invalid-output checkpoint hash mismatch")
                    if slot_protocol is not None:
                        anchors, source_parts = slot_protocol
                        recovered_segment = restore_structure_slot_output(
                            invalid_text, anchors, source_parts
                        )
                    else:
                        assert anchor_protocol is not None
                        anchors, markers = anchor_protocol
                        recovered_segment = restore_structure_anchor_output(
                            invalid_text, anchors, markers
                        )
                except (OSError, StructureMismatchError):
                    pass
                else:
                    atomic_text(sub_output, recovered_segment)
                    sub_status.update(
                        {
                            "status": "complete",
                            "finished_at": now(),
                            "output_hash": text_hash(recovered_segment),
                            "recovered_from_invalid_structure_slot": True,
                        }
                    )
                    atomic_json(status_path, status)
            if (
                sub_status.get("status") == "complete"
                and sub_status.get("request_key") == segment_key
                and isinstance(sub_status.get("output_hash"), str)
                and nonempty(sub_output)
                and text_hash(sub_output.read_text(encoding="utf-8"))
                == sub_status.get("output_hash")
            ):
                protected_parts.append(sub_output.read_text(encoding="utf-8"))
                prior_usage = sub_status.get("usage")
                if isinstance(prior_usage, dict) and sub_status.get("run_id") == run_id:
                    for key in parsed_usage:
                        parsed_usage[key] += _token_count(prior_usage.get(key))
                    latency_seconds += float(prior_usage.get("latency_seconds") or 0)
                parsed_response_ids.append(str(sub_status.get("response_id", "")))
                continue
            if (
                sub_status.get("status") in {"running", "uncertain"}
                and sub_status.get("request_key") == segment_key
            ):
                if not task.get("retry_uncertain"):
                    sub_status.update(
                        {
                            "status": "uncertain",
                            "finished_at": now(),
                            "error": "stale running paid request requires explicit retry authorization",
                        }
                    )
                    stage_status.update(
                        {
                            "status": "uncertain",
                            "finished_at": now(),
                            "error": sub_status["error"],
                        }
                    )
                    status["status"] = "uncertain"
                    status["updated_at"] = now()
                    atomic_json(status_path, status)
                    return {
                        "record_id": task["record_id"],
                        "chunk_id": chunk_id,
                        "status": "uncertain",
                    }
                if budget_guard is not None:
                    conservative_cost_rmb = float(
                        sub_status.get("conservative_cost_rmb") or 0
                    )
                    if conservative_cost_rmb <= 0:
                        conservative_cost_rmb = budget_guard.unresolved_uncertain_cost(
                            uncertainty_key
                        )
                    if conservative_cost_rmb <= 0:
                        stale_reservation = budget_guard.reserve(
                            segment_instructions + "\n" + segment_input,
                            segment_max_output,
                            uncertainty_key=uncertainty_key,
                        )
                        conservative_cost_rmb = budget_guard.commit_estimate(
                            stale_reservation
                        )
                        append_cost_ledger(
                            article_dir,
                            {
                                "event_id": uuid.uuid4().hex,
                                "kind": "uncertain_replay_reservation",
                                "stage": stage,
                                "chunk_id": chunk_id,
                                "request_key": segment_key,
                                "cost_rmb": conservative_cost_rmb,
                            },
                        )
                else:
                    conservative_cost_rmb = 0.0
                stage_status.setdefault("uncertain_replays", []).append(
                    {
                        "segment": segment_index,
                        "authorized_at": now(),
                        "request_key": segment_key,
                        "conservative_budget_committed": budget_guard is not None,
                        "conservative_cost_rmb": conservative_cost_rmb,
                    }
                )

                sub_status.clear()
            sub_status.update(
                {
                    "status": "running",
                    "started_at": now(),
                    "request_key": segment_key,
                    "output_file": sub_output.relative_to(article_dir).as_posix(),
                    "max_output_tokens": segment_max_output,
                }
            )
            if run_id is not None:
                sub_status["run_id"] = run_id
            atomic_json(status_path, status)
            reservation: str | None = None
            try:
                if budget_guard is not None:
                    reservation = budget_guard.reserve(
                        segment_instructions + "\n" + segment_input,
                        segment_max_output,
                        uncertainty_key=uncertainty_key,
                    )
                response, segment_latency = client.complete(
                    segment_instructions, segment_input, segment_max_output
                )
            except BudgetExceededError as exc:
                sub_status.update({"status": "failed", "finished_at": now(), "error": str(exc)})
                stage_status.update({"status": "failed", "finished_at": now(), "error": str(exc)})
                status["status"] = "failed"
                status["updated_at"] = now()
                atomic_json(status_path, status)
                raise
            except AmbiguousTransportError as exc:
                if reservation is not None:
                    conservative_cost_rmb = budget_guard.commit_estimate(reservation)
                else:
                    conservative_cost_rmb = 0.0
                if conservative_cost_rmb > 0:
                    append_cost_ledger(
                        article_dir,
                        {
                            "event_id": uuid.uuid4().hex,
                            "kind": "ambiguous_transport_reservation",
                            "stage": stage,
                            "chunk_id": chunk_id,
                            "request_key": segment_key,
                            "cost_rmb": conservative_cost_rmb,
                        },
                    )
                sub_status.update(
                    {
                        "status": "uncertain",
                        "finished_at": now(),
                        "error": str(exc),
                        "conservative_cost_rmb": conservative_cost_rmb,
                        "uncertainty_key": uncertainty_key,
                        "uncertainty_reservation_id": reservation,
                    }
                )
                stage_status.update({"status": "uncertain", "finished_at": now(), "error": str(exc)})
                status["status"] = "uncertain"
                status["updated_at"] = now()
                atomic_json(status_path, status)
                return {"record_id": task["record_id"], "chunk_id": chunk_id, "status": "uncertain"}
            except Exception as exc:
                if reservation is not None:
                    conservative_cost_rmb = budget_guard.commit_estimate(reservation)
                else:
                    conservative_cost_rmb = 0.0
                if conservative_cost_rmb > 0:
                    append_cost_ledger(
                        article_dir,
                        {
                            "event_id": uuid.uuid4().hex,
                            "kind": "failed_transport_reservation",
                            "stage": stage,
                            "chunk_id": chunk_id,
                            "request_key": segment_key,
                            "cost_rmb": conservative_cost_rmb,
                        },
                    )
                sub_status.update(
                    {
                        "status": "failed",
                        "finished_at": now(),
                        "error": repr(exc),
                        "conservative_cost_rmb": conservative_cost_rmb,
                    }
                )
                stage_status.update({"status": "failed", "finished_at": now(), "error": repr(exc)})
                status["status"] = "failed"
                status["updated_at"] = now()
                atomic_json(status_path, status)
                raise

            if reservation is not None:
                budget_guard.settle(reservation, coarse_response_usage(response))
                budget_guard.resolve_uncertain(uncertainty_key)
            billed_usage = coarse_response_usage(response)
            append_cost_ledger(
                article_dir,
                {
                    "event_id": str(response.get("id") or uuid.uuid4().hex),
                    "kind": "settled_response",
                    "stage": stage,
                    "chunk_id": chunk_id,
                    "request_key": segment_key,
                    "usage": billed_usage,
                    "cost_rmb": estimate_cost_rmb(
                        billed_usage,
                        budget_guard.usd_cny_rate
                        if budget_guard is not None
                        else DEFAULT_USD_CNY_RATE,
                    ),
                },
            )
            sub_status["raw_response"] = response_metadata(response)
            stage_status["raw_response"] = sub_status["raw_response"]
            stage_status["response_id"] = (
                str(response.get("id", "")) if isinstance(response, dict) else ""
            )
            atomic_json(status_path, status)
            try:
                parsed = validate_response(response, MODEL)
            except ResponseValidationError as exc:
                sub_status.update({"status": "failed", "finished_at": now(), "error": str(exc)})
                stage_status.update({"status": "failed", "finished_at": now(), "error": str(exc)})
                status["status"] = "failed"
                status["updated_at"] = now()
                atomic_json(status_path, status)
                raise
            parsed_segment_text = parsed.text
            if slot_protocol is not None:
                anchors, source_parts = slot_protocol
                try:
                    parsed_segment_text = restore_structure_slot_output(
                        parsed.text, anchors, source_parts
                    )
                except StructureMismatchError as exc:
                    invalid_output = sub_output.with_suffix(".invalid.md")
                    atomic_text(invalid_output, parsed.text)
                    sub_status.update(
                        {
                            "status": "failed",
                            "finished_at": now(),
                            "error": str(exc),
                            "structure_slot_protocol": STRUCTURE_SLOT_PROTOCOL,
                            "invalid_structure_slot_file": invalid_output.relative_to(
                                article_dir
                            ).as_posix(),
                            "invalid_structure_slot_hash": text_hash(parsed.text),
                        }
                    )
                    stage_status.update(
                        {"status": "failed", "finished_at": now(), "error": str(exc)}
                    )
                    status["status"] = "failed"
                    status["updated_at"] = now()
                    atomic_json(status_path, status)
                    if stage in OPTIONAL_STYLE_STAGES and complete_style_fallback(
                        source=source,
                        prior_text=current,
                        output_path=output_path,
                        stage_status=stage_status,
                        qc_terms=qc_terms,
                        reason=str(exc),
                    ):
                        style_fallback = True
                        break
                    raise
            elif anchor_protocol is not None:
                anchors, markers = anchor_protocol
                try:
                    parsed_segment_text = restore_structure_anchor_output(
                        parsed.text, anchors, markers
                    )
                except StructureMismatchError as exc:
                    invalid_output = sub_output.with_suffix(".invalid.md")
                    atomic_text(invalid_output, parsed.text)
                    sub_status.update(
                        {
                            "status": "failed",
                            "finished_at": now(),
                            "error": str(exc),
                            "structure_slot_protocol": STRUCTURE_ANCHOR_PROTOCOL,
                            "invalid_structure_slot_file": invalid_output.relative_to(
                                article_dir
                            ).as_posix(),
                            "invalid_structure_slot_hash": text_hash(parsed.text),
                        }
                    )
                    stage_status.update(
                        {"status": "failed", "finished_at": now(), "error": str(exc)}
                    )
                    status["status"] = "failed"
                    status["updated_at"] = now()
                    atomic_json(status_path, status)
                    if stage in OPTIONAL_STYLE_STAGES and complete_style_fallback(
                        source=source,
                        prior_text=current,
                        output_path=output_path,
                        stage_status=stage_status,
                        qc_terms=qc_terms,
                        reason=str(exc),
                    ):
                        style_fallback = True
                        break
                    raise
            expected_segment_sentinels = tuple(_MODEL_SENTINEL_RE.findall(protected_segment))
            observed_segment_sentinels = tuple(_MODEL_SENTINEL_RE.findall(parsed_segment_text))
            segment_structure_valid = (
                Counter(observed_segment_sentinels) == Counter(expected_segment_sentinels)
                if anchor_protocol is not None
                else observed_segment_sentinels == expected_segment_sentinels
            )
            if len(protected_segments) > 1 and not segment_structure_valid:
                error = StructureMismatchError(
                    f"Structure segment {segment_index}/{len(protected_segments)} changed "
                    "placeholder identity or order"
                )
                sub_status.update(
                    {
                        "status": "failed",
                        "finished_at": now(),
                        "error": str(error),
                        "expected_sentinels": list(expected_segment_sentinels),
                        "observed_sentinels": list(observed_segment_sentinels),
                    }
                )
                stage_status.update({"status": "failed", "finished_at": now(), "error": str(error)})
                status["status"] = "failed"
                status["updated_at"] = now()
                atomic_json(status_path, status)
                if stage in OPTIONAL_STYLE_STAGES and complete_style_fallback(
                    source=source,
                    prior_text=current,
                    output_path=output_path,
                    stage_status=stage_status,
                    qc_terms=qc_terms,
                    reason=str(error),
                ):
                    style_fallback = True
                    break
                raise error

            atomic_text(sub_output, parsed_segment_text)
            usage = dict(parsed.usage)
            usage["latency_seconds"] = segment_latency
            sub_status.update(
                {
                    "status": "complete",
                    "finished_at": now(),
                    "response_id": parsed.response_id,
                    "usage": usage,
                    "output_hash": text_hash(parsed_segment_text),
                    "structure_slot_protocol": (
                        STRUCTURE_SLOT_PROTOCOL
                        if slot_protocol is not None
                        else (
                            STRUCTURE_ANCHOR_PROTOCOL
                            if anchor_protocol is not None
                            else None
                        )
                    ),
                }
            )
            atomic_json(status_path, status)
            protected_parts.append(parsed_segment_text)
            parsed_response_ids.append(parsed.response_id)
            for key in parsed_usage:
                parsed_usage[key] += _token_count(parsed.usage.get(key))
            latency_seconds += segment_latency

        if style_fallback:
            atomic_json(status_path, status)
            current = output_path.read_text(encoding="utf-8")
            continue

        parsed_text = "".join(protected_parts)
        parsed_output_hash = text_hash(parsed_text)
        stage_status["raw_response"] = (
            subrequests[0].get("raw_response", {})
            if len(protected_segments) == 1
            else {"segmented": True, "segments": len(protected_segments)}
        )
        stage_status["response_id"] = ",".join(filter(None, parsed_response_ids))
        stage_status["usage"] = dict(parsed_usage)
        stage_status["usage"]["latency_seconds"] = latency_seconds
        atomic_json(status_path, status)

        try:
            restored_text = restore_stage_text(parsed_text, mapping, typed_nodes)
        except StructureMismatchError as exc:
            stage_status.update(
                persist_rejected_candidate(
                    article_dir,
                    chunk_id,
                    stage,
                    expected_key,
                    parsed_text,
                    protected=True,
                )
            )
            expected_sentinels = list(mapping) + [node.token for node in typed_nodes]
            observed_sentinels = list(sentinel_sequence(parsed_text)) + [
                node.token for node in typed_nodes if node.token in parsed_text
            ]
            expected_counts = Counter(expected_sentinels)
            observed_counts = Counter(observed_sentinels)
            missing_sentinels = list((expected_counts - observed_counts).elements())
            unexpected_sentinels = list((observed_counts - expected_counts).elements())
            stage_status.update({"status": "failed", "finished_at": now(), "error": str(exc)})
            stage_status["structure_diagnostics"] = {
                "expected_sentinels": expected_sentinels,
                "observed_sentinels": observed_sentinels,
                "missing_sentinels": missing_sentinels,
                "unexpected_sentinels": unexpected_sentinels,
            }
            status["status"] = "failed"
            status["updated_at"] = now()
            atomic_json(status_path, status)
            raise

        restored_text = normalize_source_month_names(source, restored_text)
        qc_report = validate_chunk(source, restored_text, {}, qc_terms)
        stage_status["qc"] = qc_report.to_dict()
        if not qc_report.ok:
            stage_status.update(
                persist_rejected_candidate(
                    article_dir,
                    chunk_id,
                    stage,
                    expected_key,
                    restored_text,
                    protected=False,
                )
            )
            stage_status.update(
                {
                    "status": "failed",
                    "finished_at": now(),
                    "error": f"QC failed: {', '.join(qc_report.failures)}",
                }
            )
            status["status"] = "failed"
            status["updated_at"] = now()
            atomic_json(status_path, status)
            if stage in OPTIONAL_STYLE_STAGES and complete_style_fallback(
                source=source,
                prior_text=current,
                output_path=output_path,
                stage_status=stage_status,
                qc_terms=qc_terms,
                reason=f"QC failed: {', '.join(qc_report.failures)}",
            ):
                atomic_json(status_path, status)
                current = output_path.read_text(encoding="utf-8")
                continue
            raise RuntimeError(stage_status["error"])

        atomic_text(output_path, restored_text)
        usage = dict(parsed_usage)
        usage["latency_seconds"] = latency_seconds
        stage_status.update(
            {
                "status": "complete",
                "finished_at": now(),
                "response_id": ",".join(filter(None, parsed_response_ids)),
                "usage": usage,
                "response_output_hash": parsed_output_hash,
                "output_hash": text_hash(restored_text),
            }
        )
        atomic_json(status_path, status)
        current = restored_text

    output_file = str(chunk["output_file"])
    meta_path = article_artifact_path(article_dir, f"{output_file[:-3]}.meta.json")
    if "academic" in stage_sequence and not meta_path.exists():
        atomic_json(
            meta_path,
            {
                "schema_version": 1,
                "new_entities": [],
                "alias_hypotheses": [],
                "attribute_hypotheses": [],
                "used_term_sources": [],
                "conflicts": [],
            },
        )
    status["status"] = "complete"
    status["updated_at"] = now()
    atomic_json(status_path, status)
    return {"record_id": task["record_id"], "chunk_id": chunk_id, "status": "complete"}


def figure_text_chunk_ids(
    article_dir: Path, manifest: dict[str, Any]
) -> set[str]:
    """Identify figure-owned text from new policy metadata or legacy JSON IR."""

    return babeldoc_bridge.resolve_figure_text_chunk_ids(article_dir, manifest)


def table_text_chunk_ids(
    article_dir: Path, manifest: dict[str, Any]
) -> set[str]:
    """Identify table-body text from page-layout geometry, never wording heuristics."""

    return babeldoc_bridge.resolve_table_text_chunk_ids(article_dir, manifest)


def collect_tasks(
    root: Path,
    max_articles: int,
    max_chunks: int,
    article_filter: str | None,
    allowed_record_ids: set[str],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    article_dirs = sorted(path for path in (root / "papers").iterdir() if path.is_dir())
    if article_filter:
        article_dirs = [path for path in article_dirs if path.name == article_filter]
    selected_articles = 0
    for article_dir in article_dirs:
        manifest_path = article_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record_id = json.loads((article_dir / "chunking_status.json").read_text(encoding="utf-8"))["record_id"]
        if record_id not in allowed_record_ids:
            continue
        if max_articles and selected_articles >= max_articles:
            break
        selected_articles += 1
        figure_ids = figure_text_chunk_ids(article_dir, manifest)
        table_ids = table_text_chunk_ids(article_dir, manifest)
        for chunk in sorted(manifest.get("chunks", []), key=lambda item: item.get("order", 0)):
            figure_passthrough = str(chunk["id"]) in figure_ids
            table_passthrough = str(chunk["id"]) in table_ids
            tasks.append(
                {
                    "article_dir": article_dir,
                    "record_id": record_id,
                    "chunk": chunk,
                    "passthrough": figure_passthrough or table_passthrough,
                    "passthrough_reason": (
                        "figure_internal_text_passthrough"
                        if figure_passthrough
                        else (
                            "table_internal_text_passthrough"
                            if table_passthrough
                            else None
                        )
                    ),
                }
            )
    return tasks[:max_chunks] if max_chunks else tasks


def finalize_run_states(root: Path, completed: list[dict[str, Any]]) -> None:
    by_article: dict[Path, list[str]] = {}
    for item in completed:
        by_article.setdefault(item["article_dir"], []).append(item["chunk_id"])
    runner = Path("/Users/Zhuanz/.agents/skills/translate-book/scripts/run_state.py")
    for article_dir, chunk_ids in by_article.items():
        command = ["python3", str(runner), "record", str(article_dir), *sorted(set(chunk_ids))]
        subprocess.run(command, capture_output=True, text=True, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=TRANSLATION_ROOT)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--max-articles", type=int, default=0)
    parser.add_argument("--max-chunks", type=int, default=0)
    parser.add_argument("--article", default="")
    parser.add_argument("--rights-manifest", type=Path, default=RIGHTS_MANIFEST)
    parser.add_argument("--glossary", type=Path)
    parser.add_argument("--max-cost-rmb", type=float, required=True)
    parser.add_argument("--usd-cny-rate", type=float, default=DEFAULT_USD_CNY_RATE)
    args = parser.parse_args(argv)
    if args.concurrency < 1 or args.concurrency > 64:
        parser.error("--concurrency must be between 1 and 64")
    if not math.isfinite(args.max_cost_rmb) or args.max_cost_rmb <= 0:
        parser.error("--max-cost-rmb must be finite and greater than zero")
    if not math.isfinite(args.usd_cny_rate) or args.usd_cny_rate <= 0:
        parser.error("--usd-cny-rate must be finite and positive")
    glossary_path = resolve_glossary_path(args.root, args.glossary)
    global_terms = load_glossary(glossary_path)
    allowed_record_ids = load_allowed_record_ids(args.rights_manifest)
    tasks = collect_tasks(
        args.root,
        args.max_articles,
        args.max_chunks,
        args.article or None,
        allowed_record_ids,
    )
    if not tasks:
        raise SystemExit("No chunk tasks found")
    api_key = load_api_key()
    client = DeepSeekClient(api_key)
    budget_guard = BudgetGuard(args.max_cost_rmb, args.usd_cny_rate)
    run_id = uuid.uuid4().hex
    print(
        f"START chunks={len(tasks)} eligible_records={len(allowed_record_ids)} "
        f"concurrency={args.concurrency} stages={','.join(STAGES)}",
        flush=True,
    )
    completed: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    article_dirs = {Path(task["article_dir"]) for task in tasks}
    article_terms = {
        article_dir: merge_glossary_terms(
            global_terms,
            load_article_glossary(article_dir),
        )
        for article_dir in article_dirs
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        future_map = {
            executor.submit(
                process_chunk,
                task,
                client,
                article_terms[Path(task["article_dir"])],
                run_id,
                budget_guard,
            ): task
            for task in tasks
        }
        for index, future in enumerate(concurrent.futures.as_completed(future_map), 1):
            task = future_map[future]
            try:
                result = future.result()
                if result["status"] == "complete":
                    result["article_dir"] = task["article_dir"]
                    completed.append(result)
                    print(
                        f"PROGRESS {index}/{len(tasks)} {result['record_id']} {result['chunk_id']} complete",
                        flush=True,
                    )
                elif result["status"] == "uncertain":
                    uncertain.append(summary_result(result))
                    print(
                        f"UNCERTAIN {index}/{len(tasks)} {result['record_id']} {result['chunk_id']}",
                        flush=True,
                    )
                else:
                    failures.append(summary_result(result))
            except Exception as exc:  # noqa: BLE001 - checkpointed retries happen on rerun
                failure = {"record_id": task["record_id"], "chunk_id": task["chunk"]["id"], "error": repr(exc)}
                failures.append(failure)
                print(f"FAIL {index}/{len(tasks)} {failure['record_id']} {failure['chunk_id']} {failure['error']}", flush=True)
    finalize_run_states(args.root, completed)
    summary = {
        "updated_at": now(),
        "total_chunks": len(tasks),
        "completed": len(completed),
        "uncertain": uncertain,
        "failed": failures,
        "stages": list(STAGES),
        "model": MODEL,
        "eligible_records": len(allowed_record_ids),
        "rights_manifest": str(args.rights_manifest),
        "glossary": str(glossary_path),
        "run_id": run_id,
        "usage": collect_run_usage(tasks, run_id, args.usd_cny_rate),
        "budget": budget_guard.snapshot(),
        "pricing": {
            "currency": "USD",
            "per_million_tokens": {
                "input_cache_hit": INPUT_CACHE_HIT_USD_PER_MILLION,
                "input_cache_miss": INPUT_CACHE_MISS_USD_PER_MILLION,
                "output": OUTPUT_USD_PER_MILLION,
            },
            "source": PRICING_SOURCE,
            "verified_at": PRICING_VERIFIED_AT,
            "usd_cny_rate": args.usd_cny_rate,
            "usd_cny_rate_policy": "operator-pinned conservative conversion",
        },
    }
    write_run_summary(args.root, summary)
    printable = {key: value for key, value in summary.items() if key not in {"failed", "uncertain"}}
    printable["uncertain"] = len(uncertain)
    printable["failed"] = len(failures)
    print(json.dumps(printable, ensure_ascii=False, indent=2), flush=True)
    return 0 if not failures and not uncertain else 2


if __name__ == "__main__":
    raise SystemExit(main())
