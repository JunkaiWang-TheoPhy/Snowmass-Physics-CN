#!/usr/bin/env python3
"""Run one fail-closed Snowmass A/B through PDFMathTranslate-next.

This adapter deliberately owns only publication/budget gates, the DeepSeek HTTP
budget boundary, and immutable receipts.  PDF parsing, translation placement,
refill, and rendering remain owned by the pinned upstream pdf2zh-next release.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import fcntl
import hashlib
import importlib.metadata
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, Self

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PDF2ZH_NEXT_VERSION = "2.9.0"
EXPECTED_BABELDOC_VERSION = "0.6.4"
UPSTREAM_REQUEST_TIMEOUT_SECONDS = 60
DEEPSEEK_CONNECTIVITY_TIMEOUT_SECONDS = 10
DEEPSEEK_CONNECTIVITY_RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0)
MODEL = "deepseek-v4-flash"
PROJECT_MAXIMUM_RMB = 100.0
STAGE_MAXIMUM_RMB = 100.0
USD_CNY_RATE = 7.2
PRICE_CUTOVER_UTC = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
MAX_OUTPUT_TOKENS_PER_REQUEST = 4096
DEFAULT_PAGES = "1,2,6,8,11,19-20"
DEFAULT_RECORD_ID = "arxiv:2203.07506"


class PublicationBlockedError(RuntimeError):
    """The requested paper lacks a literal publication_allowed=true record."""


class BudgetExceededError(RuntimeError):
    """The next request would exceed a finite stage or project budget."""


class RequestCapExceededError(RuntimeError):
    """The official engine attempted more requests than the authorized cap."""


class DeepSeekConnectivityError(RuntimeError):
    """The zero-paid DeepSeek reachability check failed."""


class CitationLockError(RuntimeError):
    """The model changed, reordered, omitted, or invented a citation marker."""


_NUMERIC_CITATION_PATTERN = (
    r"\[(?:\s*\d+\s*)(?:(?:,|[-–—])\s*\d+\s*)*\]"
)
_BABELDOC_PLACEHOLDER_PATTERN = r"\{c\d+\}"
_NUMERIC_CITATION_RE = re.compile(_NUMERIC_CITATION_PATTERN)
_BABELDOC_PLACEHOLDER_RE = re.compile(_BABELDOC_PLACEHOLDER_PATTERN)
_STRUCTURAL_ANCHOR_RE = re.compile(
    rf"(?:{_BABELDOC_PLACEHOLDER_PATTERN}|{_NUMERIC_CITATION_PATTERN})"
)
_CITATION_TOKEN_RE = re.compile(r"\[\[SMCIT_\d{6}\]\]")


@dataclass(frozen=True)
class CitationLock:
    tokens: tuple[str, ...]
    markers: tuple[str, ...]
    placeholders: tuple[str, ...]
    numeric_citation_count: int
    rich_placeholder_count: int

    @property
    def count(self) -> int:
        return len(self.tokens) + len(self.placeholders)


def is_numeric_citation_formula(value: str) -> bool:
    return _NUMERIC_CITATION_RE.fullmatch(str(value).strip()) is not None


def install_babeldoc_citation_placeholder_patch() -> None:
    """Emit distinct native placeholders for citation-only PDF formulas."""

    from babeldoc.format.pdf.document_il.midend.il_translator import (
        FormulaPlaceholder,
        ILTranslator,
    )
    from babeldoc.format.pdf.document_il.utils.layout_helper import (
        get_char_unicode_string,
    )

    if getattr(ILTranslator, "_snowmass_citation_placeholder_patch", False):
        return
    original = ILTranslator.create_formula_placeholder

    def create_formula_placeholder(self, formula, formula_id, paragraph):
        formula_text = get_char_unicode_string(formula.pdf_character)
        if is_numeric_citation_formula(formula_text):
            placeholder = f"{{c{formula_id}}}"
            regex_pattern = rf"\{{\s*c\s*{formula_id}\s*\}}"
            if re.search(
                regex_pattern,
                str(getattr(paragraph, "unicode", "") or ""),
                re.IGNORECASE,
            ):
                return create_formula_placeholder(
                    self, formula, formula_id + 1, paragraph
                )
            return FormulaPlaceholder(
                formula_id, formula, placeholder, regex_pattern
            )
        return original(self, formula, formula_id, paragraph)

    ILTranslator.create_formula_placeholder = create_formula_placeholder
    ILTranslator._snowmass_citation_placeholder_patch = True


def _lock_citations_in_text(
    value: str,
    *,
    tokens: list[str],
    markers: list[str],
    placeholders: list[str],
) -> str:
    if "[[SMCIT_" in value:
        raise CitationLockError("source text collides with citation lock token")

    def replace(match: re.Match[str]) -> str:
        if match.group(0).startswith("{c"):
            placeholders.append(match.group(0))
            return match.group(0)
        token = f"[[SMCIT_{len(tokens) + 1:06d}]]"
        tokens.append(token)
        markers.append(match.group(0))
        return token

    return _STRUCTURAL_ANCHOR_RE.sub(replace, value)


def lock_numeric_citations(
    messages: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], CitationLock]:
    """Replace user citation markers with unique immutable transport tokens."""

    locked = copy.deepcopy(messages)
    tokens: list[str] = []
    markers: list[str] = []
    placeholders: list[str] = []
    for message in locked:
        if str(message.get("role") or "") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = _lock_citations_in_text(
                content,
                tokens=tokens,
                markers=markers,
                placeholders=placeholders,
            )
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = _lock_citations_in_text(
                        part["text"],
                        tokens=tokens,
                        markers=markers,
                        placeholders=placeholders,
                    )
    return locked, CitationLock(
        tuple(tokens),
        tuple(markers),
        tuple(placeholders),
        len(tokens),
        len(placeholders),
    )


def unlock_numeric_citations_response(
    response_body: bytes, citation_lock: CitationLock
) -> bytes:
    """Validate lock conservation and restore exact source citation bytes."""

    if citation_lock.count == 0:
        return response_body
    try:
        payload = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CitationLockError("citation-locked response is not JSON") from error
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise CitationLockError("citation-locked response has no choices")
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise CitationLockError("citation-locked response content is missing")
        actual_tokens = tuple(_CITATION_TOKEN_RE.findall(content))
        if actual_tokens != citation_lock.tokens:
            raise CitationLockError("citation lock tokens changed or reordered")
        without_tokens = _CITATION_TOKEN_RE.sub("", content)
        if _NUMERIC_CITATION_RE.search(without_tokens):
            raise CitationLockError("model invented an unlocked citation marker")
        actual_placeholders = tuple(_BABELDOC_PLACEHOLDER_RE.findall(without_tokens))
        if actual_placeholders != citation_lock.placeholders:
            raise CitationLockError("rich placeholders changed or reordered")
        restored = content
        for token, marker in zip(
            citation_lock.tokens, citation_lock.markers, strict=True
        ):
            restored = restored.replace(token, marker, 1)
        message["content"] = restored
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


@dataclass(frozen=True)
class RunConfig:
    record_id: str
    source_pdf: Path
    rights_manifest: Path
    source_manifest: Path
    glossary_json: Path
    output_root: Path
    pages: str
    project_max_cost_rmb: float
    stage_max_cost_rmb: float
    stage_max_api_calls: int
    qps: int
    pool_max_workers: int
    project_control_dir: Path | None = None
    usd_cny_rate: float = USD_CNY_RATE
    supplemental_glossary_json: Path | None = None
    projected_request_cap: int | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_bytes(path: Path, content: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    content = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    atomic_bytes(path, content)


def normalize_record_id(value: str) -> str:
    return str(value).strip().lower()


def shared_project_run_id(
    record_id: str, source_sha256: str, output_root: Path
) -> str:
    digest = hashlib.sha256(
        (
            f"{normalize_record_id(record_id)}\0"
            f"{source_sha256}\0{Path(output_root).resolve()}"
        ).encode()
    ).hexdigest()[:24]
    return f"pdf2zh-next-ab-{digest}"


def pid_is_alive(value: Any) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def require_publication_allowed(path: Path, record_id: str) -> dict[str, Any]:
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise PublicationBlockedError("rights manifest must be a JSON list")
    wanted = normalize_record_id(record_id)
    for record in records:
        if not isinstance(record, dict):
            continue
        candidate = normalize_record_id(
            str(record.get("record_id") or record.get("paper_id") or "")
        )
        if candidate == wanted:
            if record.get("publication_allowed") is True:
                return record
            raise PublicationBlockedError(
                f"{wanted} is blocked: publication_allowed is not literal true"
            )
    raise PublicationBlockedError(f"{wanted} has no rights record")


def require_source_identity(
    source_manifest: Path, record_id: str, source_pdf: Path
) -> dict[str, Any]:
    payload = json.loads(Path(source_manifest).read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise PublicationBlockedError("source manifest must contain a records list")
    wanted = normalize_record_id(record_id)
    source_hash = sha256_file(source_pdf)
    source_size = source_pdf.stat().st_size
    for record in records:
        if not isinstance(record, dict):
            continue
        if normalize_record_id(str(record.get("record_id") or "")) != wanted:
            continue
        if record.get("pdf_status") != "complete":
            raise PublicationBlockedError(f"{wanted} source PDF is not complete")
        expected_hash = str(record.get("pdf_sha256") or "")
        expected_size = int(record.get("pdf_bytes") or -1)
        if expected_hash != source_hash or expected_size != source_size:
            raise PublicationBlockedError(
                f"{wanted} source PDF does not match the trusted source manifest"
            )
        return record
    raise PublicationBlockedError(f"{wanted} has no trusted source identity")


def _finite_positive(value: Any, *, label: str, maximum: float) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} budget must be finite and greater than zero")
    if parsed > maximum:
        raise ValueError(f"{label} budget must not exceed ¥{maximum:.2f}")
    return parsed


def validate_budgets(project: Any, stage: Any) -> tuple[float, float]:
    project_value = _finite_positive(
        project, label="project", maximum=PROJECT_MAXIMUM_RMB
    )
    stage_value = _finite_positive(stage, label="stage", maximum=STAGE_MAXIMUM_RMB)
    if stage_value > project_value:
        raise ValueError("stage budget must not exceed project budget")
    return project_value, stage_value


def validate_request_cap(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(  # noqa: TRY004 - one public validation error contract
            "request cap must be a finite positive integer"
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("request cap must be a finite positive integer") from error
    if not math.isfinite(parsed) or parsed <= 0 or not parsed.is_integer():
        raise ValueError("request cap must be a finite positive integer")
    return int(parsed)


def pricing_for_utc(value: datetime) -> dict[str, Any]:
    moment = value.astimezone(timezone.utc)
    if moment < PRICE_CUTOVER_UTC:
        return {
            "schedule": "legacy_before_2026-08-16T16:00:00Z",
            "input_cache_hit_usd_per_million": 0.0028,
            "input_cache_miss_usd_per_million": 0.14,
            "output_usd_per_million": 0.28,
        }
    decimal_hour = moment.hour + moment.minute / 60 + moment.second / 3600
    peak = 1 <= decimal_hour < 4 or 6 <= decimal_hour < 10
    return {
        "schedule": "peak" if peak else "off_peak",
        "input_cache_hit_usd_per_million": 0.014 if peak else 0.007,
        "input_cache_miss_usd_per_million": 0.44 if peak else 0.22,
        "output_usd_per_million": 1.32 if peak else 0.66,
    }


def conservative_pricing() -> dict[str, Any]:
    return {
        "schedule": "post_cutover_peak_conservative",
        "input_cache_hit_usd_per_million": 0.014,
        "input_cache_miss_usd_per_million": 0.44,
        "output_usd_per_million": 1.32,
    }


def cost_rmb(
    *,
    prompt_tokens: int,
    cache_hit_prompt_tokens: int,
    completion_tokens: int,
    pricing: Mapping[str, Any],
    usd_cny_rate: float,
) -> float:
    prompt = max(0, int(prompt_tokens))
    cached = max(0, min(prompt, int(cache_hit_prompt_tokens)))
    completion = max(0, int(completion_tokens))
    uncached = prompt - cached
    cost_usd = (
        cached * float(pricing["input_cache_hit_usd_per_million"])
        + uncached * float(pricing["input_cache_miss_usd_per_million"])
        + completion * float(pricing["output_usd_per_million"])
    ) / 1_000_000
    return cost_usd * float(usd_cny_rate)


def materialize_glossary_csv(
    source: Path | tuple[Path, ...], target: Path
) -> str:
    sources = (source,) if isinstance(source, Path) else tuple(source)
    if not sources:
        raise ValueError("at least one locked glossary is required")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=["source", "target", "tgt_lng"],
        lineterminator="\r\n",
    )
    writer.writeheader()
    decisions: dict[str, str] = {}
    spellings: dict[str, str] = {}
    for glossary_index, glossary_source in enumerate(sources):
        payload = json.loads(Path(glossary_source).read_text(encoding="utf-8"))
        terms = payload.get("terms") if isinstance(payload, dict) else None
        if not isinstance(terms, list):
            raise ValueError(  # noqa: TRY004 - malformed file, not caller type misuse
                "locked glossary must contain a terms list"
            )
        for term_index, term in enumerate(terms):
            if not isinstance(term, dict):
                raise ValueError(  # noqa: TRY004 - malformed file data
                    f"glossary term {glossary_index}:{term_index} is not an object"
                )
            source_term = str(term.get("source") or "").strip()
            target_term = str(term.get("target") or "").strip()
            if not source_term or not target_term:
                raise ValueError(
                    f"glossary term {glossary_index}:{term_index} is missing source or target"
                )
            aliases = term.get("aliases") or []
            if not isinstance(aliases, list):
                raise ValueError(
                    f"glossary term {glossary_index}:{term_index} aliases must be a list"
                )
            for candidate in (source_term, *(str(alias).strip() for alias in aliases)):
                if not candidate:
                    continue
                normalized = " ".join(candidate.casefold().split())
                previous = decisions.get(normalized)
                if previous == target_term:
                    continue
                decisions[normalized] = target_term
                spellings[normalized] = candidate
    for normalized, target_term in decisions.items():
        writer.writerow(
            {
                "source": spellings[normalized],
                "target": target_term,
                "tgt_lng": "zh",
            }
        )
    atomic_bytes(target, buffer.getvalue().encode("utf-8"))
    return sha256_file(target)


SYSTEM_PROMPT = """You are translating an academic physics paper from English to Simplified Chinese.
Translate faithfully and concisely; use natural, publication-quality Chinese academic prose.
Apply the supplied glossary as a hard terminology constraint.
Preserve every number, unit, equation, symbol, citation marker, URL, DOI, proper name, acronym,
and rich-text or formula placeholder exactly. Never add facts, explanations, notes, or headings.
Keep all citation markers in their exact source order; do not reorder clauses across them.
Transport citation-anchor tokens are immutable: copy every anchor exactly once, in the same
order, and never create a new citation-anchor token.
Text belonging to figures, plots, legends, axes, annotations, and tables must remain verbatim in
the source language. Bibliographic entries and their numbering must remain verbatim; translate
only ordinary prose and the References heading when it is presented as an independent heading.
Translate prose quotations even when they begin with bracketed letters or preserve original case.
Return only the requested translated text."""


def build_safe_settings_spec(
    *,
    output_dir: Path,
    glossary_csv: Path,
    pages: str,
    qps: int,
    pool_max_workers: int,
) -> dict[str, Any]:
    if int(qps) <= 0 or int(pool_max_workers) <= 0:
        raise ValueError("qps and pool_max_workers must be positive")
    return {
        "runtime": {
            "pdf2zh_next": EXPECTED_PDF2ZH_NEXT_VERSION,
            "babeldoc": EXPECTED_BABELDOC_VERSION,
        },
        "engine": {"provider": "DeepSeek", "model": MODEL, "thinking_mode": "disabled"},
        "translation": {
            "lang_in": "en",
            "lang_out": "zh",
            "output": str(Path(output_dir).resolve()),
            "qps": int(qps),
            "pool_max_workers": int(pool_max_workers),
            "term_qps": int(qps),
            "term_pool_max_workers": int(pool_max_workers),
            "no_auto_extract_glossary": True,
            "ignore_cache": True,
            "glossaries": str(Path(glossary_csv).resolve()),
            "primary_font_family": "serif",
            "custom_system_prompt": SYSTEM_PROMPT,
        },
        "pdf": {
            "pages": pages,
            "no_dual": False,
            "no_mono": False,
            "watermark_output_mode": "no_watermark",
            "translate_table_text": False,
            "skip_scanned_detection": True,
            "only_include_translated_page": True,
            "figure_table_protection_threshold": 0.95,
            # Contents pages use alternating section/page-number lines.
            # Keeping them separate lets the local TOC reconciler rebuild
            # rows without allowing BabelDOC to fuse neighboring entries.
            "no_merge_alternating_line_numbers": True,
        },
        # Keep debug mode enabled because it selects the stable direct path on
        # macOS.  run_official_translation disables its debug injectors before
        # rendering, so this flag never becomes a release artifact.
        "basic": {"debug": True},
    }


def sanitize_for_receipt(value: Any, *, secrets: list[str] | tuple[str, ...]) -> Any:
    secret_keys = {
        "api_key",
        "authorization",
        "access_token",
        "refresh_token",
        "password",
        "secret",
    }
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in secret_keys or str(key).lower().endswith("_api_key"):
                sanitized[str(key)] = "[REDACTED]"
            else:
                sanitized[str(key)] = sanitize_for_receipt(item, secrets=secrets)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_for_receipt(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    return value


def parse_selected_pages(specification: str, page_count: int) -> list[int]:
    selected: set[int] = set()
    for raw_part in specification.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError("empty page selector")
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text) if start_text else 1
            end = int(end_text) if end_text else page_count
        else:
            start = end = int(part)
        if start < 1 or end < start or end > page_count:
            raise ValueError(f"page selector outside 1..{page_count}: {part}")
        selected.update(range(start, end + 1))
    return sorted(selected)


def inspect_selected_pages(path: Path, pages: str) -> dict[str, int]:
    import fitz  # provided by the isolated pdf2zh-next environment

    document = fitz.open(path)
    try:
        selected = parse_selected_pages(pages, document.page_count)
        text_bytes = 0
        text_blocks = 0
        for page_number in selected:
            page = document.load_page(page_number - 1)
            for block in page.get_text("blocks"):
                text = str(block[4]).strip()
                if text:
                    text_blocks += 1
                    text_bytes += len(text.encode("utf-8"))
        return {
            "source_page_count": document.page_count,
            "selected_page_count": len(selected),
            "text_utf8_bytes": text_bytes,
            "text_blocks": text_blocks,
        }
    finally:
        document.close()


def project_maximum_cost(
    inspection: Mapping[str, int], *, request_cap: int, usd_cny_rate: float
) -> dict[str, Any]:
    # UTF-8 byte count is a safe token upper bound for the source.  The per-call
    # allowance covers repeated role/layout/glossary prompt material.
    prompt_upper = int(inspection["text_utf8_bytes"]) * 4 + int(request_cap) * 8192
    completion_upper = int(request_cap) * MAX_OUTPUT_TOKENS_PER_REQUEST
    maximum = cost_rmb(
        prompt_tokens=prompt_upper,
        cache_hit_prompt_tokens=0,
        completion_tokens=completion_upper,
        pricing=conservative_pricing(),
        usd_cny_rate=usd_cny_rate,
    )
    return {
        "method": "utf8_source_x4_plus_8192_prompt_tokens_per_call_and_hard_output_cap",
        "request_cap": int(request_cap),
        "max_output_tokens_per_request": MAX_OUTPUT_TOKENS_PER_REQUEST,
        "prompt_tokens_upper_bound": prompt_upper,
        "completion_tokens_upper_bound": completion_upper,
        "pricing": conservative_pricing(),
        "usd_cny_rate": float(usd_cny_rate),
        "max_cost_rmb": maximum,
    }


def read_project_commitment(control_dir: Path | None) -> float:
    if control_dir is None:
        return 0.0
    control_dir = Path(control_dir)
    ledger = control_dir / "budget_ledger.jsonl"
    if not ledger.is_file():
        return 0.0
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        ledger.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"malformed project budget ledger line {line_number}"
            ) from error
        if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
            raise RuntimeError(  # noqa: TRY004 - persistent ledger corruption
                f"invalid project budget ledger line {line_number}"
            )
        events.append(event)
    spent = 0.0
    active: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = event["kind"]
        if kind in {"reserve", "resume"}:
            active[str(event["reservation_id"])] = event
        elif kind in {
            "settle",
            "commit_estimate",
            "recover_orphan",
            "historical_baseline",
        }:
            spent += float(event.get("cost_rmb") or 0)
            if event.get("reservation_id"):
                active.pop(str(event["reservation_id"]), None)
    dead = {
        reservation_id: event
        for reservation_id, event in active.items()
        if not pid_is_alive(event.get("owner_pid"))
    }
    if dead:
        lock_path = control_dir / "budget.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_stream:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
            try:
                current_text = ledger.read_text(encoding="utf-8")
                current_events = [
                    json.loads(line)
                    for line in current_text.splitlines()
                    if line.strip()
                ]
                current_active: dict[str, dict[str, Any]] = {}
                for event in current_events:
                    reservation_id = str(event.get("reservation_id") or "")
                    if event["kind"] in {"reserve", "resume"}:
                        current_active[reservation_id] = event
                    elif event["kind"] in {
                        "settle",
                        "commit_estimate",
                        "recover_orphan",
                        "historical_baseline",
                    }:
                        current_active.pop(reservation_id, None)
                with ledger.open("a", encoding="utf-8") as stream:
                    for reservation_id, event in dead.items():
                        current = current_active.get(reservation_id)
                        if current is None or pid_is_alive(current.get("owner_pid")):
                            continue
                        stream.write(
                            json.dumps(
                                {
                                    "schema_version": 1,
                                    "event_id": uuid.uuid4().hex,
                                    "kind": "recover_orphan",
                                    "reservation_id": reservation_id,
                                    "run_id": current.get("run_id"),
                                    "owner_pid": current.get("owner_pid"),
                                    "cost_rmb": 0.0,
                                    "recovered_estimate_rmb": float(
                                        current.get("estimated_cost_rmb") or 0
                                    ),
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            + "\n"
                        )
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        for reservation_id in dead:
            active.pop(reservation_id, None)
    return spent + sum(
        float(event.get("estimated_cost_rmb") or 0) for event in active.values()
    )


def summarize_terminated_request_ledger(
    path: Path, *, expected_sha256: str
) -> dict[str, Any]:
    """Return a fail-closed conservative cost summary for one stopped API ledger."""
    path = Path(path)
    content = path.read_bytes()
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != str(expected_sha256).strip().lower():
        raise RuntimeError("request ledger hash mismatch")
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise RuntimeError("request ledger is not valid UTF-8") from error
    active: dict[int, dict[str, Any]] = {}
    seen_reservations: set[int] = set()
    settled_cost = 0.0
    uncertain_cost = 0.0
    max_call_index = 0
    event_count = 0
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        event_count += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"malformed request ledger line {line_number}"
            ) from error
        if not isinstance(event, dict):
            raise RuntimeError(f"invalid request ledger line {line_number}")
        kind = str(event.get("kind") or "")
        if kind not in {"reserve", "settle", "commit_uncertain", "recover_uncertain"}:
            raise RuntimeError(f"unsupported request ledger event {kind!r}")
        try:
            call_index = int(event.get("call_index") or 0)
        except (TypeError, ValueError) as error:
            raise RuntimeError("invalid request ledger call index") from error
        if call_index <= 0:
            raise RuntimeError("invalid request ledger call index")
        max_call_index = max(max_call_index, call_index)
        if kind == "reserve":
            if call_index in seen_reservations:
                raise RuntimeError("duplicate request ledger reservation")
            reserved = float(event.get("reserved_cost_rmb") or -1)
            if not math.isfinite(reserved) or reserved < 0:
                raise RuntimeError("invalid request ledger reservation cost")
            seen_reservations.add(call_index)
            active[call_index] = event
            continue
        reservation = active.pop(call_index, None)
        if reservation is None:
            raise RuntimeError("request ledger closes a missing reservation")
        cost = float(event.get("cost_rmb") or 0)
        reserved = float(reservation.get("reserved_cost_rmb") or 0)
        if not math.isfinite(cost) or cost < 0 or cost > reserved + 1e-12:
            raise RuntimeError("invalid request ledger closing cost")
        if kind == "settle":
            settled_cost += cost
        else:
            uncertain_cost += cost
    if not seen_reservations or seen_reservations != set(range(1, max_call_index + 1)):
        raise RuntimeError("request ledger call indices are not contiguous")
    for reservation in active.values():
        if pid_is_alive(reservation.get("owner_pid")):
            raise RuntimeError("request ledger still has a live owner")
    unresolved_cost = sum(
        float(event.get("reserved_cost_rmb") or 0) for event in active.values()
    )
    return {
        "request_ledger_path": path.name,
        "request_ledger_sha256": actual_sha256,
        "event_count": event_count,
        "request_count": max_call_index,
        "settled_cost_rmb": settled_cost,
        "uncertain_cost_rmb": uncertain_cost,
        "unresolved_request_count": len(active),
        "unresolved_cost_rmb": unresolved_cost,
        "conservative_cost_rmb": settled_cost + uncertain_cost + unresolved_cost,
    }


class SharedProjectReservation:
    """Reserve one stage maximum in the existing cross-paper project ledger."""

    def __init__(
        self,
        *,
        control_dir: Path,
        run_id: str,
        project_max_cost_rmb: float,
    ) -> None:
        if not str(run_id).strip():
            raise ValueError("shared project reservation run_id must be non-empty")
        self.control_dir = Path(control_dir)
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.control_dir / "budget_config.json"
        self.ledger_path = self.control_dir / "budget_ledger.jsonl"
        self.lock_path = self.control_dir / "budget.lock"
        self.run_id = str(run_id)
        self.project_max_cost_rmb = _finite_positive(
            project_max_cost_rmb,
            label="project",
            maximum=PROJECT_MAXIMUM_RMB,
        )
        self.reservation_id = uuid.uuid4().hex
        self.reserved_cost_rmb: float | None = None
        self.completed = False
        with self._locked():
            self._validate_or_create_config_locked()

    @contextmanager
    def _locked(self):
        with self.lock_path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _validate_or_create_config_locked(self) -> None:
        if self.config_path.is_file():
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            if config.get("schema_version") != 1:
                raise RuntimeError("unsupported shared project budget schema")
            configured = float(config.get("project_max_cost_rmb") or 0)
            if configured <= 0 or configured > PROJECT_MAXIMUM_RMB:
                raise RuntimeError("invalid shared project budget configuration")
            if self.project_max_cost_rmb > configured:
                raise ValueError(
                    "project budget cannot be raised above the existing cap"
                )
            return
        atomic_json(
            self.config_path,
            {
                "schema_version": 1,
                "project_max_cost_rmb": self.project_max_cost_rmb,
                "authorized_max_cost_rmb": PROJECT_MAXIMUM_RMB,
                "usd_cny_rate": USD_CNY_RATE,
                "ledger_initialized": True,
            },
        )

    def _events_locked(self) -> list[dict[str, Any]]:
        if not self.ledger_path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line_number, line in enumerate(
            self.ledger_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"malformed shared project budget ledger line {line_number}"
                ) from error
            if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
                raise RuntimeError(  # noqa: TRY004 - persistent ledger corruption
                    f"invalid shared project budget ledger line {line_number}"
                )
            events.append(event)
        return events

    @staticmethod
    def _state(
        events: list[dict[str, Any]],
    ) -> tuple[float, dict[str, dict[str, Any]]]:
        spent = 0.0
        active: dict[str, dict[str, Any]] = {}
        for event in events:
            kind = event["kind"]
            reservation_id = str(event.get("reservation_id") or "")
            if kind in {"reserve", "resume"}:
                active[reservation_id] = event
            elif kind in {
                "settle",
                "commit_estimate",
                "recover_orphan",
                "historical_baseline",
            }:
                spent += float(event.get("cost_rmb") or 0)
                if reservation_id:
                    active.pop(reservation_id, None)
        return spent, active

    def _append_locked(self, event: Mapping[str, Any]) -> None:
        with self.ledger_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def reserve(self, maximum_cost_rmb: float) -> None:
        if self.reserved_cost_rmb is not None:
            raise RuntimeError("shared project stage is already reserved")
        estimate = _finite_positive(
            maximum_cost_rmb,
            label="stage projection",
            maximum=STAGE_MAXIMUM_RMB,
        )
        with self._locked():
            spent, active = self._state(self._events_locked())
            same_run = [
                event
                for event in active.values()
                if str(event.get("run_id") or "") == self.run_id
            ]
            if len(same_run) > 1:
                raise RuntimeError(
                    "shared project ledger has duplicate active run holds"
                )
            if same_run:
                prior = same_run[0]
                if pid_is_alive(prior.get("owner_pid")):
                    raise RuntimeError("the same shared project run is already active")
                prior_estimate = float(prior.get("estimated_cost_rmb") or 0)
                if estimate > prior_estimate + 1e-12:
                    raise BudgetExceededError(
                        "restarted stage exceeds its existing shared reservation"
                    )
                self.reservation_id = str(prior["reservation_id"])
                self.reserved_cost_rmb = prior_estimate
                self._append_locked(
                    {
                        "schema_version": 1,
                        "event_id": uuid.uuid4().hex,
                        "kind": "resume",
                        "run_id": self.run_id,
                        "reservation_id": self.reservation_id,
                        "owner_pid": os.getpid(),
                        "estimated_cost_rmb": prior_estimate,
                        "uncertainty_key": f"pdf2zh-next:{self.run_id}",
                    }
                )
                return
            active_total = sum(
                float(event.get("estimated_cost_rmb") or 0) for event in active.values()
            )
            if spent + active_total + estimate > self.project_max_cost_rmb:
                raise BudgetExceededError(
                    "shared project budget cannot reserve this stage maximum"
                )
            self._append_locked(
                {
                    "schema_version": 1,
                    "event_id": uuid.uuid4().hex,
                    "kind": "reserve",
                    "run_id": self.run_id,
                    "reservation_id": self.reservation_id,
                    "owner_pid": os.getpid(),
                    "estimated_cost_rmb": estimate,
                    "uncertainty_key": f"pdf2zh-next:{self.run_id}",
                }
            )
        self.reserved_cost_rmb = estimate

    def settle(self, actual_cost_rmb: float) -> None:
        if self.reserved_cost_rmb is None or self.completed:
            raise RuntimeError("shared project stage has no active reservation")
        actual = float(actual_cost_rmb)
        if not math.isfinite(actual) or actual < 0:
            raise ValueError("actual project cost must be finite and non-negative")
        if actual > self.reserved_cost_rmb:
            raise BudgetExceededError(
                "actual stage cost exceeds its shared reservation"
            )
        with self._locked():
            _spent, active = self._state(self._events_locked())
            if self.reservation_id not in active:
                raise RuntimeError("shared project reservation is no longer active")
            self._append_locked(
                {
                    "schema_version": 1,
                    "event_id": uuid.uuid4().hex,
                    "kind": "settle",
                    "run_id": self.run_id,
                    "reservation_id": self.reservation_id,
                    "cost_rmb": actual,
                    "usage": {},
                    "uncertainty_key": f"pdf2zh-next:{self.run_id}",
                }
            )
        self.completed = True

    def reconcile_terminated(
        self,
        *,
        request_ledger_path: Path,
        expected_request_ledger_sha256: str,
    ) -> dict[str, Any]:
        """Settle a dead stage from a content-addressed per-request ledger."""
        request_ledger_path = Path(request_ledger_path)
        request_lock = request_ledger_path.with_suffix(
            request_ledger_path.suffix + ".lock"
        )
        request_lock.parent.mkdir(parents=True, exist_ok=True)
        with request_lock.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            summary = summarize_terminated_request_ledger(
                request_ledger_path,
                expected_sha256=expected_request_ledger_sha256,
            )
            actual = float(summary["conservative_cost_rmb"])
            with self._locked():
                _spent, active = self._state(self._events_locked())
                same_run = [
                    event
                    for event in active.values()
                    if str(event.get("run_id") or "") == self.run_id
                ]
                if len(same_run) != 1:
                    raise RuntimeError(
                        "terminated run must have exactly one active reservation"
                    )
                prior = same_run[0]
                if pid_is_alive(prior.get("owner_pid")):
                    raise RuntimeError("shared project reservation is still active")
                maximum = float(prior.get("estimated_cost_rmb") or 0)
                if actual > maximum + 1e-12:
                    raise BudgetExceededError(
                        "reconciled cost exceeds the shared project reservation"
                    )
                self.reservation_id = str(prior["reservation_id"])
                self.reserved_cost_rmb = maximum
                self._append_locked(
                    {
                        "schema_version": 1,
                        "event_id": uuid.uuid4().hex,
                        "kind": "settle",
                        "run_id": self.run_id,
                        "reservation_id": self.reservation_id,
                        "cost_rmb": actual,
                        "usage": {},
                        "uncertainty_key": f"pdf2zh-next:{self.run_id}",
                        "reconciliation": summary,
                    }
                )
        self.completed = True
        return summary


def commit_orphaned_request_ledger_cost(
    *,
    control_dir: Path,
    run_id: str,
    request_ledger_path: Path,
) -> dict[str, Any]:
    """Idempotently correct a zero-cost orphan recovery from request evidence."""

    request_ledger_path = Path(request_ledger_path)
    summary = summarize_terminated_request_ledger(
        request_ledger_path,
        expected_sha256=sha256_file(request_ledger_path),
    )
    config = json.loads(
        (Path(control_dir) / "budget_config.json").read_text(encoding="utf-8")
    )
    reservation = SharedProjectReservation(
        control_dir=Path(control_dir),
        run_id=run_id,
        project_max_cost_rmb=float(config["project_max_cost_rmb"]),
    )
    request_hash = str(summary["request_ledger_sha256"])
    actual = float(summary["conservative_cost_rmb"])
    with reservation._locked():
        events = reservation._events_locked()
        _spent, active = reservation._state(events)
        if any(str(event.get("run_id") or "") == run_id for event in active.values()):
            raise RuntimeError("orphaned run still has an active reservation")
        existing = [
            event
            for event in events
            if event.get("kind") == "commit_estimate"
            and str(event.get("run_id") or "") == run_id
            and (event.get("reconciliation") or {}).get("request_ledger_sha256")
            == request_hash
        ]
        if existing:
            return {**summary, "committed": False}
        settled = [
            event
            for event in events
            if event.get("kind") == "settle"
            and str(event.get("run_id") or "") == run_id
        ]
        if settled:
            settled_cost = sum(float(event.get("cost_rmb") or 0) for event in settled)
            if abs(settled_cost - actual) <= 1e-12:
                return {**summary, "committed": False}
            raise RuntimeError("orphaned run already has a conflicting settlement")
        recoveries = [
            event
            for event in events
            if event.get("kind") == "recover_orphan"
            and str(event.get("run_id") or "") == run_id
        ]
        if len(recoveries) != 1:
            raise RuntimeError("orphaned run requires exactly one recovery event")
        reservation._append_locked(
            {
                "schema_version": 1,
                "event_id": uuid.uuid4().hex,
                "kind": "commit_estimate",
                "run_id": run_id,
                "reservation_id": str(recoveries[0].get("reservation_id") or ""),
                "cost_rmb": actual,
                "uncertainty_key": f"pdf2zh-next:{run_id}:orphan-correction",
                "reconciliation": summary,
            }
        )
    return {**summary, "committed": True}


def runtime_versions() -> dict[str, str]:
    return {
        "pdf2zh_next": importlib.metadata.version("pdf2zh-next"),
        "babeldoc": importlib.metadata.version("babeldoc"),
        "python": ".".join(map(str, __import__("sys").version_info[:3])),
    }


def assert_runtime_versions() -> dict[str, str]:
    versions = runtime_versions()
    expected = {
        "pdf2zh_next": EXPECTED_PDF2ZH_NEXT_VERSION,
        "babeldoc": EXPECTED_BABELDOC_VERSION,
    }
    for name, version in expected.items():
        if versions.get(name) != version:
            raise RuntimeError(
                f"isolated runtime mismatch: {name}={versions.get(name)!r}, expected {version!r}"
            )
    return versions


def load_api_key() -> str:
    supplied = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if supplied:
        return supplied
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-a",
            "codex_0805",
            "-s",
            "codex.deepseek.api",
            "-w",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    key = result.stdout.strip()
    if result.returncode != 0 or not key:
        raise RuntimeError("DeepSeek Keychain credential is unavailable")
    return key


def check_deepseek_connectivity(
    api_key: str,
    *,
    requester: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Fail fast before reserving budget when the official API is unreachable."""
    if not api_key:
        raise ValueError("DeepSeek API key must be non-empty")
    if requester is None:
        import httpx

        requester = httpx.get
    sleeper = sleeper or time.sleep
    last_error: Exception | None = None
    attempts = len(DEEPSEEK_CONNECTIVITY_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(attempts):
        try:
            response = requester(
                "https://api.deepseek.com/models",
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                timeout=DEEPSEEK_CONNECTIVITY_TIMEOUT_SECONDS,
                trust_env=False,
            )
            status_code = int(getattr(response, "status_code", 0))
            if not 200 <= status_code < 300:
                raise DeepSeekConnectivityError(
                    f"DeepSeek connectivity check returned HTTP {status_code}"
                )
            return {
                "status": "passed",
                "endpoint": "https://api.deepseek.com/models",
                "status_code": status_code,
                "attempt": attempt + 1,
                "zero_paid": True,
            }
        except Exception as error:  # noqa: BLE001 - sanitize all transport errors
            last_error = error
            if attempt < attempts - 1:
                sleeper(DEEPSEEK_CONNECTIVITY_RETRY_DELAYS_SECONDS[attempt])
    if isinstance(last_error, DeepSeekConnectivityError):
        raise last_error
    raise RuntimeError("DeepSeek connectivity check failed before paid work") from last_error


def _usage_from_response(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    cached = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = max(cached, int(getattr(details, "cached_tokens", 0) or 0))
    return {
        "prompt_tokens": prompt,
        "cache_hit_prompt_tokens": min(prompt, cached),
        "completion_tokens": completion,
        "total_tokens": int(getattr(usage, "total_tokens", prompt + completion) or 0),
    }


def _usage_from_mapping(payload: Mapping[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise TypeError("DeepSeek response is missing token usage")
    prompt = max(0, int(usage.get("prompt_tokens") or 0))
    completion = max(0, int(usage.get("completion_tokens") or 0))
    cached = max(0, int(usage.get("prompt_cache_hit_tokens") or 0))
    details = usage.get("prompt_tokens_details")
    if isinstance(details, Mapping):
        cached = max(cached, int(details.get("cached_tokens") or 0))
    return {
        "prompt_tokens": prompt,
        "cache_hit_prompt_tokens": min(prompt, cached),
        "completion_tokens": completion,
        "total_tokens": max(0, int(usage.get("total_tokens") or prompt + completion)),
    }


@dataclass(frozen=True)
class RequestReservation:
    call_index: int
    max_output_tokens: int
    prompt_tokens_upper_bound: int
    reserved_cost_rmb: float
    pricing: Mapping[str, Any]


class RequestBudgetGate:
    """Thread-safe hard request/output/cost boundary around the OpenAI client."""

    def __init__(
        self,
        *,
        ledger_path: Path,
        stage_max_cost_rmb: float,
        project_max_cost_rmb: float,
        project_commitment_before_rmb: float,
        request_cap: int,
        usd_cny_rate: float,
    ) -> None:
        self.ledger_path = Path(ledger_path)
        self.ledger_lock_path = self.ledger_path.with_suffix(
            self.ledger_path.suffix + ".lock"
        )
        self.stage_max_cost_rmb = stage_max_cost_rmb
        self.project_max_cost_rmb = project_max_cost_rmb
        self.project_commitment_before_rmb = project_commitment_before_rmb
        self.request_cap = request_cap
        self.usd_cny_rate = usd_cny_rate
        self.lock = threading.Lock()
        self.calls = 0
        self.actual_cost_rmb = 0.0
        self.uncertain_cost_rmb = 0.0
        self.reserved_cost_rmb = 0.0
        self.usage = {
            "prompt_tokens": 0,
            "cache_hit_prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self._restore_existing_ledger()

    @contextmanager
    def _ledger_file_lock(self):
        self.ledger_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_lock_path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _append_locked(self, event: Mapping[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _read_events_locked(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if not self.ledger_path.is_file():
            return events
        for line_number, line in enumerate(
            self.ledger_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"malformed A/B API ledger line {line_number}"
                ) from error
            if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
                raise RuntimeError(  # noqa: TRY004 - persistent ledger corruption
                    f"invalid A/B API ledger line {line_number}"
                )
            events.append(event)
        return events

    def _state_from_events(
        self, events: list[dict[str, Any]]
    ) -> tuple[int, float, float, dict[str, int], dict[int, dict[str, Any]]]:
        calls = 0
        actual = 0.0
        uncertain = 0.0
        usage_totals = {key: 0 for key in self.usage}
        active: dict[int, dict[str, Any]] = {}
        for event in events:
            kind = event["kind"]
            call_index = int(event.get("call_index") or 0)
            if call_index > 0:
                calls = max(calls, call_index)
            if kind == "reserve":
                if call_index <= 0:
                    raise RuntimeError("invalid reservation in A/B API ledger")
                active[call_index] = event
            elif kind == "settle":
                active.pop(call_index, None)
                actual += float(event.get("cost_rmb") or 0)
                event_usage = event.get("usage") or {}
                for key in usage_totals:
                    usage_totals[key] += max(0, int(event_usage.get(key) or 0))
            elif kind in {"commit_uncertain", "recover_uncertain"}:
                active.pop(call_index, None)
                uncertain += float(event.get("cost_rmb") or 0)
        return calls, actual, uncertain, usage_totals, active

    def _sync_locked(self, *, recover_dead: bool) -> dict[int, dict[str, Any]]:
        events = self._read_events_locked()
        calls, actual, uncertain, usage, active = self._state_from_events(events)
        if recover_dead:
            recovered = False
            for call_index, reservation in list(active.items()):
                if pid_is_alive(reservation.get("owner_pid")):
                    continue
                cost = float(reservation.get("reserved_cost_rmb") or 0)
                self._append_locked(
                    {
                        "kind": "recover_uncertain",
                        "call_index": call_index,
                        "created_at": iso_utc(),
                        "cost_rmb": cost,
                        "reason": "unsettled_reservation_from_prior_process",
                    }
                )
                recovered = True
            if recovered:
                calls, actual, uncertain, usage, active = self._state_from_events(
                    self._read_events_locked()
                )
        self.calls = calls
        self.actual_cost_rmb = actual
        self.uncertain_cost_rmb = uncertain
        self.usage = usage
        self.reserved_cost_rmb = sum(
            float(event.get("reserved_cost_rmb") or 0) for event in active.values()
        )
        return active

    def _restore_existing_ledger(self) -> None:
        with self._ledger_file_lock():
            self._sync_locked(recover_dead=True)

    def reserve_request(
        self, *, messages: Any, max_output_tokens: Any = None
    ) -> RequestReservation:
        prompt_upper = (
            len(
                json.dumps(messages or [], ensure_ascii=False, sort_keys=True).encode(
                    "utf-8"
                )
            )
            + 4096
        )
        output_cap = min(
            max(1, int(max_output_tokens or MAX_OUTPUT_TOKENS_PER_REQUEST)),
            MAX_OUTPUT_TOKENS_PER_REQUEST,
        )
        call_time = utc_now()
        pricing = pricing_for_utc(call_time)
        reserved_cost = cost_rmb(
            prompt_tokens=prompt_upper,
            cache_hit_prompt_tokens=0,
            completion_tokens=output_cap,
            pricing=pricing,
            usd_cny_rate=self.usd_cny_rate,
        )
        with self.lock, self._ledger_file_lock():
            self._sync_locked(recover_dead=True)
            if self.calls >= self.request_cap:
                raise RequestCapExceededError(
                    f"DeepSeek request cap exhausted ({self.request_cap})"
                )
            stage_committed = (
                self.actual_cost_rmb + self.uncertain_cost_rmb + self.reserved_cost_rmb
            )
            if stage_committed + reserved_cost > self.stage_max_cost_rmb:
                raise BudgetExceededError("next DeepSeek request exceeds stage budget")
            if (
                self.project_commitment_before_rmb + stage_committed + reserved_cost
                > self.project_max_cost_rmb
            ):
                raise BudgetExceededError(
                    "next DeepSeek request exceeds project budget"
                )
            reservation = RequestReservation(
                call_index=self.calls + 1,
                max_output_tokens=output_cap,
                prompt_tokens_upper_bound=prompt_upper,
                reserved_cost_rmb=reserved_cost,
                pricing=pricing,
            )
            self._append_locked(
                {
                    "kind": "reserve",
                    "call_index": reservation.call_index,
                    "owner_pid": os.getpid(),
                    "created_at": iso_utc(call_time),
                    "max_output_tokens": output_cap,
                    "prompt_tokens_upper_bound": prompt_upper,
                    "reserved_cost_rmb": reserved_cost,
                    "pricing_schedule": pricing["schedule"],
                }
            )
            self._sync_locked(recover_dead=False)
        return reservation

    def settle_request(
        self, reservation: RequestReservation, usage: Mapping[str, Any]
    ) -> None:
        normalized_usage = {key: max(0, int(usage.get(key) or 0)) for key in self.usage}
        normalized_usage["cache_hit_prompt_tokens"] = min(
            normalized_usage["prompt_tokens"],
            normalized_usage["cache_hit_prompt_tokens"],
        )
        actual = cost_rmb(
            prompt_tokens=normalized_usage["prompt_tokens"],
            cache_hit_prompt_tokens=normalized_usage["cache_hit_prompt_tokens"],
            completion_tokens=normalized_usage["completion_tokens"],
            pricing=reservation.pricing,
            usd_cny_rate=self.usd_cny_rate,
        )
        with self.lock, self._ledger_file_lock():
            active = self._sync_locked(recover_dead=True)
            if reservation.call_index not in active:
                raise RuntimeError("A/B API reservation is no longer active")
            self._append_locked(
                {
                    "kind": "settle",
                    "call_index": reservation.call_index,
                    "created_at": iso_utc(),
                    "usage": normalized_usage,
                    "cost_rmb": actual,
                    "pricing_schedule": reservation.pricing["schedule"],
                }
            )
            self._sync_locked(recover_dead=False)

    def commit_uncertain(
        self, reservation: RequestReservation, *, error_type: str
    ) -> None:
        with self.lock, self._ledger_file_lock():
            active = self._sync_locked(recover_dead=True)
            if reservation.call_index not in active:
                raise RuntimeError("A/B API reservation is no longer active")
            self._append_locked(
                {
                    "kind": "commit_uncertain",
                    "call_index": reservation.call_index,
                    "created_at": iso_utc(),
                    "cost_rmb": reservation.reserved_cost_rmb,
                    "error_type": str(error_type),
                }
            )
            self._sync_locked(recover_dead=False)

    def wrap_create(self, create: Callable[..., Any]) -> Callable[..., Any]:
        def guarded_create(*args: Any, **kwargs: Any) -> Any:
            reservation = self.reserve_request(
                messages=kwargs.get("messages") or [],
                max_output_tokens=kwargs.get("max_tokens"),
            )
            kwargs["max_tokens"] = reservation.max_output_tokens
            try:
                response = create(*args, **kwargs)
            except Exception as error:
                self.commit_uncertain(reservation, error_type=type(error).__name__)
                raise
            self.settle_request(reservation, _usage_from_response(response))
            return response

        return guarded_create

    def snapshot(self) -> dict[str, Any]:
        with self.lock, self._ledger_file_lock():
            self._sync_locked(recover_dead=True)
            reserved = max(0.0, self.reserved_cost_rmb)
            return {
                "api_calls": self.calls,
                "request_cap": self.request_cap,
                "usage": dict(self.usage),
                "actual_cost_rmb": self.actual_cost_rmb,
                "uncertain_cost_rmb": self.uncertain_cost_rmb,
                "reserved_cost_rmb": reserved,
                "stage_committed_cost_rmb": self.actual_cost_rmb
                + self.uncertain_cost_rmb
                + reserved,
            }


class DeepSeekForwarder(Protocol):
    def __call__(
        self, body: bytes, api_key: str
    ) -> tuple[int, Mapping[str, str], bytes]: ...


class DeepSeekBudgetProxy:
    """Local process boundary for DeepSeek request, token, and RMB limits."""

    def __init__(
        self,
        *,
        api_key: str,
        gate: RequestBudgetGate,
        forwarder: DeepSeekForwarder | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DeepSeek API key must be non-empty")
        self._api_key = api_key
        self._gate = gate
        self._forwarder = forwarder or self._forward_to_deepseek
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._metrics_lock = threading.Lock()
        self._locked_marker_count = 0
        self._locked_numeric_citation_count = 0
        self._locked_rich_placeholder_count = 0
        self._validated_response_count = 0
        self._failure_count = 0

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("DeepSeek budget proxy is not running")
        port = int(self._server.server_address[1])
        return f"http://127.0.0.1:{port}/v1"

    def snapshot(self) -> dict[str, int]:
        with self._metrics_lock:
            return {
                "locked_marker_count": self._locked_marker_count,
                "locked_numeric_citation_count": self._locked_numeric_citation_count,
                "locked_rich_placeholder_count": self._locked_rich_placeholder_count,
                "locked_citation_placeholder_count": self._locked_rich_placeholder_count,
                "validated_response_count": self._validated_response_count,
                "failure_count": self._failure_count,
            }

    @staticmethod
    def _forward_to_deepseek(
        body: bytes, api_key: str
    ) -> tuple[int, Mapping[str, str], bytes]:
        import httpx

        response = httpx.post(
            "https://api.deepseek.com/v1/chat/completions",
            content=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=180,
            trust_env=False,
        )
        return (
            int(response.status_code),
            {
                "Content-Type": response.headers.get(
                    "Content-Type", "application/json"
                ),
                "X-Request-Id": response.headers.get("X-Request-Id", ""),
            },
            response.content,
        )

    @staticmethod
    def _write_response(
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes,
        *,
        content_type: str = "application/json",
        request_id: str = "",
    ) -> None:
        handler.send_response(int(status))
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        if request_id:
            handler.send_header("X-Request-Id", request_id)
        handler.end_headers()
        handler.wfile.write(body)

    @classmethod
    def _write_error(
        cls, handler: BaseHTTPRequestHandler, status: int, message: str
    ) -> None:
        body = json.dumps(
            {"error": {"message": message, "type": "local_budget_proxy_error"}},
            ensure_ascii=False,
        ).encode("utf-8")
        cls._write_response(handler, status, body)

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path.rstrip("/") != "/v1/chat/completions":
            self._write_error(handler, 404, "unsupported local proxy path")
            return
        try:
            content_length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            self._write_error(handler, 400, "invalid Content-Length")
            return
        if content_length <= 0 or content_length > 16 * 1024 * 1024:
            self._write_error(handler, 413, "invalid request body size")
            return
        try:
            payload = json.loads(handler.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_error(handler, 400, "request body must be JSON")
            return
        if not isinstance(payload, dict) or not isinstance(
            payload.get("messages"), list
        ):
            self._write_error(handler, 400, "request must contain a messages list")
            return
        try:
            locked_messages, citation_lock = lock_numeric_citations(
                payload["messages"]
            )
        except CitationLockError as error:
            self._write_error(handler, 400, str(error))
            return
        payload["messages"] = locked_messages
        with self._metrics_lock:
            self._locked_marker_count += citation_lock.count
            self._locked_numeric_citation_count += (
                citation_lock.numeric_citation_count
            )
            self._locked_rich_placeholder_count += (
                citation_lock.rich_placeholder_count
            )
        payload["model"] = MODEL
        payload["stream"] = False
        payload["thinking"] = {"type": "disabled"}
        payload.pop("reasoning_effort", None)
        try:
            reservation = self._gate.reserve_request(
                messages=payload["messages"],
                max_output_tokens=payload.get("max_tokens"),
            )
        except RequestCapExceededError as error:
            # pdf2zh-next retries HTTP 429 indefinitely.  A local hard cap is
            # terminal for this paper, so expose it as a non-retryable client
            # error; the controller will quarantine the paper and release the
            # reservation instead of holding a worker forever.
            self._write_error(handler, 400, str(error))
            return
        except BudgetExceededError as error:
            self._write_error(handler, 402, str(error))
            return
        payload["max_tokens"] = reservation.max_output_tokens
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        try:
            status, headers, response_body = self._forwarder(body, self._api_key)
        except Exception as error:  # noqa: BLE001 - arbitrary transport failures count
            self._gate.commit_uncertain(reservation, error_type=type(error).__name__)
            self._write_error(handler, 502, "DeepSeek transport failed")
            return
        if 200 <= int(status) < 300:
            try:
                response_payload = json.loads(response_body)
                if not isinstance(response_payload, Mapping):
                    raise TypeError("DeepSeek response must be an object")
                usage = _usage_from_mapping(response_payload)
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as error:
                self._gate.commit_uncertain(
                    reservation, error_type=type(error).__name__
                )
            else:
                try:
                    response_body = unlock_numeric_citations_response(
                        response_body, citation_lock
                    )
                except CitationLockError as error:
                    self._gate.settle_request(reservation, usage)
                    with self._metrics_lock:
                        self._failure_count += 1
                    self._write_error(handler, 400, str(error))
                    return
                if citation_lock.count:
                    with self._metrics_lock:
                        self._validated_response_count += 1
                self._gate.settle_request(reservation, usage)
        else:
            self._gate.commit_uncertain(
                reservation, error_type=f"DeepSeekHTTP{int(status)}"
            )
        self._write_response(
            handler,
            int(status),
            response_body,
            content_type=str(headers.get("Content-Type") or "application/json"),
            request_id=str(headers.get("X-Request-Id") or ""),
        )

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("DeepSeek budget proxy is already running")
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                proxy._handle_post(self)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="deepseek-budget-proxy",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._server = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()


def _summarize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"type": str(event.get("type") or "unknown")}
    for key in ("stage", "progress", "error", "error_type"):
        value = event.get(key)
        if isinstance(value, (str, int, float, bool)):
            result[key] = value
    return result


def _result_paths(result: Any) -> dict[str, str | None]:
    return {
        "original_pdf": str(getattr(result, "original_pdf_path", "") or "") or None,
        "mono_pdf": str(getattr(result, "mono_pdf_path", "") or "") or None,
        "dual_pdf": str(getattr(result, "dual_pdf_path", "") or "") or None,
    }


def _build_official_settings(spec: Mapping[str, Any], proxy_base_url: str) -> Any:
    from pdf2zh_next.config.model import (
        BasicSettings,
        PDFSettings,
        SettingsModel,
        TranslationSettings,
    )
    from pdf2zh_next.config.translate_engine_model import DeepSeekSettings

    engine = DeepSeekSettings(
        deepseek_model=MODEL,
        deepseek_api_key="local-budget-proxy",
        deepseek_thinking_mode="disabled",
    ).transform()
    engine.openai_base_url = proxy_base_url

    return SettingsModel(
        basic=BasicSettings(**spec["basic"]),
        translation=TranslationSettings(**spec["translation"]),
        pdf=PDFSettings(**spec["pdf"]),
        translate_engine_settings=engine,
        term_extraction_engine_settings=None,
    )


@contextmanager
def localhost_proxy_bypass_environment():
    """Isolate local and upstream HTTP clients from malformed proxy settings."""
    known_proxy_names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    proxy_names = tuple(
        sorted(
            {
                *known_proxy_names,
                *(
                    name
                    for name in os.environ
                    if "proxy" in name.casefold()
                    and name.casefold() not in {"no_proxy"}
                ),
            }
        )
    )
    previous = {
        name: os.environ.get(name)
        for name in (*proxy_names, "NO_PROXY", "no_proxy")
    }
    bypass_hosts: list[str] = []
    for value in previous.values():
        if value:
            bypass_hosts.extend(
                part.strip() for part in value.split(",") if part.strip()
            )
    for host in ("127.0.0.1", "localhost"):
        if host not in bypass_hosts:
            bypass_hosts.append(host)
    bypass = "*"
    os.environ["NO_PROXY"] = bypass
    os.environ["no_proxy"] = bypass
    for name in proxy_names:
        os.environ.pop(name, None)
    try:
        yield [*proxy_names, "NO_PROXY", "no_proxy"]
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def httpx_disable_environment_proxy():
    """Force pdf2zh-next's internally-created clients to ignore proxy env vars."""
    import httpx

    original_client = httpx.Client
    original_async_client = httpx.AsyncClient

    class EnvironmentIndependentClient(original_client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["trust_env"] = False
            # pdf2zh-next constructs its OpenAI client with a 600-second
            # timeout. Clamp that library default so a stalled upstream call
            # cannot hold a paper worker for ten minutes before retrying.
            timeout = kwargs.get("timeout")
            if timeout is None or timeout > UPSTREAM_REQUEST_TIMEOUT_SECONDS:
                kwargs["timeout"] = UPSTREAM_REQUEST_TIMEOUT_SECONDS
            super().__init__(*args, **kwargs)

    class EnvironmentIndependentAsyncClient(original_async_client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["trust_env"] = False
            timeout = kwargs.get("timeout")
            if timeout is None or timeout > UPSTREAM_REQUEST_TIMEOUT_SECONDS:
                kwargs["timeout"] = UPSTREAM_REQUEST_TIMEOUT_SECONDS
            super().__init__(*args, **kwargs)

    httpx.Client = EnvironmentIndependentClient
    httpx.AsyncClient = EnvironmentIndependentAsyncClient
    try:
        yield
    finally:
        httpx.Client = original_client
        httpx.AsyncClient = original_async_client


def run_official_translation(
    *,
    source_pdf: Path,
    settings_spec: Mapping[str, Any],
    api_key: str,
    gate: RequestBudgetGate,
) -> dict[str, Any]:
    def force_cpu_doclayout_provider() -> None:
        """Avoid the macOS CoreML ONNX path, which can leave BabelDOC hung."""
        if sys.platform != "darwin":
            return
        try:
            import onnxruntime
        except ImportError:
            return
        providers = tuple(onnxruntime.get_available_providers())
        if any(name.lower().startswith("coreml") for name in providers):
            onnxruntime.get_available_providers = (  # type: ignore[method-assign]
                lambda: ["CPUExecutionProvider"]
            )

    async def run(
        proxy_base_url: str, bypass_environment_names: list[str]
    ) -> dict[str, Any]:
        force_cpu_doclayout_provider()
        # Keep the pinned ONNX/BLAS layout path bounded on macOS.  The
        # default thread pools can spin indefinitely during BabelDOC teardown
        # on small PDFs, preventing the finish receipt from being written.
        for name in (
            "OMP_NUM_THREADS",
            "ORT_INTRA_OP_NUM_THREADS",
            "ORT_INTER_OP_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        ):
            os.environ.setdefault(name, "1")
        from pdf2zh_next import high_level

        install_babeldoc_citation_placeholder_patch()

        # BabelDOC's debug mode is also used by the stable direct execution
        # path, but its debug-information middleware writes colored layout
        # rectangles and labels into the PDF.  Suppress only those injectors;
        # retain the direct path and its watchdog behavior.
        from babeldoc.format.pdf.document_il.midend.add_debug_information import (
            AddDebugInformation,
        )
        from babeldoc.format.pdf.document_il.midend.detect_scanned_file import (
            DetectScannedFile,
        )
        from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser
        from babeldoc.format.pdf.document_il.midend.paragraph_finder import (
            ParagraphFinder,
        )
        from babeldoc.format.pdf.document_il.midend.table_parser import TableParser

        for debug_class, method_name in (
            (AddDebugInformation, "process"),
            (DetectScannedFile, "_save_debug_box_to_page"),
            (LayoutParser, "_save_debug_box_to_page"),
            (ParagraphFinder, "add_debug_info"),
            (TableParser, "_save_debug_box_to_page"),
        ):
            setattr(debug_class, method_name, lambda self, *args, **kwargs: None)

        settings = _build_official_settings(settings_spec, proxy_base_url)
        finish_event: Mapping[str, Any] | None = None
        events: list[dict[str, Any]] = []
        async for event in high_level.do_translate_async_stream(settings, source_pdf):
            summary = _summarize_event(event)
            events.append(summary)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            if event.get("type") == "finish":
                finish_event = event
                break
        if finish_event is None:
            raise RuntimeError("pdf2zh-next ended without a finish event")
        return {
            "events": events,
            "result_paths": _result_paths(finish_event.get("translate_result")),
            "localhost_bypass_environment_names": bypass_environment_names,
            "request_transport": "localhost_hard_budget_proxy",
        }

    with (
        localhost_proxy_bypass_environment() as bypass_environment_names,
        httpx_disable_environment_proxy(),
        DeepSeekBudgetProxy(api_key=api_key, gate=gate) as proxy,
    ):
        result = asyncio.run(run(proxy.base_url, bypass_environment_names))
        result["citation_lock"] = proxy.snapshot()
        return result


def execute(
    config: RunConfig,
    *,
    preflight_only: bool,
    inspector: Callable[[Path, str], Mapping[str, int]] = inspect_selected_pages,
) -> dict[str, Any]:
    project_budget, stage_budget = validate_budgets(
        config.project_max_cost_rmb, config.stage_max_cost_rmb
    )
    request_cap = validate_request_cap(config.stage_max_api_calls)
    if not config.source_pdf.is_file() or config.source_pdf.suffix.lower() != ".pdf":
        raise FileNotFoundError(f"source PDF is unavailable: {config.source_pdf}")
    glossary_sources = tuple(
        source
        for source in (config.glossary_json, config.supplemental_glossary_json)
        if source is not None
    )
    for glossary_source in glossary_sources:
        if not glossary_source.is_file():
            raise FileNotFoundError(
                f"locked glossary is unavailable: {glossary_source}"
            )
    record = require_publication_allowed(config.rights_manifest, config.record_id)
    source_identity = require_source_identity(
        config.source_manifest, config.record_id, config.source_pdf
    )
    config.output_root.mkdir(parents=True, exist_ok=True)
    glossary_csv = config.output_root / "locked-glossary.csv"
    glossary_sha256 = materialize_glossary_csv(glossary_sources, glossary_csv)
    settings_spec = build_safe_settings_spec(
        output_dir=config.output_root / "rendered",
        glossary_csv=glossary_csv,
        pages=config.pages,
        qps=config.qps,
        pool_max_workers=config.pool_max_workers,
    )
    inspection = dict(inspector(config.source_pdf, config.pages))
    projected_request_cap = config.projected_request_cap or request_cap
    projected_request_cap = validate_request_cap(projected_request_cap)
    if projected_request_cap > request_cap:
        raise ValueError("projected request cap cannot exceed runtime request cap")
    projection = project_maximum_cost(
        inspection, request_cap=projected_request_cap, usd_cny_rate=config.usd_cny_rate
    )
    projection["runtime_request_cap"] = request_cap
    projection["projected_request_cap"] = projected_request_cap
    project_commitment = read_project_commitment(config.project_control_dir)
    if projection["max_cost_rmb"] > stage_budget:
        raise BudgetExceededError(
            f"preflight maximum ¥{projection['max_cost_rmb']:.4f} exceeds stage cap ¥{stage_budget:.2f}"
        )
    if project_commitment + projection["max_cost_rmb"] > project_budget:
        raise BudgetExceededError("preflight maximum exceeds remaining project budget")
    preflight = {
        "schema_version": 1,
        "status": "preflight_passed",
        "created_at": iso_utc(),
        "record_id": normalize_record_id(config.record_id),
        "rights": {
            "publication_allowed": record.get("publication_allowed"),
            "publication_basis": record.get("publication_basis"),
            "manifest_sha256": sha256_file(config.rights_manifest),
        },
        "source_identity": {
            "manifest_sha256": sha256_file(config.source_manifest),
            "pdf_sha256": source_identity["pdf_sha256"],
            "pdf_bytes": source_identity["pdf_bytes"],
            "pdf_status": source_identity["pdf_status"],
        },
        "source": {
            "path": config.source_pdf.name,
            "sha256": sha256_file(config.source_pdf),
            **inspection,
        },
        "glossary": {
            "source_sha256": sha256_file(config.glossary_json),
            "source_sha256s": [sha256_file(path) for path in glossary_sources],
            "csv_sha256": glossary_sha256,
        },
        "settings": settings_spec,
        "budget": {
            "project_max_cost_rmb": project_budget,
            "stage_max_cost_rmb": stage_budget,
            "stage_max_api_calls": request_cap,
            "project_commitment_before_rmb": project_commitment,
        },
        "projection": projection,
        "zero_paid_preflight": True,
    }
    atomic_json(config.output_root / "preflight.json", preflight)
    if preflight_only:
        return preflight

    api_key = load_api_key()
    connectivity = check_deepseek_connectivity(api_key)
    if config.project_control_dir is None:
        raise ValueError("paid A/B requires a shared project control directory")
    project_reservation = SharedProjectReservation(
        control_dir=config.project_control_dir,
        run_id=shared_project_run_id(
            config.record_id,
            str(preflight["source"]["sha256"]),
            config.output_root,
        ),
        project_max_cost_rmb=project_budget,
    )
    project_reservation.reserve(float(projection["max_cost_rmb"]))
    gate = RequestBudgetGate(
        ledger_path=config.output_root / "api-cost-ledger.jsonl",
        stage_max_cost_rmb=stage_budget,
        project_max_cost_rmb=project_budget,
        project_commitment_before_rmb=project_commitment,
        request_cap=request_cap,
        usd_cny_rate=config.usd_cny_rate,
    )
    running = {
        **preflight,
        "status": "running",
        "started_at": iso_utc(),
        "connectivity": connectivity,
    }
    atomic_json(config.output_root / "status.json", running)
    try:
        result = run_official_translation(
            source_pdf=config.source_pdf,
            settings_spec=settings_spec,
            api_key=api_key,
            gate=gate,
        )
        paths = result["result_paths"]
        output_hashes: dict[str, dict[str, Any]] = {}
        for label in ("mono_pdf", "dual_pdf"):
            raw_path = paths.get(label)
            if not raw_path:
                raise RuntimeError(f"pdf2zh-next did not produce {label}")
            path = Path(raw_path)
            if not path.is_file():
                raise RuntimeError(f"pdf2zh-next output is missing: {path}")
            output_hashes[label] = {
                "path": path.name,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        finish = {
            **preflight,
            "status": "translated_pending_qc",
            "finished_at": iso_utc(),
            "budget_actual": gate.snapshot(),
            "outputs": output_hashes,
            "engine_events": result["events"],
            "localhost_bypass_environment_names": result[
                "localhost_bypass_environment_names"
            ],
            "request_transport": result["request_transport"],
            "citation_lock": result["citation_lock"],
        }
        settled_project_cost = float(
            finish["budget_actual"]["stage_committed_cost_rmb"]
        )
        project_reservation.settle(settled_project_cost)
        finish["shared_project_reservation"] = {
            "run_id": project_reservation.run_id,
            "reservation_id": project_reservation.reservation_id,
            "maximum_cost_rmb": project_reservation.reserved_cost_rmb,
            "settled_cost_rmb": settled_project_cost,
        }
        safe_finish = sanitize_for_receipt(finish, secrets=[api_key])
        atomic_json(config.output_root / "finish.json", safe_finish)
        atomic_json(config.output_root / "status.json", safe_finish)
        return safe_finish
    except Exception as error:
        gate_snapshot = gate.snapshot()
        if not project_reservation.completed:
            project_reservation.settle(float(gate_snapshot["stage_committed_cost_rmb"]))
        failed = {
            **preflight,
            "status": "failed",
            "failed_at": iso_utc(),
            "error_type": type(error).__name__,
            "error": str(error),
            "budget_actual": gate_snapshot,
        }
        atomic_json(
            config.output_root / "status.json",
            sanitize_for_receipt(failed, secrets=[api_key]),
        )
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-id", default=DEFAULT_RECORD_ID)
    parser.add_argument(
        "--source-pdf",
        type=Path,
        default=ROOT / "tmp/pdfs/snowmass2021/arxiv_2203.07506.pdf",
    )
    parser.add_argument(
        "--rights-manifest", type=Path, default=ROOT / "site/data/papers.json"
    )
    parser.add_argument(
        "--source-manifest",
        type=Path,
        default=ROOT / "output/snowmass2021_sources/manifest.json",
    )
    parser.add_argument(
        "--glossary-json",
        type=Path,
        default=ROOT / "translations/snowmass-global-glossary.json",
    )
    parser.add_argument("--supplemental-glossary-json", type=Path)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "output/snowmass2021/pdf2zh_next_ab/papers/arxiv_2203.07506",
    )
    parser.add_argument("--pages", default=DEFAULT_PAGES)
    parser.add_argument("--project-max-cost-rmb", type=float, default=100.0)
    parser.add_argument("--stage-max-cost-rmb", type=float, default=10.0)
    parser.add_argument("--stage-max-api-calls", type=int, default=125)
    parser.add_argument("--projected-request-cap", type=int)
    parser.add_argument("--qps", type=int, default=2)
    parser.add_argument("--pool-max-workers", type=int, default=2)
    parser.add_argument(
        "--project-control-dir",
        type=Path,
        default=ROOT / "output/snowmass2021/production_control",
    )
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    versions = assert_runtime_versions()
    config = RunConfig(
        record_id=args.record_id,
        source_pdf=args.source_pdf,
        rights_manifest=args.rights_manifest,
        source_manifest=args.source_manifest,
        glossary_json=args.glossary_json,
        output_root=args.output_root,
        pages=args.pages,
        project_max_cost_rmb=args.project_max_cost_rmb,
        stage_max_cost_rmb=args.stage_max_cost_rmb,
        stage_max_api_calls=args.stage_max_api_calls,
        projected_request_cap=args.projected_request_cap,
        qps=args.qps,
        pool_max_workers=args.pool_max_workers,
        project_control_dir=args.project_control_dir,
        supplemental_glossary_json=args.supplemental_glossary_json,
    )
    receipt = execute(config, preflight_only=args.preflight_only)
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "output_root": str(config.output_root),
                "runtime": versions,
                "projection_max_cost_rmb": receipt["projection"]["max_cost_rmb"],
                "budget_actual": receipt.get("budget_actual"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
