#!/usr/bin/env python3
"""Prepare the rights-eligible Snowmass production corpus with durable reporting."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path("output/snowmass2021_sources")
OUTPUT_ROOT = Path("output/snowmass2021_translation/production")
PIPELINE_PATH = Path(__file__).with_name("snowmass_pipeline.py")
CHUNK_PREP_PATH = Path(__file__).with_name("prepare_snowmass_chunks.py")

_PIPELINE_SPEC = importlib.util.spec_from_file_location("snowmass_pipeline", PIPELINE_PATH)
assert _PIPELINE_SPEC and _PIPELINE_SPEC.loader
PIPELINE = importlib.util.module_from_spec(_PIPELINE_SPEC)
_PIPELINE_SPEC.loader.exec_module(PIPELINE)

_CHUNK_PREP_SPEC = importlib.util.spec_from_file_location("prepare_snowmass_chunks", CHUNK_PREP_PATH)
assert _CHUNK_PREP_SPEC and _CHUNK_PREP_SPEC.loader
CHUNK_PREP = importlib.util.module_from_spec(_CHUNK_PREP_SPEC)
_CHUNK_PREP_SPEC.loader.exec_module(CHUNK_PREP)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def candidate_to_dict(candidate: Any) -> dict[str, Any]:
    return {
        "path": candidate.path.as_posix(),
        "score": candidate.score,
        "incoming_includes": candidate.incoming_includes,
        "has_document_marker": candidate.has_document_marker,
        "has_title": candidate.has_title,
        "has_abstract": candidate.has_abstract,
    }


def inspect_source_package(record: dict[str, Any], source_root: Path) -> dict[str, Any]:
    item_dir = source_root / str(record["directory"])
    archive_path = item_dir / "source.tar.gz"
    inspection: dict[str, Any] = {
        "source_package_type": None,
        "main_tex_candidates": [],
        "ambiguity_reasons": [],
    }
    if not archive_path.exists():
        return inspection

    try:
        package_type = PIPELINE.detect_source_package(archive_path)
    except Exception:  # noqa: BLE001 - production report preserves chunk-prep fallback behavior
        return inspection

    inspection["source_package_type"] = package_type
    if package_type != "tar":
        return inspection

    with tempfile.TemporaryDirectory(prefix=".snowmass-production-") as temporary:
        extract_root = Path(temporary) / "extract"
        try:
            PIPELINE.safe_extract_source(archive_path, extract_root)
            candidates = [
                candidate for candidate in PIPELINE.rank_main_tex(extract_root) if candidate.has_document_marker
            ]
        except Exception:  # noqa: BLE001 - keep report inspection best-effort
            return inspection

    inspection["main_tex_candidates"] = [candidate_to_dict(candidate) for candidate in candidates]
    if len(candidates) > 1:
        inspection["ambiguity_reasons"] = ["multiple_main_tex_candidates"]
    return inspection


def build_record_report(
    record: dict[str, Any],
    output_root: Path,
    status: dict[str, Any] | None,
    inspection: dict[str, Any],
    *,
    error: str | None = None,
) -> dict[str, Any]:
    article_dir = output_root / str(record["directory"])
    report_path = article_dir / "preparation_record.json"
    article_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "schema_version": 1,
        "record_id": record["record_id"],
        "directory": record["directory"],
        "article_dir": str(article_dir),
        "report_path": str(report_path),
        "updated_at": now(),
        "status": "failed" if error else "complete",
        "reused": bool(status and status.get("reused")),
        "source_kind": status.get("source_kind") if status else None,
        "source_package_type": inspection["source_package_type"],
        "fallback_reason": status.get("fallback_reason") if status else None,
        "chunk_count": int(status.get("chunk_count", 0)) if status else 0,
        "source_hash": status.get("source_hash") if status else None,
        "source_md": status.get("source_md") if status else None,
        "manifest": status.get("manifest") if status else None,
        "main_tex_candidates": inspection["main_tex_candidates"],
        "ambiguity_reasons": list(inspection["ambiguity_reasons"]),
        "error": error,
    }
    if error is None and report["ambiguity_reasons"]:
        report["status"] = "ambiguous"

    atomic_json(report_path, report)
    return report


def prepare_record(record: dict[str, Any], source_root: Path, output_root: Path) -> dict[str, Any]:
    inspection = inspect_source_package(record, source_root)
    try:
        status = CHUNK_PREP.build_one(record, source_root, output_root)
    except Exception as exc:  # noqa: BLE001 - production run continues after per-paper failures
        return build_record_report(record, output_root, None, inspection, error=f"{type(exc).__name__}: {exc}")
    return build_record_report(record, output_root, status, inspection)


def build_summary(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "complete": sum(record.get("status") == "complete" for record in records),
        "ambiguous": sum(record.get("status") == "ambiguous" for record in records),
        "failed": sum(record.get("status") == "failed" for record in records),
        "reused": sum(bool(record.get("reused")) for record in records),
    }


def report_exit_code(records: list[dict[str, Any]]) -> int:
    counts = build_summary(records)
    if counts["ambiguous"] or counts["failed"]:
        return 2
    return 0


def read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_existing_results(
    records: list[dict[str, Any]], output_root: Path
) -> dict[str, dict[str, Any]]:
    previous_report = read_json_object(output_root / "preparation_report.json") or {}
    previous_by_id = {
        str(item["record_id"]): item
        for item in previous_report.get("records", [])
        if isinstance(item, dict) and item.get("record_id") is not None
    }

    results: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record["record_id"])
        record_report_path = output_root / str(record["directory"]) / "preparation_record.json"
        existing = read_json_object(record_report_path)
        if existing is None or str(existing.get("record_id")) != record_id:
            existing = previous_by_id.get(record_id)
        if existing is not None:
            results[record_id] = existing
    return results


def ordered_results(
    records: list[dict[str, Any]], results_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        results_by_id[str(record["record_id"])]
        for record in records
        if str(record["record_id"]) in results_by_id
    ]


def write_preparation_report(
    output_root: Path,
    rights_manifest: Path,
    rights_snapshot: Path,
    source_root: Path,
    eligible_record_count: int,
    records: list[dict[str, Any]],
    *,
    exit_code: int,
    interrupted: bool,
) -> dict[str, Any]:
    report = {
        "schema_version": 1,
        "updated_at": now(),
        "rights_manifest": str(rights_manifest),
        "rights_snapshot": str(rights_snapshot),
        "source_root": str(source_root),
        "source_manifest": str(source_root / "manifest.json"),
        "output_root": str(output_root),
        "eligible_record_count": eligible_record_count,
        "processed_record_count": len(records),
        "interrupted": interrupted,
        "exit_code": exit_code,
        "counts": build_summary(records),
        "records": records,
    }
    atomic_json(output_root / "preparation_report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rights-manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    args = parser.parse_args(argv)

    args.output_root.mkdir(parents=True, exist_ok=True)
    rights_snapshot_path = args.output_root / "rights_snapshot.json"
    PIPELINE.build_rights_snapshot(args.rights_manifest, rights_snapshot_path)
    allowed_record_ids = CHUNK_PREP.load_rights_snapshot_record_ids(rights_snapshot_path)
    records = CHUNK_PREP.load_source_records(args.source_root, allowed_record_ids)

    results_by_id = load_existing_results(records, args.output_root)
    results = ordered_results(records, results_by_id)
    write_preparation_report(
        args.output_root,
        args.rights_manifest,
        rights_snapshot_path,
        args.source_root,
        len(records),
        results,
        exit_code=0,
        interrupted=False,
    )

    try:
        for record in records:
            result = prepare_record(record, args.source_root, args.output_root)
            results_by_id[str(record["record_id"])] = result
            results = ordered_results(records, results_by_id)
            write_preparation_report(
                args.output_root,
                args.rights_manifest,
                rights_snapshot_path,
                args.source_root,
                len(records),
                results,
                exit_code=report_exit_code(results),
                interrupted=False,
            )
    except KeyboardInterrupt:
        write_preparation_report(
            args.output_root,
            args.rights_manifest,
            rights_snapshot_path,
            args.source_root,
            len(records),
            results,
            exit_code=130,
            interrupted=True,
        )
        return 130

    return report_exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
