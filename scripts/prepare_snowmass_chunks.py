#!/usr/bin/env python3
"""Create resumable Markdown chunks for every rights-eligible Snowmass paper."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path("output/snowmass2021_sources")
TRANSLATION_ROOT = Path("output/snowmass2021_translation")
TARGET_WORDS = 1500
MIN_WORDS = 1200
MAX_WORDS = 1800
PIPELINE_PATH = Path(__file__).with_name("snowmass_pipeline.py")

_PIPELINE_SPEC = importlib.util.spec_from_file_location("snowmass_pipeline", PIPELINE_PATH)
assert _PIPELINE_SPEC and _PIPELINE_SPEC.loader
PIPELINE = importlib.util.module_from_spec(_PIPELINE_SPEC)
_PIPELINE_SPEC.loader.exec_module(PIPELINE)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def normalize_source(text: str) -> str:
    normalized = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\f", "\n\n")
    normalized = normalized.strip()
    return normalized + "\n" if normalized else ""


def manifest_is_current(article_dir: Path, source_hash: str) -> bool:
    status_path = article_dir / "chunking_status.json"
    manifest_path = article_dir / "manifest.json"
    if not status_path.exists() or not manifest_path.exists():
        return False
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    if status.get("source_hash") != source_hash or manifest.get("source_hash") != source_hash:
        return False
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return False
    return all((article_dir / str(chunk.get("source_file", ""))).exists() for chunk in chunks)


def load_rights_snapshot_record_ids(path: Path) -> set[str]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    records = snapshot.get("records")
    if not isinstance(records, list):
        raise RuntimeError(f"Rights snapshot records must be a list: {path}")
    allowed: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RuntimeError(f"Rights snapshot record {index} is not an object")
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise RuntimeError(f"Rights snapshot record {index} has no record_id")
        allowed.add(record_id)
    return allowed


def load_source_records(root: Path, allowed_record_ids: set[str]) -> list[dict[str, Any]]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    records = manifest.get("records")
    if not isinstance(records, list):
        raise RuntimeError(f"Source manifest records must be a list: {root / 'manifest.json'}")
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RuntimeError(f"Source manifest record {index} is not an object")
        record_id = record.get("record_id")
        directory = record.get("directory")
        if not isinstance(record_id, str) or not record_id.strip():
            raise RuntimeError(f"Source manifest record {index} has no record_id")
        if record_id in seen:
            raise RuntimeError(f"Duplicate record_id in source manifest: {record_id}")
        seen.add(record_id)
        if not isinstance(directory, str) or not directory.strip():
            raise RuntimeError(f"Source manifest record {record_id} has no directory")
        if record_id in allowed_record_ids:
            selected.append(record)
    return selected


def _translation_outputs_exist(article_dir: Path) -> bool:
    patterns = ("output_chunk*.md", "stage1_chunk*.md", "stage2_chunk*.md", "stage3_chunk*.md")
    return any(any(article_dir.glob(pattern)) for pattern in patterns)


def _mapping_for_chunk(chunk: str, mapping: dict[str, str]) -> dict[str, str]:
    return {sentinel: value for sentinel, value in mapping.items() if sentinel in chunk}


def _expand_archive_source(archive_path: Path) -> str:
    with tempfile.TemporaryDirectory(prefix=".snowmass-source-") as temporary:
        extract_root = Path(temporary) / "extract"
        PIPELINE.safe_extract_source(archive_path, extract_root)
        candidates = [candidate for candidate in PIPELINE.rank_main_tex(extract_root) if candidate.has_document_marker]
        if not candidates:
            raise RuntimeError("archive_no_main_tex")
        expanded = PIPELINE.expand_tex(candidates[0].path, extract_root)
        text = normalize_source(expanded.text)
        if not text:
            raise RuntimeError("archive_empty_main_tex")
        return text


def select_source_text(record: dict[str, Any], root: Path) -> tuple[str, str, str | None]:
    item_dir = root / str(record["directory"])
    archive_path = item_dir / "source.tar.gz"
    text_path = item_dir / "source.txt"
    fallback_reason: str | None = None

    if archive_path.exists():
        try:
            return _expand_archive_source(archive_path), "expanded_tex", None
        except RuntimeError as exc:
            fallback_reason = str(exc)
        except Exception:  # noqa: BLE001 - chunk prep records archive failure and falls back to PDF text
            fallback_reason = "archive_unusable"
    else:
        fallback_reason = "archive_missing"

    if fallback_reason not in {"archive_missing", "archive_no_main_tex", "archive_empty_main_tex"}:
        fallback_reason = "archive_unusable"
    if not text_path.exists():
        raise RuntimeError(f"Missing PDF text fallback for {record['record_id']}")
    return normalize_source(text_path.read_text(encoding="utf-8", errors="replace")), "pdf_text", fallback_reason


def build_manifest(source_hash: str, chunk_texts: list[str]) -> dict[str, Any]:
    chunks: list[dict[str, Any]] = []
    for index, chunk_text in enumerate(chunk_texts, 1):
        chunk_id = f"chunk{index:04d}"
        filename = f"{chunk_id}.md"
        chunks.append(
            {
                "id": chunk_id,
                "order": index,
                "source_file": filename,
                "source_hash": sha256_text(chunk_text),
                "output_file": f"output_{filename}",
            }
        )
    return {"chunk_count": len(chunks), "source_hash": source_hash, "chunks": chunks}


def build_one(record: dict[str, Any], root: Path, translation_root: Path) -> dict[str, Any]:
    article_dir = translation_root / str(record["directory"])
    article_dir.mkdir(parents=True, exist_ok=True)
    source_text, source_kind, fallback_reason = select_source_text(record, root)
    source_hash = sha256_text(source_text)

    if manifest_is_current(article_dir, source_hash):
        status = json.loads((article_dir / "chunking_status.json").read_text(encoding="utf-8"))
        status.update(
            {
                "source_kind": source_kind,
                "fallback_reason": fallback_reason,
                "reused": True,
                "updated_at": now(),
            }
        )
        atomic_json(article_dir / "chunking_status.json", status)
        return status

    old_source_hash = None
    status_path = article_dir / "chunking_status.json"
    manifest_path = article_dir / "manifest.json"
    if status_path.exists():
        try:
            old_source_hash = json.loads(status_path.read_text(encoding="utf-8")).get("source_hash")
        except json.JSONDecodeError:
            old_source_hash = None
    if old_source_hash is None and manifest_path.exists():
        try:
            old_source_hash = json.loads(manifest_path.read_text(encoding="utf-8")).get("source_hash")
        except json.JSONDecodeError:
            old_source_hash = None
    if old_source_hash is not None and old_source_hash != source_hash and _translation_outputs_exist(article_dir):
        raise RuntimeError("source changed after translation outputs existed; manual review required")

    protected = PIPELINE.protect_structures(source_text)
    chunk_texts = [
        PIPELINE.validate_and_restore(chunk, _mapping_for_chunk(chunk, protected.mapping))
        for chunk in PIPELINE.semantic_chunks(
            protected.text,
            target_words=TARGET_WORDS,
            min_words=MIN_WORDS,
            max_words=MAX_WORDS,
        )
    ]
    if not chunk_texts:
        raise RuntimeError(f"No chunks were produced for {record['record_id']}")

    atomic_text(article_dir / "source.md", source_text)
    for path in article_dir.glob("chunk[0-9][0-9][0-9][0-9].md"):
        path.unlink()
    for index, chunk_text in enumerate(chunk_texts, 1):
        atomic_text(article_dir / f"chunk{index:04d}.md", chunk_text)

    manifest = build_manifest(source_hash, chunk_texts)
    atomic_json(article_dir / "manifest.json", manifest)
    glossary_path = article_dir / "glossary.json"
    if not glossary_path.exists():
        atomic_json(
            glossary_path,
            {"version": 2, "terms": [], "high_frequency_top_n": 20, "applied_meta_hashes": {}},
        )
    status = {
        "schema_version": 1,
        "record_id": record["record_id"],
        "source_hash": source_hash,
        "source_md": "source.md",
        "source_kind": source_kind,
        "fallback_reason": fallback_reason,
        "chunk_count": len(chunk_texts),
        "chunk_target_words": TARGET_WORDS,
        "chunk_min_words": MIN_WORDS,
        "chunk_max_words": MAX_WORDS,
        "chunker": "snowmass_pipeline.semantic_chunks",
        "manifest": "manifest.json",
        "status": "complete",
        "reused": False,
        "updated_at": now(),
    }
    atomic_json(article_dir / "chunking_status.json", status)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--translation-root", type=Path, default=TRANSLATION_ROOT)
    parser.add_argument("--rights-snapshot", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-records", type=int, default=0)
    args = parser.parse_args(argv)
    allowed_record_ids = load_rights_snapshot_record_ids(args.rights_snapshot)
    records = load_source_records(args.root, allowed_record_ids)[: args.max_records or None]
    args.translation_root.mkdir(parents=True, exist_ok=True)
    print(
        f"START total={len(records)} workers={args.workers} eligible_records={len(allowed_record_ids)}",
        flush=True,
    )
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(build_one, record, args.root, args.translation_root): record for record in records
        }
        for index, future in enumerate(concurrent.futures.as_completed(future_map), 1):
            record = future_map[future]
            try:
                status = future.result()
            except Exception as exc:  # noqa: BLE001 - preserve per-paper failure
                status = {"record_id": record["record_id"], "status": "failed", "error": repr(exc)}
            results.append(status)
            print(f"PROGRESS {index}/{len(records)} {status['record_id']} {status['status']}", flush=True)
    summary = {
        "updated_at": now(),
        "total_records": len(records),
        "complete": sum(item.get("status") == "complete" for item in results),
        "failed": [item for item in results if item.get("status") != "complete"],
        "total_chunks": sum(int(item.get("chunk_count", 0)) for item in results),
        "rights_snapshot": str(args.rights_snapshot),
    }
    atomic_json(args.translation_root / "chunk_summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "failed"}, ensure_ascii=False, indent=2), flush=True)
    return 0 if not summary["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
