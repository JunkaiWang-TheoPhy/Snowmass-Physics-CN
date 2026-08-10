#!/usr/bin/env python3
"""Bridge BabelDOC paragraph IR into the translate-book file contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Iterable


BABELDOC_VERSION = "0.6.4"
MAX_STRUCTURE_COUNT = 40


@dataclass(frozen=True)
class DocumentUnit:
    page_number: int
    paragraph_index: int
    layout_label: str
    text: str
    structure_count: int


@dataclass(frozen=True)
class ExtractionResult:
    units: tuple[DocumentUnit, ...]
    ir_json_path: Path
    ir_xml_path: Path
    babeldoc_version: str


@dataclass(frozen=True)
class RefillTranslation:
    page_number: int
    paragraph_index: int
    source_text: str
    translated_text: str


@dataclass(frozen=True)
class RefillResult:
    output_xml_path: Path
    refilled_unit_count: int


_BABELDOC_PLACEHOLDER = re.compile(
    r"\{v\d+\}|<\s*style\s+id\s*=\s*['\"]\d+['\"]\s*>|<\s*/\s*style\s*>",
    re.IGNORECASE,
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)


def _validate_units(units: Iterable[DocumentUnit]) -> list[DocumentUnit]:
    validated = list(units)
    for index, unit in enumerate(validated):
        if unit.page_number < 1:
            raise ValueError(f"unit {index} has invalid page_number {unit.page_number}")
        if unit.paragraph_index < 0:
            raise ValueError(f"unit {index} has invalid paragraph_index {unit.paragraph_index}")
        if unit.structure_count < 0:
            raise ValueError(f"unit {index} has invalid structure_count {unit.structure_count}")
        if not unit.text.strip():
            raise ValueError(f"unit {index} has blank text")
    return validated


def write_translation_workspace(
    article_dir: Path,
    *,
    record_id: str,
    source_pdf: Path,
    units: Iterable[DocumentUnit],
    allowed_record_ids: set[str],
    ir_json_path: Path | None = None,
    ir_xml_path: Path | None = None,
) -> dict[str, Any]:
    """Persist IR units using the manifest consumed by translate-book."""

    if record_id not in allowed_record_ids:
        raise PermissionError(f"Record is outside the publication rights gate: {record_id}")
    source_pdf = Path(source_pdf)
    if not source_pdf.is_file():
        raise FileNotFoundError(source_pdf)
    validated = _validate_units(units)
    if (ir_json_path is None) != (ir_xml_path is None):
        raise ValueError("BabelDOC JSON and XML IR must be supplied together")
    if ir_json_path is not None and ir_xml_path is not None:
        ir_json_path = Path(ir_json_path)
        ir_xml_path = Path(ir_xml_path)
        if not ir_json_path.is_file() or not ir_xml_path.is_file():
            raise FileNotFoundError("BabelDOC JSON or XML IR is missing")

    chunks: list[dict[str, Any]] = []
    unit_records: list[dict[str, Any]] = []
    for order, unit in enumerate(validated, 1):
        chunk_id = f"chunk{order:04d}"
        source_file = f"{chunk_id}.md"
        output_file = f"output_{chunk_id}.md"
        source_hash = _sha256_bytes(unit.text.encode("utf-8"))
        _atomic_text(article_dir / source_file, unit.text)
        unit_id = f"p{unit.page_number:04d}-i{unit.paragraph_index:04d}"
        chunks.append(
            {
                "id": chunk_id,
                "order": order,
                "source_file": source_file,
                "output_file": output_file,
                "source_hash": source_hash,
                "babeldoc_unit_id": unit_id,
                "page_number": unit.page_number,
                "paragraph_index": unit.paragraph_index,
                "layout_label": unit.layout_label,
                "structure_count": unit.structure_count,
            }
        )
        unit_records.append({"chunk_id": chunk_id, "unit_id": unit_id, **asdict(unit)})

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "record_id": record_id,
        "input_mode": "babeldoc_ir",
        "babeldoc_version": BABELDOC_VERSION,
        "source_pdf_path": str(source_pdf.resolve()),
        "source_pdf_sha256": _sha256_file(source_pdf),
        "chunks": chunks,
    }
    if ir_json_path is not None and ir_xml_path is not None:
        exported_json = article_dir / "babeldoc_ir.json"
        exported_xml = article_dir / "babeldoc_ir.xml"
        _atomic_copy(ir_json_path, exported_json)
        _atomic_copy(ir_xml_path, exported_xml)
        manifest.update(
            {
                "babeldoc_ir_json_file": exported_json.name,
                "babeldoc_ir_json_sha256": _sha256_file(exported_json),
                "babeldoc_ir_xml_file": exported_xml.name,
                "babeldoc_ir_xml_sha256": _sha256_file(exported_xml),
            }
        )
    ir_units_path = article_dir / "ir_units.json"
    _atomic_json(ir_units_path, {"version": 1, "units": unit_records})
    manifest.update(
        {
            "ir_units_file": ir_units_path.name,
            "ir_units_sha256": _sha256_file(ir_units_path),
        }
    )
    _atomic_json(article_dir / "manifest.json", manifest)
    _atomic_json(
        article_dir / "chunking_status.json",
        {
            "record_id": record_id,
            "input_mode": "babeldoc_ir",
            "babeldoc_version": BABELDOC_VERSION,
            "unit_count": len(validated),
            "source_pdf_sha256": manifest["source_pdf_sha256"],
        },
    )
    return manifest


def _babeldoc_placeholder_translator():
    from babeldoc.translator.translator import BaseTranslator

    class PlaceholderTranslator(BaseTranslator):
        name = "snowmass-ir"
        model = "no-network"

        def do_translate(self, text, rate_limit_params=None):
            raise RuntimeError("IR extraction must not call a translation provider")

        def do_llm_translate(self, text, rate_limit_params=None):
            if text is None:
                return None
            raise RuntimeError("IR extraction must not call a translation provider")

        def get_formular_placeholder(self, placeholder_id):
            return "{v" + str(placeholder_id) + "}", rf"\{{\s*v\s*{placeholder_id}\s*\}}"

        def get_rich_text_left_placeholder(self, placeholder_id):
            return (
                f"<style id='{placeholder_id}'>",
                rf"<\s*style\s*id\s*=\s*'\s*{placeholder_id}\s*'\s*>",
            )

        def get_rich_text_right_placeholder(self, placeholder_id):
            return "</style>", r"<\s*\/\s*style\s*>"

    return PlaceholderTranslator("en", "zh", ignore_cache=True)


def _with_terminal_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _placeholder_sequence(text: str) -> tuple[str, ...]:
    return tuple(match.group(0).lower() for match in _BABELDOC_PLACEHOLDER.finditer(text))


def placeholder_sequence_matches(source: str, translated: str) -> bool:
    """Require every BabelDOC object marker to retain identity and order."""

    return _placeholder_sequence(source) == _placeholder_sequence(translated)


def refill_document_units(
    ir_xml_path: Path,
    *,
    source_pdf: Path,
    working_dir: Path,
    output_xml: Path,
    translations: Iterable[RefillTranslation],
) -> RefillResult:
    """Refill translated paragraphs into a persisted BabelDOC XML document IR."""

    installed_version = metadata.version("babeldoc")
    if installed_version != BABELDOC_VERSION:
        raise RuntimeError(
            f"BabelDOC version mismatch: expected {BABELDOC_VERSION}, got {installed_version}"
        )
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    from babeldoc.format.pdf.document_il.midend.il_translator import ParagraphTranslateTracker
    from babeldoc.format.pdf.document_il.xml_converter import XMLConverter
    from babeldoc.format.pdf.parse_shared import build_parse_only_config

    ir_xml_path = Path(ir_xml_path)
    source_pdf = Path(source_pdf)
    output_xml = Path(output_xml)
    if not ir_xml_path.is_file():
        raise FileNotFoundError(ir_xml_path)
    if not source_pdf.is_file():
        raise FileNotFoundError(source_pdf)
    requested = list(translations)
    identities = [(item.page_number, item.paragraph_index) for item in requested]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate BabelDOC paragraph identity in refill translations")

    config = build_parse_only_config(source_pdf, working_dir=Path(working_dir), debug=True)
    config.lang_in = "en"
    config.lang_out = "zh"
    config.disable_rich_text_translate = True
    config.auto_extract_glossary = False
    config.progress_monitor.disable = True
    try:
        converter = XMLConverter()
        docs = converter.read_xml(str(ir_xml_path))
        il_translator = ILTranslator(_babeldoc_placeholder_translator(), config)
        for item in requested:
            if item.page_number < 1 or item.page_number > len(docs.page):
                raise IndexError(f"BabelDOC page out of range: {item.page_number}")
            page = docs.page[item.page_number - 1]
            if item.paragraph_index < 0 or item.paragraph_index >= len(page.pdf_paragraph):
                raise IndexError(
                    f"BabelDOC paragraph out of range: {item.page_number}/{item.paragraph_index}"
                )
            paragraph = page.pdf_paragraph[item.paragraph_index]
            page_font_map = {font.font_id: font for font in page.pdf_font}
            xobj_font_map: dict[int, dict[str, Any]] = {}
            for xobj in page.pdf_xobject:
                xobj_font_map[xobj.xobj_id] = page_font_map.copy()
                for font in xobj.pdf_font:
                    xobj_font_map[xobj.xobj_id][font.font_id] = font
            tracker = ParagraphTranslateTracker()
            source_text, translate_input = il_translator.pre_translate_paragraph(
                paragraph,
                tracker,
                page_font_map,
                xobj_font_map,
            )
            if source_text is None or translate_input is None:
                raise RuntimeError(
                    f"BabelDOC paragraph is not translatable: {item.page_number}/{item.paragraph_index}"
                )
            if _with_terminal_newline(source_text) != _with_terminal_newline(item.source_text):
                raise RuntimeError(
                    f"BabelDOC source changed before refill: {item.page_number}/{item.paragraph_index}"
                )
            if not placeholder_sequence_matches(source_text, item.translated_text):
                raise RuntimeError(
                    "BabelDOC placeholder identity or order changed in translation: "
                    f"{item.page_number}/{item.paragraph_index}"
                )
            il_translator.post_translate_paragraph(
                paragraph,
                tracker,
                translate_input,
                item.translated_text,
            )
        output_xml.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_xml.with_name(output_xml.name + ".tmp")
        converter.write_xml(docs, str(temporary))
        os.replace(temporary, output_xml)
        return RefillResult(output_xml, len(requested))
    finally:
        config.cleanup_temp_files()


def extract_document_units(
    source_pdf: Path,
    *,
    working_dir: Path,
) -> ExtractionResult:
    """Parse a PDF into BabelDOC paragraph/formula units without model calls."""

    installed_version = metadata.version("babeldoc")
    if installed_version != BABELDOC_VERSION:
        raise RuntimeError(
            f"BabelDOC version mismatch: expected {BABELDOC_VERSION}, got {installed_version}"
        )

    from babeldoc.docvision.doclayout import DocLayoutModel
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    from babeldoc.format.pdf.document_il.midend.il_translator import ParagraphTranslateTracker
    from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
    from babeldoc.format.pdf.document_il.midend.styles_and_formulas import StylesAndFormulas
    from babeldoc.format.pdf.document_il.xml_converter import XMLConverter
    from babeldoc.format.pdf.new_parser.native_parse import (
        parse_prepared_pdf_with_new_parser_to_legacy_ir,
    )
    from babeldoc.format.pdf.parse_shared import build_parse_only_config
    from babeldoc.format.pdf.parse_shared import prepare_pdf_for_parse

    source_pdf = Path(source_pdf)
    if not source_pdf.is_file():
        raise FileNotFoundError(source_pdf)
    working_dir = Path(working_dir)
    config = build_parse_only_config(
        source_pdf,
        working_dir=working_dir,
        debug=True,
    )
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
        docs = parse_prepared_pdf_with_new_parser_to_legacy_ir(
            prepared_pdf,
            config=config,
            doc_pdf=doc_pdf,
        )
        docs = LayoutParser(config).process(docs, doc_pdf)
        ParagraphFinder(config).process(docs)
        StylesAndFormulas(config).process(docs)

        ir_json_path = Path(config.get_working_file_path("styles_and_formulas.json"))
        ir_xml_path = Path(config.get_working_file_path("styles_and_formulas.xml"))
        converter = XMLConverter()
        converter.write_json(docs, str(ir_json_path))
        converter.write_xml(docs, str(ir_xml_path))

        translator = _babeldoc_placeholder_translator()
        il_translator = ILTranslator(translator, config)
        units: list[DocumentUnit] = []
        for page_number, page in enumerate(docs.page, 1):
            page_font_map = {font.font_id: font for font in page.pdf_font}
            xobj_font_map: dict[int, dict[str, Any]] = {}
            for xobj in page.pdf_xobject:
                xobj_font_map[xobj.xobj_id] = page_font_map.copy()
                for font in xobj.pdf_font:
                    xobj_font_map[xobj.xobj_id][font.font_id] = font
            for paragraph_index, paragraph in enumerate(page.pdf_paragraph):
                tracker = ParagraphTranslateTracker()
                text, translate_input = il_translator.pre_translate_paragraph(
                    paragraph,
                    tracker,
                    page_font_map,
                    xobj_font_map,
                )
                if not text or not translate_input:
                    continue
                structure_count = len(translate_input.placeholders)
                if structure_count > MAX_STRUCTURE_COUNT:
                    raise RuntimeError(
                        "BabelDOC paragraph exceeds the structure-density limit: "
                        f"page={page_number} paragraph={paragraph_index} "
                        f"count={structure_count} limit={MAX_STRUCTURE_COUNT}"
                    )
                if not text.endswith("\n"):
                    text += "\n"
                units.append(
                    DocumentUnit(
                        page_number=page_number,
                        paragraph_index=paragraph_index,
                        layout_label=str(paragraph.layout_label or ""),
                        text=text,
                        structure_count=structure_count,
                    )
                )
        return ExtractionResult(
            units=tuple(units),
            ir_json_path=ir_json_path,
            ir_xml_path=ir_xml_path,
            babeldoc_version=installed_version,
        )
    finally:
        if doc_pdf is not None:
            doc_pdf.close()
        config.cleanup_temp_files()
