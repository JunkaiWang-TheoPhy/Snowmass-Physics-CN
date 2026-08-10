#!/usr/bin/env python3
"""Task 1 helpers for the Snowmass production translation pipeline."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal


class UnsafeArchiveError(RuntimeError):
    """Raised when an archive member would escape the extraction root."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_manifest_records(manifest_path: Path) -> list[dict[str, Any]]:
    records = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"Rights manifest must be a JSON list: {manifest_path}")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"Rights manifest record {index} is not an object")
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError(f"Rights manifest record {index} has no record_id")
        if record_id in seen:
            raise ValueError(f"Duplicate record_id: {record_id}")
        seen.add(record_id)
        validated.append(record)
    return validated


def build_rights_snapshot(manifest_path: Path, output_path: Path) -> dict[str, Any]:
    records = _load_manifest_records(manifest_path)
    selected = [record for record in records if record.get("publication_allowed") is True]
    snapshot = {
        "schema_version": 1,
        "source_manifest_path": str(manifest_path),
        "source_manifest_sha256": sha256_file(manifest_path),
        "created_at": now(),
        "eligible_count": len(selected),
        "records": selected,
    }
    atomic_json(output_path, snapshot)
    return snapshot


def _gunzip_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        return gzip.decompress(stream.read())


def _is_tar_payload(payload: bytes) -> bool:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:"):
            return True
    except tarfile.TarError:
        return False


def detect_source_package(path: Path) -> Literal["tar", "single_tex"]:
    payload = _gunzip_bytes(path)
    if _is_tar_payload(payload):
        return "tar"
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Unsupported gzip payload: {path}") from exc
    return "single_tex"


def _validated_target(destination: Path, member_name: str) -> Path:
    relative = PurePosixPath(member_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise UnsafeArchiveError(f"Unsafe archive member path: {member_name}")
    logical_target = destination / Path(*relative.parts)
    resolved_destination = destination.resolve()
    resolved_target = logical_target.resolve(strict=False)
    if resolved_target != resolved_destination and resolved_destination not in resolved_target.parents:
        raise UnsafeArchiveError(f"Archive member escapes destination: {member_name}")
    return logical_target


def _extract_tar_payload(payload: bytes, destination: Path) -> list[Path]:
    extracted: list[Path] = []
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            _validated_target(destination, member.name)
            if member.issym() or member.islnk():
                raise UnsafeArchiveError(f"Archive links are not allowed: {member.name}")
            if not (member.isdir() or member.isreg()):
                raise UnsafeArchiveError(f"Unsupported archive member type: {member.name}")

        for member in members:
            target = _validated_target(destination, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise UnsafeArchiveError(f"Archive member has no readable payload: {member.name}")
            target.write_bytes(source.read())
            extracted.append(target)
    return sorted(extracted)


def _extract_single_tex(payload: bytes, path: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    output_name = path.name[:-3] if path.name.endswith(".gz") else path.name
    target = destination / output_name
    resolved_destination = destination.resolve()
    resolved_target = target.resolve(strict=False)
    if resolved_target != resolved_destination and resolved_destination not in resolved_target.parents:
        raise UnsafeArchiveError(f"Single-file extraction escapes destination: {path.name}")
    target.write_bytes(payload)
    return [target]


def safe_extract_source(path: Path, destination: Path) -> list[Path]:
    payload = _gunzip_bytes(path)
    package_type = detect_source_package(path)
    if package_type == "tar":
        return _extract_tar_payload(payload, destination)
    return _extract_single_tex(payload, path, destination)
