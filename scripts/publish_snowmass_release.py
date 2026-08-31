#!/usr/bin/env python3
"""Fail-closed, resumable Snowmass publication orchestrator."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from typing import Any, Callable, Mapping, Optional, Sequence


PLAN_SCHEMA_VERSION = 1
PLAN_FILENAME = "release-plan.json"
STATE_FILENAME = "release-state.json"
LOCK_FILENAME = "apply.lock"
SEAL_FILENAME = "release-seal.json"
RELEASE_PHASES = (
    "preflight_public_repo",
    "create_draft_release",
    "upload_assets",
    "verify_assets",
    "publish_release",
    "update_registry",
    "sync_public_repo",
    "build_public_manifest",
    "run_public_tests",
    "commit_push_publication",
    "deploy_netlify",
    "verify_publication",
    "write_release_seal",
)
REQUIRED_QC_KEYS = ("semantic", "structural", "visual")
DEFAULT_PUBLICATION_FILES = (
    "translations/snowmass-publications.json",
    "site/data/papers.json",
    "site/data/stats.json",
)

CommandRunner = Callable[[str, dict[str, object], Optional[Path]], Mapping[str, Any]]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json_sha256(value: Any) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _atomic_write_json(path: Path, value: Any) -> None:
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


def _positive_finite_number(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    if int(value) != value:
        raise ValueError(f"{name} must be a positive finite integer")
    return int(value)


def _normalize_limits(limits: Mapping[str, Any]) -> dict[str, int]:
    required = (
        "max_assets",
        "expected_public_manifest_count",
        "expected_homepage_download_count",
    )
    normalized: dict[str, int] = {}
    for key in required:
        if key not in limits:
            raise ValueError(f"Missing limit: {key}")
        normalized[key] = _positive_finite_number(key, limits[key])
    return normalized


def _normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _plan_path(state_dir: str | Path) -> Path:
    return _normalize_path(state_dir) / PLAN_FILENAME


def _state_path(state_dir: str | Path) -> Path:
    return _normalize_path(state_dir) / STATE_FILENAME


def _lock_path(state_dir: str | Path) -> Path:
    return _normalize_path(state_dir) / LOCK_FILENAME


def _seal_path(state_dir: str | Path) -> Path:
    return _normalize_path(state_dir) / SEAL_FILENAME


def _ensure_file(path: Path, *, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")


def _portable_path(root: Path, path: Path, *, label: str) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError as error:
        raise RuntimeError(f"{label} must stay under {root}") from error


def _ensure_sha256(label: str, value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return text


def _load_rights_index(path: Path) -> dict[str, Mapping[str, Any]]:
    payload = _load_json(path)
    records = payload.get("papers") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("Rights manifest must be a list or contain a papers list")
    index: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("record_id", "")).strip()
        if not record_id:
            continue
        if record_id in index:
            raise ValueError(f"duplicate rights record_id: {record_id}")
        index[record_id] = record
    return index


def _load_registry(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if not isinstance(payload, list):
        raise ValueError("Private translation registry must be a list")
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for record in payload:
        if not isinstance(record, dict):
            raise ValueError("Private translation registry entries must be objects")
        record_id = str(record.get("record_id", "")).strip()
        if not record_id:
            raise ValueError("Private translation registry entries must include record_id")
        if record_id in seen:
            raise ValueError(f"duplicate private registry record_id: {record_id}")
        seen.add(record_id)
        records.append(record)
    return records


def _validate_published_registry_metadata(records: Sequence[Mapping[str, Any]]) -> None:
    for record in records:
        if record.get("publication_translation_url"):
            if not str(record.get("machine_model", "")).strip():
                raise RuntimeError(
                    f"published registry row missing machine_model: {record.get('record_id')}"
                )
            if not str(record.get("translation_license", "")).strip():
                raise RuntimeError(
                    f"published registry row missing translation_license: {record.get('record_id')}"
                )


def _matching_paper_seals(
    evidence_root: Path,
    *,
    record_id: str,
    protected_pdf_sha256: str,
    qc_receipt_hashes: Mapping[str, Any],
) -> list[tuple[Path, dict[str, Any]]]:
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(evidence_root.rglob("qc/paper-seal.json")):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        if payload.get("passed") is not True:
            continue
        if str(payload.get("record_id", "")).strip() != record_id:
            continue
        if str(payload.get("protected_pdf_sha256", "")).strip() != protected_pdf_sha256:
            continue
        if payload.get("qc_receipt_hashes") != dict(qc_receipt_hashes):
            continue
        matches.append((path, payload))
    return matches


def _validate_evidence_bundle(
    evidence_root: Path,
    *,
    record_id: str,
    source_pdf_sha256: str,
    qc_receipt_hashes: Mapping[str, Any],
) -> dict[str, Any]:
    matches = _matching_paper_seals(
        evidence_root,
        record_id=record_id,
        protected_pdf_sha256=source_pdf_sha256,
        qc_receipt_hashes=qc_receipt_hashes,
    )
    if not matches:
        raise RuntimeError(f"paper seal evidence missing or stale for {record_id}")
    if len(matches) != 1:
        raise RuntimeError(f"multiple matching paper seals for {record_id}")
    paper_seal_path, paper_seal = matches[0]
    paper_root = paper_seal_path.parent.parent
    evidence: dict[str, Any] = {
        "paper_seal_path": _portable_path(evidence_root, paper_seal_path, label="paper seal"),
        "paper_seal_sha256": _sha256_file(paper_seal_path),
        "paper_root": _portable_path(evidence_root, paper_root, label="paper root"),
        "qc_receipts": {},
    }
    for kind in REQUIRED_QC_KEYS:
        receipt_path = paper_seal_path.parent / f"{kind}.json"
        if not receipt_path.is_file():
            raise RuntimeError(f"{kind} evidence receipt missing for {record_id}")
        actual_hash = _sha256_file(receipt_path)
        if actual_hash != qc_receipt_hashes[kind]:
            raise RuntimeError(f"{kind} evidence hash mismatch for {record_id}")
        evidence["qc_receipts"][kind] = {
            "path": _portable_path(evidence_root, receipt_path, label=f"{kind} evidence receipt"),
            "sha256": actual_hash,
        }
    artifact_manifest_path = paper_root / "artifact-manifest.json"
    if not artifact_manifest_path.is_file():
        raise RuntimeError(f"artifact manifest missing for {record_id}")
    artifact_manifest_sha = _sha256_file(artifact_manifest_path)
    if artifact_manifest_sha != str(paper_seal.get("artifact_manifest_sha256", "")).strip():
        raise RuntimeError(f"artifact manifest hash mismatch for {record_id}")
    evidence["artifact_manifest_path"] = _portable_path(
        evidence_root,
        artifact_manifest_path,
        label="artifact manifest",
    )
    evidence["artifact_manifest_sha256"] = artifact_manifest_sha
    environment_lock_path = paper_root / "environment-lock.json"
    if not environment_lock_path.is_file():
        raise RuntimeError(f"environment lock missing for {record_id}")
    environment_lock = _load_json(environment_lock_path)
    if not isinstance(environment_lock, dict):
        raise RuntimeError(f"environment lock is unreadable for {record_id}")
    if str(environment_lock.get("lock_sha256", "")).strip() != str(
        paper_seal.get("environment_lock_sha256", "")
    ).strip():
        raise RuntimeError(f"environment lock identity mismatch for {record_id}")
    environment_lock_sha = _sha256_file(environment_lock_path)
    if environment_lock_sha != str(paper_seal.get("environment_lock_file_sha256", "")).strip():
        raise RuntimeError(f"environment lock file hash mismatch for {record_id}")
    evidence["environment_lock_path"] = _portable_path(
        evidence_root,
        environment_lock_path,
        label="environment lock",
    )
    evidence["environment_lock_file_sha256"] = environment_lock_sha
    evidence["environment_lock_sha256"] = str(environment_lock["lock_sha256"])
    return evidence


def _validate_receipt(
    receipt_path: Path,
    *,
    rights_index: Mapping[str, Mapping[str, Any]],
    github_repo: str,
    github_tag: str,
    evidence_root: Path,
    machine_model: str,
    translation_license: str,
) -> dict[str, Any]:
    receipt = _load_json(receipt_path)
    if not isinstance(receipt, dict):
        raise ValueError(f"Receipt must be a JSON object: {receipt_path}")
    if receipt.get("packaging_contract_version") != 4:
        raise ValueError(f"Receipt must use packaging contract v4: {receipt_path}")
    record_id = str(receipt.get("record_id", "")).strip()
    if not record_id:
        raise ValueError(f"Receipt record_id is required: {receipt_path}")
    rights = rights_index.get(record_id)
    if rights is None:
        raise ValueError(f"Rights record not found for {record_id}")
    if rights.get("publication_allowed") is not True:
        raise ValueError(f"{record_id}: publication_allowed must be literal true")
    asset_name = str(receipt.get("output_pdf_path", "")).strip()
    if not asset_name:
        raise ValueError(f"{record_id}: output_pdf_path is required")
    asset_path = Path(asset_name)
    if asset_path.is_absolute() or asset_path.name != asset_name:
        raise ValueError(f"{record_id}: output_pdf_path must be a portable basename")
    pdf_path = receipt_path.parent / asset_name
    _ensure_file(pdf_path, label=f"Packaged PDF for {record_id}")
    actual_pdf_sha = _sha256_file(pdf_path)
    source_pdf_sha256 = _ensure_sha256(f"{record_id}: source_pdf_sha256", receipt.get("source_pdf_sha256"))
    expected_pdf_sha = _ensure_sha256(f"{record_id}: packaged_pdf_sha256", receipt.get("packaged_pdf_sha256"))
    if actual_pdf_sha != expected_pdf_sha:
        raise ValueError(f"{record_id}: packaged PDF hash mismatch")
    qc_receipt_hashes = receipt.get("qc_receipt_hashes")
    if not isinstance(qc_receipt_hashes, dict) or tuple(sorted(qc_receipt_hashes)) != REQUIRED_QC_KEYS:
        raise ValueError(f"{record_id}: semantic, structural, and visual QC receipt hashes are required")
    for key in REQUIRED_QC_KEYS:
        _ensure_sha256(f"{record_id}: QC receipt hash {key}", qc_receipt_hashes.get(key))
    evidence = _validate_evidence_bundle(
        evidence_root,
        record_id=record_id,
        source_pdf_sha256=source_pdf_sha256,
        qc_receipt_hashes=qc_receipt_hashes,
    )
    return {
        "record_id": record_id,
        "asset_name": asset_name,
        "asset_size_bytes": pdf_path.stat().st_size,
        "pdf_path": str(pdf_path),
        "packaged_pdf_sha256": actual_pdf_sha,
        "source_pdf_sha256": source_pdf_sha256,
        "receipt_path": str(receipt_path),
        "receipt_sha256": _sha256_file(receipt_path),
        "translation_version": receipt.get("version"),
        "machine_model": machine_model,
        "translation_license": translation_license,
        "qc_receipt_hashes": {key: qc_receipt_hashes[key] for key in REQUIRED_QC_KEYS},
        "evidence": evidence,
        "release_url": (
            f"https://github.com/{github_repo}/releases/download/{github_tag}/{asset_name}"
        ),
    }


def _build_plan_payload(
    *,
    receipt_paths: Sequence[Path],
    rights_manifest_path: Path,
    private_registry_path: Path,
    public_repo_path: Path,
    github_repo: str,
    github_tag: str,
    netlify_site: str,
    state_dir: Path,
    evidence_root: Path,
    machine_model: str,
    translation_license: str,
    limits: Mapping[str, Any],
) -> dict[str, Any]:
    if not github_repo.strip():
        raise ValueError("github_repo is required")
    if not github_tag.strip():
        raise ValueError("github_tag is required")
    if not netlify_site.strip():
        raise ValueError("netlify_site is required")
    machine_model = machine_model.strip()
    if not machine_model:
        raise ValueError("machine_model is required")
    translation_license = translation_license.strip()
    if not translation_license:
        raise ValueError("translation_license is required")
    _ensure_file(rights_manifest_path, label="Rights manifest")
    _ensure_file(private_registry_path, label="Private translation registry")
    if not public_repo_path.is_dir():
        raise FileNotFoundError(f"Public repo path not found: {public_repo_path}")
    if not evidence_root.is_dir():
        raise FileNotFoundError(f"Evidence root not found: {evidence_root}")
    normalized_limits = _normalize_limits(limits)
    rights_index = _load_rights_index(rights_manifest_path)
    registry_records = _load_registry(private_registry_path)
    _validate_published_registry_metadata(registry_records)
    if not receipt_paths:
        raise ValueError("At least one receipt is required")
    records = [
        _validate_receipt(
            path,
            rights_index=rights_index,
            github_repo=github_repo,
            github_tag=github_tag,
            evidence_root=evidence_root,
            machine_model=machine_model,
            translation_license=translation_license,
        )
        for path in sorted({_normalize_path(path) for path in receipt_paths}, key=str)
    ]
    seen_record_ids: set[str] = set()
    seen_asset_names: set[str] = set()
    for record in records:
        if record["record_id"] in seen_record_ids:
            raise ValueError(f"duplicate planned record_id: {record['record_id']}")
        if record["asset_name"] in seen_asset_names:
            raise ValueError(f"duplicate planned asset name: {record['asset_name']}")
        seen_record_ids.add(str(record["record_id"]))
        seen_asset_names.add(str(record["asset_name"]))
    if len(records) > normalized_limits["max_assets"]:
        raise ValueError("Planned asset count exceeds max_assets")
    records = sorted(records, key=lambda item: (str(item["record_id"]), str(item["asset_name"])))
    plan_path = state_dir / PLAN_FILENAME
    payload: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "github_repo": github_repo,
        "github_tag": github_tag,
        "netlify_site": netlify_site,
        "rights_manifest_path": str(rights_manifest_path),
        "private_registry_path": str(private_registry_path),
        "public_repo_path": str(public_repo_path),
        "state_dir": str(state_dir),
        "plan_path": str(plan_path),
        "evidence_root": str(evidence_root),
        "machine_model": machine_model,
        "translation_license": translation_license,
        "limits": normalized_limits,
        "public_files": list(DEFAULT_PUBLICATION_FILES),
        "input_fingerprints": {
            "rights_manifest_sha256": _sha256_file(rights_manifest_path),
            "private_registry_sha256": _sha256_file(private_registry_path),
            "receipts": [
                {
                    "record_id": record["record_id"],
                    "receipt_path": record["receipt_path"],
                    "receipt_sha256": record["receipt_sha256"],
                    "pdf_path": record["pdf_path"],
                    "pdf_sha256": record["packaged_pdf_sha256"],
                    "pdf_size_bytes": record["asset_size_bytes"],
                    "paper_seal_sha256": record["evidence"]["paper_seal_sha256"],
                    "artifact_manifest_sha256": record["evidence"]["artifact_manifest_sha256"],
                    "environment_lock_file_sha256": record["evidence"]["environment_lock_file_sha256"],
                    "environment_lock_sha256": record["evidence"]["environment_lock_sha256"],
                    "qc_receipts": {
                        key: value["sha256"] for key, value in record["evidence"]["qc_receipts"].items()
                    },
                }
                for record in records
            ],
        },
        "input_snapshots": {
            "private_registry_records": registry_records,
        },
        "records": records,
    }
    payload["plan_sha256"] = _json_sha256({key: value for key, value in payload.items() if key != "plan_sha256"})
    return payload


def build_release_plan(
    *,
    receipt_paths: Sequence[str | Path],
    rights_manifest_path: str | Path,
    private_registry_path: str | Path,
    public_repo_path: str | Path,
    github_repo: str,
    github_tag: str,
    netlify_site: str,
    state_dir: str | Path,
    evidence_root: str | Path,
    machine_model: str,
    translation_license: str,
    limits: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate inputs and write a deterministic release plan."""

    plan_payload = _build_plan_payload(
        receipt_paths=[_normalize_path(path) for path in receipt_paths],
        rights_manifest_path=_normalize_path(rights_manifest_path),
        private_registry_path=_normalize_path(private_registry_path),
        public_repo_path=_normalize_path(public_repo_path),
        github_repo=github_repo,
        github_tag=github_tag,
        netlify_site=netlify_site,
        state_dir=_normalize_path(state_dir),
        evidence_root=_normalize_path(evidence_root),
        machine_model=machine_model,
        translation_license=translation_license,
        limits=limits,
    )
    _atomic_write_json(Path(str(plan_payload["plan_path"])), plan_payload)
    return plan_payload


def _validate_plan_document(plan: Mapping[str, Any]) -> None:
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError("Unsupported plan schema version")
    expected_hash = _json_sha256({key: value for key, value in plan.items() if key != "plan_sha256"})
    if plan.get("plan_sha256") != expected_hash:
        raise ValueError("Plan content hash mismatch")
    if tuple(plan.get("public_files") or ()) != DEFAULT_PUBLICATION_FILES:
        raise ValueError("Plan public file set mismatch")
    _normalize_limits(plan.get("limits") or {})
    records = plan.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("Plan must include at least one record")


def _planned_registry_records(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    baseline = plan.get("input_snapshots", {}).get("private_registry_records")
    if not isinstance(baseline, list):
        raise ValueError("Plan is missing the private registry snapshot")
    by_record_id = {}
    for record in baseline:
        if not isinstance(record, dict):
            raise ValueError("Plan private registry snapshot is invalid")
        record_id = str(record.get("record_id", "")).strip()
        if not record_id:
            raise ValueError("Plan private registry snapshot is invalid")
        by_record_id[record_id] = dict(record)
    publication_date = _publication_date_from_tag(str(plan["github_tag"]))
    for record in plan["records"]:
        record_id = str(record["record_id"])
        current = by_record_id.get(record_id, {"record_id": record_id})
        current["translation_status"] = current.get("translation_status") or "machine-draft"
        current["human_reviewers"] = list(current.get("human_reviewers") or [])
        current["translation_version"] = record.get("translation_version")
        current["translation_published_at"] = current.get("translation_published_at") or publication_date
        current["machine_model"] = record["machine_model"]
        current["translation_license"] = record["translation_license"]
        current["publication_translation_url"] = record["release_url"]
        current["publication_translation_sha256"] = record["packaged_pdf_sha256"]
        current["publication_translation_size_bytes"] = record["asset_size_bytes"]
        by_record_id[record_id] = current
    ordered = [by_record_id[key] for key in sorted(by_record_id)]
    _validate_published_registry_metadata(ordered)
    return ordered


def _revalidate_plan_inputs(plan: Mapping[str, Any], state: Mapping[str, Any] | None = None) -> None:
    rights_manifest_path = _normalize_path(str(plan["rights_manifest_path"]))
    private_registry_path = _normalize_path(str(plan["private_registry_path"]))
    current_rights_sha = _sha256_file(rights_manifest_path)
    if current_rights_sha != plan["input_fingerprints"]["rights_manifest_sha256"]:
        raise RuntimeError(f"fingerprint mismatch: {rights_manifest_path}")
    rights_index = _load_rights_index(rights_manifest_path)
    github_repo = str(plan["github_repo"])
    github_tag = str(plan["github_tag"])
    evidence_root = _normalize_path(str(plan["evidence_root"]))
    completed = set((state or {}).get("completed_phases") or [])
    if "update_registry" in completed:
        if _load_registry(private_registry_path) != _planned_registry_records(plan):
            raise RuntimeError(f"fingerprint mismatch: {private_registry_path}")
    else:
        current_registry_sha = _sha256_file(private_registry_path)
        if current_registry_sha != plan["input_fingerprints"]["private_registry_sha256"]:
            raise RuntimeError(f"fingerprint mismatch: {private_registry_path}")
    expected_receipts = {
        (entry["record_id"], entry["receipt_path"]): entry
        for entry in plan["input_fingerprints"]["receipts"]
    }
    current_records = plan["records"]
    for record in current_records:
        key = (record["record_id"], record["receipt_path"])
        expected = expected_receipts[key]
        receipt_path = _normalize_path(str(record["receipt_path"]))
        pdf_path = _normalize_path(str(record["pdf_path"]))
        if _sha256_file(receipt_path) != expected["receipt_sha256"]:
            raise RuntimeError(f"fingerprint mismatch: {receipt_path}")
        if _sha256_file(pdf_path) != expected["pdf_sha256"]:
            raise RuntimeError(f"fingerprint mismatch: {pdf_path}")
        if pdf_path.stat().st_size != expected["pdf_size_bytes"]:
            raise RuntimeError(f"fingerprint mismatch: {pdf_path}")
        refreshed = _validate_receipt(
            receipt_path,
            rights_index=rights_index,
            github_repo=github_repo,
            github_tag=github_tag,
            evidence_root=evidence_root,
            machine_model=str(plan["machine_model"]),
            translation_license=str(plan["translation_license"]),
        )
        if refreshed["release_url"] != record["release_url"]:
            raise RuntimeError(f"fingerprint mismatch: {receipt_path}")
        if refreshed["machine_model"] != record["machine_model"]:
            raise RuntimeError(f"fingerprint mismatch: {receipt_path}")
        if refreshed["translation_license"] != record["translation_license"]:
            raise RuntimeError(f"fingerprint mismatch: {receipt_path}")
        if refreshed["evidence"] != record["evidence"]:
            raise RuntimeError(f"fingerprint mismatch: {receipt_path}")
        if refreshed["evidence"]["paper_seal_sha256"] != expected["paper_seal_sha256"]:
            raise RuntimeError(f"fingerprint mismatch: {receipt_path}")


def load_validated_plan(plan_path: str | Path) -> dict[str, Any]:
    """Load a plan from disk and validate its static content."""

    plan = _load_json(plan_path)
    if not isinstance(plan, dict):
        raise ValueError("Plan must be a JSON object")
    _validate_plan_document(plan)
    return plan


def _publication_date_from_tag(github_tag: str) -> str | None:
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", github_tag)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _state_template(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "plan_path": plan["plan_path"],
        "plan_sha256": plan["plan_sha256"],
        "completed_phases": [],
        "phase_results": {},
        "last_error": None,
    }


def _load_or_init_state(state_dir: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    state_path = _state_path(state_dir)
    if not state_path.exists():
        return _state_template(plan)
    state = _load_json(state_path)
    if not isinstance(state, dict):
        raise ValueError("Release state must be a JSON object")
    if state.get("plan_path") != plan["plan_path"] or state.get("plan_sha256") != plan["plan_sha256"]:
        raise RuntimeError("Existing release state belongs to a different plan")
    completed = state.get("completed_phases") or []
    if not isinstance(completed, list) or any(phase not in RELEASE_PHASES for phase in completed):
        raise ValueError("Release state completed_phases is invalid")
    phase_results = state.get("phase_results") or {}
    if not isinstance(phase_results, dict):
        raise ValueError("Release state phase_results is invalid")
    return {
        "plan_path": plan["plan_path"],
        "plan_sha256": plan["plan_sha256"],
        "completed_phases": list(completed),
        "phase_results": dict(phase_results),
        "last_error": state.get("last_error"),
    }


def _save_state(state_dir: Path, state: Mapping[str, Any]) -> None:
    _atomic_write_json(_state_path(state_dir), state)


def _phase_done(state: Mapping[str, Any], phase: str) -> bool:
    return phase in set(state.get("completed_phases") or [])


def _record_phase_success(state: dict[str, Any], phase: str, result: Mapping[str, Any]) -> None:
    if phase not in state["completed_phases"]:
        state["completed_phases"].append(phase)
    state["phase_results"][phase] = dict(result)
    state["last_error"] = None


def _record_phase_failure(state: dict[str, Any], phase: str, error: BaseException) -> None:
    state["last_error"] = {
        "phase": phase,
        "message": str(error),
        "type": error.__class__.__name__,
    }


def _run_command(
    command_runner: CommandRunner,
    step: str,
    payload: dict[str, object],
    *,
    cwd: Path | None = None,
) -> Mapping[str, Any]:
    result = command_runner(step, payload, cwd)
    if result is None:
        return {}
    if not isinstance(result, Mapping):
        raise RuntimeError(f"Command runner returned a non-mapping for {step}")
    return result


def _validate_release_asset(record: Mapping[str, Any], asset: Mapping[str, Any]) -> dict[str, Any]:
    asset_name = str(record["asset_name"])
    if int(asset.get("size", -1)) != int(record["asset_size_bytes"]):
        raise RuntimeError(f"release asset size mismatch for {asset_name}")
    digest = str(asset.get("digest", "")).strip()
    expected_digest = f"sha256:{record['packaged_pdf_sha256']}"
    if digest != expected_digest:
        raise RuntimeError(f"release asset digest mismatch for {asset_name}")
    if str(asset.get("state", "")).strip() != "uploaded":
        raise RuntimeError(f"release asset state mismatch for {asset_name}")
    browser_url = str(asset.get("browser_download_url", "")).strip()
    if browser_url != str(record["release_url"]):
        raise RuntimeError(f"release asset URL mismatch for {asset_name}")
    return {
        "name": asset_name,
        "size": int(asset["size"]),
        "digest": digest,
        "state": "uploaded",
        "browser_download_url": browser_url,
    }


def _phase_preflight_public_repo(plan: Mapping[str, Any], command_runner: CommandRunner) -> Mapping[str, Any]:
    public_repo_path = _normalize_path(str(plan["public_repo_path"]))
    clean = _run_command(
        command_runner,
        "public_repo.assert_clean",
        {"public_repo_path": str(public_repo_path)},
        cwd=public_repo_path,
    )
    if clean.get("clean") is not True:
        raise RuntimeError("public repo is not clean")
    return {"clean": True}


def _release_assets_by_name(result: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    assets = result.get("assets") or []
    if not isinstance(assets, list):
        raise RuntimeError("Release asset list is invalid")
    by_name: dict[str, Mapping[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, Mapping):
            raise RuntimeError("Release asset entry is invalid")
        name = str(asset.get("name", "")).strip()
        if name:
            by_name[name] = asset
    return by_name


def _phase_create_draft_release(plan: Mapping[str, Any], command_runner: CommandRunner) -> Mapping[str, Any]:
    release = _run_command(
        command_runner,
        "github.release.get",
        {"github_repo": plan["github_repo"], "github_tag": plan["github_tag"]},
    )
    if not release.get("exists"):
        _run_command(
            command_runner,
            "github.release.create_draft",
            {"github_repo": plan["github_repo"], "github_tag": plan["github_tag"]},
        )
        release = _run_command(
            command_runner,
            "github.release.get",
            {"github_repo": plan["github_repo"], "github_tag": plan["github_tag"]},
        )
    return {"exists": bool(release.get("exists")), "is_draft": bool(release.get("is_draft", True))}


def _phase_upload_assets(plan: Mapping[str, Any], command_runner: CommandRunner) -> Mapping[str, Any]:
    release = _run_command(
        command_runner,
        "github.release.get",
        {"github_repo": plan["github_repo"], "github_tag": plan["github_tag"]},
    )
    if not release.get("exists"):
        raise RuntimeError("Draft release does not exist")
    assets = _release_assets_by_name(release)
    uploaded: list[str] = []
    skipped: list[str] = []
    for record in plan["records"]:
        asset_name = str(record["asset_name"])
        expected_size = int(record["asset_size_bytes"])
        existing = assets.get(asset_name)
        if existing is not None:
            _validate_release_asset(record, existing)
            skipped.append(asset_name)
            continue
        _run_command(
            command_runner,
            "github.release.upload_asset",
            {
                "github_repo": plan["github_repo"],
                "github_tag": plan["github_tag"],
                "asset_name": asset_name,
                "asset_path": record["pdf_path"],
            },
        )
        uploaded.append(asset_name)
    return {"uploaded": uploaded, "skipped": skipped, "planned_count": len(plan["records"])}


def _phase_verify_assets(plan: Mapping[str, Any], command_runner: CommandRunner) -> Mapping[str, Any]:
    release = _run_command(
        command_runner,
        "github.release.get",
        {"github_repo": plan["github_repo"], "github_tag": plan["github_tag"]},
    )
    assets = _release_assets_by_name(release)
    inventory: list[dict[str, Any]] = []
    for record in plan["records"]:
        asset_name = str(record["asset_name"])
        existing = assets.get(asset_name)
        if existing is None:
            raise RuntimeError(f"release asset missing after upload: {asset_name}")
        inventory.append(_validate_release_asset(record, existing))
    return {"verified": [item["name"] for item in inventory], "inventory": inventory}


def _phase_publish_release(plan: Mapping[str, Any], command_runner: CommandRunner) -> Mapping[str, Any]:
    release = _run_command(
        command_runner,
        "github.release.get",
        {"github_repo": plan["github_repo"], "github_tag": plan["github_tag"]},
    )
    if not release.get("exists"):
        raise RuntimeError("Cannot publish a missing release")
    if release.get("is_draft") is False:
        return {"published": True, "already_published": True}
    result = _run_command(
        command_runner,
        "github.release.publish",
        {"github_repo": plan["github_repo"], "github_tag": plan["github_tag"]},
    )
    return {"published": bool(result.get("published", True)), "already_published": False}


def _upsert_registry_entries(plan: Mapping[str, Any]) -> Mapping[str, Any]:
    registry_path = _normalize_path(str(plan["private_registry_path"]))
    ordered = _planned_registry_records(plan)
    _atomic_write_json(registry_path, ordered)
    return {"updated_records": [record["record_id"] for record in plan["records"]]}


def _copy_registry_to_public_repo(plan: Mapping[str, Any], command_runner: CommandRunner) -> Mapping[str, Any]:
    public_repo_path = _normalize_path(str(plan["public_repo_path"]))
    registry_source = _normalize_path(str(plan["private_registry_path"]))
    registry_target = public_repo_path / "translations" / "snowmass-publications.json"
    registry_target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=registry_target.name + ".", dir=registry_target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(registry_source.read_text(encoding="utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, registry_target)
        directory = os.open(registry_target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return {"synced_registry": str(registry_target)}


def _phase_build_public_manifest(plan: Mapping[str, Any], command_runner: CommandRunner) -> Mapping[str, Any]:
    public_repo_path = _normalize_path(str(plan["public_repo_path"]))
    return _run_command(
        command_runner,
        "public_repo.build_manifest",
        {
            "public_repo_path": str(public_repo_path),
            "registry_path": str(public_repo_path / "translations" / "snowmass-publications.json"),
        },
        cwd=public_repo_path,
    )


def _phase_run_public_tests(plan: Mapping[str, Any], command_runner: CommandRunner) -> Mapping[str, Any]:
    public_repo_path = _normalize_path(str(plan["public_repo_path"]))
    return _run_command(
        command_runner,
        "public_repo.run_tests",
        {
            "public_repo_path": str(public_repo_path),
            "suites": [
                "scripts.test_public_manifest",
                "scripts.test_site_interface",
                "scripts.test_community_pages",
            ],
        },
        cwd=public_repo_path,
    )


def _phase_commit_push_publication(plan: Mapping[str, Any], command_runner: CommandRunner) -> Mapping[str, Any]:
    public_repo_path = _normalize_path(str(plan["public_repo_path"]))
    changed = _run_command(
        command_runner,
        "public_repo.changed_files",
        {"public_repo_path": str(public_repo_path)},
        cwd=public_repo_path,
    )
    files = sorted(str(item) for item in (changed.get("files") or []))
    expected = sorted(str(item) for item in (plan.get("public_files") or []))
    if any(path not in expected for path in files):
        raise RuntimeError(f"public repo changed file set mismatch: {files}")
    if not files:
        current = _run_command(
            command_runner,
            "public_repo.current_commit",
            {"public_repo_path": str(public_repo_path)},
            cwd=public_repo_path,
        )
        return {
            "ok": True,
            "files": [],
            "noop": True,
            "commit_sha": current["commit_sha"],
        }
    return _run_command(
        command_runner,
        "public_repo.commit_push",
        {
            "public_repo_path": str(public_repo_path),
            "files": files,
            "github_tag": plan["github_tag"],
        },
        cwd=public_repo_path,
    )


def _phase_deploy_netlify(plan: Mapping[str, Any], command_runner: CommandRunner) -> Mapping[str, Any]:
    public_repo_path = _normalize_path(str(plan["public_repo_path"]))
    return _run_command(
        command_runner,
        "netlify.deploy",
        {
            "public_repo_path": str(public_repo_path),
            "netlify_site": plan["netlify_site"],
            "github_tag": plan["github_tag"],
            "message": f"Publish Snowmass release {plan['github_tag']}",
        },
        cwd=public_repo_path,
    )


def _phase_verify_publication(plan: Mapping[str, Any], command_runner: CommandRunner) -> Mapping[str, Any]:
    public_repo_path = _normalize_path(str(plan["public_repo_path"]))
    result = _run_command(
        command_runner,
        "netlify.verify",
        {
            "public_repo_path": str(public_repo_path),
            "netlify_site": plan["netlify_site"],
            "expected_public_manifest_count": plan["limits"]["expected_public_manifest_count"],
            "expected_homepage_download_count": plan["limits"]["expected_homepage_download_count"],
            "planned_records": plan["records"],
        },
        cwd=public_repo_path,
    )
    if result.get("homepage_ok") is not True:
        raise RuntimeError("homepage verification failed")
    if int(result.get("manifest_count", -1)) != int(plan["limits"]["expected_public_manifest_count"]):
        raise RuntimeError("public manifest count mismatch")
    if int(result.get("download_count", -1)) != int(plan["limits"]["expected_homepage_download_count"]):
        raise RuntimeError("homepage download count mismatch")
    return result


def _write_release_seal(state_dir: Path, plan: Mapping[str, Any], state: Mapping[str, Any]) -> Mapping[str, Any]:
    seal_records = [
        {
            "record_id": record["record_id"],
            "asset_name": record["asset_name"],
            "asset_size_bytes": record["asset_size_bytes"],
            "packaged_pdf_sha256": record["packaged_pdf_sha256"],
            "release_url": record["release_url"],
            "source_pdf_sha256": record["source_pdf_sha256"],
            "machine_model": record["machine_model"],
            "translation_license": record["translation_license"],
            "evidence": record["evidence"],
        }
        for record in plan["records"]
    ]
    seal: dict[str, Any] = {
        "plan_path": PLAN_FILENAME,
        "plan_sha256": plan["plan_sha256"],
        "github_repo": plan["github_repo"],
        "github_tag": plan["github_tag"],
        "netlify_site": plan["netlify_site"],
        "records": seal_records,
        "verified_release_inventory": state["phase_results"]["verify_assets"]["inventory"],
        "public_commit_result": state["phase_results"]["commit_push_publication"],
        "netlify_deploy_result": state["phase_results"]["deploy_netlify"],
        "online_verification": state["phase_results"]["verify_publication"],
    }
    seal["seal_sha256"] = _json_sha256({key: value for key, value in seal.items() if key != "seal_sha256"})
    _atomic_write_json(_seal_path(state_dir), seal)
    return {"seal_path": str(_seal_path(state_dir)), "seal_sha256": seal["seal_sha256"]}


def apply_release_plan(
    *,
    plan_path: str | Path,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Apply a previously written release plan with resumable checkpoints."""

    plan = load_validated_plan(plan_path)
    runner = command_runner or ShellCommandRunner()
    state_dir = _normalize_path(str(plan["state_dir"]))
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        state = _load_or_init_state(state_dir, plan)
        _revalidate_plan_inputs(plan, state)
        phase_handlers: dict[str, Callable[[Mapping[str, Any], CommandRunner], Mapping[str, Any]]] = {
            "preflight_public_repo": _phase_preflight_public_repo,
            "create_draft_release": _phase_create_draft_release,
            "upload_assets": _phase_upload_assets,
            "verify_assets": _phase_verify_assets,
            "publish_release": _phase_publish_release,
            "update_registry": lambda current_plan, _: _upsert_registry_entries(current_plan),
            "sync_public_repo": _copy_registry_to_public_repo,
            "build_public_manifest": _phase_build_public_manifest,
            "run_public_tests": _phase_run_public_tests,
            "commit_push_publication": _phase_commit_push_publication,
            "deploy_netlify": _phase_deploy_netlify,
            "verify_publication": _phase_verify_publication,
            "write_release_seal": lambda current_plan, _: _write_release_seal(state_dir, current_plan, state),
        }
        for phase in RELEASE_PHASES:
            if _phase_done(state, phase):
                continue
            try:
                result = phase_handlers[phase](plan, runner)
            except BaseException as error:
                _record_phase_failure(state, phase, error)
                _save_state(state_dir, state)
                raise
            _record_phase_success(state, phase, dict(result))
            _save_state(state_dir, state)
        final_status = read_release_status(state_dir=state_dir)
        return final_status


def read_release_status(*, state_dir: str | Path) -> dict[str, Any]:
    """Return the current plan/state summary without mutating on-disk state."""

    state_dir_path = _normalize_path(state_dir)
    plan_path = _plan_path(state_dir_path)
    state_path = _state_path(state_dir_path)
    plan_exists = plan_path.exists()
    state_exists = state_path.exists()
    plan: dict[str, Any] | None = None
    if plan_exists:
        plan = load_validated_plan(plan_path)
    completed: list[str] = []
    phase_results: dict[str, Any] = {}
    last_error: Any = None
    if state_exists:
        payload = _load_json(state_path)
        if not isinstance(payload, dict):
            raise ValueError("Release state must be a JSON object")
        completed = list(payload.get("completed_phases") or [])
        phase_results = dict(payload.get("phase_results") or {})
        last_error = payload.get("last_error")
    pending = [phase for phase in RELEASE_PHASES if phase not in completed]
    if completed and not pending and last_error is None:
        status = "complete"
    elif last_error is not None:
        status = "failed"
    elif plan_exists:
        status = "not-started" if not completed else "in-progress"
    else:
        status = "missing-plan"
    return {
        "status": status,
        "plan_path": str(plan_path),
        "state_path": str(state_path),
        "plan_exists": plan_exists,
        "state_exists": state_exists,
        "completed_phases": completed,
        "pending_phases": pending,
        "last_error": last_error,
        "phase_results": phase_results,
        "record_count": len(plan["records"]) if plan else 0,
    }


class ShellCommandRunner:
    """Default subprocess-backed runner for live publication steps."""

    def __init__(
        self,
        *,
        python_executable: str = "python3",
        run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.python_executable = python_executable
        self.repo_root = Path(__file__).resolve().parents[1]
        self._run_impl = run_command or self._default_run

    def _netlify_origin(self, value: str) -> str:
        if value.startswith("http://") or value.startswith("https://"):
            return value.rstrip("/")
        if "." in value:
            return f"https://{value.rstrip('/')}"
        return f"https://{value}.netlify.app"

    def __call__(self, step: str, payload: dict[str, object], cwd: Path | None = None) -> Mapping[str, Any]:
        if step == "github.release.get":
            result = self._run(
                [
                    "gh",
                    "api",
                    f"repos/{payload['github_repo']}/releases/tags/{payload['github_tag']}",
                ],
                cwd=cwd,
                allow_failure=True,
            )
            if result.returncode != 0:
                text = (result.stderr or "") + (result.stdout or "")
                if "HTTP 404" in text:
                    return {"exists": False, "is_draft": True, "assets": []}
                raise RuntimeError(text.strip() or f"gh api failed for {payload['github_tag']}")
            data = json.loads(result.stdout or "{}")
            return {
                "exists": True,
                "is_draft": bool(data.get("draft", data.get("isDraft", True))),
                "assets": data.get("assets") or [],
                "url": data.get("url"),
            }
        if step == "github.release.create_draft":
            self._run(
                [
                    "gh",
                    "release",
                    "create",
                    str(payload["github_tag"]),
                    "--repo",
                    str(payload["github_repo"]),
                    "--draft",
                    "--title",
                    str(payload["github_tag"]),
                    "--notes",
                    "",
                ],
                cwd=cwd,
            )
            return {"exists": True, "is_draft": True}
        if step == "github.release.upload_asset":
            self._run(
                [
                    "gh",
                    "release",
                    "upload",
                    str(payload["github_tag"]),
                    str(payload["asset_path"]),
                    "--repo",
                    str(payload["github_repo"]),
                ],
                cwd=cwd,
            )
            return {"uploaded": payload["asset_name"]}
        if step == "github.release.publish":
            self._run(
                [
                    "gh",
                    "release",
                    "edit",
                    str(payload["github_tag"]),
                    "--repo",
                    str(payload["github_repo"]),
                    "--draft=false",
                ],
                cwd=cwd,
            )
            return {"published": True}
        if step == "public_repo.assert_clean":
            result = self._run(["git", "status", "--porcelain"], cwd=cwd)
            return {"clean": not bool((result.stdout or "").strip())}
        if step == "public_repo.build_manifest":
            public_repo = Path(str(payload["public_repo_path"]))
            self._run(
                [
                    self.python_executable,
                    str(public_repo / "scripts" / "build_public_manifest.py"),
                ],
                cwd=cwd,
            )
            return {"ok": True}
        if step == "public_repo.run_tests":
            suites = [str(item) for item in payload.get("suites", [])] or [
                "scripts.test_public_manifest",
                "scripts.test_site_interface",
                "scripts.test_community_pages",
            ]
            for suite in suites:
                self._run([self.python_executable, "-m", "unittest", suite], cwd=cwd)
            return {"ok": True, "suites": suites}
        if step == "public_repo.changed_files":
            result = self._run(
                ["git", "status", "--short", "--untracked-files=no"],
                cwd=cwd,
            )
            files = []
            for line in (result.stdout or "").splitlines():
                if not line.strip():
                    continue
                files.append(line[3:].strip())
            return {"files": sorted(files)}
        if step == "public_repo.commit_push":
            files = [str(item) for item in payload.get("files", [])]
            if files:
                self._run(["git", "add", *files], cwd=cwd)
            message = (
                f"Publish Snowmass translations for {payload['github_tag']}\n\n"
                "Constraint: Publication pipeline must not replace existing release assets\n"
                "Confidence: medium\n"
                "Scope-risk: narrow\n"
                f"Tested: Public publication files for {payload['github_tag']}"
            )
            self._run(["git", "commit", "-m", message], cwd=cwd)
            self._run(["git", "push"], cwd=cwd)
            commit = self._run(["git", "rev-parse", "HEAD"], cwd=cwd)
            return {
                "ok": True,
                "files": files,
                "commit_sha": (commit.stdout or "").strip(),
            }
        if step == "public_repo.current_commit":
            commit = self._run(["git", "rev-parse", "HEAD"], cwd=cwd)
            value = (commit.stdout or "").strip()
            if not re.fullmatch(r"[0-9a-f]{40}", value):
                raise RuntimeError("public repo commit SHA is invalid")
            return {"commit_sha": value}
        if step == "netlify.deploy":
            message = str(payload.get("message") or f"Publish Snowmass release {payload['github_tag']}")
            result = self._run(
                [
                    "netlify",
                    "deploy",
                    "--prod",
                    "--no-build",
                    "--dir",
                    str(Path(str(payload["public_repo_path"])) / "site"),
                    "--site",
                    str(payload["netlify_site"]),
                    "--message",
                    message,
                ],
                cwd=cwd,
            )
            urls = re.findall(r"<(https://[^>]+\.netlify\.app)>", result.stdout or "")
            if len(urls) < 2:
                raise RuntimeError("Netlify deploy output did not contain production and unique URLs")
            return {
                "production_url": urls[0],
                "unique_deploy_url": urls[1],
                "message": message,
            }
        if step == "netlify.verify":
            origin = self._netlify_origin(str(payload["netlify_site"]))
            self._run(["curl", "-fsSL", "--max-time", "30", origin], cwd=cwd)
            manifest_result = self._run(
                ["curl", "-fsSL", "--max-time", "30", f"{origin}/data/papers.json"],
                cwd=cwd,
            )
            manifest = json.loads(manifest_result.stdout or "[]")
            if not isinstance(manifest, list):
                raise RuntimeError("online manifest payload is invalid")
            by_record_id = {
                str(record.get("record_id", "")).strip(): record
                for record in manifest
                if isinstance(record, Mapping)
            }
            download_count = sum(1 for record in manifest if isinstance(record, Mapping) and record.get("publication_translation_url"))
            planned_records = payload.get("planned_records") or []
            verified_records: list[str] = []
            for planned in planned_records:
                if not isinstance(planned, Mapping):
                    raise RuntimeError("planned record payload is invalid")
                online = by_record_id.get(str(planned["record_id"]))
                if online is None:
                    raise RuntimeError(f"online manifest record missing: {planned['record_id']}")
                head_result = self._run(
                    ["curl", "-fsSIL", "--max-time", "30", str(planned["release_url"])],
                    cwd=cwd,
                )
                header_lines = [line.strip() for line in (head_result.stdout or "").splitlines() if line.strip()]
                if not any(line.startswith("HTTP/") and " 200" in line for line in header_lines):
                    raise RuntimeError(f"release URL did not resolve with HTTP 200: {planned['record_id']}")
                content_lengths: list[int] = []
                for line in header_lines:
                    if line.lower().startswith("content-length:"):
                        content_lengths.append(int(line.split(":", 1)[1].strip()))
                content_length = content_lengths[-1] if content_lengths else None
                if content_length != int(planned["asset_size_bytes"]):
                    raise RuntimeError(f"release URL content length mismatch for {planned['record_id']}")
                if online.get("publication_translation_url") != planned["release_url"]:
                    raise RuntimeError(f"online manifest release URL mismatch for {planned['record_id']}")
                if online.get("publication_translation_sha256") != planned["packaged_pdf_sha256"]:
                    raise RuntimeError(f"online manifest hash mismatch for {planned['record_id']}")
                if int(online.get("publication_translation_size_bytes", -1)) != int(planned["asset_size_bytes"]):
                    raise RuntimeError(f"online manifest size mismatch for {planned['record_id']}")
                verified_records.append(str(planned["record_id"]))
            return {
                "homepage_ok": True,
                "manifest_count": len(manifest),
                "download_count": download_count,
                "verified_records": verified_records,
                "origin": origin,
            }
        raise RuntimeError(f"Unsupported command runner step: {step}")

    def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = self._run_impl(list(argv), cwd=cwd, allow_failure=allow_failure)
        if not allow_failure and result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or shlex.join(argv))
        return result

    def _default_run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )


def _parse_limits(args: argparse.Namespace) -> dict[str, int]:
    return {
        "max_assets": args.max_assets,
        "expected_public_manifest_count": args.expected_public_manifest_count,
        "expected_homepage_download_count": args.expected_homepage_download_count,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan_parser = commands.add_parser("plan", help="Validate receipts and write a deterministic release plan")
    plan_parser.add_argument("--receipt", dest="receipt_paths", action="append", required=True)
    plan_parser.add_argument("--rights-manifest", required=True)
    plan_parser.add_argument("--private-registry", required=True)
    plan_parser.add_argument("--public-repo", required=True)
    plan_parser.add_argument("--github-repo", required=True)
    plan_parser.add_argument("--github-tag", required=True)
    plan_parser.add_argument("--netlify-site", required=True)
    plan_parser.add_argument("--state-dir", required=True)
    plan_parser.add_argument("--evidence-root", required=True)
    plan_parser.add_argument("--machine-model", required=True)
    plan_parser.add_argument("--translation-license", required=True)
    plan_parser.add_argument("--max-assets", type=int, required=True)
    plan_parser.add_argument("--expected-public-manifest-count", type=int, required=True)
    plan_parser.add_argument("--expected-homepage-download-count", type=int, required=True)

    apply_parser = commands.add_parser("apply", help="Apply an existing validated release plan")
    apply_parser.add_argument("--state-dir", required=True)

    status_parser = commands.add_parser("status", help="Read current plan/apply status without mutating state")
    status_parser.add_argument("--state-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, command_runner: CommandRunner | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "plan":
        result = build_release_plan(
            receipt_paths=args.receipt_paths,
            rights_manifest_path=args.rights_manifest,
            private_registry_path=args.private_registry,
            public_repo_path=args.public_repo,
            github_repo=args.github_repo,
            github_tag=args.github_tag,
            netlify_site=args.netlify_site,
            state_dir=args.state_dir,
            evidence_root=args.evidence_root,
            machine_model=args.machine_model,
            translation_license=args.translation_license,
            limits=_parse_limits(args),
        )
    elif args.command == "apply":
        result = apply_release_plan(
            plan_path=_plan_path(args.state_dir),
            command_runner=command_runner,
        )
    else:
        result = read_release_status(state_dir=args.state_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
