#!/usr/bin/env python3
"""Prepare resumable, rights-gated Snowmass translation workspaces with BabelDOC."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import snowmass_constraint_compiler as constraint_compiler


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RIGHTS_MANIFEST = ROOT / "site/data/papers.json"
DEFAULT_PDF_ROOT = ROOT / "tmp/pdfs/snowmass2021"
DEFAULT_OUTPUT_ROOT = ROOT / "output/snowmass2021/babeldoc_translation"
DEFAULT_HARD_CONSTRAINTS = ROOT / "translations/snowmass-hard-constraints.json"


def _load_bridge():
    path = Path(__file__).with_name("snowmass_babeldoc_bridge.py")
    spec = importlib.util.spec_from_file_location("snowmass_babeldoc_bridge", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load BabelDOC bridge: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BRIDGE = _load_bridge()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_allowed_record_ids(path: Path) -> set[str]:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise RuntimeError(f"Rights manifest must be a JSON list: {path}")
    allowed: set[str] = set()
    seen: set[str] = set()
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
            allowed.add(record_id)
    if not allowed:
        raise RuntimeError(f"Rights manifest contains no explicitly allowed records: {path}")
    return allowed


def safe_record_name(record_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", record_id).strip("_")


def workspace_is_current(article_dir: Path, source_pdf: Path, record_id: str) -> bool:
    manifest_path = article_dir / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("record_id") != record_id:
            return False
        if manifest.get("input_mode") != "babeldoc_ir":
            return False
        if manifest.get("babeldoc_version") != BRIDGE.BABELDOC_VERSION:
            return False
        if manifest.get("ir_pipeline_version") != BRIDGE.IR_PIPELINE_VERSION:
            return False
        if manifest.get("source_pdf_sha256") != _sha256(source_pdf):
            return False
        for file_key, hash_key in (
            ("babeldoc_ir_json_file", "babeldoc_ir_json_sha256"),
            ("babeldoc_ir_xml_file", "babeldoc_ir_xml_sha256"),
            ("ir_units_file", "ir_units_sha256"),
        ):
            artifact = article_dir / manifest[file_key]
            if not artifact.is_file() or _sha256(artifact) != manifest[hash_key]:
                return False
        chunks = manifest.get("chunks")
        if not chunks:
            return False
        for chunk in chunks:
            source = article_dir / chunk["source_file"]
            if not source.is_file() or _sha256(source) != chunk.get("source_hash"):
                return False
        return True
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _write_report(
    output_root: Path,
    rights_manifest: Path,
    eligible_count: int,
    results: list[dict[str, Any]],
) -> None:
    _atomic_json(
        output_root / "preparation_report.json",
        {
            "schema_version": 1,
            "rights_manifest_path": str(rights_manifest.resolve()),
            "rights_manifest_sha256": _sha256(rights_manifest),
            "eligible_record_count": eligible_count,
            "selected_record_count": len(results),
            "completed_record_count": sum(
                item["status"] in {"completed", "reused"} for item in results
            ),
            "records": results,
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rights-manifest", type=Path, default=DEFAULT_RIGHTS_MANIFEST)
    parser.add_argument("--pdf-root", type=Path, default=DEFAULT_PDF_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--record-id", action="append", dest="record_ids")
    args = parser.parse_args(argv)

    allowed = load_allowed_record_ids(args.rights_manifest)
    requested = args.record_ids or sorted(allowed)
    blocked = sorted(set(requested) - allowed)
    if blocked:
        print(
            "Refusing records outside the publication rights gate: " + ", ".join(blocked),
            file=sys.stderr,
        )
        return 2

    results: list[dict[str, Any]] = []
    exit_code = 0
    for record_id in requested:
        safe_name = safe_record_name(record_id)
        source_pdf = args.pdf_root / f"{safe_name}.pdf"
        article_dir = args.output_root / "papers" / safe_name
        if not source_pdf.is_file():
            results.append(
                {"record_id": record_id, "status": "failed", "error": "source_pdf_missing"}
            )
            exit_code = 1
            _write_report(args.output_root, args.rights_manifest, len(allowed), results)
            continue
        if workspace_is_current(article_dir, source_pdf, record_id):
            try:
                manifest = json.loads((article_dir / "manifest.json").read_text(encoding="utf-8"))
                constraints = constraint_compiler.load_constraints(
                    article_dir, record_id, DEFAULT_HARD_CONSTRAINTS
                )
                constraint_compiler.write_constraint_plan(
                    article_dir,
                    constraint_compiler.compile_constraint_plan(article_dir, manifest, constraints),
                )
                results.append({"record_id": record_id, "status": "reused"})
            except Exception as error:
                results.append(
                    {"record_id": record_id, "status": "failed", "error": f"{type(error).__name__}: {error}"}
                )
                exit_code = 1
            _write_report(args.output_root, args.rights_manifest, len(allowed), results)
            continue
        try:
            extraction = BRIDGE.extract_document_units(
                source_pdf,
                working_dir=args.output_root / ".babeldoc-work" / safe_name,
            )
            BRIDGE.write_translation_workspace(
                article_dir,
                record_id=record_id,
                source_pdf=source_pdf,
                units=extraction.units,
                allowed_record_ids=allowed,
                ir_json_path=extraction.ir_json_path,
                ir_xml_path=extraction.ir_xml_path,
            )
            manifest = json.loads((article_dir / "manifest.json").read_text(encoding="utf-8"))
            constraints = constraint_compiler.load_constraints(
                article_dir, record_id, DEFAULT_HARD_CONSTRAINTS
            )
            constraint_compiler.write_constraint_plan(
                article_dir,
                constraint_compiler.compile_constraint_plan(article_dir, manifest, constraints),
            )
            results.append(
                {"record_id": record_id, "status": "completed", "unit_count": len(extraction.units)}
            )
        except Exception as error:
            results.append(
                {"record_id": record_id, "status": "failed", "error": f"{type(error).__name__}: {error}"}
            )
            exit_code = 1
        _write_report(args.output_root, args.rights_manifest, len(allowed), results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
