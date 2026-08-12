#!/usr/bin/env python3
"""Refill verified Snowmass translations into persisted BabelDOC XML IR."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RIGHTS_MANIFEST = ROOT / "site/data/papers.json"
REFILL_SCHEMA_VERSION = 3


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
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _allowed_record_ids(path: Path) -> set[str]:
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
    return allowed


def _resolve_source_pdf(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _translation_inputs(article_dir: Path, manifest: dict[str, Any]):
    translations = []
    hashes: dict[str, dict[str, str]] = {}
    for chunk in sorted(manifest.get("chunks", []), key=lambda item: item.get("order", 0)):
        chunk_id = str(chunk["id"])
        source_path = article_dir / str(chunk["source_file"])
        output_path = article_dir / str(chunk["output_file"])
        status_path = article_dir / "chunk_status" / f"{chunk_id}.json"
        if not source_path.is_file() or not output_path.is_file() or not status_path.is_file():
            raise RuntimeError(f"Translation checkpoint is incomplete: {chunk_id}")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        academic = status.get("stages", {}).get("academic", {})
        source_hash = _sha256(source_path)
        output_hash = _sha256(output_path)
        if (
            chunk.get("source_hash") != source_hash
            or status.get("source_hash") != source_hash
            or status.get("status") != "complete"
            or academic.get("status") != "complete"
            or academic.get("output_hash") != output_hash
        ):
            raise RuntimeError(f"Academic translation checkpoint is not verified: {chunk_id}")
        source_text = source_path.read_text(encoding="utf-8")
        translated_text = output_path.read_text(encoding="utf-8")
        translations.append(
            BRIDGE.RefillTranslation(
                page_number=int(chunk["page_number"]),
                paragraph_index=int(chunk["paragraph_index"]),
                source_text=source_text,
                translated_text=translated_text,
            )
        )
        hashes[chunk_id] = {"source_sha256": source_hash, "output_sha256": output_hash}
    if not translations:
        raise RuntimeError("BabelDOC manifest contains no translation units")
    return translations, hashes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--article-dir", type=Path, required=True)
    parser.add_argument("--rights-manifest", type=Path, default=DEFAULT_RIGHTS_MANIFEST)
    args = parser.parse_args(argv)
    manifest = json.loads((args.article_dir / "manifest.json").read_text(encoding="utf-8"))
    record_id = manifest.get("record_id")
    if manifest.get("input_mode") != "babeldoc_ir":
        raise RuntimeError("Article is not a BabelDOC IR workspace")
    if record_id not in _allowed_record_ids(args.rights_manifest):
        print(f"Refusing record outside the publication rights gate: {record_id}", file=sys.stderr)
        return 2

    source_pdf = _resolve_source_pdf(str(manifest["source_pdf_path"]))
    ir_xml = args.article_dir / str(manifest["babeldoc_ir_xml_file"])
    translations, chunk_hashes = _translation_inputs(args.article_dir, manifest)
    signature_payload = {
        "refill_schema_version": REFILL_SCHEMA_VERSION,
        "babeldoc_version": BRIDGE.BABELDOC_VERSION,
        "ir_pipeline_version": BRIDGE.IR_PIPELINE_VERSION,
        "record_id": record_id,
        "source_pdf_sha256": _sha256(source_pdf),
        "ir_xml_sha256": _sha256(ir_xml),
        "chunks": chunk_hashes,
    }
    signature = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    output_xml = args.article_dir / "babeldoc_translated_ir.xml"
    render_dir = args.article_dir / "rendered"
    mono_pdf = render_dir / "translated_mono.pdf"
    dual_pdf = render_dir / "translated_dual.pdf"
    status_path = args.article_dir / "refill_status.json"
    if (
        status_path.is_file()
        and output_xml.is_file()
        and mono_pdf.is_file()
        and dual_pdf.is_file()
    ):
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if (
            status.get("status") == "complete"
            and status.get("refill_schema_version") == REFILL_SCHEMA_VERSION
            and status.get("input_signature") == signature
            and status.get("output_xml_sha256") == _sha256(output_xml)
            and status.get("mono_pdf_sha256") == _sha256(mono_pdf)
            and status.get("dual_pdf_sha256") == _sha256(dual_pdf)
        ):
            return 0

    result = BRIDGE.refill_document_units(
        ir_xml,
        source_pdf=source_pdf,
        working_dir=args.article_dir / ".babeldoc-refill-work",
        output_xml=output_xml,
        translations=translations,
    )
    rendered = BRIDGE.render_translated_document(
        output_xml,
        source_pdf=source_pdf,
        working_dir=args.article_dir / ".babeldoc-render-work",
        output_dir=render_dir,
    )
    _atomic_json(
        status_path,
        {
            "schema_version": 1,
            "refill_schema_version": REFILL_SCHEMA_VERSION,
            "babeldoc_version": BRIDGE.BABELDOC_VERSION,
            "ir_pipeline_version": BRIDGE.IR_PIPELINE_VERSION,
            "status": "complete",
            "record_id": record_id,
            "input_signature": signature,
            "refilled_unit_count": result.refilled_unit_count,
            "output_xml_file": output_xml.name,
            "output_xml_sha256": _sha256(output_xml),
            "mono_pdf_file": str(rendered.mono_pdf_path.relative_to(args.article_dir)),
            "mono_pdf_sha256": _sha256(rendered.mono_pdf_path),
            "dual_pdf_file": str(rendered.dual_pdf_path.relative_to(args.article_dir)),
            "dual_pdf_sha256": _sha256(rendered.dual_pdf_path),
            "chunks": chunk_hashes,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
