#!/usr/bin/env python3
"""Deterministically restore source-only PDF regions after pdf2zh-next rendering."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET

import fitz


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).replace("\x03", "")


def _box(element: ET.Element) -> tuple[float, float, float, float] | None:
    child = element.find("box")
    if child is None:
        return None
    try:
        return tuple(float(child.attrib[name]) for name in ("x", "y", "x2", "y2"))  # type: ignore[return-value]
    except (KeyError, ValueError):
        return None


def _ir_regions(
    ir_xml: Path,
) -> tuple[
    list[tuple[int, tuple[float, float, float, float]]],
    list[tuple[int, tuple[float, float, float, float]]],
]:
    root = ET.parse(ir_xml).getroot()
    figures: list[tuple[int, tuple[float, float, float, float]]] = []
    tables: list[tuple[int, tuple[float, float, float, float]]] = []
    for page in root.findall(".//page"):
        page_number = int(page.attrib.get("pageNumber", "0")) + 1
        owned_xobjects = {
            int(paragraph.attrib.get("xobjId", "0"))
            for paragraph in page.findall(".//pdfParagraph")
            if int(paragraph.attrib.get("xobjId", "0")) != 0
        }
        for xobject in page.findall(".//pdfXobject"):
            xobj_id = int(xobject.attrib.get("xobjId", "0"))
            region = _box(xobject)
            if xobj_id in owned_xobjects and region is not None:
                figures.append((page_number, region))
        for layout in page.findall("pageLayout"):
            label = str(layout.attrib.get("class_name", "")).casefold()
            region = _box(layout)
            if label == "table" and region is not None:
                tables.append((page_number, region))
    return figures, tables


def _top_rect(
    page: fitz.Page, bottom_origin_box: tuple[float, float, float, float]
) -> fitz.Rect:
    x0, y0, x1, y1 = bottom_origin_box
    return fitz.Rect(x0, page.rect.height - y1, x1, page.rect.height - y0)


def _coalesce(rectangles: list[fitz.Rect]) -> list[fitz.Rect]:
    merged: list[fitz.Rect] = []
    for rectangle in rectangles:
        for index, existing in enumerate(merged):
            intersection = rectangle & existing
            smaller = min(rectangle.get_area(), existing.get_area())
            if smaller and intersection.get_area() / smaller >= 0.95:
                merged[index] = rectangle | existing
                break
        else:
            merged.append(rectangle)
    return merged


def _coalesce_adjacent_text(rectangles: list[fitz.Rect]) -> list[fitz.Rect]:
    """Join neighboring explicit verbatim lines into one redaction/restore block."""

    pending = sorted(rectangles, key=lambda rectangle: (rectangle.y0, rectangle.x0))
    merged: list[fitz.Rect] = []
    for rectangle in pending:
        if not merged:
            merged.append(rectangle)
            continue
        existing = merged[-1]
        horizontal_overlap = max(
            0.0, min(existing.x1, rectangle.x1) - max(existing.x0, rectangle.x0)
        )
        smaller_width = min(existing.width, rectangle.width)
        vertical_gap = rectangle.y0 - existing.y1
        if smaller_width and horizontal_overlap / smaller_width >= 0.5 and vertical_gap <= 5:
            merged[-1] = existing | rectangle
        else:
            merged.append(rectangle)
    return merged


def _reference_clips(source: fitz.Document) -> dict[int, fitz.Rect]:
    clips: dict[int, fitz.Rect] = {}
    in_references = False
    for page_index, page in enumerate(source):
        blocks = [
            block
            for block in page.get_text("blocks", sort=True)
            if str(block[4]).strip()
        ]
        heading_rects = [
            *page.search_for("References"),
            *page.search_for("Bibliography"),
        ]
        heading = (
            min(heading_rects, key=lambda rectangle: rectangle.y0)
            if heading_rects
            else None
        )
        if heading is not None:
            in_references = True
        if not in_references:
            continue
        numbered = [block for block in blocks if re.match(r"\s*\[\d+\]", str(block[4]))]
        if not numbered:
            if heading is None:
                in_references = False
            continue
        body_blocks = [
            block
            for block in blocks
            if block[1] < page.rect.height - 35
            and (heading is None or block[1] > heading.y1)
            and not re.fullmatch(r"\s*\d+\s*", str(block[4]))
        ]
        y0 = max(0.0, min(block[1] for block in numbered) - 7.0)
        y1 = min(page.rect.height, max(block[3] for block in body_blocks) + 3.0)
        clips[page_index + 1] = fitz.Rect(65.0, y0, page.rect.width - 65.0, y1)
    return clips


def _find_header_rect(page: fitz.Page, source_header: str) -> fitz.Rect | None:
    matches = [
        rectangle
        for rectangle in page.search_for(source_header)
        if rectangle.y0 < page.rect.height * 0.16
    ]
    return min(matches, key=lambda rectangle: rectangle.y0) if matches else None


def _insert_header(page: fitz.Page, rectangle: fitz.Rect, text: str) -> None:
    patch = fitz.Rect(
        rectangle.x0 - 2, rectangle.y0 - 1, rectangle.x1 + 2, rectangle.y1 + 0.5
    )
    page.add_redact_annot(patch, fill=(1, 1, 1))
    page.apply_redactions()
    maximum = max(8.0, min(12.0, rectangle.height * 0.9))
    for font_size in (maximum, 10.0, 9.0, 8.0, 7.0, 6.0):
        result = page.insert_textbox(
            patch,
            text,
            fontsize=min(maximum, font_size),
            fontname="china-s",
            color=(0, 0, 0),
            align=fitz.TEXT_ALIGN_CENTER,
            overlay=True,
        )
        if result >= 0:
            return
    raise RuntimeError("Canonical running header does not fit its source rectangle")


def _source_text_rect(page: fitz.Page, text: str) -> fitz.Rect | None:
    matches = page.search_for(text)
    if matches:
        rectangle = fitz.Rect(matches[0])
        for match in matches[1:]:
            rectangle |= match
        return rectangle
    expected = _normalize(text)
    for block in page.get_text("blocks", sort=True):
        if _normalize(str(block[4])) == expected:
            return fitz.Rect(*block[:4])
    return None


def _replace_fixed_text(
    page: fitz.Page,
    source_rectangle: fitz.Rect,
    target: str,
    *,
    align: int = fitz.TEXT_ALIGN_CENTER,
) -> None:
    patch = fitz.Rect(source_rectangle)
    for block in page.get_text("blocks", sort=True):
        block_rectangle = fitz.Rect(*block[:4])
        if (block_rectangle & source_rectangle).get_area() > 0:
            patch |= block_rectangle
    patch = fitz.Rect(patch.x0 - 2, patch.y0 - 2, patch.x1 + 2, patch.y1 + 2)
    page.add_redact_annot(patch, fill=(1, 1, 1))
    page.apply_redactions()
    lines = target.splitlines() or [target]
    segmented_lines = [
        [
            segment
            for segment in re.findall(r"[\x20-\x7e]+|[^\x20-\x7e]+", line)
            if segment
        ]
        for line in lines
    ]
    for font_size in (12.0, 11.0, 10.0, 9.0, 8.0, 7.0, 6.0):
        line_widths = [
            [
                fitz.Font(
                    fontname="helv" if segment.isascii() else "china-s"
                ).text_length(segment, fontsize=font_size)
                for segment in segments
            ]
            for segments in segmented_lines
        ]
        line_height = font_size * 1.25
        if max((sum(widths) for widths in line_widths), default=0.0) > patch.width:
            continue
        if line_height * len(lines) > patch.height:
            continue
        baseline = patch.y0 + (patch.height - line_height * len(lines)) / 2 + font_size
        for segments, widths in zip(segmented_lines, line_widths, strict=True):
            x = (
                patch.x0
                if align == fitz.TEXT_ALIGN_LEFT
                else patch.x0 + (patch.width - sum(widths)) / 2
            )
            for segment, width in zip(segments, widths, strict=True):
                page.insert_text(
                    (x, baseline),
                    segment,
                    fontsize=font_size,
                    fontname="helv" if segment.isascii() else "china-s",
                    color=(0, 0, 0),
                    overlay=True,
                )
                x += width
            baseline += line_height
        return
    for font_size in (12.0, 11.0, 10.0, 9.0, 8.0, 7.0, 6.0):
        result = page.insert_textbox(
            patch,
            target,
            fontsize=font_size,
            fontname="china-s",
            color=(0, 0, 0),
            align=align,
            overlay=True,
        )
        if result >= 0:
            return
    raise RuntimeError(f"Fixed text does not fit source region: {target[:40]}")


def _replace_output_text(page: fitz.Page, source_text: str, target_text: str) -> int:
    matches = page.search_for(source_text)
    if not matches:
        raise RuntimeError(f"Translated output text was not found: {source_text}")
    patches = [
        fitz.Rect(match.x0 - 1, match.y0 - 1, match.x1 + 1, match.y1 + 1)
        for match in matches
    ]
    for patch in patches:
        page.add_redact_annot(patch, fill=(1, 1, 1))
    page.apply_redactions()
    for patch in patches:
        for font_size in (patch.height * 0.78, 10.0, 9.0, 8.0, 7.0, 6.0):
            result = page.insert_textbox(
                patch,
                target_text,
                fontsize=min(font_size, patch.height * 0.78),
                fontname="helv" if target_text.isascii() else "china-s",
                color=(0, 0, 0),
                align=fitz.TEXT_ALIGN_CENTER,
                overlay=True,
            )
            if result >= 0:
                break
        else:
            raise RuntimeError(f"Output replacement does not fit: {target_text}")
    return len(patches)


def protect_pdf(
    *,
    source_pdf: Path,
    translated_pdf: Path,
    output_pdf: Path,
    selected_source_pages: tuple[int, ...],
    ir_xml: Path,
    source_header: str,
    target_header: str,
    fixed_replacements: tuple[tuple[int, str, str], ...] = (),
    left_fixed_replacements: tuple[tuple[int, str, str], ...] = (),
    verbatim_texts: tuple[tuple[int, str], ...] = (),
    output_replacements: tuple[tuple[int, str, str], ...] = (),
) -> dict[str, object]:
    """Restore figure/table/reference regions and enforce one canonical header."""

    figures, tables = _ir_regions(ir_xml)
    page_map = {
        source_page: output_index
        for output_index, source_page in enumerate(selected_source_pages)
    }
    source = fitz.open(source_pdf)
    reference_clips = _reference_clips(source)
    translated = fitz.open(translated_pdf)
    if len(translated) != len(selected_source_pages):
        source.close()
        translated.close()
        raise RuntimeError("Translated page count does not match selected source pages")

    rectangles_by_output: dict[int, list[fitz.Rect]] = {}
    kind_counts = {"figure": 0, "table": 0, "reference": 0}
    for kind, regions in (("figure", figures), ("table", tables)):
        for source_page_number, box in regions:
            output_index = page_map.get(source_page_number)
            if output_index is None:
                continue
            rectangles_by_output.setdefault(output_index, []).append(
                _top_rect(source[source_page_number - 1], box)
            )
            kind_counts[kind] += 1
    for source_page_number, clip in reference_clips.items():
        output_index = page_map.get(source_page_number)
        if output_index is None:
            continue
        rectangles_by_output.setdefault(output_index, []).append(clip)
        kind_counts["reference"] += 1
    verbatim_text_count = 0
    verbatim_rectangles_by_output: dict[int, list[fitz.Rect]] = {}
    for source_page_number, source_text in verbatim_texts:
        output_index = page_map.get(source_page_number)
        if output_index is None:
            continue
        rectangle = _source_text_rect(source[source_page_number - 1], source_text)
        if rectangle is None:
            raise RuntimeError(
                f"Verbatim source text was not found on page {source_page_number}"
            )
        verbatim_rectangles_by_output.setdefault(output_index, []).append(rectangle)
        verbatim_text_count += 1
    for output_index, rectangles in verbatim_rectangles_by_output.items():
        rectangles_by_output.setdefault(output_index, []).extend(
            _coalesce_adjacent_text(rectangles)
        )

    protected_regions: list[tuple[int, int, fitz.Rect]] = []
    for output_index, rectangles in rectangles_by_output.items():
        source_page_number = selected_source_pages[output_index]
        output_page = translated[output_index]
        for rectangle in _coalesce(rectangles):
            output_page.add_redact_annot(rectangle, fill=(1, 1, 1))
        output_page.apply_redactions()
        for rectangle in _coalesce(rectangles):
            output_page.show_pdf_page(
                rectangle,
                source,
                source_page_number - 1,
                clip=rectangle,
                keep_proportion=False,
                overlay=True,
            )
            protected_regions.append((output_index, source_page_number, rectangle))

    header_count = 0
    for output_index, source_page_number in enumerate(selected_source_pages):
        source_rect = _find_header_rect(source[source_page_number - 1], source_header)
        if source_rect is None:
            continue
        _insert_header(translated[output_index], source_rect, target_header)
        header_count += 1

    fixed_replacement_count = 0
    fixed_checks: list[tuple[int, fitz.Rect, str]] = []
    for source_page_number, source_text, target_text in fixed_replacements:
        output_index = page_map.get(source_page_number)
        if output_index is None:
            continue
        source_rectangle = _source_text_rect(
            source[source_page_number - 1], source_text
        )
        if source_rectangle is None:
            raise RuntimeError(
                f"Fixed source text was not found on page {source_page_number}"
            )
        _replace_fixed_text(translated[output_index], source_rectangle, target_text)
        fixed_replacement_count += 1
        fixed_checks.append((output_index, source_rectangle, target_text))
    for source_page_number, source_text, target_text in left_fixed_replacements:
        output_index = page_map.get(source_page_number)
        if output_index is None:
            continue
        source_rectangle = _source_text_rect(
            source[source_page_number - 1], source_text
        )
        if source_rectangle is None:
            raise RuntimeError(
                f"Left-aligned source text was not found on page {source_page_number}"
            )
        _replace_fixed_text(
            translated[output_index],
            source_rectangle,
            target_text,
            align=fitz.TEXT_ALIGN_LEFT,
        )
        fixed_replacement_count += 1
        fixed_checks.append((output_index, source_rectangle, target_text))

    output_replacement_count = 0
    for source_page_number, source_text, target_text in output_replacements:
        output_index = page_map.get(source_page_number)
        if output_index is None:
            continue
        output_replacement_count += _replace_output_text(
            translated[output_index], source_text, target_text
        )

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    translated.save(output_pdf, garbage=4, deflate=True)
    translated.close()

    failures: list[str] = []
    with fitz.open(output_pdf) as protected:
        for output_index, source_page_number, rectangle in protected_regions:
            expected = _normalize(
                source[source_page_number - 1].get_text(clip=rectangle, sort=True)
            )
            actual = _normalize(
                protected[output_index].get_text(clip=rectangle, sort=True)
            )
            if not expected or expected != actual:
                failures.append(
                    f"protected_region_mismatch:output_page_{output_index + 1}"
                )
        for output_index, source_page_number in enumerate(selected_source_pages):
            if _find_header_rect(source[source_page_number - 1], source_header) is None:
                continue
            if target_header not in protected[output_index].get_text():
                failures.append(
                    f"canonical_header_missing:output_page_{output_index + 1}"
                )
        for output_index, rectangle, target_text in fixed_checks:
            verification_clip = fitz.Rect(
                rectangle.x0 - 4,
                rectangle.y0 - 4,
                rectangle.x1 + 4,
                rectangle.y1 + 4,
            )
            rendered = _normalize(
                protected[output_index].get_text(clip=verification_clip)
            )
            if _normalize(target_text) not in rendered:
                failures.append(
                    f"fixed_replacement_missing:output_page_{output_index + 1}"
                )
    source.close()
    receipt: dict[str, object] = {
        "schema_version": 1,
        "source_pdf_sha256": _sha256(source_pdf),
        "translated_pdf_sha256": _sha256(translated_pdf),
        "output_pdf_sha256": _sha256(output_pdf),
        "selected_source_pages": list(selected_source_pages),
        "figure_region_count": kind_counts["figure"],
        "table_region_count": kind_counts["table"],
        "reference_page_count": kind_counts["reference"],
        "canonical_header_count": header_count,
        "fixed_replacement_count": fixed_replacement_count,
        "output_replacement_count": output_replacement_count,
        "verbatim_text_count": verbatim_text_count,
        "protected_regions": [
            {
                "output_page": output_index + 1,
                "source_page": source_page_number,
                "bbox": [rectangle.x0, rectangle.y0, rectangle.x1, rectangle.y1],
            }
            for output_index, source_page_number, rectangle in protected_regions
        ],
        "failures": failures,
        "verified": not failures,
    }
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pdf", type=Path, required=True)
    parser.add_argument("--translated-pdf", type=Path, required=True)
    parser.add_argument("--output-pdf", type=Path, required=True)
    parser.add_argument("--selected-source-pages", required=True)
    parser.add_argument("--ir-xml", type=Path, required=True)
    parser.add_argument("--source-header", required=True)
    parser.add_argument("--target-header", required=True)
    parser.add_argument(
        "--fixed-replacement",
        nargs=3,
        action="append",
        metavar=("SOURCE_PAGE", "SOURCE", "TARGET"),
        default=[],
    )
    parser.add_argument(
        "--fixed-replacement-left",
        nargs=3,
        action="append",
        metavar=("SOURCE_PAGE", "SOURCE", "TARGET"),
        default=[],
    )
    parser.add_argument(
        "--verbatim-text",
        nargs=2,
        action="append",
        metavar=("SOURCE_PAGE", "SOURCE"),
        default=[],
    )
    parser.add_argument(
        "--output-replacement",
        nargs=3,
        action="append",
        metavar=("SOURCE_PAGE", "OUTPUT", "TARGET"),
        default=[],
    )
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    selected = tuple(
        int(value) for value in args.selected_source_pages.split(",") if value.strip()
    )
    receipt = protect_pdf(
        source_pdf=args.source_pdf,
        translated_pdf=args.translated_pdf,
        output_pdf=args.output_pdf,
        selected_source_pages=selected,
        ir_xml=args.ir_xml,
        source_header=args.source_header,
        target_header=args.target_header,
        fixed_replacements=tuple(
            (int(source_page), source, target)
            for source_page, source, target in args.fixed_replacement
        ),
        left_fixed_replacements=tuple(
            (int(source_page), source, target)
            for source_page, source, target in args.fixed_replacement_left
        ),
        verbatim_texts=tuple(
            (int(source_page), source) for source_page, source in args.verbatim_text
        ),
        output_replacements=tuple(
            (int(source_page), source, target)
            for source_page, source, target in args.output_replacement
        ),
    )
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
