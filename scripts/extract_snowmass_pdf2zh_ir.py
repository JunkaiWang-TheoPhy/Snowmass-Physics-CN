#!/usr/bin/env python3
"""Persist official BabelDOC 0.6.4 layout IR without any model calls."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
from typing import Any

EXPECTED_BABELDOC_VERSION = "0.6.4"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def extract_ir(source_pdf: Path, output_dir: Path) -> dict[str, Any]:
    installed = metadata.version("babeldoc")
    if installed != EXPECTED_BABELDOC_VERSION:
        raise RuntimeError(
            f"BabelDOC version mismatch: expected {EXPECTED_BABELDOC_VERSION}, got {installed}"
        )
    from babeldoc.docvision.doclayout import DocLayoutModel
    from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
    from babeldoc.format.pdf.document_il.midend.styles_and_formulas import (
        StylesAndFormulas,
    )
    from babeldoc.format.pdf.document_il.xml_converter import XMLConverter
    from babeldoc.format.pdf.new_parser.native_parse import (
        parse_prepared_pdf_with_new_parser_to_legacy_ir,
    )
    from babeldoc.format.pdf.parse_shared import build_parse_only_config
    from babeldoc.format.pdf.parse_shared import prepare_pdf_for_parse

    try:
        from snowmass_babeldoc_bridge import materialize_lazy_passthrough_instructions
    except ModuleNotFoundError:
        from scripts.snowmass_babeldoc_bridge import (
            materialize_lazy_passthrough_instructions,
        )

    source_pdf = Path(source_pdf).resolve()
    if not source_pdf.is_file():
        raise FileNotFoundError(source_pdf)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    working_dir = output_dir / "work"
    config = build_parse_only_config(source_pdf, working_dir=working_dir, debug=False)
    config.lang_in = "en"
    config.lang_out = "zh"
    config.skip_scanned_detection = True
    config.disable_rich_text_translate = True
    config.auto_extract_glossary = False
    config.progress_monitor.disable = True
    config.doc_layout_model = DocLayoutModel.load_onnx()
    doc_pdf = None
    try:
        doc_pdf, prepared_pdf = prepare_pdf_for_parse(source_pdf, config)
        document = parse_prepared_pdf_with_new_parser_to_legacy_ir(
            prepared_pdf,
            config=config,
            doc_pdf=doc_pdf,
        )
        document = LayoutParser(config).process(document, doc_pdf)
        ParagraphFinder(config).process(document)
        StylesAndFormulas(config).process(document)
        materialize_lazy_passthrough_instructions(document)
        json_path = output_dir / "babeldoc_ir.json"
        xml_path = output_dir / "babeldoc_ir.xml"
        converter = XMLConverter()
        converter.write_json(document, str(json_path))
        converter.write_xml(document, str(xml_path))
        figure_count = sum(
            1
            for page in document.page
            for paragraph in page.pdf_paragraph
            if int(paragraph.xobj_id or 0) != 0
        )
        table_count = sum(
            1
            for page in document.page
            for layout in page.page_layout
            if str(layout.class_name or "").casefold() == "table"
        )
        receipt = {
            "schema_version": 1,
            "zero_paid": True,
            "babeldoc_version": installed,
            "source_pdf": source_pdf.name,
            "source_pdf_sha256": _sha256(source_pdf),
            "page_count": len(document.page),
            "figure_text_paragraph_count": figure_count,
            "table_region_count": table_count,
            "ir_json": json_path.name,
            "ir_json_sha256": _sha256(json_path),
            "ir_xml": xml_path.name,
            "ir_xml_sha256": _sha256(xml_path),
        }
        (output_dir / "receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return receipt
    finally:
        if doc_pdf is not None:
            doc_pdf.close()
        config.cleanup_temp_files()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            extract_ir(args.source_pdf, args.output_dir), indent=2, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
