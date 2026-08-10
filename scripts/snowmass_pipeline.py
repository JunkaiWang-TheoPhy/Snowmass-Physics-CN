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
    outgoing_includes: int
    path_depth: int
    content_hash: str
    has_document_marker: bool
    has_title: bool
    has_abstract: bool


class ExpandedTex(NamedTuple):
    text: str
    includes: tuple[Path, ...]
    missing_includes: tuple[Path, ...]
    cycles: tuple[Path, ...]


class ProtectedText(NamedTuple):
    text: str
    mapping: dict[str, str]


class StructureMismatchError(RuntimeError):
    """Raised when protected sentinels are missing, duplicated, or unexpected."""


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
_INCLUDE_PATTERN = re.compile(
    r"\\(?P<command>input|include|subfile)(?![A-Za-z@])\s*"
    r"(?:\{(?P<braced>[^{}]+)\}|(?P<unbraced>[^\s%{}]+))"
)
_OPTIONAL_INCLUDE_PREFIX_PATTERN = re.compile(
    r"\\IfFileExists\s*\{(?P<probe>[^{}]+)\}\s*\{\s*$"
)
_EXTERNAL_TEX_INPUTS = {"epsf"}
_DISPLAY_ENV_PATTERN = re.compile(
    r"\\begin\{(?P<env>equation\*?|align\*?|gather\*?|multline\*?)\}.*?\\end\{(?P=env)\}",
    re.DOTALL,
)
_STRUCTURE_PATTERNS = (
    re.compile(r"\$\$.*?\$\$", re.DOTALL),
    re.compile(r"\\\[.*?\\\]", re.DOTALL),
    _DISPLAY_ENV_PATTERN,
    re.compile(r"\\\(.+?\\\)", re.DOTALL),
    re.compile(r"(?<!\$)\$(?!\$)(?:\\.|[^$\\\n])+\$(?!\$)"),
    re.compile(r"\\(?:cite|citet|citep|ref|eqref|autoref|label)\{[^{}]+\}"),
    re.compile(r"https?://[^\s<>()，。；：！？]*[^\s<>().,;:!?，。；：！？]"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)
_SENTINEL_PATTERN = re.compile(r"\[\[SM_[0-9]{4}_[0-9a-f]{10}\]\]")
_WORD_PATTERN = re.compile(r"\S+")


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
    if output_name.endswith(".tar"):
        output_name = output_name[:-4] + ".tex"
    elif "." not in Path(output_name).name:
        output_name = output_name + ".tex"
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


def _mask_tex_comments(text: str) -> str:
    masked: list[str] = []
    for line in text.splitlines(keepends=True):
        visible = _strip_tex_comment(line)
        masked.append(visible + "".join("\n" if char == "\n" else " " for char in line[len(visible) :]))
    return "".join(masked)


def _iter_include_specs(text: str) -> list[tuple[int, int, str, str, bool]]:
    matches: list[tuple[int, int, str, str, bool]] = []
    visible = _mask_tex_comments(text)
    for match in _INCLUDE_PATTERN.finditer(visible):
        spec = (match.group("braced") or match.group("unbraced")).strip()
        external_name = Path(spec).name.casefold()
        if "#" in spec or external_name in _EXTERNAL_TEX_INPUTS or external_name == "epsf.tex":
            continue
        optional_prefix = _OPTIONAL_INCLUDE_PREFIX_PATTERN.search(visible[: match.start()])
        optional = bool(optional_prefix and optional_prefix.group("probe").strip() == spec)
        matches.append(
            (
                match.start(),
                match.end(),
                match.group("command"),
                spec,
                optional,
            )
        )
    return matches


def _ensure_within_root(path: Path, root: Path, error_type: type[RuntimeError], message: str) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve(strict=False)
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise error_type(message)
    return path


def _candidate_include_paths(spec: str, base_dir: Path) -> list[Path]:
    logical = base_dir / Path(spec)
    if logical.suffix.lower() in {".tex", ".sty", ".cls", ".ltx"}:
        return [logical]
    return [logical.with_suffix(logical.suffix + ".tex"), logical]


def _resolve_include_target(spec: str, base_dir: Path, root: Path, command: str) -> Path:
    if command == "subfile" and spec.startswith("\\main/"):
        spec = spec[len("\\main/") :]
        base_dir = root
    logical_spec = Path(spec)
    if logical_spec.is_absolute():
        raise UnsafeIncludeError(f"Unsafe include path: {spec}")
    search_dirs = [base_dir]
    if not spec.startswith(".") and base_dir.resolve() != root.resolve():
        search_dirs.append(root)
    candidates = [
        candidate
        for directory in search_dirs
        for candidate in _candidate_include_paths(spec, directory)
    ]
    validated = [
        _ensure_within_root(candidate, root, UnsafeIncludeError, f"Unsafe include path: {spec}")
        for candidate in candidates
    ]
    for candidate in validated:
        if candidate.exists():
            return candidate
    return validated[0]


def _tex_source_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.tex") if path.is_file() and not _is_backup_tex(path))


def rank_main_tex(root: Path) -> list[MainCandidate]:
    tex_files = _tex_source_files(root)
    contents = {path.resolve(): path.read_text(encoding="utf-8") for path in tex_files}
    incoming_counts = {path.resolve(): 0 for path in tex_files}
    outgoing_targets: dict[Path, set[Path]] = {path.resolve(): set() for path in tex_files}
    candidate_paths = set(incoming_counts)

    for path in tex_files:
        source_path = path.resolve()
        for _, _, command, spec, _optional in _iter_include_specs(contents[source_path]):
            try:
                target = _resolve_include_target(spec, path.parent, root, command)
            except UnsafeIncludeError:
                continue
            resolved_target = target.resolve(strict=False)
            if resolved_target in candidate_paths and resolved_target != source_path:
                incoming_counts[resolved_target] += 1
                outgoing_targets[source_path].add(resolved_target)

    ranked: list[MainCandidate] = []
    for path in tex_files:
        resolved_path = path.resolve()
        text = contents[resolved_path]
        visible_text = _strip_tex_comments(text)
        lower_stem = path.stem.lower()
        has_document_marker = _DOCUMENT_MARKER_PATTERN.search(visible_text) is not None
        has_title = _TITLE_PATTERN.search(visible_text) is not None
        has_abstract = _ABSTRACT_PATTERN.search(visible_text) is not None
        outgoing_includes = len(outgoing_targets[resolved_path])
        path_depth = len(path.resolve().relative_to(root.resolve()).parent.parts)

        score = 0
        if has_document_marker:
            score += 200
        if has_title:
            score += 35
        if has_abstract:
            score += 35
        score -= incoming_counts[resolved_path] * 80
        score += min(outgoing_includes * 8, 160)
        score -= min(path_depth, 8) * 20
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
                outgoing_includes=outgoing_includes,
                path_depth=path_depth,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
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
        for start, end, command, spec, optional in _iter_include_specs(text):
            expanded_parts.append(text[cursor:start])
            target = _resolve_include_target(spec, path.parent, root, command)
            target_key = target.resolve(strict=False)
            display_target = display_paths.setdefault(target_key, target)
            if not target.exists():
                if not optional and target_key not in missing_seen:
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


def _sentinel_for(index: int, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"[[SM_{index:04d}_{digest}]]"


def protect_structures(text: str) -> ProtectedText:
    mapping: dict[str, str] = {}
    selected: list[tuple[int, int, str]] = []
    index = 1

    for pattern in _STRUCTURE_PATTERNS:
        occupied = sorted((start, end) for start, end, _sentinel in selected)
        gaps: list[tuple[int, int]] = []
        cursor = 0
        for start, end in occupied:
            if cursor < start:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < len(text):
            gaps.append((cursor, len(text)))

        for gap_start, gap_end in gaps:
            for match in pattern.finditer(text, gap_start, gap_end):
                value = match.group(0)
                sentinel = _sentinel_for(index, value)
                while sentinel in text or sentinel in mapping:
                    index += 1
                    sentinel = _sentinel_for(index, value)
                mapping[sentinel] = value
                selected.append((match.start(), match.end(), sentinel))
                index += 1

    protected = text
    for start, end, sentinel in sorted(selected, reverse=True):
        protected = protected[:start] + sentinel + protected[end:]

    ordered_mapping = dict(sorted(mapping.items(), key=lambda item: protected.index(item[0])))
    return ProtectedText(text=protected, mapping=ordered_mapping)


def protected_literals(text: str) -> tuple[str, ...]:
    return tuple(protect_structures(text).mapping.values())


def sentinel_sequence(text: str) -> tuple[str, ...]:
    return tuple(_SENTINEL_PATTERN.findall(text))


def validate_and_restore(text: str, mapping: dict[str, str]) -> str:
    for sentinel in mapping:
        count = text.count(sentinel)
        if count != 1:
            raise StructureMismatchError(f"Expected sentinel {sentinel} exactly once, found {count}")
    observed_sentinels = list(sentinel_sequence(text))
    if any(sentinel not in mapping for sentinel in observed_sentinels):
        raise StructureMismatchError("Unexpected protected sentinel remained before restore")
    if observed_sentinels != list(mapping):
        raise StructureMismatchError("Protected sentinel sequence changed before restore")

    restored = text
    for sentinel, value in mapping.items():
        restored = restored.replace(sentinel, value)
    if _SENTINEL_PATTERN.search(restored):
        raise StructureMismatchError("Unexpected protected sentinel remained after restore")
    return restored


def _word_count(text: str) -> int:
    return len(_WORD_PATTERN.findall(text))


def _split_blocks(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    return [block.strip() for block in re.split(r"\n\s*\n+", stripped) if block.strip()]


def semantic_chunks(
    text: str,
    target_words: int = 1500,
    min_words: int = 1200,
    max_words: int = 1800,
) -> list[str]:
    if min_words > target_words or target_words > max_words:
        raise ValueError("Chunk thresholds must satisfy min_words <= target_words <= max_words")

    blocks = _split_blocks(text)
    if not blocks:
        return []

    chunks: list[str] = []
    current_blocks: list[str] = []
    current_words = 0

    for block in blocks:
        block_words = _word_count(block)
        if not current_blocks:
            current_blocks = [block]
            current_words = block_words
            continue

        combined_words = current_words + block_words
        if combined_words <= target_words:
            current_blocks.append(block)
            current_words = combined_words
            continue

        if current_words < min_words and combined_words <= max_words:
            current_blocks.append(block)
            chunks.append("\n\n".join(current_blocks).strip() + "\n")
            current_blocks = []
            current_words = 0
            continue

        chunks.append("\n\n".join(current_blocks).strip() + "\n")
        current_blocks = [block]
        current_words = block_words

    if current_blocks:
        chunks.append("\n\n".join(current_blocks).strip() + "\n")

    return chunks
