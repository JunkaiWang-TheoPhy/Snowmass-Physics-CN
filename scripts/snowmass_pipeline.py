#!/usr/bin/env python3
"""Task 1 helpers for the Snowmass production translation pipeline."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NamedTuple


class UnsafeArchiveError(RuntimeError):
    """Raised when an archive member would escape the extraction root."""


class UnsafeIncludeError(RuntimeError):
    """Raised when TeX include traversal escapes the allowed root."""


class MainCandidate(NamedTuple):
    path: Path
    score: int
    incoming_includes: int
    has_document_marker: bool
    has_title: bool
    has_abstract: bool


class ExpandedTex(NamedTuple):
    text: str
    includes: tuple[Path, ...]
    missing_includes: tuple[Path, ...]
    cycles: tuple[Path, ...]


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


_TEX_MARKER = re.compile(
    rb"\\(?:documentclass|documentstyle|begin\s*\{document\}|"
    rb"(?:input|include)\s*\{|(?:newcommand|renewcommand|providecommand|def)\b)"
)
_DOCUMENT_MARKER_PATTERN = re.compile(r"\\(?:documentclass|documentstyle|begin\s*\{document\})")
_TITLE_PATTERN = re.compile(r"\\title\s*\{")
_ABSTRACT_PATTERN = re.compile(r"\\begin\s*\{abstract\}")
_INCLUDE_PATTERN = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")


def _classify_source_payload(payload: bytes, path: Path) -> Literal["tar", "single_tex"]:
    if _is_tar_payload(payload):
        return "tar"
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Unsupported gzip payload: {path}") from exc
    if _TEX_MARKER.search(payload) is None:
        raise ValueError(f"Unsupported gzip payload: {path}")
    return "single_tex"


def detect_source_package(path: Path) -> Literal["tar", "single_tex"]:
    return _classify_source_payload(_gunzip_bytes(path), path)


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
    package_type = _classify_source_payload(payload, path)
    if package_type == "tar":
        return _extract_tar_payload(payload, destination)
    return _extract_single_tex(payload, path, destination)


def _is_backup_tex(path: Path) -> bool:
    name = path.name.lower()
    stem = path.stem.lower()
    return (
        "~" in name
        or ".bak." in name
        or ".backup." in name
        or ".orig." in name
        or ".tmp." in name
        or stem.endswith("_backup")
        or stem.endswith("-backup")
        or stem.startswith(".#")
    )


def _strip_tex_comment(line: str) -> str:
    for index, character in enumerate(line):
        if character == "%" and (index == 0 or line[index - 1] != "\\"):
            return line[:index]
    return line


def _strip_tex_comments(text: str) -> str:
    return "".join(_strip_tex_comment(line) for line in text.splitlines(keepends=True))


def _iter_include_specs(text: str) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        visible = _strip_tex_comment(line)
        for match in _INCLUDE_PATTERN.finditer(visible):
            matches.append((offset + match.start(), offset + match.end(), match.group(1).strip()))
        offset += len(line)
    return matches


def _ensure_within_root(path: Path, root: Path, error_type: type[RuntimeError], message: str) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve(strict=False)
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise error_type(message)
    return path


def _candidate_include_paths(spec: str, base_dir: Path) -> list[Path]:
    logical = base_dir / Path(spec)
    if logical.suffix:
        return [logical]
    return [logical.with_suffix(".tex"), logical]


def _resolve_include_target(spec: str, base_dir: Path, root: Path) -> Path:
    logical_spec = Path(spec)
    if logical_spec.is_absolute():
        raise UnsafeIncludeError(f"Unsafe include path: {spec}")
    candidates = _candidate_include_paths(spec, base_dir)
    for candidate in candidates:
        resolved = _ensure_within_root(candidate, root, UnsafeIncludeError, f"Unsafe include path: {spec}")
        if candidate.exists():
            return resolved
    return _ensure_within_root(candidates[0], root, UnsafeIncludeError, f"Unsafe include path: {spec}")


def _tex_source_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.tex") if path.is_file() and not _is_backup_tex(path))


def rank_main_tex(root: Path) -> list[MainCandidate]:
    tex_files = _tex_source_files(root)
    contents = {path.resolve(): path.read_text(encoding="utf-8") for path in tex_files}
    incoming_counts = {path.resolve(): 0 for path in tex_files}
    candidate_paths = set(incoming_counts)

    for path in tex_files:
        source_path = path.resolve()
        for _, _, spec in _iter_include_specs(contents[source_path]):
            try:
                target = _resolve_include_target(spec, path.parent, root)
            except UnsafeIncludeError:
                continue
            resolved_target = target.resolve(strict=False)
            if resolved_target in candidate_paths and resolved_target != source_path:
                incoming_counts[resolved_target] += 1

    ranked: list[MainCandidate] = []
    for path in tex_files:
        resolved_path = path.resolve()
        text = contents[resolved_path]
        visible_text = _strip_tex_comments(text)
        lower_stem = path.stem.lower()
        has_document_marker = _DOCUMENT_MARKER_PATTERN.search(visible_text) is not None
        has_title = _TITLE_PATTERN.search(visible_text) is not None
        has_abstract = _ABSTRACT_PATTERN.search(visible_text) is not None

        score = 0
        if has_document_marker:
            score += 200
        if has_title:
            score += 35
        if has_abstract:
            score += 35
        score -= incoming_counts[resolved_path] * 80
        if lower_stem == "main":
            score += 40
        if any(token in lower_stem for token in ("supplement", "supp", "appendix", "response", "cover")):
            score -= 25
        score += min(len(text) // 200, 25)

        ranked.append(
            MainCandidate(
                path=path,
                score=score,
                incoming_includes=incoming_counts[resolved_path],
                has_document_marker=has_document_marker,
                has_title=has_title,
                has_abstract=has_abstract,
            )
        )

    return sorted(ranked, key=lambda candidate: (-candidate.score, candidate.path.as_posix()))


def expand_tex(main_path: Path, root: Path) -> ExpandedTex:
    main_path = _ensure_within_root(main_path, root, UnsafeIncludeError, f"Unsafe main path: {main_path}")
    max_depth = max(len(_tex_source_files(root)) + 1, 1)
    includes: list[Path] = []
    include_seen: set[Path] = set()
    missing_includes: list[Path] = []
    missing_seen: set[Path] = set()
    cycles: list[Path] = []
    cycle_seen: set[Path] = set()
    main_key = main_path.resolve(strict=False)
    display_paths = {main_key: main_path}

    def visit(path: Path, stack: tuple[Path, ...]) -> str:
        if len(stack) > max_depth:
            path_key = path.resolve(strict=False)
            if path_key not in cycle_seen:
                cycle_seen.add(path_key)
                cycles.append(display_paths.setdefault(path_key, path))
            return ""

        text = path.read_text(encoding="utf-8")
        expanded_parts: list[str] = []
        cursor = 0
        for start, end, spec in _iter_include_specs(text):
            expanded_parts.append(text[cursor:start])
            target = _resolve_include_target(spec, path.parent, root)
            target_key = target.resolve(strict=False)
            display_target = display_paths.setdefault(target_key, target)
            if not target.exists():
                if target_key not in missing_seen:
                    missing_seen.add(target_key)
                    missing_includes.append(display_target)
            elif target_key in stack:
                if target_key not in cycle_seen:
                    cycle_seen.add(target_key)
                    cycles.append(display_target)
            else:
                if target_key not in include_seen:
                    include_seen.add(target_key)
                    includes.append(display_target)
                expanded_parts.append(visit(target, stack + (target_key,)))
            cursor = end
        expanded_parts.append(text[cursor:])
        return "".join(expanded_parts)

    text = visit(main_path, (main_key,))
    return ExpandedTex(
        text=text,
        includes=tuple(includes),
        missing_includes=tuple(missing_includes),
        cycles=tuple(cycles),
    )
