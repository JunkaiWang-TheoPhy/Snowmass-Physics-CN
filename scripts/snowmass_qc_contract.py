#!/usr/bin/env python3
"""Hash-bound QC receipts for Snowmass publication gating."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
ALLOWED_KINDS = ("semantic", "structural", "visual")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
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


def _resolve_under_root(article_root: str | Path, raw_path: str | Path, *, label: str) -> tuple[Path, str]:
    root = Path(article_root).resolve()
    candidate = Path(raw_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f"{label} escapes article directory") from error
    return resolved, relative.as_posix()


def _base_receipt_hash(payload: Mapping[str, Any]) -> str:
    sanitized = dict(payload)
    sanitized.pop("receipt_hash", None)
    return _json_sha256(sanitized)


def write_qc_receipt(
    *,
    receipt_path: str | Path,
    article_root: str | Path,
    record_id: str,
    kind: str,
    target_artifact_id: str,
    target_path: str | Path,
    environment_lock_sha256: str,
    contract_version: Any,
    ok: bool,
    evidence_summary: Mapping[str, Any],
) -> dict[str, Any]:
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {ALLOWED_KINDS}")
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("record_id must be non-empty")
    if not isinstance(target_artifact_id, str) or not target_artifact_id.strip():
        raise ValueError("target_artifact_id must be non-empty")
    if not isinstance(environment_lock_sha256, str) or not environment_lock_sha256:
        raise ValueError("environment_lock_sha256 must be non-empty")
    if not isinstance(evidence_summary, Mapping):
        raise ValueError("evidence_summary must be a mapping")

    target_resolved, target_relative = _resolve_under_root(
        article_root,
        target_path,
        label="target path",
    )
    if not target_resolved.is_file():
        raise FileNotFoundError(f"target artifact not found: {target_resolved}")
    receipt_resolved, _ = _resolve_under_root(article_root, receipt_path, label="receipt path")

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id,
        "kind": kind,
        "ok": ok,
        "target": {
            "artifact_id": target_artifact_id,
            "relative_path": target_relative,
            "sha256": _sha256_file(target_resolved),
        },
        "environment_lock_sha256": environment_lock_sha256,
        "contract_version": contract_version,
        "evidence_summary": dict(evidence_summary),
    }
    payload["receipt_hash"] = _base_receipt_hash(payload)
    _atomic_json(receipt_resolved, payload)
    return payload


def validate_qc_receipt(
    receipt_path: str | Path,
    *,
    article_root: str | Path,
    expected_record_id: str | None = None,
    expected_kind: str | None = None,
    expected_target_artifact_id: str | None = None,
    current_environment_lock_sha256: str | None = None,
    required_contract_version: Any | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "ok": False,
        "record_id": None,
        "kind": None,
        "errors": [],
    }
    try:
        receipt_resolved, _ = _resolve_under_root(article_root, receipt_path, label="receipt path")
    except RuntimeError:
        report["errors"].append("receipt_path_escape")
        return report
    try:
        receipt = json.loads(receipt_resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as error:
        report["errors"].append(f"invalid_receipt:{type(error).__name__}")
        return report

    errors: list[str] = []
    report["record_id"] = receipt.get("record_id")
    report["kind"] = receipt.get("kind")

    if receipt.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    kind = receipt.get("kind")
    if kind not in ALLOWED_KINDS:
        errors.append("kind_invalid")
    if expected_kind is not None and kind != expected_kind:
        errors.append("kind_mismatch")
    if expected_record_id is not None and receipt.get("record_id") != expected_record_id:
        errors.append("record_id_mismatch")
    if receipt.get("ok") is not True:
        errors.append("receipt_not_ok")
    if receipt.get("receipt_hash") != _base_receipt_hash(receipt):
        errors.append("receipt_hash_mismatch")
    if (
        current_environment_lock_sha256 is not None
        and receipt.get("environment_lock_sha256") != current_environment_lock_sha256
    ):
        errors.append("environment_lock_sha256_drift")
    if (
        required_contract_version is not None
        and receipt.get("contract_version") != required_contract_version
    ):
        errors.append("contract_version_drift")

    target = receipt.get("target")
    if not isinstance(target, dict):
        errors.append("target_invalid")
    else:
        if expected_target_artifact_id is not None and target.get("artifact_id") != expected_target_artifact_id:
            errors.append("target_artifact_id_mismatch")
        try:
            target_resolved, target_relative = _resolve_under_root(
                article_root,
                target.get("relative_path"),
                label="target path",
            )
        except RuntimeError:
            errors.append("target_path_escape")
        else:
            if not target_resolved.is_file():
                errors.append("target_missing")
            elif target.get("sha256") != _sha256_file(target_resolved):
                errors.append("target_sha256_drift")
            target["relative_path"] = target_relative

    report["ok"] = not errors
    report["errors"] = errors
    report["receipt"] = receipt
    report["target"] = receipt.get("target")
    return report


def validate_publishability_receipts(
    receipt_paths: Iterable[str | Path],
    *,
    article_root: str | Path,
    expected_record_id: str | None = None,
    current_environment_lock_sha256: str | None = None,
    required_contract_version: Any | None = None,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    receipts_by_kind: dict[str, list[dict[str, Any]]] = {}
    for path in receipt_paths:
        report = validate_qc_receipt(
            path,
            article_root=article_root,
            expected_record_id=expected_record_id,
            current_environment_lock_sha256=current_environment_lock_sha256,
            required_contract_version=required_contract_version,
        )
        reports.append(report)
        kind = report.get("kind")
        if isinstance(kind, str):
            receipts_by_kind.setdefault(kind, []).append(report)

    errors: list[str] = []
    for kind in ALLOWED_KINDS:
        count = len(receipts_by_kind.get(kind, []))
        if count == 0:
            errors.append(f"missing_kind:{kind}")
        elif count > 1:
            errors.append(f"duplicate_kind:{kind}")

    for report in reports:
        kind = report.get("kind") if isinstance(report.get("kind"), str) else "<unknown>"
        if report["ok"] is not True:
            errors.extend(f"{kind}:{error}" for error in report["errors"])

    if not errors:
        unique_reports = [receipts_by_kind[kind][0] for kind in ALLOWED_KINDS]
        targets = {
            (
                report["target"]["artifact_id"],
                report["target"]["relative_path"],
                report["target"]["sha256"],
            )
            for report in unique_reports
        }
        environments = {
            report["receipt"]["environment_lock_sha256"] for report in unique_reports
        }
        record_ids = {report["receipt"]["record_id"] for report in unique_reports}
        if len(targets) != 1:
            errors.append("target_mismatch_between_receipts")
        if len(environments) != 1:
            errors.append("environment_lock_sha256_mismatch_between_receipts")
        if len(record_ids) != 1:
            errors.append("record_id_mismatch_between_receipts")

    publishable = not errors
    target = None
    if publishable:
        target = dict(receipts_by_kind["semantic"][0]["target"])

    return {
        "ok": publishable,
        "publishable": publishable,
        "record_id": expected_record_id,
        "errors": errors,
        "target": target,
        "receipts": reports,
    }
