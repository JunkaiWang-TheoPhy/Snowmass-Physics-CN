#!/usr/bin/env python3
"""Rights-gated, budgeted and resumable Snowmass multi-paper production."""

from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import difflib
import hashlib
import json
import math
import os
import fcntl
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import package_snowmass_translation_pdf as packager
import prepare_snowmass_babeldoc as prepare
import refill_snowmass_babeldoc as refill
import run_snowmass_refined_translation as refined
import run_snowmass_translation as runner
import snowmass_constraint_compiler as constraint_compiler
import snowmass_publication_qc as publication_qc
import snowmass_qc_contract as qc_contract
import snowmass_production_contract as production_contract
import audit_snowmass_translation_pdf as pdf_audit
from snowmass_batch_budget import (
    AUTHORIZED_PROJECT_MAX_RMB,
    PersistentBudgetGuard,
    RequestLimitExceededError,
    validate_budget,
    validate_request_limit,
)


DEFAULT_RIGHTS_MANIFEST = ROOT / "site/data/papers.json"
DEFAULT_PDF_ROOT = ROOT / "tmp/pdfs/snowmass2021"
DEFAULT_OUTPUT_ROOT = ROOT / "output/snowmass2021/babeldoc_production"
DEFAULT_CONTROL_DIR = ROOT / "output/snowmass2021/production_control"
DEFAULT_HISTORICAL_ROOTS = (ROOT / "output/snowmass2021/babeldoc_ab_v1",)
STAGE_LIMITS = {"shadow": 1, "pilot5": 5, "pilot10": 10, "pilot25": 25, "batch50": 50}
TERMINAL_STAGES = (
    "prepared",
    "translated",
    "revision_ready",
    "rendered",
    "qc_passed",
    "packaged",
)


def _production_environment_lock() -> dict[str, Any]:
    contract_files = (
        Path(__file__),
        Path(runner.__file__),
        Path(refined.__file__),
        Path(refill.__file__),
        Path(prepare.BRIDGE.__file__),
        Path(packager.__file__),
        Path(qc_contract.__file__),
        Path(production_contract.__file__),
        Path(pdf_audit.__file__),
        Path(publication_qc.__file__),
    )
    return production_contract.build_environment_lock(
        root=ROOT,
        babeldoc_version=prepare.BRIDGE.BABELDOC_VERSION,
        ir_version=str(prepare.BRIDGE.IR_PIPELINE_VERSION),
        model=runner.MODEL,
        provider="deepseek",
        pricing_contract={
            "currency": "USD",
            "input_cache_hit": runner.INPUT_CACHE_HIT_USD_PER_MILLION,
            "input_cache_miss": runner.INPUT_CACHE_MISS_USD_PER_MILLION,
            "output": runner.OUTPUT_USD_PER_MILLION,
        },
        contract_versions={
            "translation_qc": runner.QC_CONTRACT_VERSION,
            "refill": refill.REFILL_SCHEMA_VERSION,
            "packaging": packager.PACKAGING_CONTRACT_VERSION,
            "qc_receipt": qc_contract.SCHEMA_VERSION,
            "source_sha256": {path.name: _sha256(path) for path in contract_files},
        },
        font_paths=[packager.SYSTEM_CJK_FONT],
        cover_asset_paths=[packager.DEFAULT_MOUNTAIN_SVG_PATH, packager.DEFAULT_QR_IMAGE_PATH],
    )


def _artifact_manifest_path(article_dir: Path) -> Path:
    return article_dir / "production_artifacts.json"


def _ensure_artifact_contract(config: BatchConfig, record: dict[str, Any], article_dir: Path) -> dict[str, Any]:
    environment_lock = _production_environment_lock()
    manifest_path = _artifact_manifest_path(article_dir)
    if not manifest_path.is_file():
        production_contract.write_artifact_manifest(
            manifest_path=manifest_path,
            record_id=str(record["record_id"]),
            publication_allowed=record.get("publication_allowed") is True,
            rights_manifest_path=config.rights_manifest,
            article_root=article_dir,
            environment_lock=environment_lock,
        )
    report = production_contract.validate_artifact_manifest(
        manifest_path,
        article_root=article_dir,
        current_environment_lock=environment_lock,
        rights_manifest_path=config.rights_manifest,
    )
    if not report["ok"]:
        raise RuntimeError("Production artifact contract failed: " + ", ".join(report["errors"]))
    return environment_lock


def _record_stage_artifact(
    config: BatchConfig,
    record: dict[str, Any],
    article_dir: Path,
    *,
    artifact_id: str,
    relative_path: str,
    producer: str,
    artifact_type: str,
    paper_stage: str,
    parents: tuple[str, ...] = (),
) -> None:
    environment_lock = _ensure_artifact_contract(config, record, article_dir)
    manifest_path = _artifact_manifest_path(article_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing = next((item for item in manifest.get("artifacts", []) if item.get("artifact_id") == artifact_id), None)
    target = _article_path(article_dir, relative_path)
    if existing is not None:
        if not _valid_hash(target, existing.get("sha256")):
            raise RuntimeError(f"Immutable production artifact drifted: {artifact_id}")
        return
    production_contract.record_artifact(
        manifest_path=manifest_path,
        article_root=article_dir,
        artifact_id=artifact_id,
        relative_path=relative_path,
        producer=producer,
        artifact_type=artifact_type,
        paper_stage=paper_stage,
        environment_lock=environment_lock,
        parents=parents,
        contract_versions={"production": 1},
    )


class RunAlreadyActiveError(RuntimeError):
    pass


class ProjectionGateRefusedError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str = "projection_not_ready") -> None:
        super().__init__(message)
        self.reason_code = reason_code


_LOCAL_RUN_LOCKS: set[Path] = set()
_LOCAL_RUN_LOCK_GUARD = threading.Lock()


@contextmanager
def exclusive_run_lock(run_dir: Path):
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / "run.lock"
    with _LOCAL_RUN_LOCK_GUARD:
        if run_dir in _LOCAL_RUN_LOCKS:
            raise RunAlreadyActiveError(f"Snowmass run is already active: {run_dir.name}")
        _LOCAL_RUN_LOCKS.add(run_dir)
    stream = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RunAlreadyActiveError(f"Snowmass run is already active: {run_dir.name}") from error
        yield
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            with _LOCAL_RUN_LOCK_GUARD:
                _LOCAL_RUN_LOCKS.discard(run_dir)


@dataclass(frozen=True)
class BatchConfig:
    rights_manifest: Path
    pdf_root: Path
    output_root: Path
    control_dir: Path
    stage: str
    explicit_ids: tuple[str, ...]
    max_articles: int | None
    project_max_cost_rmb: float
    stage_max_cost_rmb: float
    usd_cny_rate: float
    chunk_concurrency: int
    article_concurrency: int
    through_stage: str
    translation_version: str
    packaged_on: str
    stage_max_api_calls: int = 1000
    retry_uncertain: bool = False
    historical_roots: tuple[Path, ...] = DEFAULT_HISTORICAL_ROOTS
    preflight_only: bool = False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
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


def _write_or_refresh_run_snapshot(
    path: Path,
    identity: dict[str, Any],
    projection: dict[str, Any],
) -> None:
    """Keep run identity immutable while refreshing checkpoint-dependent forecasts."""

    overlap = sorted(identity.keys() & projection.keys())
    if overlap:
        raise ValueError("Run snapshot identity/projection fields overlap: " + ", ".join(overlap))
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        for key, value in identity.items():
            if existing.get(key) != value:
                raise RuntimeError(f"Run snapshot collision: {identity.get('run_id')}")
    _atomic_json(path, {**identity, **projection})


def load_publication_records(path: Path) -> list[dict[str, Any]]:
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise RuntimeError(f"Rights manifest must be a JSON list: {path}")
    seen: set[str] = set()
    allowed: list[dict[str, Any]] = []
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
            allowed.append(dict(record))
    if not allowed:
        raise RuntimeError("Rights manifest contains no publication-allowed records")
    return allowed


def _selection_key(record: dict[str, Any]) -> tuple[Any, ...]:
    page_count = record.get("page_count")
    pages = float(page_count) if isinstance(page_count, (int, float)) else math.inf
    frontiers = record.get("frontiers") or []
    frontier = str(frontiers[0]) if isinstance(frontiers, list) and frontiers else "~"
    return (frontier.casefold(), pages, str(record["record_id"]).casefold())


def _stratified_partition(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Create deterministic, disjoint baseline/pilot/batch/remainder cohorts."""

    remaining = sorted(records, key=_selection_key)
    cohorts: dict[str, list[dict[str, Any]]] = {}
    for stage in STAGE_LIMITS:
        count = min(STAGE_LIMITS[stage], len(remaining))
        if count == 0:
            cohorts[stage] = []
            continue
        indices = (
            [round(index * (len(remaining) - 1) / (count - 1)) for index in range(count)]
            if count > 1
            else [len(remaining) // 2]
        )
        selected = [remaining[index] for index in indices]
        selected_ids = {str(record["record_id"]) for record in selected}
        cohorts[stage] = selected
        remaining = [record for record in remaining if str(record["record_id"]) not in selected_ids]
    cohorts["remainder"] = remaining
    return cohorts


def select_stage_records(
    records: Iterable[dict[str, Any]],
    stage: str,
    explicit_ids: tuple[str, ...] = (),
    max_articles: int | None = None,
) -> list[dict[str, Any]]:
    records = list(records)
    by_id = {str(record["record_id"]): record for record in records}
    if explicit_ids:
        missing = sorted(set(explicit_ids) - set(by_id))
        if missing:
            raise ValueError("Records are not publication-allowed: " + ", ".join(missing))
        selected = [by_id[record_id] for record_id in explicit_ids]
    elif stage in (*STAGE_LIMITS, "remainder"):
        selected = _stratified_partition(records)[stage]
    else:
        raise ValueError(f"Unsupported production stage: {stage}")
    if max_articles is not None:
        if max_articles <= 0:
            raise ValueError("max_articles must be greater than zero")
        selected = selected[:max_articles]
    return selected


def _valid_hash(path: Path, expected: Any) -> bool:
    return path.is_file() and isinstance(expected, str) and _sha256(path) == expected


def _article_path(article_dir: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise RuntimeError("Manifest artifact path must be a non-empty relative path")
    root = Path(article_dir).resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f"Manifest artifact path escapes article directory: {relative}") from error
    return resolved


def evaluate_article_qc(article_dir: Path) -> dict[str, Any]:
    article_dir = Path(article_dir)
    failures: list[str] = []
    try:
        manifest = json.loads((article_dir / "manifest.json").read_text(encoding="utf-8"))
        paper_status = json.loads((article_dir / "paper_status.json").read_text(encoding="utf-8"))
        refill_status = json.loads((article_dir / "refill_status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as error:
        return {"ok": False, "failures": [f"missing_or_invalid_status:{type(error).__name__}"]}
    if paper_status.get("status") != "complete":
        failures.append("paper_not_complete")
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        failures.append("manifest_has_no_chunks")
        chunks = []
    for chunk in chunks:
        chunk_id = str(chunk.get("id") or "")
        if not re.fullmatch(r"chunk\d{4,}", chunk_id):
            failures.append(f"unsafe_chunk_id:{chunk_id}")
            continue
        try:
            status = json.loads(
                (article_dir / "chunk_status" / f"{chunk_id}.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            failures.append(f"chunk_status_missing:{chunk_id}")
            continue
        academic = status.get("stages", {}).get("academic", {})
        try:
            source = _article_path(article_dir, chunk.get("source_file"))
            output = _article_path(article_dir, chunk.get("output_file"))
        except RuntimeError as error:
            failures.append(f"unsafe_chunk_path:{chunk_id}:{error}")
            continue
        if status.get("status") != "complete" or academic.get("status") != "complete":
            failures.append(f"chunk_not_complete:{chunk_id}")
        if academic.get("qc", {}).get("ok") is not True:
            failures.append(f"chunk_qc_failed:{chunk_id}")
        if not _valid_hash(output, academic.get("output_hash")):
            failures.append(f"chunk_output_hash_mismatch:{chunk_id}")
        else:
            try:
                output_text = output.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                failures.append(f"chunk_output_unreadable:{chunk_id}")
            else:
                if publication_qc.contains_model_meta_response(output_text):
                    failures.append(f"model_meta_response:{chunk_id}")
        refill_chunk = refill_status.get("chunks", {}).get(chunk_id, {})
        if (
            refill_chunk.get("source_sha256") != _sha256(source)
            or refill_chunk.get("output_sha256") != _sha256(output)
        ):
            failures.append(f"refill_chunk_hash_mismatch:{chunk_id}")
        publication_hash = (
            refill_status.get("publication_qc", {})
            .get("publication_chunk_sha256", {})
            .get(chunk_id)
        )
        publication_path = article_dir / "publication_chunks" / f"{chunk_id}.md"
        if not _valid_hash(publication_path, publication_hash):
            failures.append(f"publication_chunk_hash_mismatch:{chunk_id}")
    if refill_status.get("status") != "complete":
        failures.append("refill_not_complete")
    if refill_status.get("refill_schema_version") != refill.REFILL_SCHEMA_VERSION:
        failures.append("refill_contract_stale")
    if refill_status.get("publication_qc", {}).get("ok") is not True:
        failures.append("publication_qc_failed")
    if refill_status.get("reference_qc", {}).get("verified") is not True:
        failures.append("references_not_verified")
    for prefix in ("figure", "table"):
        count = int(refill_status.get(f"{prefix}_region_count") or 0)
        verified = refill_status.get(f"{prefix}_regions_verified")
        if count > 0 and verified is not True:
            failures.append(f"{prefix}_regions_not_verified")
        if count == 0 and refill_status.get(f"{prefix}_regions_not_applicable") is not True:
            failures.append(f"{prefix}_region_classification_missing")
    for label in ("mono", "dual"):
        path = article_dir / "rendered" / f"translated_{label}.pdf"
        if not _valid_hash(path, refill_status.get(f"{label}_pdf_sha256")):
            failures.append(f"{label}_pdf_hash_mismatch")
    return {
        "ok": not failures,
        "record_id": manifest.get("record_id"),
        "chunk_count": len(chunks),
        "failures": failures,
    }


def discover_historical_spend(roots: Iterable[Path], usd_cny_rate: float) -> float:
    article_dirs: set[Path] = set()
    for root in roots:
        papers = Path(root) / "papers"
        if papers.is_dir():
            article_dirs.update(path.resolve() for path in papers.iterdir() if path.is_dir())
    return sum(refined.existing_article_cost_rmb(path, usd_cny_rate) for path in article_dirs)


def _run_id(config: BatchConfig, records: list[dict[str, Any]]) -> str:
    payload = {
        "rights_sha256": _sha256(config.rights_manifest),
        "stage": config.stage,
        "records": [record["record_id"] for record in records],
        "stage_max_cost_rmb": config.stage_max_cost_rmb,
        "stage_max_api_calls": config.stage_max_api_calls,
        "through_stage": config.through_stage,
        "translation_version": config.translation_version,
        "packaged_on": config.packaged_on,
    }
    suffix = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return f"{config.stage}-{suffix}"


def _article_dir(config: BatchConfig, record_id: str) -> Path:
    return config.output_root / "papers" / prepare.safe_record_name(record_id)


def _quarantine_path(config: BatchConfig, record_id: str) -> Path:
    return _article_dir(config, record_id) / "quarantine.json"


def _translation_contract_fingerprint() -> str:
    contract_paths = (
        Path(runner.__file__),
        Path(refined.__file__),
        SCRIPT_DIR / "snowmass_document_units.py",
        SCRIPT_DIR / "snowmass_translation_qc.py",
        ROOT / "translations/snowmass-global-glossary.json",
        ROOT / "translations/snowmass-hard-constraints.json",
    )
    payload = [
        {"path": path.relative_to(ROOT).as_posix(), "sha256": _sha256(path)}
        for path in contract_paths
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _quarantine_fingerprint(article_dir: Path) -> dict[str, str]:
    evidence: dict[str, str] = {
        "translation_contract": _translation_contract_fingerprint()
    }
    for name in ("manifest.json", "chunking_status.json", "paper_status.json", "refill_status.json"):
        path = article_dir / name
        if path.is_file():
            evidence[name] = _sha256(path)
    return evidence


def _persist_quarantine(config: BatchConfig, record_id: str, error: BaseException) -> None:
    article_dir = _article_dir(config, record_id)
    _atomic_json(
        _quarantine_path(config, record_id),
        {
            "schema_version": 1,
            "record_id": record_id,
            "reason": f"{type(error).__name__}: {error}",
            "input_fingerprint": _quarantine_fingerprint(article_dir),
            "transition_required": "change_input_or_explicitly_remove_quarantine_after_review",
        },
    )


def _unchanged_quarantine(config: BatchConfig, record_id: str) -> dict[str, Any] | None:
    path = _quarantine_path(config, record_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if payload.get("input_fingerprint") == _quarantine_fingerprint(_article_dir(config, record_id)):
        return payload
    return None


_TITLE_PLACEHOLDER_RE = re.compile(r"\{v\d+\}")


def restore_plain_title_placeholders(
    source_title: str,
    translated_title: str,
    canonical_english_title: str,
) -> str:
    """Resolve BabelDOC object markers for the plain-text cover title."""

    placeholders: list[tuple[str, int]] = []
    plain_parts: list[str] = []
    boundary = 0
    for part in re.split(r"(\{v\d+\})", source_title.strip()):
        if _TITLE_PLACEHOLDER_RE.fullmatch(part):
            placeholders.append((part, boundary))
        else:
            plain_parts.append(part)
            boundary += len(part)
    if not placeholders:
        if _TITLE_PLACEHOLDER_RE.search(translated_title):
            raise RuntimeError("unresolved title placeholder without a source mapping")
        return " ".join(translated_title.split())

    plain_source = "".join(plain_parts)
    canonical = canonical_english_title.strip()
    matcher = difflib.SequenceMatcher(
        None, plain_source.casefold(), canonical.casefold(), autojunk=False
    )
    if matcher.ratio() < 0.55:
        raise RuntimeError("unresolved title placeholder: canonical title does not match source")
    insertions: dict[int, str] = {}
    for operation, source_start, source_end, canonical_start, canonical_end in matcher.get_opcodes():
        if operation == "insert" and source_start == source_end:
            insertions[source_start] = canonical[canonical_start:canonical_end].strip()

    resolved = translated_title.strip()
    for placeholder, position in placeholders:
        replacement = insertions.get(position, "")
        if not replacement and position not in {0, len(plain_source)}:
            raise RuntimeError(f"unresolved title placeholder: {placeholder}")
        resolved = resolved.replace(placeholder, replacement, 1)
    if _TITLE_PLACEHOLDER_RE.search(resolved):
        raise RuntimeError("unresolved title placeholder remains after restoration")
    resolved = " ".join(resolved.split())
    resolved = re.sub(
        r"(?<=[A-Za-z0-9])\s*([+\-−])\s*(?=[A-Za-z0-9])", r"\1", resolved
    )
    resolved = re.sub(
        r"(?<=[A-Za-z0-9])\s*([+\-−])\s*(?=[\u3400-\u9fff])", r"\1 ", resolved
    )
    return resolved


def _chinese_title_from_manifest(
    article_dir: Path,
    manifest: dict[str, Any],
    canonical_english_title: str | None = None,
) -> str:
    record_id = str(manifest.get("record_id") or "")
    if canonical_english_title and record_id:
        constraints = constraint_compiler.load_constraints(
            article_dir, record_id, refined.TRACKED_HARD_CONSTRAINTS
        )
        canonical_key = " ".join(canonical_english_title.split()).casefold()
        locked_titles = {
            " ".join(str(rule.get("source", "")).split()).casefold(): str(
                rule.get("target", "")
            ).strip()
            for rule in constraints.get("exact_translations", [])
            if str(rule.get("source", "")).strip()
            and str(rule.get("target", "")).strip()
        }
        if canonical_key in locked_titles:
            return locked_titles[canonical_key]

    candidates = [
        chunk
        for chunk in sorted(manifest.get("chunks", []), key=lambda item: item.get("order", 0))
        if chunk.get("page_number") in {None, 1}
        and _article_path(article_dir, chunk.get("output_file")).is_file()
        and _article_path(article_dir, chunk.get("output_file"))
        .read_text(encoding="utf-8")
        .strip()
    ]
    if canonical_english_title and candidates:
        def title_similarity(chunk: dict[str, Any]) -> float:
            source = _article_path(article_dir, chunk.get("source_file"))
            if not source.is_file():
                return 0.0
            plain_source = _TITLE_PLACEHOLDER_RE.sub("", source.read_text(encoding="utf-8"))
            normalized_source = re.sub(r"[^0-9a-z]+", "", plain_source.casefold())
            normalized_canonical = re.sub(
                r"[^0-9a-z]+", "", canonical_english_title.casefold()
            )
            if not normalized_source or not normalized_canonical:
                return 0.0
            return difflib.SequenceMatcher(
                None,
                normalized_source,
                normalized_canonical,
                autojunk=False,
            ).ratio()

        candidates.sort(key=title_similarity, reverse=True)
        if title_similarity(candidates[0]) < 0.70:
            raise RuntimeError(
                "No high-confidence first-page title matches the canonical English title"
            )
    for chunk in candidates[:1] if canonical_english_title else candidates:
        translated = _article_path(article_dir, chunk.get("output_file"))
        if translated.is_file() and translated.read_text(encoding="utf-8").strip():
            translated_text = translated.read_text(encoding="utf-8")
            if _TITLE_PLACEHOLDER_RE.search(translated_text):
                if not canonical_english_title:
                    raise RuntimeError("Canonical English title is required for cover placeholders")
                source = _article_path(article_dir, chunk.get("source_file"))
                return restore_plain_title_placeholders(
                    source.read_text(encoding="utf-8"),
                    translated_text,
                    canonical_english_title,
                )
            return " ".join(translated_text.split())
    raise RuntimeError("No verified translated title is available for packaging")


def _chinese_title(article_dir: Path, canonical_english_title: str | None = None) -> str:
    manifest = json.loads((article_dir / "manifest.json").read_text(encoding="utf-8"))
    return _chinese_title_from_manifest(article_dir, manifest, canonical_english_title)


def _source_character_count(article_dir: Path) -> int:
    manifest = json.loads((article_dir / "manifest.json").read_text(encoding="utf-8"))
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list):
        raise RuntimeError("Article manifest has no chunk list")
    return sum(
        len(_article_path(article_dir, chunk.get("source_file")).read_text(encoding="utf-8"))
        for chunk in chunks
    )


def _merge_usage(
    base: dict[str, Any] | None,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = {
        "api_calls": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    for payload in (base, extra):
        if not isinstance(payload, dict):
            continue
        for key in merged:
            merged[key] += max(0, int(payload.get(key) or 0))
    return merged


def collect_article_run_usage(article_dir: Path, run_id: str | None = None) -> dict[str, Any]:
    article_dir = Path(article_dir)
    totals = {
        "api_calls": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }

    def add_usage(usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        totals["api_calls"] += 1
        for key in ("input_tokens", "cached_tokens", "output_tokens", "total_tokens"):
            totals[key] += max(0, int(usage.get(key) or 0))

    paper_status_path = article_dir / "paper_status.json"
    if paper_status_path.is_file():
        try:
            paper_status = json.loads(paper_status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            paper_status = {}
        phases = paper_status.get("phases") if isinstance(paper_status, dict) else {}
        if isinstance(phases, dict):
            for phase in phases.values():
                if not isinstance(phase, dict):
                    continue
                if run_id is not None and phase.get("run_id") != run_id:
                    continue
                raw_response = phase.get("raw_response")
                if isinstance(raw_response, dict):
                    add_usage(raw_response.get("usage"))

    chunk_dir = article_dir / "chunk_status"
    if chunk_dir.is_dir():
        for path in chunk_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            stages = payload.get("stages") if isinstance(payload, dict) else {}
            if not isinstance(stages, dict):
                continue
            for stage in stages.values():
                if not isinstance(stage, dict):
                    continue
                if run_id is not None and stage.get("run_id") != run_id:
                    continue
                if stage.get("request_key"):
                    continue
                add_usage(stage.get("usage"))

    style_status_path = article_dir / "style_batch_status.json"
    if style_status_path.is_file():
        try:
            payload = json.loads(style_status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        stages = payload.get("stages") if isinstance(payload, dict) else {}
        seen_attempt_ids: set[str] = set()
        if isinstance(stages, dict):
            for stage_payload in stages.values():
                requests = stage_payload.get("requests") if isinstance(stage_payload, dict) else None
                if not isinstance(requests, list):
                    continue
                for entry in requests:
                    if not isinstance(entry, dict):
                        continue
                    attempt_id = str(entry.get("attempt_id") or "")
                    if not attempt_id or attempt_id in seen_attempt_ids:
                        continue
                    seen_attempt_ids.add(attempt_id)
                    add_usage(entry.get("usage"))

    return totals


def _projection_report_for_record(
    config: BatchConfig,
    record: dict[str, Any],
) -> dict[str, Any]:
    record_id = str(record["record_id"])
    article_dir = _article_dir(config, record_id)
    if not (article_dir / "manifest.json").is_file() or not (article_dir / "chunking_status.json").is_file():
        return {
            "record_id": record_id,
            "projection_ready": True,
            "projection_skipped": True,
            "style_projection": {
                "planned": {
                    "anti_ai": {"normal_requests": 0, "worst_case_requests": 0},
                    "academic": {"normal_requests": 0, "worst_case_requests": 0},
                }
            },
            "projected_normal_api_calls": 0,
            "projected_worst_case_api_calls": 0,
        }
    try:
        revision_projection = refined.revision_ready_projection(article_dir)
        if revision_projection.get("projection_ready") is not True:
            return {
                "record_id": record_id,
                "projection_ready": False,
                "missing_revision_chunk_ids": [],
                "revision_projection": revision_projection,
                "error": "revision checkpoint dependency validation failed",
            }
        if int(revision_projection.get("projected_worst_case_api_calls") or 0) > 0:
            return {
                "record_id": record_id,
                "projection_ready": False,
                "missing_revision_chunk_ids": ["checkpoint_dependency_revalidation"],
                "revision_projection": revision_projection,
            }
        glossary_path = runner.resolve_glossary_path(config.output_root, None)
        terms = runner.merge_glossary_terms(
            runner.load_glossary(glossary_path),
            runner.load_article_glossary(article_dir),
        )
        report = refined.style_projection_report(article_dir, terms=terms)
    except Exception as error:
        return {
            "record_id": record_id,
            "projection_ready": False,
            "missing_revision_chunk_ids": [],
            "error": f"{type(error).__name__}: {error}",
        }
    report.setdefault("record_id", record_id)
    return report


def _aggregate_style_projection(
    config: BatchConfig,
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    projections = [_projection_report_for_record(config, record) for record in records]
    ready = [report for report in projections if report.get("projection_ready") is True]
    not_ready = [report for report in projections if report.get("projection_ready") is not True]
    style_projection = {
        "papers": len(projections),
        "ready_papers": len(ready),
        "not_ready_record_ids": [str(report.get("record_id") or "") for report in not_ready],
        "missing_revision_chunk_ids": {
            str(report.get("record_id") or ""): list(report.get("missing_revision_chunk_ids") or [])
            for report in not_ready
        },
        "errors": {
            str(report.get("record_id") or ""): str(report.get("error"))
            for report in not_ready
            if report.get("error")
        },
        "planned": {
            stage_name: {
                "normal_requests": sum(
                    int(
                        (
                            report.get("style_projection", {})
                            .get("planned", {})
                            .get(stage_name, {})
                            .get("normal_requests")
                        )
                        or 0
                    )
                    for report in ready
                ),
                "worst_case_requests": sum(
                    int(
                        (
                            report.get("style_projection", {})
                            .get("planned", {})
                            .get(stage_name, {})
                            .get("worst_case_requests")
                        )
                        or 0
                    )
                    for report in ready
                ),
            }
            for stage_name in ("anti_ai", "academic")
        },
        "projection_ready": not not_ready,
    }
    return {
        "style_projection": style_projection,
        "projected_normal_api_calls": sum(
            int(report.get("projected_normal_api_calls") or 0) for report in ready
        ),
        "projected_worst_case_api_calls": sum(
            int(report.get("projected_worst_case_api_calls") or 0) for report in ready
        ),
        "launch_worst_case_api_calls": sum(
            int(
                report.get("launch_worst_case_api_calls")
                if report.get("launch_worst_case_api_calls") is not None
                else report.get("projected_worst_case_api_calls")
                or 0
            )
            for report in ready
        ),
    }


def _revision_ready_projection_report_for_record(
    config: BatchConfig,
    record: dict[str, Any],
) -> dict[str, Any]:
    record_id = str(record["record_id"])
    article_dir = _article_dir(config, record_id)
    try:
        report = refined.revision_ready_projection(article_dir)
    except Exception as error:
        return {
            "record_id": record_id,
            "projection_ready": False,
            "projected_worst_case_api_calls": 0,
            "missing_stage_api_calls": {
                "analysis": 0,
                "translate": 0,
                "terminology": 0,
                "critique": 0,
                "revision": 0,
            },
            "identity_diagnostics": {
                "record_identity_mismatches": [],
                "invalid_checkpoint_hashes": [],
                "blocking_uncertain_checkpoints": [],
            },
            "error": f"{type(error).__name__}: {error}",
        }
    report.setdefault("record_id", record_id)
    return report


def _aggregate_revision_ready_projection(
    config: BatchConfig,
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    projections = [_revision_ready_projection_report_for_record(config, record) for record in records]
    ready = [report for report in projections if report.get("projection_ready") is True]
    not_ready = [report for report in projections if report.get("projection_ready") is not True]
    diagnostic_keys = (
        "record_identity_mismatches",
        "invalid_checkpoint_hashes",
        "blocking_uncertain_checkpoints",
    )
    revision_ready_projection = {
        "papers": len(projections),
        "ready_papers": len(ready),
        "not_ready_record_ids": [str(report.get("record_id") or "") for report in not_ready],
        "missing_stage_api_calls": {
            stage_name: sum(
                int((report.get("missing_stage_api_calls", {}) or {}).get(stage_name) or 0)
                for report in ready
            )
            for stage_name in ("analysis", "translate", "terminology", "critique", "revision")
        },
        "identity_diagnostics": {
            key: {
                str(report.get("record_id") or ""): list(
                    ((report.get("identity_diagnostics", {}) or {}).get(key) or [])
                )
                for report in not_ready
                if ((report.get("identity_diagnostics", {}) or {}).get(key) or [])
            }
            for key in diagnostic_keys
        },
        "errors": {
            str(report.get("record_id") or ""): str(report.get("error"))
            for report in not_ready
            if report.get("error")
        },
        "projection_ready": not not_ready,
    }
    return {
        "revision_ready_projection": revision_ready_projection,
        "projected_worst_case_api_calls": sum(
            int(report.get("projected_worst_case_api_calls") or 0) for report in ready
        ),
    }


def _projection_summary(
    config: BatchConfig,
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    records = list(records)
    if config.through_stage == "revision_ready":
        return _aggregate_revision_ready_projection(config, records)
    style_summary = _aggregate_style_projection(config, records)
    style_projection = style_summary["style_projection"]
    missing_by_record = style_projection.get("missing_revision_chunk_ids") or {}
    style_errors = style_projection.get("errors") or {}
    missing_revision_ids = {
        str(record_id)
        for record_id, chunk_ids in missing_by_record.items()
        if chunk_ids and str(record_id) not in style_errors
    }
    hard_style_not_ready = set(style_projection.get("not_ready_record_ids") or []) - missing_revision_ids
    revision_records = [
        record for record in records if str(record["record_id"]) in missing_revision_ids
    ]
    revision_summary = _aggregate_revision_ready_projection(config, revision_records)
    revision_projection = revision_summary["revision_ready_projection"]
    style_ready_ids = [
        str(record["record_id"])
        for record in records
        if str(record["record_id"]) not in missing_revision_ids
    ]
    launch_projection = {
        "projection_ready": revision_projection["projection_ready"] and not hard_style_not_ready,
        "revision_ready_record_ids": [str(record["record_id"]) for record in revision_records],
        "style_ready_record_ids": style_ready_ids,
        "projected_worst_case_api_calls": (
            int(style_summary.get("launch_worst_case_api_calls") or 0)
            + int(revision_summary.get("projected_worst_case_api_calls") or 0)
        ),
        "not_ready_record_ids": sorted(
            hard_style_not_ready | set(revision_projection.get("not_ready_record_ids") or [])
        ),
    }
    return {
        **style_summary,
        "revision_ready_projection": revision_projection,
        "launch_projection": launch_projection,
    }


def production_metrics_and_gate(
    *,
    stage: str,
    through_stage: str,
    eligible_record_count: int,
    selected_count: int,
    results: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    budget: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Summarize cost efficiency and fail closed before expanding a campaign."""

    recovered_results = [
        result
        for result in results
        if result.get("resumed_from_verified_artifacts")
        or result.get("resumed_from_verified_translation")
    ]
    fresh_results = [result for result in results if result not in recovered_results]
    source_characters = sum(
        max(0, int(result.get("source_characters") or 0)) for result in fresh_results
    )
    stage_spent = float(budget.get("stage_spent_rmb") or 0)
    completed = sum(result.get("status") == through_stage for result in results)
    usage = budget.get("stage_usage") if isinstance(budget.get("stage_usage"), dict) else {}
    api_calls = max(0, int(usage.get("api_calls") or 0))
    uncertain_calls = max(0, int(usage.get("uncertain_calls") or 0))
    unresolved_uncertain_calls = max(
        0,
        int(usage.get("unresolved_uncertain_calls", uncertain_calls) or 0),
    )
    cost_per_article = stage_spent / len(fresh_results) if fresh_results else None
    cost_per_10k = stage_spent * 10_000 / source_characters if source_characters else None
    historical_before_stage = max(0.0, float(budget.get("project_spent_rmb") or 0) - stage_spent)
    projected_total = (
        historical_before_stage + cost_per_article * eligible_record_count
        if cost_per_article is not None
        else None
    )
    style_projections = [
        result["style_batch_projection"]
        for result in results
        if isinstance(result.get("style_batch_projection"), dict)
    ]
    style_current_requests = sum(
        max(0, int(item.get("current_style_requests") or 0))
        for item in style_projections
    )
    style_projected_requests = sum(
        max(0, int(item.get("projected_style_requests") or 0))
        for item in style_projections
    )
    style_projection = {
        "papers": len(style_projections),
        "eligible_chunks": sum(
            max(0, int(item.get("eligible_chunks") or 0)) for item in style_projections
        ),
        "groupable_chunks": sum(
            max(0, int(item.get("groupable_chunks") or 0)) for item in style_projections
        ),
        "current_style_requests": style_current_requests,
        "projected_style_requests": style_projected_requests,
        "projected_request_reduction_fraction": (
            (style_current_requests - style_projected_requests) / style_current_requests
            if style_current_requests
            else 0.0
        ),
    }
    metrics = {
        "source_characters": source_characters,
        "completed_articles": completed,
        "fresh_completed_articles": sum(
            result.get("status") == through_stage for result in fresh_results
        ),
        "recovered_articles": len(recovered_results),
        "failed_articles": len(failures),
        "api_calls": api_calls,
        "uncertain_paid_requests": uncertain_calls,
        "unresolved_uncertain_paid_requests": unresolved_uncertain_calls,
        "input_tokens": max(0, int(usage.get("input_tokens") or 0)),
        "cached_tokens": max(0, int(usage.get("cached_tokens") or 0)),
        "output_tokens": max(0, int(usage.get("output_tokens") or 0)),
        "total_tokens": max(0, int(usage.get("total_tokens") or 0)),
        "stage_spent_rmb": stage_spent,
        "cost_rmb_per_article": round(cost_per_article, 6) if cost_per_article is not None else None,
        "cost_rmb_per_10k_source_characters": round(cost_per_10k, 6) if cost_per_10k is not None else None,
        "projected_total_rmb_for_eligible_records": round(projected_total, 6) if projected_total is not None else None,
        "uncertain_request_rate": round(uncertain_calls / api_calls, 6) if api_calls else 0.0,
        "style_batch_projection": style_projection,
    }
    reasons: list[str] = []
    expected_status = "qc_passed" if through_stage in {"rendered", "qc_passed"} else through_stage
    if failures:
        reasons.append("article_failures")
    if recovered_results:
        reasons.append("recovered_results_not_promotion_evidence")
    if not fresh_results:
        reasons.append("no_fresh_production_evidence")
    if len(results) != selected_count:
        reasons.append("selected_articles_incomplete")
    required_fresh = STAGE_LIMITS.get(stage)
    if required_fresh is not None and selected_count != min(required_fresh, eligible_record_count):
        reasons.append("stage_fresh_sample_size_not_met")
    if required_fresh is not None and len(fresh_results) != selected_count:
        reasons.append("stage_fresh_sample_size_not_met")
    if any(result.get("status") != expected_status for result in results):
        reasons.append(f"selected_articles_not_{expected_status}")
    if unresolved_uncertain_calls:
        reasons.append("uncertain_paid_requests")
    if float(budget.get("project_reserved_rmb") or 0) or float(budget.get("stage_reserved_rmb") or 0):
        reasons.append("active_budget_reservations")
    if projected_total is None:
        reasons.append("insufficient_cost_evidence")
    elif projected_total > float(budget.get("project_max_cost_rmb") or 0) + 1e-12:
        reasons.append("projected_full_corpus_cost_exceeds_cap")
    next_stage = {
        "shadow": "pilot5",
        "pilot5": "pilot10",
        "pilot10": "pilot25",
        "pilot25": "batch50",
        "batch50": "remainder",
    }.get(stage)
    gate = {
        "allowed": not reasons and next_stage is not None,
        "next_stage": next_stage,
        "reasons": reasons or (["campaign_complete"] if next_stage is None else []),
    }
    return metrics, gate


def _package_article(config: BatchConfig, record: dict[str, Any], article_dir: Path) -> dict[str, Any]:
    environment_lock = _production_environment_lock()
    article_qc = evaluate_article_qc(article_dir)
    if not article_qc["ok"]:
        raise RuntimeError("publication QC failed: " + ", ".join(article_qc["failures"]))
    _adopt_verified_translation_chain(config, record, article_dir)
    _write_article_qc_receipts(config, record, article_dir, article_qc)
    qc_paths = [article_dir / "qc" / f"{kind}.json" for kind in qc_contract.ALLOWED_KINDS]
    qc_report = qc_contract.validate_publishability_receipts(
        qc_paths,
        article_root=article_dir,
        expected_record_id=str(record["record_id"]),
        current_environment_lock_sha256=environment_lock["lock_sha256"],
        required_contract_version=1,
    )
    if not qc_report["publishable"]:
        raise RuntimeError("QC receipt gate failed: " + ", ".join(qc_report["errors"]))
    safe_id = prepare.safe_record_name(str(record["record_id"])).replace("arxiv_", "")
    output = article_dir / "packaged" / f"snowmass-{safe_id}.zh-CN.pdf"
    receipt = packager.package_translation_pdf(
        record=record,
        chinese_title=_chinese_title(article_dir, str(record.get("title") or "")),
        source_pdf_path=article_dir / "rendered/translated_mono.pdf",
        output_pdf_path=output,
        version=config.translation_version,
        packaged_on=config.packaged_on,
        qc_receipt_hashes={
            report["kind"]: report["receipt"]["receipt_hash"] for report in qc_report["receipts"]
        },
    )
    _record_stage_artifact(
        config, record, article_dir,
        artifact_id="packaged", relative_path=str(output.relative_to(article_dir)),
        producer="package_snowmass_translation_pdf", artifact_type="publication_pdf",
        paper_stage="packaged", parents=("visual_qc",),
    )
    return receipt


def _adopt_verified_translation_chain(config: BatchConfig, record: dict[str, Any], article_dir: Path) -> None:
    """Bind verified legacy outputs before a package-only recovery.

    This records recovery evidence but does not make the paper fresh promotion
    evidence; callers retain the recovered-result flag.
    """

    stages = (
        ("prepared", "manifest.json", "prepare_snowmass_babeldoc", "article_manifest", "prepared", ()),
        ("revision_ready", refined.REVISION_FILE, "run_snowmass_refined_translation", "revision", "revision_ready", ("prepared",)),
        ("translated", refined.FINAL_FILE, "run_snowmass_refined_translation", "translation", "translated", ("revision_ready",)),
        ("rendered", "rendered/translated_mono.pdf", "refill_snowmass_babeldoc", "rendered_pdf", "rendered", ("translated",)),
    )
    for artifact_id, relative_path, producer, artifact_type, paper_stage, parents in stages:
        _record_stage_artifact(
            config,
            record,
            article_dir,
            artifact_id=artifact_id,
            relative_path=relative_path,
            producer=producer,
            artifact_type=artifact_type,
            paper_stage=paper_stage,
            parents=parents,
        )


def _write_article_qc_receipts(config: BatchConfig, record: dict[str, Any], article_dir: Path, qc: dict[str, Any]) -> dict[str, Any]:
    environment_lock = _production_environment_lock()
    target = article_dir / "rendered/translated_mono.pdf"
    audit = pdf_audit.audit_pdf(target)
    evidence = {
        "semantic": {"article_qc": qc, "checks": ["numbers", "units", "citations", "protected_literals", "model_meta"]},
        "structural": {"article_qc": qc, "checks": ["chunk_order", "parent_hashes", "references", "figure_table_regions"]},
        "visual": audit,
    }
    receipts = {}
    for kind in qc_contract.ALLOWED_KINDS:
        receipts[kind] = qc_contract.write_qc_receipt(
            receipt_path=article_dir / "qc" / f"{kind}.json",
            article_root=article_dir,
            record_id=str(record["record_id"]),
            kind=kind,
            target_artifact_id="rendered-mono",
            target_path=target,
            environment_lock_sha256=environment_lock["lock_sha256"],
            contract_version=1,
            ok=qc["ok"] is True and (audit["ok"] is True if kind == "visual" else True),
            evidence_summary=evidence[kind],
        )
    verdict = qc_contract.validate_publishability_receipts(
        [article_dir / "qc" / f"{kind}.json" for kind in qc_contract.ALLOWED_KINDS],
        article_root=article_dir,
        expected_record_id=str(record["record_id"]),
        current_environment_lock_sha256=environment_lock["lock_sha256"],
        required_contract_version=1,
    )
    if not verdict["publishable"]:
        raise RuntimeError("QC receipt creation failed: " + ", ".join(verdict["errors"]))
    parent = "rendered"
    for kind, stage in (("semantic", "semantic_qc"), ("structural", "structural_qc"), ("visual", "visual_qc")):
        _record_stage_artifact(
            config, record, article_dir,
            artifact_id=f"{kind}_qc", relative_path=f"qc/{kind}.json",
            producer="snowmass_qc_contract", artifact_type="qc_receipt",
            paper_stage=stage, parents=(parent,),
        )
        parent = f"{kind}_qc"
    return receipts


def _package_only_result(
    config: BatchConfig,
    record: dict[str, Any],
) -> dict[str, Any]:
    article_dir = _article_dir(config, str(record["record_id"]))
    qc = evaluate_article_qc(article_dir)
    if not qc["ok"]:
        raise RuntimeError("publication QC failed: " + ", ".join(qc["failures"]))
    return {
        "record_id": str(record["record_id"]),
        "status": "packaged",
        "source_characters": _source_character_count(article_dir),
        "qc": qc,
        "package": _package_article(config, record, article_dir),
        "resumed_from_verified_translation": True,
    }


def _resume_article_result(
    config: BatchConfig,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """Reconstruct a completed result only from current, hash-verified artifacts."""

    article_dir = _article_dir(config, str(record["record_id"]))
    qc = evaluate_article_qc(article_dir)
    if not qc["ok"]:
        return None
    result: dict[str, Any] = {
        "record_id": str(record["record_id"]),
        "status": "qc_passed",
        "source_characters": _source_character_count(article_dir),
        "qc": qc,
        "resumed_from_verified_artifacts": True,
    }
    if config.through_stage in {"rendered", "qc_passed"}:
        return result
    if config.through_stage in {"prepared", "translated", "revision_ready"}:
        return None
    safe_id = prepare.safe_record_name(str(record["record_id"])).replace("arxiv_", "")
    receipt_path = article_dir / "packaged" / f"snowmass-{safe_id}.zh-CN.json"
    output_path = article_dir / "packaged" / f"snowmass-{safe_id}.zh-CN.pdf"
    source_path = article_dir / "rendered/translated_mono.pdf"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    environment_lock = _production_environment_lock()
    qc_paths = [article_dir / "qc" / f"{kind}.json" for kind in qc_contract.ALLOWED_KINDS]
    receipt_gate = qc_contract.validate_publishability_receipts(
        qc_paths,
        article_root=article_dir,
        expected_record_id=str(record["record_id"]),
        current_environment_lock_sha256=environment_lock["lock_sha256"],
        required_contract_version=1,
    )
    observed_qc_hashes = {
        report["kind"]: report["receipt"]["receipt_hash"]
        for report in receipt_gate.get("receipts", [])
        if report.get("ok") is True and isinstance(report.get("kind"), str)
    }
    artifact_state = production_contract.derive_paper_state(
        _artifact_manifest_path(article_dir),
        article_root=article_dir,
        current_environment_lock=environment_lock,
        rights_manifest_path=config.rights_manifest,
    )
    if (
        receipt.get("record_id") != record["record_id"]
        or receipt.get("packaging_contract_version") != packager.PACKAGING_CONTRACT_VERSION
        or receipt.get("version") != config.translation_version
        or receipt.get("packaged_on") != config.packaged_on
        or not _valid_hash(source_path, receipt.get("source_pdf_sha256"))
        or not _valid_hash(output_path, receipt.get("packaged_pdf_sha256"))
        or receipt_gate["publishable"] is not True
        or receipt.get("qc_receipt_hashes") != observed_qc_hashes
        or artifact_state.get("publishable") is not True
        or artifact_state.get("artifact_id") != "packaged"
    ):
        return None
    result.update({"status": "packaged", "package": receipt})
    return result


def _classify_selected_records(
    config: BatchConfig,
    selected: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    recoverable: list[dict[str, Any]] = []
    package_only: list[dict[str, Any]] = []
    paid_pending: list[dict[str, Any]] = []
    for record in selected:
        quarantine = _unchanged_quarantine(config, str(record["record_id"]))
        if quarantine is not None:
            recoverable.append(
                {
                    "record_id": str(record["record_id"]),
                    "status": "quarantined",
                    "source_characters": 0,
                    "quarantine": quarantine,
                    "resumed_from_verified_artifacts": True,
                }
            )
            continue
        resumed = _resume_article_result(config, record)
        if resumed is not None:
            recoverable.append(resumed)
            continue
        if config.through_stage == "packaged" and evaluate_article_qc(
            _article_dir(config, str(record["record_id"]))
        )["ok"]:
            package_only.append(record)
            continue
        paid_pending.append(record)
    return recoverable, package_only, paid_pending


def resolve_babeldoc_python(console_script: Path | None = None) -> Path:
    if console_script is None:
        resolved = shutil.which("babeldoc")
        if not resolved:
            raise RuntimeError("The BabelDOC console script is unavailable")
        console_script = Path(resolved)
    console_script = Path(console_script).resolve()
    try:
        first_line = console_script.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, UnicodeDecodeError, IndexError) as error:
        raise RuntimeError(f"Cannot inspect BabelDOC runtime: {console_script}") from error
    if not first_line.startswith("#!"):
        raise RuntimeError(f"BabelDOC console script has no Python shebang: {console_script}")
    words = shlex.split(first_line[2:].strip())
    if not words:
        raise RuntimeError(f"BabelDOC console script has an empty shebang: {console_script}")
    if Path(words[0]).name == "env":
        command = next((word for word in words[1:] if not word.startswith("-")), None)
        resolved_runtime = shutil.which(command) if command else None
        if not resolved_runtime:
            raise RuntimeError(
                f"BabelDOC env Python runtime is unavailable: {command or '<missing>'}"
            )
        return Path(resolved_runtime)
    runtime = Path(words[0])
    if not runtime.is_file():
        raise RuntimeError(f"BabelDOC Python runtime is unavailable: {runtime}")
    return runtime


def _prepare_all(config: BatchConfig, records: list[dict[str, Any]]) -> None:
    arguments = [
        "--rights-manifest", str(config.rights_manifest),
        "--pdf-root", str(config.pdf_root),
        "--output-root", str(config.output_root),
    ]
    for record in records:
        arguments.extend(("--record-id", str(record["record_id"])))
    try:
        import importlib.metadata
        has_babeldoc = importlib.metadata.version("babeldoc") == prepare.BRIDGE.BABELDOC_VERSION
    except importlib.metadata.PackageNotFoundError:
        has_babeldoc = False
    if has_babeldoc:
        exit_code = prepare.main(arguments)
    else:
        process = subprocess.run(
            [str(resolve_babeldoc_python()), str(prepare.__file__), *arguments],
            cwd=ROOT,
            text=True,
            check=False,
        )
        exit_code = process.returncode
    if exit_code != 0:
        raise RuntimeError(f"BabelDOC preparation failed with exit code {exit_code}")


def _refill_article(config: BatchConfig, article_dir: Path) -> None:
    arguments = [
        "--article-dir", str(article_dir),
        "--rights-manifest", str(config.rights_manifest),
    ]
    try:
        import importlib.metadata
        has_babeldoc = importlib.metadata.version("babeldoc") == refill.BRIDGE.BABELDOC_VERSION
    except importlib.metadata.PackageNotFoundError:
        has_babeldoc = False
    if has_babeldoc:
        exit_code = refill.main(arguments)
    else:
        process = subprocess.run(
            [str(resolve_babeldoc_python()), str(refill.__file__), *arguments],
            cwd=ROOT,
            text=True,
            check=False,
        )
        exit_code = process.returncode
    if exit_code != 0:
        raise RuntimeError(f"BabelDOC refill failed with exit code {exit_code}")


def _run_article(
    config: BatchConfig,
    record: dict[str, Any],
    run_id: str,
    client: Any,
    budget: PersistentBudgetGuard,
) -> dict[str, Any]:
    record_id = str(record["record_id"])
    article_dir = _article_dir(config, record_id)
    result: dict[str, Any] = {
        "record_id": record_id,
        "status": "prepared",
        "source_characters": _source_character_count(article_dir),
    }
    _record_stage_artifact(
        config, record, article_dir,
        artifact_id="prepared", relative_path="manifest.json", producer="prepare_snowmass_babeldoc",
        artifact_type="article_manifest", paper_stage="prepared",
    )
    if config.through_stage == "packaged":
        existing_qc = evaluate_article_qc(article_dir)
        if existing_qc["ok"]:
            receipt = _package_article(config, record, article_dir)
            return {
                **result,
                "status": "packaged",
                "qc": existing_qc,
                "package": receipt,
                "resumed_from_verified_translation": True,
            }
    if config.through_stage == "prepared":
        return result
    glossary_path = runner.resolve_glossary_path(config.output_root, None)
    terms = runner.merge_glossary_terms(
        runner.load_glossary(glossary_path),
        runner.load_article_glossary(article_dir),
    )
    refined.run_refined_article(
        article_dir,
        client=client,
        terms=terms,
        run_id=run_id,
        budget_guard=budget,
        concurrency=config.chunk_concurrency,
        retry_uncertain=config.retry_uncertain,
        stop_after_revision=config.through_stage == "revision_ready",
    )
    _record_stage_artifact(
        config, record, article_dir,
        artifact_id="revision_ready", relative_path=refined.REVISION_FILE,
        producer="run_snowmass_refined_translation", artifact_type="revision",
        paper_stage="revision_ready", parents=("prepared",),
    )
    if config.through_stage == "revision_ready":
        result["status"] = "revision_ready"
        return result
    projection_path = article_dir / "style_batch_projection.json"
    if projection_path.is_file():
        projection = json.loads(projection_path.read_text(encoding="utf-8"))
        if not isinstance(projection, dict):
            raise RuntimeError("style batch projection must be a JSON object")
        result["style_batch_projection"] = projection
    result["status"] = "translated"
    _record_stage_artifact(
        config, record, article_dir,
        artifact_id="translated", relative_path=refined.FINAL_FILE,
        producer="run_snowmass_refined_translation", artifact_type="translation",
        paper_stage="translated", parents=("revision_ready",),
    )
    if config.through_stage == "translated":
        return result
    _refill_article(config, article_dir)
    result["status"] = "rendered"
    _record_stage_artifact(
        config, record, article_dir,
        artifact_id="rendered", relative_path="rendered/translated_mono.pdf",
        producer="refill_snowmass_babeldoc", artifact_type="rendered_pdf",
        paper_stage="rendered", parents=("translated",),
    )
    qc = evaluate_article_qc(article_dir)
    result["qc"] = qc
    if not qc["ok"]:
        raise RuntimeError("publication QC failed: " + ", ".join(qc["failures"]))
    result["status"] = "qc_passed"
    if config.through_stage in {"rendered", "qc_passed"}:
        return result
    receipt = _package_article(config, record, article_dir)
    result.update({"status": "packaged", "package": receipt})
    return result


def run_batch(config: BatchConfig, *, client: Any = None) -> dict[str, Any]:
    validate_budget(
        config.project_max_cost_rmb,
        label="project",
        maximum=AUTHORIZED_PROJECT_MAX_RMB,
    )
    validate_budget(
        config.stage_max_cost_rmb,
        label="stage",
        maximum=config.project_max_cost_rmb,
    )
    validate_request_limit(config.stage_max_api_calls, label="stage")
    if config.chunk_concurrency < 1 or config.chunk_concurrency > 64:
        raise ValueError("chunk_concurrency must be between 1 and 64")
    if config.article_concurrency < 1 or config.article_concurrency > 4:
        raise ValueError("article_concurrency must be between 1 and 4")
    if config.through_stage not in TERMINAL_STAGES:
        raise ValueError(f"Unsupported through stage: {config.through_stage}")
    records = load_publication_records(config.rights_manifest)
    selected = select_stage_records(
        records,
        config.stage,
        config.explicit_ids,
        config.max_articles,
    )
    run_id = _run_id(config, selected)
    snapshot = {
        "schema_version": 1,
        "run_id": run_id,
        "stage": config.stage,
        "rights_manifest_sha256": _sha256(config.rights_manifest),
        "eligible_record_count": len(records),
        "selected_record_ids": [record["record_id"] for record in selected],
        "project_max_cost_rmb": config.project_max_cost_rmb,
        "stage_max_cost_rmb": config.stage_max_cost_rmb,
        "stage_max_api_calls": config.stage_max_api_calls,
        "through_stage": config.through_stage,
        "translation_version": config.translation_version,
        "packaged_on": config.packaged_on,
    }
    historical_roots = tuple(dict.fromkeys((*config.historical_roots, config.output_root)))
    if config.preflight_only:
        # Preparation is local and zero-paid. Projection is meaningless until the
        # source PDF has been parsed into a concrete chunk manifest.
        _prepare_all(config, selected)
        recoverable, package_only_records, paid_pending = _classify_selected_records(config, selected)
        quarantined = [result for result in recoverable if result.get("status") == "quarantined"]
        recoverable = [result for result in recoverable if result.get("status") != "quarantined"]
        projection_summary = _projection_summary(config, paid_pending)
        package_only_ids = [str(record["record_id"]) for record in package_only_records]
        return {
            **snapshot,
            **projection_summary,
            "status": "preflight",
            "historical_spent_rmb": discover_historical_spend(
                historical_roots, config.usd_cny_rate
            ),
            "verified_resume_count": len(recoverable),
            "verified_resume_record_ids": [result["record_id"] for result in recoverable],
            "verified_package_only_count": len(package_only_ids),
            "verified_package_only_record_ids": package_only_ids,
            "paid_translation_pending_count": len(paid_pending),
            "quarantined_count": len(quarantined),
            "quarantined_record_ids": [result["record_id"] for result in quarantined],
            "pending_record_count": len(selected) - len(recoverable),
        }
    run_dir = config.control_dir / "runs" / run_id
    with exclusive_run_lock(config.control_dir / "campaign"):
        with exclusive_run_lock(run_dir):
            return _run_batch_locked(
                config,
                selected=selected,
                snapshot=snapshot,
                run_id=run_id,
                run_dir=run_dir,
                historical_roots=historical_roots,
                client=client,
            )


def _run_batch_locked(
    config: BatchConfig,
    *,
    selected: list[dict[str, Any]],
    snapshot: dict[str, Any],
    run_id: str,
    run_dir: Path,
    historical_roots: tuple[Path, ...],
    client: Any,
) -> dict[str, Any]:
    run_snapshot_path = run_dir / "snapshot.json"
    historical_spent = discover_historical_spend(historical_roots, config.usd_cny_rate)
    stop_event = threading.Event()
    budget = PersistentBudgetGuard(
        config.control_dir,
        project_max_cost_rmb=config.project_max_cost_rmb,
        stage_max_cost_rmb=config.stage_max_cost_rmb,
        stage_max_api_calls=config.stage_max_api_calls,
        run_id=run_id,
        usd_cny_rate=config.usd_cny_rate,
        historical_spent_rmb=historical_spent,
        stop_event=stop_event,
    )
    _prepare_all(config, selected)
    state_lock = threading.Lock()
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    hard_failures: list[dict[str, Any]] = []
    stop_reason: str | None = None
    consecutive_content_failures = 0
    resolved_content_outcomes: dict[int, bool] = {}
    next_outcome_ordinal = 0
    recoverable, package_only_records, pending_records = _classify_selected_records(config, selected)
    quarantined = [result for result in recoverable if result.get("status") == "quarantined"]
    recoverable = [result for result in recoverable if result.get("status") != "quarantined"]
    results.extend(recoverable)
    failures.extend(
        {"record_id": result["record_id"], "error": result["quarantine"]["reason"], "persisted_quarantine": True}
        for result in quarantined
    )
    projection_summary = _projection_summary(config, pending_records)
    _write_or_refresh_run_snapshot(run_snapshot_path, snapshot, projection_summary)
    snapshot.update(projection_summary)

    for record in package_only_records:
        try:
            results.append(_package_only_result(config, record))
        except Exception as error:
            failures.append(
                {
                    "record_id": str(record["record_id"]),
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    actual_client = None
    if pending_records:
        stage_remaining_api_calls = max(
            0,
            int(budget.snapshot().get("stage_remaining_api_calls") or 0),
        )
        if config.through_stage == "revision_ready":
            revision_projection = projection_summary["revision_ready_projection"]
            if not revision_projection["projection_ready"]:
                details = {
                    "not_ready_record_ids": revision_projection["not_ready_record_ids"],
                    "identity_diagnostics": revision_projection["identity_diagnostics"],
                    "errors": revision_projection["errors"],
                }
                raise ProjectionGateRefusedError(
                    "revision-ready projection is not ready for paid launch: "
                    + json.dumps(details, ensure_ascii=False, sort_keys=True),
                )
        elif not projection_summary["launch_projection"]["projection_ready"]:
            missing = projection_summary["launch_projection"]["not_ready_record_ids"]
            raise ProjectionGateRefusedError(
                "next-stage projection is not ready for paid launch: "
                + json.dumps(missing, ensure_ascii=False, sort_keys=True),
            )
        projected_worst_case = int(
            projection_summary["launch_projection"]["projected_worst_case_api_calls"]
            if config.through_stage != "revision_ready"
            else projection_summary["projected_worst_case_api_calls"]
            or 0
        )
        if projected_worst_case > stage_remaining_api_calls:
            raise RequestLimitExceededError(
                (
                    "revision-ready projection worst case would exceed the remaining stage request cap: "
                    if config.through_stage == "revision_ready"
                    else "style projection worst case would exceed the remaining stage request cap: "
                )
                +
                f"{projected_worst_case} > {stage_remaining_api_calls}"
            )
        actual_client = client or runner.DeepSeekClient(runner.load_api_key())

    def persist() -> None:
        with state_lock:
            budget_snapshot = budget.snapshot()
            resumed_usage = {
                "api_calls": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }
            for result in results:
                if not (
                    result.get("resumed_from_verified_artifacts")
                    or result.get("resumed_from_verified_translation")
                ):
                    continue
                resumed_usage = _merge_usage(
                    resumed_usage,
                    collect_article_run_usage(_article_dir(config, str(result["record_id"]))),
                )
            budget_snapshot = {
                **budget_snapshot,
                "stage_usage": _merge_usage(budget_snapshot.get("stage_usage"), resumed_usage),
            }
            metrics, promotion_gate = production_metrics_and_gate(
                stage=config.stage,
                through_stage=config.through_stage,
                eligible_record_count=int(snapshot["eligible_record_count"]),
                selected_count=len(selected),
                results=results,
                failures=failures,
                budget=budget_snapshot,
            )
            _atomic_json(
                run_dir / "run.json",
                {
                    **snapshot,
                    "status": (
                        "stopped"
                        if hard_failures or stop_reason
                        else (
                            "complete_with_quarantine"
                            if failures and len(results) + len(failures) == len(selected)
                            else ("complete" if len(results) == len(selected) else "running")
                        )
                    ),
                    "completed": len(results),
                    "failed": len(failures),
                    "quarantined": len(failures),
                    "hard_failures": sorted(hard_failures, key=lambda item: item["record_id"]),
                    **({"stop_reason": stop_reason} if stop_reason else {}),
                    "results": sorted(results, key=lambda item: item["record_id"]),
                    "failures": sorted(failures, key=lambda item: item["record_id"]),
                    "budget": budget_snapshot,
                    **projection_summary,
                    "metrics": metrics,
                    "promotion_gate": promotion_gate,
                },
            )
    persist()
    # A rolling executor limits already-started work. Content failures quarantine one
    # paper; budget/uncertainty gates and the systemic-failure circuit breaker stop intake.
    iterator = iter(enumerate(pending_records))
    futures: dict[
        concurrent.futures.Future[dict[str, Any]], tuple[int, dict[str, Any]]
    ] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.article_concurrency) as executor:
        for _ in range(config.article_concurrency):
            try:
                ordinal, record = next(iterator)
            except StopIteration:
                break
            futures[executor.submit(_run_article, config, record, run_id, actual_client, budget)] = (ordinal, record)
        while futures:
            done, _pending = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                ordinal, record = futures.pop(future)
                try:
                    results.append(future.result())
                    resolved_content_outcomes[ordinal] = True
                except Exception as error:
                    failure = (
                        {
                            "record_id": str(record["record_id"]),
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
                    if isinstance(error, runner.BudgetExceededError):
                        stop_event.set()
                        hard_failures.append(failure)
                    else:
                        failures.append(failure)
                        _persist_quarantine(config, str(record["record_id"]), error)
                        resolved_content_outcomes[ordinal] = False
                while next_outcome_ordinal in resolved_content_outcomes:
                    if resolved_content_outcomes.pop(next_outcome_ordinal):
                        consecutive_content_failures = 0
                    else:
                        consecutive_content_failures += 1
                    next_outcome_ordinal += 1
                    if consecutive_content_failures >= 3:
                        stop_event.set()
                        stop_reason = "systemic_content_failure_circuit_breaker"
                persist()
                if hard_failures or stop_reason:
                    continue
                try:
                    next_ordinal, next_record = next(iterator)
                except StopIteration:
                    continue
                futures[
                    executor.submit(
                        _run_article,
                        config,
                        next_record,
                        run_id,
                        actual_client,
                        budget,
                    )
                ] = (next_ordinal, next_record)
    persist()
    summary = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    summary["not_started"] = (
        len(selected) - len(results) - len(failures) - len(hard_failures)
    )
    return summary


def _parse_args(argv: list[str] | None) -> BatchConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rights-manifest", type=Path, default=DEFAULT_RIGHTS_MANIFEST)
    parser.add_argument("--pdf-root", type=Path, default=DEFAULT_PDF_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--control-dir", type=Path, default=DEFAULT_CONTROL_DIR)
    parser.add_argument("--stage", choices=(*STAGE_LIMITS, "remainder"), required=True)
    parser.add_argument("--record-id", action="append", default=[])
    parser.add_argument("--max-articles", type=int)
    parser.add_argument("--project-max-cost-rmb", type=float, required=True)
    parser.add_argument("--stage-max-cost-rmb", type=float, required=True)
    parser.add_argument("--stage-max-api-calls", type=int, default=1000)
    parser.add_argument("--usd-cny-rate", type=float, default=runner.DEFAULT_USD_CNY_RATE)
    parser.add_argument("--chunk-concurrency", type=int, default=4)
    parser.add_argument("--article-concurrency", type=int, default=1)
    parser.add_argument("--through-stage", choices=TERMINAL_STAGES, default="packaged")
    parser.add_argument("--translation-version", default="v0.1")
    parser.add_argument("--packaged-on", default=dt.date.today().isoformat())
    parser.add_argument("--historical-root", action="append", type=Path)
    parser.add_argument("--retry-uncertain", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        project = validate_budget(
            args.project_max_cost_rmb,
            label="project",
            maximum=AUTHORIZED_PROJECT_MAX_RMB,
        )
        stage = validate_budget(args.stage_max_cost_rmb, label="stage", maximum=project)
        stage_max_api_calls = validate_request_limit(
            args.stage_max_api_calls, label="stage"
        )
    except ValueError as error:
        parser.error(str(error))
    return BatchConfig(
        rights_manifest=args.rights_manifest,
        pdf_root=args.pdf_root,
        output_root=args.output_root,
        control_dir=args.control_dir,
        stage=args.stage,
        explicit_ids=tuple(args.record_id),
        max_articles=args.max_articles,
        project_max_cost_rmb=project,
        stage_max_cost_rmb=stage,
        stage_max_api_calls=stage_max_api_calls,
        usd_cny_rate=args.usd_cny_rate,
        chunk_concurrency=args.chunk_concurrency,
        article_concurrency=args.article_concurrency,
        through_stage=args.through_stage,
        translation_version=args.translation_version,
        packaged_on=args.packaged_on,
        retry_uncertain=args.retry_uncertain,
        historical_roots=tuple(args.historical_root or DEFAULT_HISTORICAL_ROOTS),
        preflight_only=args.preflight_only,
    )


def main(argv: list[str] | None = None) -> int:
    config = _parse_args(argv)
    try:
        summary = run_batch(config)
    except ProjectionGateRefusedError as error:
        print(
            json.dumps(
                {
                    "status": "gate_refused",
                    "reason_code": error.reason_code,
                    "message": str(error),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 2
    except RequestLimitExceededError as error:
        print(
            json.dumps(
                {
                    "status": "gate_refused",
                    "reason_code": "stage_request_limit",
                    "message": str(error),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["status"] in {"preflight", "complete", "complete_with_quarantine"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
