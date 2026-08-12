#!/usr/bin/env python3
"""Rights-gated, budgeted and resumable Snowmass multi-paper production."""

from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import math
import os
import fcntl
from pathlib import Path
import re
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
from snowmass_batch_budget import (
    AUTHORIZED_PROJECT_MAX_RMB,
    PersistentBudgetGuard,
    validate_budget,
)


DEFAULT_RIGHTS_MANIFEST = ROOT / "site/data/papers.json"
DEFAULT_PDF_ROOT = ROOT / "tmp/pdfs/snowmass2021"
DEFAULT_OUTPUT_ROOT = ROOT / "output/snowmass2021/babeldoc_production"
DEFAULT_CONTROL_DIR = ROOT / "output/snowmass2021/production_control"
DEFAULT_HISTORICAL_ROOTS = (ROOT / "output/snowmass2021/babeldoc_ab_v1",)
STAGE_LIMITS = {"baseline": 1, "pilot10": 10, "batch50": 50}
TERMINAL_STAGES = ("prepared", "translated", "rendered", "qc_passed", "packaged")


class RunAlreadyActiveError(RuntimeError):
    pass


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
    for stage in ("baseline", "pilot10", "batch50"):
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
    if refill_status.get("publication_qc", {}).get("ok") is not True:
        failures.append("publication_qc_failed")
    if refill_status.get("reference_qc", {}).get("verified") is not True:
        failures.append("references_not_verified")
    for prefix in ("figure", "table"):
        count = int(refill_status.get(f"{prefix}_region_count") or 0)
        verified = refill_status.get(f"{prefix}_regions_verified")
        if count > 0 and verified is not True:
            failures.append(f"{prefix}_regions_not_verified")
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
        "through_stage": config.through_stage,
        "translation_version": config.translation_version,
        "packaged_on": config.packaged_on,
    }
    suffix = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return f"{config.stage}-{suffix}"


def _article_dir(config: BatchConfig, record_id: str) -> Path:
    return config.output_root / "papers" / prepare.safe_record_name(record_id)


def _chinese_title_from_manifest(article_dir: Path, manifest: dict[str, Any]) -> str:
    for chunk in sorted(manifest.get("chunks", []), key=lambda item: item.get("order", 0)):
        if chunk.get("layout_label") != "title":
            continue
        translated = _article_path(article_dir, chunk.get("output_file"))
        if translated.is_file() and translated.read_text(encoding="utf-8").strip():
            return " ".join(translated.read_text(encoding="utf-8").split())
    raise RuntimeError("No verified translated title is available for packaging")


def _chinese_title(article_dir: Path) -> str:
    manifest = json.loads((article_dir / "manifest.json").read_text(encoding="utf-8"))
    return _chinese_title_from_manifest(article_dir, manifest)


def _source_character_count(article_dir: Path) -> int:
    manifest = json.loads((article_dir / "manifest.json").read_text(encoding="utf-8"))
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list):
        raise RuntimeError("Article manifest has no chunk list")
    return sum(
        len(_article_path(article_dir, chunk.get("source_file")).read_text(encoding="utf-8"))
        for chunk in chunks
    )


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

    source_characters = sum(max(0, int(result.get("source_characters") or 0)) for result in results)
    stage_spent = float(budget.get("stage_spent_rmb") or 0)
    completed = sum(result.get("status") == through_stage for result in results)
    usage = budget.get("stage_usage") if isinstance(budget.get("stage_usage"), dict) else {}
    api_calls = max(0, int(usage.get("api_calls") or 0))
    uncertain_calls = max(0, int(usage.get("uncertain_calls") or 0))
    unresolved_uncertain_calls = max(
        0,
        int(usage.get("unresolved_uncertain_calls", uncertain_calls) or 0),
    )
    cost_per_article = stage_spent / len(results) if results else None
    cost_per_10k = stage_spent * 10_000 / source_characters if source_characters else None
    historical_before_stage = max(0.0, float(budget.get("project_spent_rmb") or 0) - stage_spent)
    projected_total = (
        historical_before_stage + cost_per_article * eligible_record_count
        if cost_per_article is not None
        else None
    )
    metrics = {
        "source_characters": source_characters,
        "completed_articles": completed,
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
    }
    reasons: list[str] = []
    expected_status = "qc_passed" if through_stage in {"rendered", "qc_passed"} else through_stage
    if failures:
        reasons.append("article_failures")
    if len(results) != selected_count:
        reasons.append("selected_articles_incomplete")
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
    next_stage = {"baseline": "pilot10", "pilot10": "batch50", "batch50": "remainder"}.get(stage)
    gate = {
        "allowed": not reasons and next_stage is not None,
        "next_stage": next_stage,
        "reasons": reasons or (["campaign_complete"] if next_stage is None else []),
    }
    return metrics, gate


def _package_article(config: BatchConfig, record: dict[str, Any], article_dir: Path) -> dict[str, Any]:
    safe_id = prepare.safe_record_name(str(record["record_id"])).replace("arxiv_", "")
    output = article_dir / "packaged" / f"snowmass-{safe_id}.zh-CN.pdf"
    return packager.package_translation_pdf(
        record=record,
        chinese_title=_chinese_title(article_dir),
        source_pdf_path=article_dir / "rendered/translated_mono.pdf",
        output_pdf_path=output,
        version=config.translation_version,
        packaged_on=config.packaged_on,
    )


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
    runtime = Path(first_line[2:].strip())
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
    )
    result["status"] = "translated"
    if config.through_stage == "translated":
        return result
    _refill_article(config, article_dir)
    result["status"] = "rendered"
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
        "through_stage": config.through_stage,
        "translation_version": config.translation_version,
        "packaged_on": config.packaged_on,
    }
    historical_roots = tuple(dict.fromkeys((*config.historical_roots, config.output_root)))
    if config.preflight_only:
        return {**snapshot, "status": "preflight", "historical_spent_rmb": discover_historical_spend(historical_roots, config.usd_cny_rate)}
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
    if run_snapshot_path.is_file():
        if json.loads(run_snapshot_path.read_text(encoding="utf-8")) != snapshot:
            raise RuntimeError(f"Run snapshot collision: {run_id}")
    else:
        _atomic_json(run_snapshot_path, snapshot)
    historical_spent = discover_historical_spend(historical_roots, config.usd_cny_rate)
    stop_event = threading.Event()
    budget = PersistentBudgetGuard(
        config.control_dir,
        project_max_cost_rmb=config.project_max_cost_rmb,
        stage_max_cost_rmb=config.stage_max_cost_rmb,
        run_id=run_id,
        usd_cny_rate=config.usd_cny_rate,
        historical_spent_rmb=historical_spent,
        stop_event=stop_event,
    )
    _prepare_all(config, selected)
    actual_client = client or runner.DeepSeekClient(runner.load_api_key())
    state_lock = threading.Lock()
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    def persist() -> None:
        with state_lock:
            budget_snapshot = budget.snapshot()
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
                    "status": "failed" if failures else ("complete" if len(results) == len(selected) else "running"),
                    "completed": len(results),
                    "failed": len(failures),
                    "results": sorted(results, key=lambda item: item["record_id"]),
                    "failures": sorted(failures, key=lambda item: item["record_id"]),
                    "budget": budget_snapshot,
                    "metrics": metrics,
                    "promotion_gate": promotion_gate,
                },
            )

    persist()
    # A rolling executor limits already-started work; after a failure no new paper is submitted.
    iterator = iter(selected)
    futures: dict[concurrent.futures.Future[dict[str, Any]], dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.article_concurrency) as executor:
        for _ in range(config.article_concurrency):
            try:
                record = next(iterator)
            except StopIteration:
                break
            futures[executor.submit(_run_article, config, record, run_id, actual_client, budget)] = record
        while futures:
            done, _pending = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                record = futures.pop(future)
                try:
                    results.append(future.result())
                except Exception as error:
                    stop_event.set()
                    failures.append(
                        {
                            "record_id": str(record["record_id"]),
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
                persist()
                if failures:
                    continue
                try:
                    next_record = next(iterator)
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
                ] = next_record
    persist()
    summary = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    if failures:
        summary["not_started"] = len(selected) - len(results) - len(failures)
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
    summary = run_batch(config)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["status"] in {"preflight", "complete"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
