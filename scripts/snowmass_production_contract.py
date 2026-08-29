#!/usr/bin/env python3
"""Canonical artifact and environment evidence for Snowmass production."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from contextlib import contextmanager
import fcntl
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
ENVIRONMENT_LOCK_SCHEMA_VERSION = 2
STAGE_SEQUENCE = (
    "prepared",
    "revision_ready",
    "translated",
    "rendered",
    "protected",
    "semantic_qc",
    "structural_qc",
    "visual_qc",
    "packaged",
)
STAGE_RANK = {stage: index for index, stage in enumerate(STAGE_SEQUENCE)}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
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


def _article_path(article_root: Path, relative_path: Any, *, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path or Path(relative_path).is_absolute():
        raise RuntimeError(f"{label} must be a non-empty relative path")
    root = Path(article_root).resolve()
    resolved = (root / relative_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f"{label} escapes article directory: {relative_path}") from error
    return resolved


def _git_rev(root: Path, target: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", target],
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _installed_packages() -> dict[str, str]:
    packages: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name") or distribution.metadata.get("Summary")
        if not name:
            continue
        packages[str(name)] = distribution.version
    return dict(sorted(packages.items()))


def _asset_entries(root: Path, paths: Iterable[str | Path]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            raise FileNotFoundError(f"Artifact environment asset not found: {path}")
        try:
            portable = str(path.resolve().relative_to(root.resolve()))
        except ValueError:
            portable = str(path)
        entries.append({"path": portable, "sha256": _sha256_file(path)})
    return sorted(entries, key=lambda item: item["path"])


def _load_manifest(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _rights_record(path: str | Path, record_id: str) -> Mapping[str, Any] | None:
    payload = _load_manifest(path)
    records = payload.get("papers") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("Rights manifest must be a list or contain a papers list")
    matches = [item for item in records if isinstance(item, dict) and item.get("record_id") == record_id]
    if len(matches) != 1:
        return None
    return matches[0]


def _environment_lock_errors(environment_lock: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    python = environment_lock.get("python")
    contracts = environment_lock.get("contracts")
    assets = environment_lock.get("assets")
    git = environment_lock.get("git")
    if not isinstance(python, dict) or not python.get("executable") or not python.get("version"):
        errors.append("environment_python_incomplete")
    if not isinstance(environment_lock.get("packages"), dict) or not environment_lock.get("packages"):
        errors.append("environment_packages_incomplete")
    required_contracts = ("babeldoc_version", "ir_version", "model", "provider")
    if not isinstance(contracts, dict) or any(not contracts.get(key) for key in required_contracts):
        errors.append("environment_contracts_incomplete")
    elif not contracts.get("pricing") or not contracts.get("versions"):
        errors.append("environment_contracts_incomplete")
    if not isinstance(assets, dict) or not assets.get("fonts") or not assets.get("cover_assets"):
        errors.append("environment_assets_incomplete")
    if not isinstance(git, dict) or not git.get("commit") or not git.get("tree"):
        errors.append("environment_git_incomplete")
    lock_hash = environment_lock.get("lock_sha256")
    if not isinstance(lock_hash, str) or _json_sha256(
        {key: value for key, value in environment_lock.items() if key != "lock_sha256"}
    ) != lock_hash:
        errors.append("environment_lock_content_mismatch")
    if environment_lock.get("lock_schema_version") == ENVIRONMENT_LOCK_SCHEMA_VERSION:
        pipeline_payload = _pipeline_lock_payload(environment_lock)
        execution_payload = _execution_lock_payload(environment_lock)
        if environment_lock.get("pipeline_lock_sha256") != _json_sha256(pipeline_payload):
            errors.append("pipeline_lock_content_mismatch")
        if environment_lock.get("execution_lock_sha256") != _json_sha256(execution_payload):
            errors.append("execution_lock_content_mismatch")
    return errors


def _pipeline_lock_payload(environment_lock: Mapping[str, Any]) -> dict[str, Any]:
    contracts = dict(environment_lock.get("contracts") or {})
    for key in ("model", "provider", "pricing", "execution_binding"):
        contracts.pop(key, None)
    return {
        "schema_version": ENVIRONMENT_LOCK_SCHEMA_VERSION,
        "python": environment_lock.get("python"),
        "packages": environment_lock.get("packages"),
        "contracts": contracts,
        "assets": environment_lock.get("assets"),
        "git": environment_lock.get("git"),
    }


def _execution_lock_payload(environment_lock: Mapping[str, Any]) -> dict[str, Any]:
    contracts = environment_lock.get("contracts") or {}
    return {
        "schema_version": ENVIRONMENT_LOCK_SCHEMA_VERSION,
        "model": contracts.get("model"),
        "provider": contracts.get("provider"),
        "pricing": contracts.get("pricing"),
        "execution_binding": contracts.get("execution_binding") or {},
    }


@contextmanager
def _manifest_lock(manifest_path: Path):
    lock_path = manifest_path.with_suffix(manifest_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)


def build_environment_lock(
    *,
    root: str | Path | None = None,
    python_executable: str | None = None,
    python_version: str | None = None,
    installed_packages: Mapping[str, str] | None = None,
    babeldoc_version: str | None = None,
    ir_version: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    pricing_contract: Mapping[str, Any] | None = None,
    execution_binding: Mapping[str, Any] | None = None,
    contract_versions: Mapping[str, Any] | None = None,
    font_paths: Iterable[str | Path] = (),
    cover_asset_paths: Iterable[str | Path] = (),
    git_commit: str | None = None,
    git_tree: str | None = None,
) -> dict[str, Any]:
    repo_root = Path(root or Path.cwd()).resolve()
    packages = (
        dict(sorted((str(key), str(value)) for key, value in installed_packages.items()))
        if installed_packages is not None
        else _installed_packages()
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "python": {
            "executable": python_executable or sys.executable,
            "version": python_version or sys.version.split()[0],
        },
        "packages": packages,
        "contracts": {
            "babeldoc_version": babeldoc_version,
            "ir_version": ir_version,
            "model": model,
            "provider": provider,
            "pricing": dict(pricing_contract or {}),
            "execution_binding": dict(execution_binding or {}),
            "versions": dict(contract_versions or {}),
        },
        "assets": {
            "fonts": _asset_entries(repo_root, font_paths),
            "cover_assets": _asset_entries(repo_root, cover_asset_paths),
        },
        "git": {
            "commit": git_commit or _git_rev(repo_root, "HEAD"),
            "tree": git_tree or _git_rev(repo_root, "HEAD^{tree}"),
        },
    }
    payload["lock_schema_version"] = ENVIRONMENT_LOCK_SCHEMA_VERSION
    payload["pipeline_lock_sha256"] = _json_sha256(_pipeline_lock_payload(payload))
    payload["execution_lock_sha256"] = _json_sha256(_execution_lock_payload(payload))
    payload["lock_sha256"] = _json_sha256(payload)
    return payload


def write_artifact_manifest(
    *,
    manifest_path: str | Path,
    record_id: str,
    publication_allowed: bool,
    rights_manifest_path: str | Path | None = None,
    rights_manifest_sha256: str | None = None,
    article_root: str | Path | None = None,
    environment_lock: Mapping[str, Any],
    artifacts: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if publication_allowed is not True:
        raise ValueError("publication_allowed must be literal True")
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("record_id must be non-empty")
    if not isinstance(environment_lock.get("lock_sha256"), str):
        raise ValueError("environment_lock must include lock_sha256")
    environment_errors = _environment_lock_errors(environment_lock)
    if environment_errors:
        raise ValueError("Incomplete environment lock: " + ",".join(environment_errors))
    if rights_manifest_sha256 is None:
        if rights_manifest_path is None:
            raise ValueError("rights_manifest_path or rights_manifest_sha256 is required")
        rights_manifest_sha256 = _sha256_file(Path(rights_manifest_path))
    if rights_manifest_path is not None:
        live_record = _rights_record(rights_manifest_path, record_id)
        if live_record is None or live_record.get("publication_allowed") is not True:
            raise ValueError(f"Live publication rights do not allow record: {record_id}")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id,
        "publication_allowed": True,
        "rights_manifest_sha256": rights_manifest_sha256,
        "environment_lock_sha256": environment_lock["lock_sha256"],
        "pipeline_lock_sha256": environment_lock.get("pipeline_lock_sha256"),
        "execution_lock_sha256": environment_lock.get("execution_lock_sha256"),
        "environment_lock": dict(environment_lock),
        "artifacts": [dict(record) for record in artifacts],
    }
    artifact_ids: set[str] = set()
    for record in manifest["artifacts"]:
        artifact_id = str(record.get("artifact_id") or "")
        if not artifact_id or artifact_id in artifact_ids:
            raise ValueError(f"Duplicate artifact_id in manifest: {artifact_id or '<missing>'}")
        artifact_ids.add(artifact_id)
        if article_root is not None:
            _article_path(Path(article_root), record.get("relative_path"), label="Artifact path")
    with _manifest_lock(Path(manifest_path)):
        _atomic_json(Path(manifest_path), manifest)
    return manifest


def record_artifact(
    *,
    manifest_path: str | Path,
    article_root: str | Path,
    artifact_id: str,
    relative_path: str,
    producer: str,
    artifact_type: str,
    paper_stage: str,
    environment_lock: Mapping[str, Any],
    parents: Iterable[str | Mapping[str, Any]] = (),
    contract_versions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if paper_stage not in STAGE_RANK:
        raise ValueError(f"Unknown paper_stage: {paper_stage}")
    manifest_file = Path(manifest_path)
    article_dir = Path(article_root)
    with _manifest_lock(manifest_file):
        manifest = _load_manifest(manifest_file)
        if manifest.get("environment_lock_sha256") != environment_lock.get("lock_sha256"):
            raise RuntimeError("Environment lock drifted before artifact recording")
        if artifact_id in {record.get("artifact_id") for record in manifest.get("artifacts", [])}:
            raise ValueError(f"Duplicate artifact_id in manifest: {artifact_id}")
        artifact_path = _article_path(article_dir, relative_path, label="Artifact path")
        if not artifact_path.is_file():
            raise FileNotFoundError(f"Artifact file not found: {artifact_path}")
        by_id = {str(record["artifact_id"]): record for record in manifest.get("artifacts", [])}
        parent_refs: list[dict[str, str]] = []
        for parent in parents:
            if isinstance(parent, str):
                if parent not in by_id:
                    raise RuntimeError(f"Parent artifact not found: {parent}")
                parent_refs.append({"artifact_id": parent, "sha256": str(by_id[parent]["sha256"])})
                continue
            parent_id = str(parent.get("artifact_id") or "")
            parent_hash = str(parent.get("sha256") or "")
            if not parent_id or not parent_hash:
                raise ValueError("Parent references must include artifact_id and sha256")
            if parent_id not in by_id:
                raise RuntimeError(f"Parent artifact not found: {parent_id}")
            if str(by_id[parent_id].get("sha256") or "") != parent_hash:
                raise RuntimeError(f"Parent artifact hash does not match manifest: {parent_id}")
            parent_refs.append({"artifact_id": parent_id, "sha256": parent_hash})
        record = {
            "schema_version": SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "relative_path": relative_path,
            "sha256": _sha256_file(artifact_path),
            "producer": producer,
            "artifact_type": artifact_type,
            "paper_stage": paper_stage,
            "environment_lock_sha256": manifest["environment_lock_sha256"],
            "contract_versions": dict(contract_versions or {}),
            "parents": parent_refs,
        }
        manifest["artifacts"].append(record)
        _atomic_json(manifest_file, manifest)
    return record


def validate_artifact_manifest(
    manifest_path: str | Path,
    *,
    article_root: str | Path | None = None,
    current_environment_lock: Mapping[str, Any] | None = None,
    rights_manifest_path: str | Path | None = None,
    rights_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    manifest_file = Path(manifest_path)
    report: dict[str, Any] = {
        "ok": False,
        "record_id": None,
        "errors": [],
    }
    try:
        manifest = _load_manifest(manifest_file)
    except (OSError, json.JSONDecodeError, TypeError) as error:
        report["errors"].append(f"invalid_manifest:{type(error).__name__}")
        return report
    errors: list[str] = []
    article_dir = Path(article_root) if article_root is not None else manifest_file.parent
    report["record_id"] = manifest.get("record_id")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("manifest_schema_version_mismatch")
    if manifest.get("publication_allowed") is not True:
        errors.append("publication_not_allowed")
    if rights_manifest_path is not None:
        try:
            live_record = _rights_record(rights_manifest_path, str(manifest.get("record_id") or ""))
        except (OSError, ValueError, json.JSONDecodeError):
            live_record = None
        if live_record is None:
            errors.append("live_rights_record_missing")
        elif live_record.get("publication_allowed") is not True:
            errors.append("live_publication_not_allowed")
    if current_environment_lock is not None:
        stored_pipeline = manifest.get("pipeline_lock_sha256")
        current_pipeline = current_environment_lock.get("pipeline_lock_sha256")
        if isinstance(stored_pipeline, str) and isinstance(current_pipeline, str):
            if stored_pipeline != current_pipeline:
                errors.append("pipeline_lock_drift")
        elif manifest.get("environment_lock_sha256") != current_environment_lock.get("lock_sha256"):
            errors.append("environment_lock_drift")
    stored_environment = manifest.get("environment_lock")
    if not isinstance(stored_environment, dict) or stored_environment.get("lock_sha256") != manifest.get(
        "environment_lock_sha256"
    ):
        errors.append("stored_environment_lock_mismatch")
    elif _json_sha256({key: value for key, value in stored_environment.items() if key != "lock_sha256"}) != stored_environment.get(
        "lock_sha256"
    ):
        errors.append("stored_environment_lock_content_mismatch")
    if isinstance(stored_environment, dict):
        errors.extend(_environment_lock_errors(stored_environment))
        for field in ("pipeline_lock_sha256", "execution_lock_sha256"):
            stored_value = stored_environment.get(field)
            manifest_value = manifest.get(field)
            if stored_value is not None and stored_value != manifest_value:
                errors.append(f"stored_{field}_mismatch")
    if rights_manifest_sha256 is None and rights_manifest_path is not None:
        rights_manifest_sha256 = _sha256_file(Path(rights_manifest_path))
    if rights_manifest_sha256 is not None and manifest.get("rights_manifest_sha256") != rights_manifest_sha256:
        errors.append("rights_manifest_sha256_drift")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("artifacts_not_list")
        artifacts = []
    by_id: dict[str, dict[str, Any]] = {}
    current_hashes: dict[str, str] = {}
    for record in artifacts:
        artifact_id = str(record.get("artifact_id") or "")
        if not artifact_id:
            errors.append("artifact_id_missing")
            continue
        if artifact_id in by_id:
            errors.append(f"duplicate_artifact_id:{artifact_id}")
            continue
        by_id[artifact_id] = record
        if record.get("paper_stage") not in STAGE_RANK:
            errors.append(f"artifact_stage_invalid:{artifact_id}")
        try:
            path = _article_path(article_dir, record.get("relative_path"), label="Artifact path")
        except RuntimeError:
            errors.append(f"artifact_path_escape:{artifact_id}")
            continue
        if not path.is_file():
            errors.append(f"artifact_missing:{artifact_id}")
            continue
        current_hash = _sha256_file(path)
        current_hashes[artifact_id] = current_hash
        if record.get("sha256") != current_hash:
            errors.append(f"artifact_hash_mismatch:{artifact_id}")
        if record.get("environment_lock_sha256") != manifest.get("environment_lock_sha256"):
            errors.append(f"artifact_environment_lock_mismatch:{artifact_id}")
    for artifact_id, record in by_id.items():
        parents = record.get("parents")
        if not isinstance(parents, list):
            errors.append(f"artifact_parents_invalid:{artifact_id}")
            continue
        for parent in parents:
            parent_id = str(parent.get("artifact_id") or "")
            expected_hash = str(parent.get("sha256") or "")
            if not parent_id or not expected_hash:
                errors.append(f"parent_reference_invalid:{artifact_id}")
                continue
            parent_record = by_id.get(parent_id)
            if parent_record is None:
                errors.append(f"missing_parent:{artifact_id}:{parent_id}")
                continue
            if parent_id not in current_hashes:
                errors.append(f"parent_missing_file:{artifact_id}:{parent_id}")
                continue
            if parent_record.get("sha256") != expected_hash or current_hashes[parent_id] != expected_hash:
                errors.append(f"parent_hash_mismatch:{artifact_id}:{parent_id}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(artifact_id: str) -> None:
        if artifact_id in visiting:
            errors.append(f"artifact_cycle:{artifact_id}")
            return
        if artifact_id in visited:
            return
        visiting.add(artifact_id)
        for parent in by_id[artifact_id].get("parents", []):
            parent_id = str(parent.get("artifact_id") or "")
            if parent_id in by_id:
                visit(parent_id)
        visiting.remove(artifact_id)
        visited.add(artifact_id)

    for artifact_id in by_id:
        visit(artifact_id)
    report["ok"] = not errors
    report["errors"] = errors
    report["manifest"] = manifest
    return report


def _collect_ancestor_stages(
    artifact_id: str,
    by_id: Mapping[str, Mapping[str, Any]],
    seen: set[str] | None = None,
) -> set[str]:
    visited = set() if seen is None else seen
    if artifact_id in visited:
        return set()
    visited.add(artifact_id)
    record = by_id[artifact_id]
    stages = {str(record.get("paper_stage") or "")}
    for parent in record.get("parents", []):
        parent_id = str(parent.get("artifact_id") or "")
        if parent_id in by_id:
            stages.update(_collect_ancestor_stages(parent_id, by_id, visited))
    return stages


def _chain_complete(record: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]) -> bool:
    stage = str(record.get("paper_stage") or "")
    if stage not in STAGE_RANK:
        return False
    required = set(STAGE_SEQUENCE[: STAGE_RANK[stage] + 1])
    stages_present = _collect_ancestor_stages(str(record["artifact_id"]), by_id)
    return required.issubset(stages_present)


def derive_paper_state(
    manifest_path: str | Path,
    *,
    article_root: str | Path | None = None,
    current_environment_lock: Mapping[str, Any] | None = None,
    rights_manifest_path: str | Path | None = None,
    rights_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    report = validate_artifact_manifest(
        manifest_path,
        article_root=article_root,
        current_environment_lock=current_environment_lock,
        rights_manifest_path=rights_manifest_path,
        rights_manifest_sha256=rights_manifest_sha256,
    )
    manifest = report.get("manifest") or {}
    if not report["ok"]:
        return {
            "ok": False,
            "record_id": manifest.get("record_id"),
            "state": "quarantined",
            "publishable": False,
            "artifact_id": None,
            "errors": report["errors"],
        }
    by_id = {
        str(record["artifact_id"]): record
        for record in manifest.get("artifacts", [])
        if isinstance(record, dict) and isinstance(record.get("artifact_id"), str)
    }
    best_record: dict[str, Any] | None = None
    best_records: list[dict[str, Any]] = []
    best_rank = -1
    for record in by_id.values():
        if not _chain_complete(record, by_id):
            continue
        stage = str(record.get("paper_stage") or "")
        rank = STAGE_RANK.get(stage, -1)
        if rank > best_rank:
            best_record = record
            best_records = [record]
            best_rank = rank
        elif rank == best_rank:
            best_records.append(record)
    if len(best_records) > 1:
        return {
            "ok": False,
            "record_id": manifest.get("record_id"),
            "state": "quarantined",
            "publishable": False,
            "artifact_id": None,
            "errors": ["ambiguous_state_tips:" + ",".join(sorted(str(item["artifact_id"]) for item in best_records))],
        }
    state = "not_started"
    artifact_id = None
    publishable = False
    if best_record is not None:
        state = str(best_record["paper_stage"])
        artifact_id = str(best_record["artifact_id"])
        publishable = state == "packaged"
    return {
        "ok": True,
        "record_id": manifest.get("record_id"),
        "state": state,
        "publishable": publishable,
        "artifact_id": artifact_id,
        "errors": [],
    }
