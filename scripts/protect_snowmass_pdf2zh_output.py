#!/usr/bin/env python3
"""Deterministically restore source-only PDF regions after pdf2zh-next rendering."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import cast

import fitz


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).replace("\x03", "")


def _clean_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\x03", "")
    lines = [" ".join(line.split()) for line in normalized.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _header_similarity_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def _header_display_penalty(value: str) -> tuple[int, int]:
    return (
        sum(character.isspace() for character in value),
        sum(
            unicodedata.category(character).startswith("P")
            for character in value
        ),
    )


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def _text_dict(
    page: fitz.Page,
    *,
    sort: bool = True,
    clip: fitz.Rect | None = None,
) -> dict[str, object]:
    """Extract text geometry without materializing embedded image payloads.

    ``page.get_text("dict")`` includes image blocks and their encoded bytes.
    Protection/QC only needs text lines and bounding boxes, so including those
    payloads causes needless PNG encoding, memory growth, and long seal times
    on papers with large figures.
    """

    textpage = page.get_textpage(clip=clip, flags=fitz.TEXTFLAGS_TEXT)
    return textpage.extractDICT(sort=sort)


def _coalesce(rectangles: list[fitz.Rect]) -> list[fitz.Rect]:
    merged: list[fitz.Rect] = []
    for rectangle in rectangles:
        for index, existing in enumerate(merged):
            intersection = rectangle & existing
            smaller = min(rectangle.get_area(), existing.get_area())
            # Nested/overlapping source-controlled regions must be restored
            # once, or later image insertion can invalidate the pixel check.
            # Require substantial overlap so neighboring columns/figures do
            # not get merged accidentally.
            if smaller and intersection.get_area() / smaller >= 0.50:
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


def _coalesce_identity_regions(rectangles: list[fitz.Rect]) -> list[fitz.Rect]:
    regions = [fitz.Rect(rectangle) for rectangle in rectangles]
    changed = True
    while changed:
        changed = False
        merged: list[fitz.Rect] = []
        for rectangle in sorted(regions, key=lambda item: (item.y0, item.x0)):
            for index, existing in enumerate(merged):
                horizontal_overlap = max(
                    0.0,
                    min(existing.x1, rectangle.x1)
                    - max(existing.x0, rectangle.x0),
                )
                smaller_width = min(existing.width, rectangle.width)
                vertical_gap = max(
                    0.0,
                    rectangle.y0 - existing.y1,
                    existing.y0 - rectangle.y1,
                )
                if (
                    smaller_width
                    and horizontal_overlap / smaller_width >= 0.2
                    and vertical_gap <= 12.0
                ):
                    merged[index] = existing | rectangle
                    changed = True
                    break
            else:
                merged.append(rectangle)
        regions = merged
    return regions


def _coalesce_reference_columns(rectangles: list[fitz.Rect]) -> list[fitz.Rect]:
    columns: list[fitz.Rect] = []
    for rectangle in sorted(rectangles, key=lambda item: (item.x0, item.y0)):
        for index, column in enumerate(columns):
            horizontal_overlap = max(
                0.0, min(column.x1, rectangle.x1) - max(column.x0, rectangle.x0)
            )
            smaller_width = min(column.width, rectangle.width)
            if smaller_width and horizontal_overlap / smaller_width >= 0.5:
                columns[index] = column | rectangle
                break
        else:
            columns.append(rectangle)
    return sorted(columns, key=lambda item: item.x0)


def _reference_clips(
    source: fitz.Document, textpages: dict[int, object] | None = None
) -> dict[int, list[fitz.Rect]]:
    clips: dict[int, list[fitz.Rect]] = {}
    in_references = False
    numbered_reference_start: int | None = None
    numbered_reference_start_y: float | None = None
    numbered_candidates: list[tuple[int, str]] = []
    for page_index, page in enumerate(source):
        for block in _text_dict(page, sort=True).get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                line_text = "".join(
                    str(span.get("text", "")) for span in line.get("spans", [])
                ).strip()
                if line_text:
                    numbered_candidates.append((page_index, line_text))
    # A few source papers omit the bibliography heading entirely.  Only use a
    # numbered entry as a boundary when a short forward scan finds the next
    # two entries in order; this avoids treating an isolated body citation as
    # the bibliography start.
    for index, (page_index, line_text) in enumerate(numbered_candidates):
        if not re.match(r"^\[1\]\s+\S", line_text):
            continue
        expected = 2
        for _candidate_page, candidate_text in numbered_candidates[index + 1 : index + 40]:
            match = re.match(r"^\[(\d+)\]\s+\S", candidate_text)
            if match and int(match.group(1)) == expected:
                expected += 1
                if expected == 4:
                    numbered_reference_start = page_index
                    start_block = _text_dict(source[page_index], sort=True).get("blocks", [])
                    for start_block_item in start_block:
                        for start_line in start_block_item.get("lines", []):
                            start_text = "".join(
                                str(span.get("text", ""))
                                for span in start_line.get("spans", [])
                            ).strip()
                            if re.match(r"^\[1\]\s+\S", start_text):
                                numbered_reference_start_y = float(start_line["bbox"][1])
                                break
                        if numbered_reference_start_y is not None:
                            break
                    break
        if numbered_reference_start is not None:
            break
    for page_index, page in enumerate(source):
        textpage = textpages.get(page_index) if textpages else None
        heading_rects: list[fitz.Rect] = []
        blocks = _text_dict(page, sort=True)
        for block in blocks.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                line_text = "".join(str(span.get("text", "")) for span in line.get("spans", [])).strip()
                if re.fullmatch(r"(?:References?|Bibliography|参考文献|文献)\s*[:：]?", line_text, re.IGNORECASE):
                    heading_rects.append(fitz.Rect(*line["bbox"]))
        heading = (
            min(heading_rects, key=lambda rectangle: rectangle.y0)
            if heading_rects
            else None
        )
        if heading is not None:
            in_references = True
        if numbered_reference_start == page_index:
            in_references = True
        if not in_references:
            continue
        page_clips: list[fitz.Rect] = []
        all_text_rectangles: list[fitz.Rect] = []
        blocks = _text_dict(page, sort=True)
        for block in blocks.get("blocks", []):
            if block.get("type") != 0:
                continue
            lines = []
            for line in block.get("lines", []):
                text = "".join(
                    str(span.get("text", "")) for span in line.get("spans", [])
                ).strip()
                if text:
                    line_rectangle = fitz.Rect(*line["bbox"])
                    lines.append((line_rectangle, text))
                    all_text_rectangles.append(line_rectangle)
            starts = [
                index
                for index, (_, text) in enumerate(lines)
                if re.match(r"^\[\d+\]\s+\S", text)
            ]
            if not starts and not in_references:
                continue
            if starts:
                first_reference_line = min(starts)
                prefix_text = " ".join(
                    text for _, text in lines[:first_reference_line]
                ).casefold()
                preserve_prefix = bool(
                    re.search(r"(?:https?://|doi\s*:|doi\.org|arxiv\s*:)", prefix_text)
                )
                reference_lines = lines if preserve_prefix else lines[first_reference_line:]
            else:
                # Author-year bibliographies and continuation pages do not
                # begin with ``[n]``. Once the heading has been seen, protect
                # the remaining text on the page as bibliography as well.
                reference_lines = lines
                if heading is not None:
                    reference_lines = [
                        (rectangle, text)
                        for rectangle, text in lines
                        if rectangle.y0 >= heading.y1 - 1.0
                    ]
                elif (
                    numbered_reference_start == page_index
                    and numbered_reference_start_y is not None
                ):
                    reference_lines = [
                        (rectangle, text)
                        for rectangle, text in lines
                        if rectangle.y0 >= numbered_reference_start_y - 1.0
                    ]
            if not reference_lines:
                continue
            page_clips.append(
                fitz.Rect(
                    max(0.0, min(rectangle.x0 for rectangle, _ in reference_lines) - 2.0),
                    max(0.0, reference_lines[0][0].y0),
                    min(
                        page.rect.width,
                        max(rectangle.x1 for rectangle, _ in reference_lines) + 2.0,
                    ),
                    min(
                        page.rect.height,
                        max(rectangle.y1 for rectangle, _ in reference_lines) + 3.0,
                    ),
                )
            )
        if not page_clips:
            if heading is None:
                in_references = False
            continue
        columns = _coalesce_reference_columns(page_clips)
        expanded_columns: list[fitz.Rect] = []
        for column in columns:
            expanded = fitz.Rect(column)
            for line_rectangle in all_text_rectangles:
                horizontal_overlap = max(
                    0.0,
                    min(expanded.x1, line_rectangle.x1)
                    - max(expanded.x0, line_rectangle.x0),
                )
                smaller_width = min(expanded.width, line_rectangle.width)
                if (
                    line_rectangle.y0 >= column.y0 - 1.0
                    and line_rectangle.y0 < page.rect.height * 0.94
                    and smaller_width
                    and horizontal_overlap / smaller_width >= 0.5
                ):
                    expanded |= line_rectangle
            expanded_columns.append(
                fitz.Rect(
                    max(0.0, expanded.x0 - 2.0),
                    expanded.y0,
                    min(page.rect.width, expanded.x1 + 2.0),
                    min(page.rect.height, expanded.y1 + 3.0),
                )
            )
        clips[page_index + 1] = expanded_columns
    return clips


def _find_header_rect(
    page: fitz.Page, source_header: str, textpage: object | None = None
) -> fitz.Rect | None:
    matches = [
        fitz.Rect(rectangle)
        for rectangle in (
            textpage.search(source_header, quads=0)
            if textpage is not None
            else page.search_for(source_header)
        )
        if fitz.Rect(rectangle).y0 < page.rect.height * 0.16
    ]
    return min(matches, key=lambda rectangle: rectangle.y0) if matches else None


def _insert_header(
    page: fitz.Page,
    rectangle: fitz.Rect,
    text: str,
    *,
    already_redacted: bool = False,
) -> None:
    patch = fitz.Rect(
        rectangle.x0 - 2, rectangle.y0 - 1, rectangle.x1 + 2, rectangle.y1 + 0.5
    )
    if not already_redacted:
        page.add_redact_annot(patch, fill=(1, 1, 1))
        page.apply_redactions()
    segments = [
        segment
        for segment in re.findall(r"[\x20-\x7e]+|[^\x20-\x7e]+", text)
        if segment
    ]
    maximum = max(4.0, min(12.0, rectangle.height * 0.65))
    candidate_sizes = [maximum, 10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.5, 3.0, 2.5]
    for font_size in candidate_sizes:
        if font_size > maximum:
            continue
        widths = [
            fitz.Font(fontname="helv" if segment.isascii() else "china-s").text_length(
                segment, fontsize=font_size
            )
            for segment in segments
        ]
        line_height = font_size * 1.25
        if sum(widths) > patch.width or line_height > patch.height:
            continue
        baseline = patch.y0 + (patch.height - line_height) / 2 + font_size
        x = patch.x0 + (patch.width - sum(widths)) / 2
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
        return
    raise RuntimeError("Canonical running header does not fit its source rectangle")


def _insert_reference_heading(page: fitz.Page, rectangle: fitz.Rect) -> None:
    """Restore the canonical Chinese heading after reference-body redaction."""

    _insert_header(page, rectangle, "参考文献")


def _insert_rasterized_source_clip(
    output_page: fitz.Page,
    source_page: fitz.Page,
    rectangle: fitz.Rect,
    *,
    dpi: int = 216,
) -> str:
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pixmap = source_page.get_pixmap(matrix=matrix, clip=rectangle, alpha=False)
    output_page.insert_image(
        rectangle,
        # Passing a Pixmap lets MuPDF choose an encoded image representation;
        # on some pages that changes the decoded samples and breaks the
        # deterministic protection hash. PNG preserves the exact source
        # raster while remaining self-contained in the output PDF.
        stream=pixmap.tobytes("png"),
        keep_proportion=False,
        overlay=True,
    )
    return hashlib.sha256(pixmap.samples).hexdigest()


def _expanded_redaction_rectangle(
    output_page: fitz.Page, rectangle: fitz.Rect
) -> fitz.Rect:
    patch = fitz.Rect(rectangle)
    for block in output_page.get_text("blocks", sort=True):
        block_rectangle = fitz.Rect(*block[:4])
        if (block_rectangle & rectangle).get_area() > 0:
            patch |= block_rectangle
    return fitz.Rect(patch.x0 - 1, patch.y0 - 1, patch.x1 + 1, patch.y1 + 1)


def _reference_redaction_rectangle(
    output_page: fitz.Page, rectangle: fitz.Rect
) -> fitz.Rect:
    expanded = _expanded_redaction_rectangle(output_page, rectangle)
    return fitz.Rect(
        expanded.x0,
        expanded.y0,
        expanded.x1,
        max(expanded.y1, output_page.rect.height * 0.94),
    )


def _has_embedded_raster_clip(
    output_page: fitz.Page,
    rectangle: fitz.Rect,
    expected_pixel_sha256: str,
) -> bool:
    """Verify the saved page embeds the exact source raster at the intended bbox."""

    document = output_page.parent
    for image in output_page.get_image_info(xrefs=True):
        xref = int(image.get("xref") or 0)
        if xref <= 0:
            continue
        image_rectangle = fitz.Rect(image["bbox"])
        if any(
            abs(left - right) > 0.05
            for left, right in zip(image_rectangle, rectangle)
        ):
            continue
        pixmap = fitz.Pixmap(document, xref)
        if hashlib.sha256(pixmap.samples).hexdigest() == expected_pixel_sha256:
            return True
    # Some MuPDF versions rewrite an inserted image's xref dimensions during
    # save, even though the visible protected region is exact. Compare the
    # rendered clip as a deterministic fallback; this still proves the
    # protected pixels, without coupling QC to xref bookkeeping.
    rendered = output_page.get_pixmap(
        matrix=fitz.Matrix(216 / 72, 216 / 72),
        clip=rectangle,
        alpha=False,
    )
    return hashlib.sha256(rendered.samples).hexdigest() == expected_pixel_sha256
    return False


def _source_text_rect(
    page: fitz.Page, text: str, textpage: object | None = None
) -> fitz.Rect | None:
    matches = (
        [fitz.Rect(rectangle) for rectangle in textpage.search(text, quads=0)]
        if textpage is not None
        else page.search_for(text)
    )
    if matches:
        return fitz.Rect(matches[0])
    expected = _normalize(text)
    # PDF text extraction commonly attaches punctuation to a neighboring
    # word, e.g. ``([::1]).``.  Resolve these short protected tokens from
    # word boxes before falling back to an entire text block; the latter can
    # accidentally rasterize a whole page when a diagram and prose share a
    # block in the source PDF.
    word_matches: list[fitz.Rect] = []
    words = (
        textpage.extractWORDS()
        if textpage is not None
        else page.get_text("words", sort=True)
    )
    for word in words:
        candidate = _normalize(str(word[4]))
        if expected and (
            expected in candidate
            or (len(expected) >= 4 and candidate in expected)
        ):
            word_matches.append(fitz.Rect(*word[:4]))
    if word_matches:
        rectangle = word_matches[0]
        for match in word_matches[1:]:
            rectangle |= match
        return rectangle
    blocks = (
        textpage.extractBLOCKS()
        if textpage is not None
        else page.get_text("blocks", sort=True)
    )
    for block in blocks:
        if _normalize(str(block[4])) == expected:
            return fitz.Rect(*block[:4])
    return None


def _page_text_blocks(
    page: fitz.Page, textpage: object | None = None
) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    text_blocks = _text_dict(page, sort=False)
    for block in text_blocks.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines: list[str] = []
        sizes: list[float] = []
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            line_text = _clean_text("".join(str(span.get("text", "")) for span in spans))
            if line_text:
                lines.append(line_text)
            for span in spans:
                text = str(span.get("text", ""))
                if text.strip():
                    sizes.append(float(span.get("size", 0.0)))
        text = "\n".join(lines)
        if not text:
            continue
        rectangle = fitz.Rect(*block["bbox"])
        blocks.append(
            {
                "rect": rectangle,
                "text": text,
                "normalized": _normalize(text),
                "max_size": max(sizes, default=0.0),
                "avg_size": (sum(sizes) / len(sizes)) if sizes else 0.0,
            }
        )
    return sorted(
        blocks,
        key=lambda item: (
            cast(fitz.Rect, item["rect"]).y0,
            cast(fitz.Rect, item["rect"]).x0,
        ),
    )


def _page_text_lines(page: fitz.Page) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for block in _text_dict(page, sort=False).get("blocks", []):
        if block.get("type") != 0:
            continue
        block_rectangle = fitz.Rect(*block["bbox"])
        for line in block.get("lines", []):
            spans = [
                span for span in line.get("spans", []) if str(span.get("text", "")).strip()
            ]
            if not spans:
                continue
            text = _clean_text("".join(str(span.get("text", "")) for span in spans))
            if not text:
                continue
            sizes = [float(span.get("size", 0.0)) for span in spans]
            lines.append(
                {
                    "rect": fitz.Rect(*line["bbox"]),
                    "block_rect": block_rectangle,
                    "text": text,
                    "normalized": _normalize(text),
                    "max_size": max(sizes, default=0.0),
                }
            )
    return sorted(
        lines,
        key=lambda item: (
            cast(fitz.Rect, item["rect"]).y0,
            cast(fitz.Rect, item["rect"]).x0,
        ),
    )


def _looks_like_date(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b",
            text,
        )
        and re.search(r"\b\d{4}\b", text)
    )


def _looks_like_email(text: str) -> bool:
    return "@" in text


def _looks_like_affiliation(text: str) -> bool:
    folded = _clean_text(text).casefold()
    markers = (
        "university",
        "institute",
        "laboratory",
        "department",
        "school",
        "college",
        "center",
        "centre",
        "observatory",
        "usa",
    )
    return bool(re.match(r"^\d+\s*[A-Za-z(]", text)) or any(
        marker in folded for marker in markers
    )


def _looks_like_group_line(text: str) -> bool:
    folded = _clean_text(text).casefold()
    return any(
        marker in folded
        for marker in ("group", "collaboration", "consortium", "for the ")
    )


def _looks_like_author_line(text: str) -> bool:
    cleaned = _clean_text(text)
    folded = cleaned.casefold()
    if not cleaned or _looks_like_group_line(cleaned):
        return False
    if any(
        marker in folded
        for marker in (
            "arxiv",
            "preprint",
            "report",
            "submitted",
            "proceedings",
            "white paper",
        )
    ):
        return False
    if cleaned.isupper():
        return False
    name_tokens = re.findall(
        r"(?:[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]*|[A-Z]\.)"
        r"(?:\d+(?:,\d+)*)?",
        cleaned,
    )
    if len(name_tokens) < 2:
        return False
    separators = bool(re.search(r",|\band\b|&", cleaned, re.IGNORECASE))
    return separators or len(name_tokens) <= 6


def _discover_running_headers(
    source: fitz.Document,
    translated: fitz.Document,
    selected_source_pages: tuple[int, ...],
    page_map: dict[int, int],
    *,
    min_recurrence: int,
    source_textpages: dict[int, object] | None = None,
) -> list[dict[str, object]]:
    candidates: dict[str, dict[str, object]] = {}
    for source_page_number in selected_source_pages:
        if source_page_number <= 1:
            continue
        page = source[source_page_number - 1]
        top_limit = page.rect.height * 0.10
        maximum_header_height = page.rect.height * 0.022
        for block in _page_text_blocks(
            page,
            textpage=(source_textpages or {}).get(source_page_number - 1),
        ):
            rectangle = cast(fitz.Rect, block["rect"])
            normalized = str(block["normalized"])
            if (
                rectangle.y0 >= top_limit
                or rectangle.height > maximum_header_height
                or len(normalized) < 8
            ):
                continue
            entry = candidates.setdefault(
                normalized,
                {
                    "source_text": str(block["text"]),
                    "pages": set(),
                    "rectangles": [],
                    "average_y0": 0.0,
                    "average_x0": 0.0,
                },
            )
            cast(set[int], entry["pages"]).add(source_page_number)
            cast(list[tuple[int, fitz.Rect]], entry["rectangles"]).append(
                (source_page_number, rectangle)
            )

    qualified = [
        {
            **entry,
            "page_count": len(cast(set[int], entry["pages"])),
            "average_y0": sum(
                rectangle.y0
                for _, rectangle in cast(list[tuple[int, fitz.Rect]], entry["rectangles"])
            )
            / len(cast(list[tuple[int, fitz.Rect]], entry["rectangles"])),
            "average_x0": sum(
                rectangle.x0
                for _, rectangle in cast(list[tuple[int, fitz.Rect]], entry["rectangles"])
            )
            / len(cast(list[tuple[int, fitz.Rect]], entry["rectangles"])),
            "normalized": normalized,
        }
        for normalized, entry in candidates.items()
        if len(cast(set[int], entry["pages"])) >= min_recurrence
        and len(cast(list[tuple[int, fitz.Rect]], entry["rectangles"]))
        == len(cast(set[int], entry["pages"]))
        and all(
            right - left <= 2
            for left, right in zip(
                sorted(cast(set[int], entry["pages"])),
                sorted(cast(set[int], entry["pages"]))[1:],
            )
        )
    ]
    if not qualified:
        return []
    qualified.sort(
        key=lambda entry: (
            -cast(int, entry["page_count"]),
            cast(float, entry["average_y0"]),
            cast(float, entry["average_x0"]),
            str(entry["source_text"]),
        )
    )
    for index, left in enumerate(qualified):
        left_pages = cast(set[int], left["pages"])
        for right in qualified[index + 1 :]:
            shared_pages = left_pages & cast(set[int], right["pages"])
            if not shared_pages:
                continue
            left_rectangles = dict(
                cast(list[tuple[int, fitz.Rect]], left["rectangles"])
            )
            right_rectangles = dict(
                cast(list[tuple[int, fitz.Rect]], right["rectangles"])
            )
            compound_components = True
            for page_number in shared_pages:
                upper, lower = sorted(
                    (left_rectangles[page_number], right_rectangles[page_number]),
                    key=lambda rectangle: rectangle.y0,
                )
                horizontal_overlap = max(
                    0.0,
                    min(upper.x1, lower.x1) - max(upper.x0, lower.x0),
                )
                smaller_width = min(upper.width, lower.width)
                vertical_gap = lower.y0 - upper.y1
                if not (
                    smaller_width
                    and horizontal_overlap / smaller_width >= 0.8
                    and 0.0 <= vertical_gap <= 5.0
                ):
                    compound_components = False
                    break
            if not compound_components:
                raise RuntimeError("Ambiguous recurring source header candidates")

    families: list[dict[str, object]] = []
    for winner in qualified:
        source_text = str(winner["source_text"])
        source_rectangles = sorted(
            cast(list[tuple[int, fitz.Rect]], winner["rectangles"]),
            key=lambda item: item[0],
        )
        if len(source_rectangles) < min_recurrence:
            raise RuntimeError("Auto-discovered running header could not be re-located")

        translated_candidates: dict[str, dict[str, object]] = {}
        for source_page_number, rectangle in source_rectangles:
            output_index = page_map[source_page_number]
            candidate = _clean_text(
                translated[output_index].get_text(clip=rectangle, sort=True)
            )
            normalized = _normalize(candidate)
            if not normalized:
                raise RuntimeError(
                    "Translated running header was empty on output page "
                    f"{output_index + 1}"
                )
            entry = translated_candidates.setdefault(
                normalized,
                {"count": 0, "display_counts": Counter(), "pages": []},
            )
            entry["count"] = int(entry["count"]) + 1
            cast(Counter[str], entry["display_counts"])[candidate] += 1
            cast(list[int], entry["pages"]).append(output_index + 1)

        sorted_candidates = sorted(
            (
                {
                    "normalized_text": normalized,
                    "display_text": sorted(
                        cast(Counter[str], entry["display_counts"]).items(),
                        key=lambda item: (-item[1], item[0]),
                    )[0][0],
                    "count": int(entry["count"]),
                    "pages": sorted(cast(list[int], entry["pages"])),
                }
                for normalized, entry in translated_candidates.items()
            ),
            key=lambda entry: (
                -cast(int, entry["count"]),
                min(cast(list[int], entry["pages"])),
                str(entry["display_text"]),
            ),
        )
        top_count = cast(int, sorted_candidates[0]["count"])
        tied_candidates = [
            candidate
            for candidate in sorted_candidates
            if cast(int, candidate["count"]) == top_count
        ]
        tied = (
            len(sorted_candidates) > 1
            and len(tied_candidates) > 1
        )
        if tied and len(source_rectangles) > 2:
            # A stable source header can legitimately have several model
            # renderings across pages. The source family is authoritative:
            # choose the first observed output deterministically, then
            # overwrite every member of the family with that canonical value.
            # All candidates remain in the receipt for auditability.
            folded = [
                _header_similarity_text(str(candidate["display_text"]))
                for candidate in tied_candidates
            ]
            near_duplicates = all(
                left
                and right
                and SequenceMatcher(None, left, right).ratio() >= 0.85
                for index, left in enumerate(folded)
                for right in folded[index + 1 :]
            )
            tied_candidates.sort(
                key=lambda candidate: (
                    (_header_display_penalty(str(candidate["display_text"])) if near_duplicates else 0),
                    min(cast(list[int], candidate["pages"])),
                    str(candidate["display_text"]),
                )
            )
            winner = tied_candidates[0]
            sorted_candidates = [
                winner,
                *[candidate for candidate in sorted_candidates if candidate is not winner],
            ]
        canonical_target = str(sorted_candidates[0]["display_text"])
        families.append(
            {
                "source_text": source_text,
                "source_text_sha256": _text_sha256(source_text),
                "source_occurrence_count": len(source_rectangles),
                "recurrence_threshold": min_recurrence,
                "rectangles": [
                    {
                        "page": source_page_number,
                        "bbox": [
                            rectangle.x0,
                            rectangle.y0,
                            rectangle.x1,
                            rectangle.y1,
                        ],
                    }
                    for source_page_number, rectangle in source_rectangles
                ],
                "translated_candidates": sorted_candidates,
                "canonical_target": canonical_target,
                "canonical_target_sha256": _text_sha256(canonical_target),
            }
        )
    return families


def _discover_front_matter_lines_strict(
    source_page: fitz.Page,
) -> list[dict[str, object]]:
    page_width = source_page.rect.width
    lines = [
        line
        for line in _page_text_lines(source_page)
        if cast(fitz.Rect, line["rect"]).x0 > page_width * 0.08
        and cast(fitz.Rect, line["rect"]).y0 < source_page.rect.height * 0.6
    ]
    if not lines:
        raise RuntimeError("Unable to auto-discover first-page front matter lines")

    abstract_candidates = [
        line
        for line in lines
        if str(line["text"]).casefold().startswith(
            ("abstract", "executive summary")
        )
    ]
    abstract_top = (
        min(cast(fitz.Rect, line["rect"]).y0 for line in abstract_candidates)
        if abstract_candidates
        else source_page.rect.height * 0.5
    )
    title_candidates = [
        line
        for line in lines
        if cast(fitz.Rect, line["rect"]).y0 < abstract_top
        and cast(fitz.Rect, line["rect"]).width > page_width * 0.2
        and not _looks_like_date(str(line["text"]))
        and not _looks_like_affiliation(str(line["text"]))
        and not _looks_like_email(str(line["text"]))
    ]
    if not title_candidates:
        raise RuntimeError("Unable to identify the first-page title region")
    title_font = max(cast(float, line["max_size"]) for line in title_candidates)
    title_lines = [
        line for line in title_candidates if cast(float, line["max_size"]) >= title_font - 1.0
    ]
    title_bottom = max(cast(fitz.Rect, line["rect"]).y1 for line in title_lines)

    front_matter_lines = [
        line
        for line in lines
        if title_bottom - 4 < cast(fitz.Rect, line["rect"]).y0 < abstract_top
        and not _looks_like_date(str(line["text"]))
        and not _looks_like_affiliation(str(line["text"]))
        and not _looks_like_email(str(line["text"]))
        and not str(line["text"]).casefold().startswith(
            ("abstract", "executive summary")
        )
    ]
    affiliation_lines = [
        line
        for line in lines
        if title_bottom - 4 < cast(fitz.Rect, line["rect"]).y0 < abstract_top
        and not _looks_like_date(str(line["text"]))
        and not _looks_like_email(str(line["text"]))
        and not str(line["text"]).casefold().startswith(
            ("abstract", "executive summary")
        )
        and _looks_like_affiliation(str(line["text"]))
    ]
    candidate_lines = [
        line
        for line in lines
        if title_bottom - 4 < cast(fitz.Rect, line["rect"]).y0 < abstract_top
        and not _looks_like_date(str(line["text"]))
        and not str(line["text"]).casefold().startswith(
            ("abstract", "executive summary")
        )
        and _looks_like_author_line(str(line["text"]))
    ]
    if not candidate_lines:
        raise RuntimeError("Unable to identify first-page author-name candidates")

    author_font = max(cast(float, line["max_size"]) for line in candidate_lines)
    author_lines = [
        line
        for line in candidate_lines
        if cast(float, line["max_size"]) >= author_font - 0.6
    ]
    if not author_lines:
        raise RuntimeError("Unable to identify first-page author-name lines")

    short_adjacent_affiliations = [
        line
        for line in lines
        if title_bottom - 4 < cast(fitz.Rect, line["rect"]).y0 < abstract_top
        and str(line["text"]).count(",") >= 2
        and len(_clean_text(str(line["text"])).split()) <= 12
        and cast(float, line["max_size"]) < author_font - 0.6
        and any(
            -2.0
            <= cast(fitz.Rect, line["rect"]).y0
            - cast(fitz.Rect, author["rect"]).y1
            <= 35.0
            for author in author_lines
        )
    ]
    affiliation_lines = list(
        {
            tuple(round(value, 3) for value in cast(fitz.Rect, line["rect"])): line
            for line in [*affiliation_lines, *short_adjacent_affiliations]
        }.values()
    )

    group_lines = [
        line
        for line in front_matter_lines
        if _looks_like_group_line(str(line["text"]))
        and any(
            -2.0 <= cast(fitz.Rect, line["rect"]).y0 - cast(fitz.Rect, author["rect"]).y1 <= 18.0
            for author in author_lines
        )
    ]
    contact_lines: list[dict[str, object]] = []
    for line in lines:
        line_rectangle = cast(fitz.Rect, line["rect"])
        if not (
            title_bottom - 4 < line_rectangle.y0 < abstract_top
            and _looks_like_email(str(line["text"]))
        ):
            continue
        block_rectangle = cast(fitz.Rect, line["block_rect"])
        contact_line = dict(line)
        contact_line["rect"] = fitz.Rect(block_rectangle)
        contact_lines.append(contact_line)
    if contact_lines:
        unique_contacts = {
            tuple(round(value, 3) for value in cast(fitz.Rect, line["rect"])): line
            for line in contact_lines
        }
        contact_columns = sorted(
            unique_contacts.values(),
            key=lambda line: cast(fitz.Rect, line["rect"]).x0,
        )
        contact_centers = [
            (cast(fitz.Rect, line["rect"]).x0 + cast(fitz.Rect, line["rect"]).x1)
            / 2
            for line in contact_columns
        ]
        boundaries = [
            (left + right) / 2
            for left, right in zip(contact_centers, contact_centers[1:])
        ]
        atomic_columns: list[dict[str, object]] = []
        for index, contact in enumerate(contact_columns):
            left_bound = boundaries[index - 1] if index else 0.0
            right_bound = (
                boundaries[index]
                if index < len(boundaries)
                else source_page.rect.width
            )
            matching_authors = [
                line
                for line in author_lines
                if left_bound
                <= (
                    cast(fitz.Rect, line["rect"]).x0
                    + cast(fitz.Rect, line["rect"]).x1
                )
                / 2
                < right_bound
            ]
            if not matching_authors:
                raise RuntimeError(
                    "Unable to pair first-page author and contact columns"
                )
            column_top = min(
                cast(fitz.Rect, line["rect"]).y0 for line in matching_authors
            )
            column_bottom = cast(fitz.Rect, contact["rect"]).y1
            column_lines = [
                line
                for line in lines
                if column_top - 2
                <= cast(fitz.Rect, line["rect"]).y0
                <= column_bottom
                and left_bound
                <= (
                    cast(fitz.Rect, line["rect"]).x0
                    + cast(fitz.Rect, line["rect"]).x1
                )
                / 2
                < right_bound
            ]
            column_lines.sort(
                key=lambda line: (
                    cast(fitz.Rect, line["rect"]).y0,
                    cast(fitz.Rect, line["rect"]).x0,
                )
            )
            if not column_lines:
                raise RuntimeError(
                    "Unable to collect first-page identity column lines"
                )
            rectangle = fitz.Rect(cast(fitz.Rect, column_lines[0]["rect"]))
            for line in column_lines[1:]:
                rectangle |= cast(fitz.Rect, line["rect"])
            rectangle = fitz.Rect(
                max(0.0, rectangle.x0 - 2.0),
                max(0.0, rectangle.y0 - 2.0),
                min(source_page.rect.width, rectangle.x1 + 2.0),
                min(source_page.rect.height, rectangle.y1 + 2.0),
            )
            source_text = "\n".join(str(line["text"]) for line in column_lines)
            atomic_columns.append(
                {
                    "page": 1,
                    "source_text": source_text,
                    "source_text_sha256": _text_sha256(source_text),
                    "bbox": [rectangle.x0, rectangle.y0, rectangle.x1, rectangle.y1],
                    "rect": rectangle,
                }
            )
        return atomic_columns
    discovered = sorted(
        {
            (
                cast(fitz.Rect, line["rect"]).x0,
                cast(fitz.Rect, line["rect"]).y0,
                cast(fitz.Rect, line["rect"]).x1,
                cast(fitz.Rect, line["rect"]).y1,
                str(line["text"]),
            ): line
            for line in [
                *author_lines,
                *affiliation_lines,
                *group_lines,
                *contact_lines,
            ]
        }.values(),
        key=lambda line: (
            cast(fitz.Rect, line["rect"]).y0,
            cast(fitz.Rect, line["rect"]).x0,
        ),
    )
    return [
        {
            "page": 1,
            "source_text": str(line["text"]),
            "source_text_sha256": _text_sha256(str(line["text"])),
            "bbox": [
                cast(fitz.Rect, line["rect"]).x0,
                cast(fitz.Rect, line["rect"]).y0,
                cast(fitz.Rect, line["rect"]).x1,
                cast(fitz.Rect, line["rect"]).y1,
            ],
            "rect": cast(fitz.Rect, line["rect"]),
        }
        for line in discovered
    ]


def _looks_like_confident_identity_seed(text: str) -> bool:
    cleaned = _clean_text(text)
    if not cleaned:
        return False
    if _looks_like_email(cleaned) or _looks_like_group_line(cleaned):
        return True
    if not _looks_like_author_line(cleaned):
        return False
    initials = re.findall(r"(?<![A-Za-z])[A-Z](?:\.[A-Z])?\.", cleaned)
    comma_count = cleaned.count(",")
    middle_dot_count = cleaned.count("·")
    return bool(initials) or comma_count >= 2 or middle_dot_count >= 1


def _front_matter_fallback_blocks(source_page: fitz.Page) -> list[dict[str, object]]:
    page_width = source_page.rect.width
    lines = [
        line
        for line in _page_text_lines(source_page)
        if cast(fitz.Rect, line["rect"]).x0 > page_width * 0.03
        and cast(fitz.Rect, line["rect"]).x1 < page_width * 0.97
    ]
    lines_by_block: dict[tuple[float, float, float, float], list[dict[str, object]]] = {}
    for line in lines:
        block_key = tuple(
            round(value, 3) for value in cast(fitz.Rect, line["block_rect"])
        )
        lines_by_block.setdefault(block_key, []).append(line)

    def block_text(line: dict[str, object]) -> str:
        return _clean_text(
            source_page.get_text(
                clip=cast(fitz.Rect, line["block_rect"]),
                sort=True,
            )
        )

    def is_group_label(text: str) -> bool:
        cleaned = _clean_text(text)
        return bool(
            _looks_like_group_line(cleaned)
            and re.search(
                r"\b(?:collaborations?|consortium|working groups?|interest group|"
                r"topical group|team)\d*(?:\s+[A-Z0-9][A-Z0-9-]*)?$",
                cleaned,
                re.IGNORECASE,
            )
        )

    def is_seed(line: dict[str, object]) -> bool:
        text = _clean_text(str(line["text"]))
        folded = text.casefold()
        entire_block = block_text(line)
        entire_folded = entire_block.casefold()
        if any(
            marker in folded or marker in entire_folded
            for marker in (
                "copyright",
                "for the benefit of",
                "keywords",
                "abstract",
                "introduction",
            )
        ):
            return False
        if is_group_label(text):
            return len(text.split()) <= 16 and len(entire_block.split()) <= 40
        if _looks_like_group_line(text):
            return False
        if _looks_like_email(text):
            return len(entire_block.split()) <= 160
        if not _looks_like_confident_identity_seed(text):
            return False
        block_key = tuple(
            round(value, 3) for value in cast(fitz.Rect, line["block_rect"])
        )
        block_lines = lines_by_block[block_key]
        strong_line_count = sum(
            1
            for member in block_lines
            if not _looks_like_group_line(str(member["text"]))
            and not _looks_like_email(str(member["text"]))
            and _looks_like_confident_identity_seed(str(member["text"]))
        )
        return (
            len(entire_block.split()) <= 80
            or strong_line_count / len(block_lines) >= 0.4
        )

    seed_lines = [line for line in lines if is_seed(line)]
    abstract_tops = [
        cast(fitz.Rect, line["rect"]).y0
        for line in lines
        if str(line["text"])
        .casefold()
        .startswith(("abstract", "executive summary"))
    ]
    if abstract_tops:
        abstract_top = min(abstract_tops)
        below_abstract = [
            line
            for line in seed_lines
            if cast(fitz.Rect, line["rect"]).y0 > abstract_top
            and not _looks_like_email(str(line["text"]))
            and not is_group_label(str(line["text"]))
        ]
        dense_author_rectangles: list[fitz.Rect] = []
        for line in below_abstract:
            block_key = tuple(
                round(value, 3)
                for value in cast(fitz.Rect, line["block_rect"])
            )
            block_lines = lines_by_block[block_key]
            strong_lines = [
                member
                for member in block_lines
                if not _looks_like_group_line(str(member["text"]))
                and not _looks_like_email(str(member["text"]))
                and _looks_like_confident_identity_seed(str(member["text"]))
            ]
            block_source = block_text(line)
            if (
                len(block_lines) >= 2
                and len(strong_lines) / len(block_lines) >= 0.5
                and (
                    block_source.count(",") >= 2
                    or len(
                        re.findall(
                            r"(?<![A-Za-z])[A-Z](?:\.[A-Z])?\.",
                            block_source,
                        )
                    )
                    >= 2
                )
            ):
                dense_author_rectangles.append(
                    cast(fitz.Rect, line["block_rect"])
                )

        filtered_seed_lines: list[dict[str, object]] = []
        for line in seed_lines:
            line_rectangle = cast(fitz.Rect, line["block_rect"])
            if cast(fitz.Rect, line["rect"]).y0 <= abstract_top:
                filtered_seed_lines.append(line)
                continue
            if _looks_like_email(str(line["text"])) or is_group_label(
                str(line["text"])
            ):
                filtered_seed_lines.append(line)
                continue
            for dense_rectangle in dense_author_rectangles:
                horizontal_overlap = max(
                    0.0,
                    min(line_rectangle.x1, dense_rectangle.x1)
                    - max(line_rectangle.x0, dense_rectangle.x0),
                )
                smaller_width = min(line_rectangle.width, dense_rectangle.width)
                vertical_gap = max(
                    0.0,
                    line_rectangle.y0 - dense_rectangle.y1,
                    dense_rectangle.y0 - line_rectangle.y1,
                )
                if (
                    smaller_width
                    and horizontal_overlap / smaller_width >= 0.35
                    and vertical_gap <= 60.0
                ):
                    filtered_seed_lines.append(line)
                    break
        seed_lines = filtered_seed_lines
    if not seed_lines:
        return []

    selected_block_rectangles: set[tuple[float, float, float, float]] = set()
    seed_rectangles: list[fitz.Rect] = []
    for line in seed_lines:
        text = str(line["text"])
        group_label = is_group_label(text) and not _looks_like_email(text)
        rectangle = (
            cast(fitz.Rect, line["rect"])
            if group_label
            else cast(fitz.Rect, line["block_rect"])
        )
        selected_block_rectangles.add(
            tuple(round(value, 3) for value in rectangle)
        )
        seed_rectangles.append(rectangle)
        if group_label:
            for sibling in lines:
                sibling_rectangle = cast(fitz.Rect, sibling["rect"])
                if (
                    -3.0
                    <= sibling_rectangle.y0 - cast(fitz.Rect, line["rect"]).y1
                    <= 25.0
                    and re.fullmatch(
                        r"[A-Z]{1,4}\d+[A-Z0-9-]*(?:\s*,\s*"
                        r"[A-Z]{1,4}\d+[A-Z0-9-]*)+",
                        _clean_text(str(sibling["text"])),
                    )
                ):
                    selected_block_rectangles.add(
                        tuple(round(value, 3) for value in sibling_rectangle)
                    )
    for line in lines:
        if not _looks_like_affiliation(str(line["text"])):
            continue
        block_rectangle = cast(fitz.Rect, line["block_rect"])
        affiliation_text = block_text(line)
        if (
            _looks_like_date(str(line["text"]))
            or len(affiliation_text.split()) > 60
            or not re.search(
                r"(?:university|universit[ée]|laborator(?:y|ies)|institute|"
                r"institution|department|centre|center|college|collaboration)\b",
                str(line["text"]),
                re.IGNORECASE,
            )
        ):
            continue
        for seed_rectangle in seed_rectangles:
            horizontal_overlap = max(
                0.0,
                min(block_rectangle.x1, seed_rectangle.x1)
                - max(block_rectangle.x0, seed_rectangle.x0),
            )
            smaller_width = min(block_rectangle.width, seed_rectangle.width)
            vertical_gap = max(
                0.0,
                block_rectangle.y0 - seed_rectangle.y1,
            )
            if (
                block_rectangle.y0 >= seed_rectangle.y0
                and smaller_width
                and horizontal_overlap / smaller_width >= 0.35
                and vertical_gap <= 100.0
            ):
                selected_block_rectangles.add(
                    tuple(round(value, 3) for value in block_rectangle)
                )
                break

    blocks: list[dict[str, object]] = []
    for coordinates in sorted(
        selected_block_rectangles,
        key=lambda item: (item[1], item[0]),
    ):
        rectangle = fitz.Rect(coordinates)
        containment_rectangle = fitz.Rect(
            rectangle.x0 - 0.1,
            rectangle.y0 - 0.1,
            rectangle.x1 + 0.1,
            rectangle.y1 + 0.1,
        )
        matching_lines = [
            line
            for line in lines
            if containment_rectangle.contains(cast(fitz.Rect, line["rect"]).tl)
            and containment_rectangle.contains(cast(fitz.Rect, line["rect"]).br)
        ]
        matching_lines.sort(
            key=lambda line: (
                cast(fitz.Rect, line["rect"]).y0,
                cast(fitz.Rect, line["rect"]).x0,
            )
        )
        source_text = "\n".join(
            _clean_text(str(line["text"])) for line in matching_lines
        ).strip()
        if not source_text:
            continue
        blocks.append(
            {
                "page": 1,
                "source_text": source_text,
                "source_text_sha256": _text_sha256(source_text),
                "bbox": [rectangle.x0, rectangle.y0, rectangle.x1, rectangle.y1],
                "rect": rectangle,
            }
        )
    return blocks


def _discover_front_matter_lines(source_page: fitz.Page) -> list[dict[str, object]]:
    try:
        return _discover_front_matter_lines_strict(source_page)
    except RuntimeError as error:
        if str(error) not in {
            "Unable to auto-discover first-page front matter lines",
            "Unable to identify the first-page title region",
            "Unable to identify first-page author-name candidates",
            "Unable to identify first-page author-name lines",
            "Unable to pair first-page author and contact columns",
            "Unable to collect first-page identity column lines",
        }:
            raise
        return _front_matter_fallback_blocks(source_page)


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
    nonempty = [
        (line, segments)
        for line, segments in zip(lines, segmented_lines, strict=True)
        if segments
    ]
    if not nonempty:
        raise RuntimeError("Fixed replacement target is empty")
    lines = [line for line, _segments in nonempty]
    segmented_lines = [segments for _line, segments in nonempty]
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


_NUMERIC_CITATION_MARKER = re.compile(
    r"\[\s*(?:\d+(?:\s*[-\u2013\u2014]\s*\d+)?)(?:\s*,\s*"
    r"\d+(?:\s*[-\u2013\u2014]\s*\d+)?)*\s*\]"
)
_NUMERIC_CITATION_SPAN = re.compile(
    _NUMERIC_CITATION_MARKER.pattern + r"\s*[,.;:]?\s*$"
)
_TOC_SECTION_PREFIX = re.compile(r"^\s*(\d+(?:\.\d+)*)(?=[^\d.]|$)")
_DEBUG_LABEL_RE = re.compile(
    r"^(?:plain(?:\x03|\s)text|plaintext|paragraph\[[^\]]+\]-\[plain(?:\x03|\s)text\]|paragraph\[[^\]]+\]-\[plaintext\]|"
    r"title|formula|figure_caption|isolate_formula|figure|fallback_line|"
    r"pagenumber:\x03?\d+|Form\[[^\]]+\])$",
    re.IGNORECASE,
)
_DEBUG_LABEL_SEARCH_RE = re.compile(
    r"(?:titlearagraph\[[^\]]+\]-\[(?:plain(?:\x03|\s)text|plaintext)\]|"
    r"paragraph\[[^\]]+\]-\[(?:plain(?:\x03|\s)text|plaintext|title)\]|"
    r"plain(?:\x03|\s)text|plaintext|formula|figure_caption|isolate_formula|"
    r"figure|abandon|fallback_line|pagenumber:\x03?\d+|Form\[[^\]]+\])",
    re.IGNORECASE,
)


def _visual_text_rows(page: fitz.Page) -> list[list[dict[str, object]]]:
    """Group extractor lines that share one visual baseline."""

    rows: list[list[dict[str, object]]] = []
    for line in _page_text_lines(page):
        rectangle = cast(fitz.Rect, line["rect"])
        center = (rectangle.y0 + rectangle.y1) / 2
        for row in reversed(rows):
            row_rectangle = cast(fitz.Rect, row[0]["rect"])
            row_center = (row_rectangle.y0 + row_rectangle.y1) / 2
            if abs(center - row_center) <= max(
                2.5, min(rectangle.height, row_rectangle.height) * 0.35
            ):
                row.append(line)
                break
            if center - row_center > 4.0:
                rows.append([line])
                break
        else:
            rows.append([line])
    for row in rows:
        row.sort(key=lambda item: cast(fitz.Rect, item["rect"]).x0)
    return rows


def _remove_debug_labels(document: fitz.Document) -> int:
    """Remove BabelDOC debug-only labels before release QC.

    ``basic.debug=True`` is currently required to avoid a nested macOS
    multiprocessing teardown hang, but the pinned runtime also emits labels
    such as ``plain text`` and ``Form[...]`` into the PDF text layer.  They
    are implementation metadata, not paper content.  Only remove exact
    labels on lines from pages containing a recognizable debug marker; this
    keeps ordinary English words in the paper untouched.
    """
    removed = 0
    for page in document:
        lines = []
        has_debug_marker = False
        page_removed = 0
        for block in _text_dict(page, sort=True).get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(str(span.get("text", "")) for span in line.get("spans", []))
                normalized = text.replace("\x03", " ").strip()
                if (
                    _DEBUG_LABEL_RE.fullmatch(text.strip())
                    or normalized.startswith("paragraph[")
                    or _DEBUG_LABEL_SEARCH_RE.search(text)
                ):
                    has_debug_marker = True
                lines.append((fitz.Rect(*line["bbox"]), text))
        if not has_debug_marker:
            continue
        is_contents_page = any(
            _clean_text(text).casefold()
            in {"contents", "table of contents", "目录"}
            for _rectangle, text in lines
        )
        if is_contents_page:
            # Debug spans often overlap real TOC rows.  Character-level
            # redaction would erase legitimate section titles, so remove only
            # standalone metadata lines on a Contents page.
            for rectangle, text in lines:
                if _DEBUG_LABEL_RE.fullmatch(text.strip()):
                    page.add_redact_annot(rectangle, fill=(1, 1, 1))
                    removed += 1
            page.apply_redactions()
            continue
        # Redact only characters belonging to a debug label. Redacting the
        # whole extraction line destroys adjacent TOC titles/page numbers
        # when BabelDOC overlaps metadata with real text.
        for block in page.get_text("rawdict", sort=True).get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                characters = [
                    (str(char.get("c", "")), fitz.Rect(*char["bbox"]))
                    for span in line.get("spans", [])
                    for char in span.get("chars", [])
                ]
                line_text = "".join(char for char, _rect in characters)
                matches = list(_DEBUG_LABEL_SEARCH_RE.finditer(line_text))
                if _DEBUG_LABEL_RE.fullmatch(line_text.strip()) and not matches:
                    matches.append(
                        re.match(r"(?s)^.*$", line_text)
                    )
                for match in matches:
                    if match is None:
                        continue
                    matched = characters[match.start():match.end()]
                    if not matched:
                        continue
                    rectangle = fitz.Rect(matched[0][1])
                    for _char, char_rect in matched[1:]:
                        rectangle |= char_rect
                    page.add_redact_annot(rectangle, fill=False)
                    removed += 1
                    page_removed += 1
        if page_removed:
            page.apply_redactions()
    return removed


def _numbered_toc_rows(page: fitz.Page) -> list[dict[str, object]]:
    """Return numbered rows only when the page is an explicit contents page."""

    if not any(
        _clean_text(str(line["text"])).casefold()
        in {"contents", "table of contents"}
        for line in _page_text_lines(page)
    ):
        return []
    entries: list[dict[str, object]] = []
    for row in _visual_text_rows(page):
        row_text = " ".join(_clean_text(str(item["text"])) for item in row)
        section_match = _TOC_SECTION_PREFIX.match(row_text)
        last_text = _clean_text(str(row[-1]["text"]))
        if (
            section_match is None
            or not last_text.isdigit()
            or cast(fitz.Rect, row[-1]["rect"]).x0 < page.rect.width * 0.70
        ):
            continue
        section_id = section_match.group(1)
        source_title = row_text[section_match.end() :].strip()
        source_title = re.sub(
            rf"[.·…\s]*{re.escape(last_text)}\s*$", "", source_title
        ).strip(" .·…")
        if not source_title:
            continue
        rectangle = fitz.Rect(cast(fitz.Rect, row[0]["rect"]))
        for item in row[1:]:
            rectangle |= cast(fitz.Rect, item["rect"])
        entries.append(
            {
                "section_id": section_id,
                "destination": last_text,
                "source_title": source_title,
                "source_title_sha256": _text_sha256(_normalize(source_title)),
                "rect": rectangle,
                "destination_rect": fitz.Rect(cast(fitz.Rect, row[-1]["rect"])),
                "font_size": max(float(item["max_size"]) for item in row),
            }
        )
    return entries


def _toc_text(value: str) -> str:
    return re.sub(r"\s+", " ", _clean_text(value).replace("\x03", " ")).strip()


def _toc_id_matches(
    value: str,
    section_id: str,
    *,
    allow_fused_left: bool = False,
) -> list[re.Match[str]]:
    left_boundary = "" if allow_fused_left else r"(?<![\d.])"
    return list(
        re.finditer(
            rf"{left_boundary}{re.escape(section_id)}(?![\d.])",
            _toc_text(value),
        )
    )


def _strip_toc_segment(segment: str, destination: str) -> str | None:
    match = re.search(
        rf"[.·…\s]*{re.escape(destination)}\s*$",
        segment,
    )
    if match is None:
        return None
    title = segment[: match.start()].strip(" .·…")
    return title or None


def _toc_title_segments(
    text: str,
    entries: list[dict[str, object]],
) -> list[tuple[dict[str, object], str]]:
    clean = _toc_text(text)
    candidates = [
        _toc_id_matches(
            clean,
            str(entry["section_id"]),
            allow_fused_left=index > 0,
        )
        for index, entry in enumerate(entries)
    ]
    if any(not matches for matches in candidates):
        return []
    solutions: list[list[tuple[dict[str, object], str]]] = []

    def search(index: int, chosen: list[re.Match[str]]) -> None:
        if len(solutions) > 1:
            return
        if index == len(entries):
            parsed: list[tuple[dict[str, object], str]] = []
            for item_index, (entry, match) in enumerate(
                zip(entries, chosen, strict=True)
            ):
                end = (
                    chosen[item_index + 1].start()
                    if item_index + 1 < len(chosen)
                    else len(clean)
                )
                title = _strip_toc_segment(
                    clean[match.end() : end], str(entry["destination"])
                )
                if title is None:
                    return
                parsed.append((entry, title))
            solutions.append(parsed)
            return
        minimum = chosen[-1].end() if chosen else 0
        for match in candidates[index]:
            if match.start() < minimum:
                continue
            search(index + 1, [*chosen, match])

    search(0, [])
    return solutions[0] if len(solutions) == 1 else []


def _insert_segmented_toc_line(
    page: fitz.Page,
    rectangle: fitz.Rect,
    text: str,
    *,
    maximum_size: float,
) -> float:
    segments = [
        segment
        for segment in re.findall(r"[\x20-\x7e]+|[^\x20-\x7e]+", text)
        if segment
    ]
    for font_size in (
        maximum_size,
        9.5,
        9.0,
        8.5,
        8.0,
        7.5,
        7.0,
        6.5,
        6.0,
    ):
        if font_size > maximum_size:
            continue
        widths = [
            fitz.Font(
                fontname="helv" if segment.isascii() else "china-s"
            ).text_length(segment, fontsize=font_size)
            for segment in segments
        ]
        line_height = font_size * 1.25
        if sum(widths) > rectangle.width or line_height > rectangle.height:
            continue
        baseline = rectangle.y0 + (rectangle.height - line_height) / 2 + font_size
        x = rectangle.x0
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
        return font_size
    raise RuntimeError("Repaired TOC title does not fit its source row")


def _repair_merged_toc_rows(
    source_page: fitz.Page,
    output_page: fitz.Page,
) -> tuple[list[dict[str, object]], int]:
    """Split a BabelDOC paragraph that collapsed adjacent numbered TOC rows."""

    source_entries = _numbered_toc_rows(source_page)
    if len(source_entries) < 2:
        return [], 0
    repaired: list[dict[str, object]] = []
    group_count = 0
    output_lines = [
        line
        for line in _page_text_lines(output_page)
        if not _DEBUG_LABEL_RE.fullmatch(str(line["text"]).strip())
        and not _DEBUG_LABEL_SEARCH_RE.search(str(line["text"]))
    ]
    combined_lines: list[dict[str, object]] = []
    for line in output_lines:
        text = _toc_text(str(line["text"]))
        section_ids = re.findall(r"(?<![\d.])\d+(?:\.\d+)*", text)
        if (
            combined_lines
            and len(set(section_ids)) >= 2
            and not re.search(r"\d+(?:\.\d+)*", _toc_text(str(combined_lines[-1]["text"])))
            and len(_toc_text(str(combined_lines[-1]["text"]))) >= 3
            and cast(fitz.Rect, line["rect"]).y0
            - cast(fitz.Rect, combined_lines[-1]["rect"]).y0
            <= 24.0
        ):
            previous = combined_lines.pop()
            combined_lines.append(
                {
                    **line,
                    "text": f"{previous['text']} {line['text']}",
                    "rect": cast(fitz.Rect, previous["rect"]) | cast(fitz.Rect, line["rect"]),
                }
            )
        else:
            combined_lines.append(line)
    output_lines = combined_lines
    consumed_continuation_lines: set[int] = set()
    for line_index, line in enumerate(output_lines):
        if line_index in consumed_continuation_lines:
            continue
        text = str(line["text"])
        line_rectangle = cast(fitz.Rect, line["rect"])
        line_section = _TOC_SECTION_PREFIX.match(_toc_text(text))
        normalized_text = _toc_text(text)
        if line_section is not None and line_index + 1 < len(output_lines):
            next_line = output_lines[line_index + 1]
            next_text = _toc_text(str(next_line["text"]))
            next_rectangle = cast(fitz.Rect, next_line["rect"])
            if (
                (
                    re.match(r"^\s*[.·…]", next_text) is not None
                    or (
                        re.fullmatch(r"\d+", next_text)
                        and next_rectangle.x0 >= output_page.rect.width * 0.70
                    )
                )
                and next_rectangle.y0 - line_rectangle.y0 <= 24.0
            ):
                normalized_text = _toc_text(f"{text} {next_text}")
                line_rectangle |= next_rectangle
                consumed_continuation_lines.add(line_index + 1)
        if line_section is None:
            if re.match(r"^\s*[.·…]", normalized_text) is None:
                continue
            embedded_sections = [
                (
                    match.start(),
                    entry,
                )
                for entry in source_entries
                for match in _toc_id_matches(
                    normalized_text,
                    str(entry["section_id"]),
                    allow_fused_left=True,
                )
            ]
            if not embedded_sections:
                continue
            first_section = min(embedded_sections, key=lambda item: item[0])[1]
            first_section_id = str(first_section["section_id"])
            line_y_tolerance = 35.0
        else:
            first_section_id = line_section.group(1)
            line_y_tolerance = 8.0
        if not first_section_id:
            continue
        candidate_groups: list[
            tuple[list[dict[str, object]], list[tuple[dict[str, object], str]]]
        ] = []
        for start in range(len(source_entries)):
            source_rectangle = cast(fitz.Rect, source_entries[start]["rect"])
            if (
                first_section_id != str(source_entries[start]["section_id"])
                or abs(line_rectangle.x0 - source_rectangle.x0) > 24.0
                or abs(line_rectangle.y0 - source_rectangle.y0) > line_y_tolerance
            ):
                continue
            if not _toc_id_matches(
                normalized_text, str(source_entries[start]["section_id"])
            ):
                continue
            for end in range(start + 1, len(source_entries) + 1):
                entries = source_entries[start:end]
                segments = _toc_title_segments(normalized_text, entries)
                if len(segments) == len(entries):
                    candidate_groups.append((entries, segments))
        if not candidate_groups:
            continue
        longest_size = max(len(candidate[0]) for candidate in candidate_groups)
        longest_groups = [
            candidate
            for candidate in candidate_groups
            if len(candidate[0]) == longest_size
        ]
        if len(longest_groups) != 1:
            continue
        contained, segments = longest_groups[0]
        group_count += 1
        patch = fitz.Rect(line_rectangle)
        for entry in contained:
            patch |= cast(fitz.Rect, entry["rect"])
        patch = fitz.Rect(
            max(0.0, patch.x0 - 2.0),
            max(0.0, patch.y0 - 2.0),
            min(output_page.rect.width, patch.x1 + 2.0),
            min(output_page.rect.height, patch.y1 + 2.0),
        )
        output_page.add_redact_annot(patch, fill=(1, 1, 1))
        output_page.apply_redactions()
        for entry, title in segments:
            row_rectangle = cast(fitz.Rect, entry["rect"])
            destination_rectangle = cast(fitz.Rect, entry["destination_rect"])
            title_rectangle = fitz.Rect(
                row_rectangle.x0,
                row_rectangle.y0 - 2.0,
                destination_rectangle.x0 - 8.0,
                row_rectangle.y1 + 4.0,
            )
            target = f"{entry['section_id']} {title}"
            maximum_size = min(float(entry["font_size"]), 10.5)
            inserted_font_size = _insert_segmented_toc_line(
                output_page,
                title_rectangle,
                target,
                maximum_size=maximum_size,
            )
            output_page.insert_text(
                (destination_rectangle.x0, destination_rectangle.y1 - 1.0),
                str(entry["destination"]),
                fontsize=min(float(entry["font_size"]), 10.5),
                fontname="tiro",
                color=(0, 0, 1),
                overlay=True,
            )
            repaired.append(
                {
                    "section_id": str(entry["section_id"]),
                    "destination": str(entry["destination"]),
                    "title": title,
                    "title_sha256": _text_sha256(_normalize(title)),
                    "font_size": inserted_font_size,
                    "source_bbox": list(row_rectangle),
                    "source_text_sha256": _text_sha256(
                        f"{entry['section_id']}\n{entry['destination']}"
                    ),
                }
            )
    return repaired, group_count


def _toc_topology_conservation(
    source_page: fitz.Page,
    output_page: fitz.Page,
    *,
    output_page_number: int,
    source_page_number: int,
    expected_titles: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    expected_titles = expected_titles or {}
    output_rows: list[dict[str, object]] = []
    for row in _visual_text_rows(output_page):
        text = " ".join(_clean_text(str(item["text"])) for item in row)
        section_match = _TOC_SECTION_PREFIX.match(text)
        destination = _clean_text(str(row[-1]["text"]))
        destination_rectangle = cast(fitz.Rect, row[-1]["rect"])
        if (
            section_match is None
            or not destination.isdigit()
            or destination_rectangle.x0 < output_page.rect.width * 0.70
        ):
            continue
        title = _strip_toc_segment(
            text[section_match.end() :], destination
        )
        output_rows.append(
            {
                "section_id": section_match.group(1),
                "destination": destination,
                "title": title,
                "title_sha256": (
                    _text_sha256(_normalize(title)) if title is not None else None
                ),
                "y": min(cast(fitz.Rect, item["rect"]).y0 for item in row),
            }
        )
    for entry in _numbered_toc_rows(source_page):
        section_id = str(entry["section_id"])
        matches = [
            row
            for row in output_rows
            if row["section_id"] == section_id
        ]
        source_rectangle = cast(fitz.Rect, entry["rect"])
        output_y = matches[0]["y"] if len(matches) == 1 else None
        output_title = matches[0]["title"] if len(matches) == 1 else None
        expected_title = expected_titles.get(section_id)
        matched = (
            output_y is not None
            and abs(output_y - source_rectangle.y0) <= 8.0
            and matches[0]["destination"] == str(entry["destination"])
            and output_title is not None
            and (
                expected_title is None
                or _normalize(str(output_title)) == _normalize(expected_title)
            )
        )
        results.append(
            {
                "output_page": output_page_number,
                "source_page": source_page_number,
                "section_id": section_id,
                "destination": str(entry["destination"]),
                "source_title": str(entry["source_title"]),
                "source_title_sha256": str(entry["source_title_sha256"]),
                "source_y": source_rectangle.y0,
                "output_y": output_y,
                "output_title": output_title,
                "output_title_sha256": (
                    matches[0]["title_sha256"] if len(matches) == 1 else None
                ),
                "expected_repaired_title_sha256": (
                    _text_sha256(_normalize(expected_title))
                    if expected_title is not None
                    else None
                ),
                "matched": matched,
            }
        )
    return results


def _normalize_numeric_citation_marker(value: str) -> str:
    return (
        re.sub(r"\s+", "", value)
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )


def _numeric_citation_line_groups(
    page: fitz.Page,
    *,
    excluded_rectangles: tuple[fitz.Rect, ...] = (),
    textpage: object | None = None,
) -> list[list[str]]:
    fragments: list[tuple[fitz.Rect, list[str]]] = []
    pending_fragment: tuple[fitz.Rect, str] | None = None
    text_blocks = _text_dict(page, sort=True)
    for block in text_blocks.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            rectangle = fitz.Rect(*line["bbox"])
            center = fitz.Point(
                (rectangle.x0 + rectangle.x1) / 2,
                (rectangle.y0 + rectangle.y1) / 2,
            )
            if any(excluded.contains(center) for excluded in excluded_rectangles):
                continue
            line_text = "".join(
                str(span.get("text", "")) for span in line.get("spans", [])
            )
            line_consumed_by_pending = False
            if pending_fragment is not None:
                pending_rect, pending_text = pending_fragment
                combined = pending_text + line_text
                combined_markers = [
                    _normalize_numeric_citation_marker(match.group(0))
                    for match in _NUMERIC_CITATION_MARKER.finditer(combined)
                ]
                if combined_markers:
                    fragments.append((pending_rect, combined_markers))
                    pending_fragment = None
                    line_consumed_by_pending = True
                elif pending_text.count("[") > pending_text.count("]"):
                    pending_fragment = (pending_rect, combined)
                    continue
                else:
                    pending_fragment = None
            line_markers = [] if line_consumed_by_pending else [
                _normalize_numeric_citation_marker(match.group(0))
                for match in _NUMERIC_CITATION_MARKER.finditer(line_text)
            ]
            if line_markers:
                fragments.append((rectangle, line_markers))
            elif line_text.count("[") > line_text.count("]"):
                pending_fragment = (rectangle, line_text)
    clustered: list[list[tuple[fitz.Rect, list[str]]]] = []
    midpoint = page.rect.width / 2.0

    def column(rectangle: fitz.Rect) -> str:
        if rectangle.x1 <= midpoint:
            return "left"
        if rectangle.x0 >= midpoint:
            return "right"
        return "full"

    for rectangle, markers in fragments:
        target: list[tuple[fitz.Rect, list[str]]] | None = None
        for group in reversed(clustered):
            group_rectangle = group[0][0]
            same_baseline = abs(rectangle.y0 - group_rectangle.y0) <= 2.0
            same_column = (
                column(rectangle) == column(group_rectangle)
                or "full" in {column(rectangle), column(group_rectangle)}
            )
            if same_baseline and same_column:
                target = group
                break
            if rectangle.y0 - group_rectangle.y0 > 2.0:
                break
        if target is None:
            clustered.append([(rectangle, markers)])
        else:
            target.append((rectangle, markers))
    return [
        [
            marker
            for _rectangle, markers in sorted(group, key=lambda item: item[0].x0)
            for marker in markers
        ]
        for group in clustered
    ]


def _numeric_citation_markers(
    page: fitz.Page,
    *,
    excluded_rectangles: tuple[fitz.Rect, ...] = (),
) -> list[str]:
    return [
        marker
        for group in _numeric_citation_line_groups(
            page,
            excluded_rectangles=excluded_rectangles,
        )
        for marker in group
    ]


def _citation_permutation_within_source_lines(
    source_groups: list[list[str]],
    output_groups: list[list[str]],
) -> bool:
    source_markers = [marker for group in source_groups for marker in group]
    output_markers = [marker for group in output_groups for marker in group]
    if Counter(source_markers) != Counter(output_markers):
        return False
    output_index = 0
    for group in source_groups:
        accumulated: list[str] = []
        while output_index < len(output_groups) and len(accumulated) < len(group):
            output_group = output_groups[output_index]
            if len(accumulated) + len(output_group) > len(group):
                return False
            accumulated.extend(output_group)
            output_index += 1
        if Counter(group) != Counter(accumulated):
            return False
    return output_index == len(output_groups)


def _document_citations_match(
    source_groups: list[list[str]], output_groups: list[list[str]]
) -> bool:
    """Accept exact document order before considering safe source-line reflow."""
    source_markers = [marker for group in source_groups for marker in group]
    output_markers = [marker for group in output_groups for marker in group]
    if source_markers == output_markers:
        return True
    return _citation_permutation_within_source_lines(source_groups, output_groups)


def _citation_positions_match(
    source: fitz.Document,
    output: fitz.Document,
    *,
    excluded_source: dict[int, list[fitz.Rect]],
    excluded_output: dict[int, list[fitz.Rect]],
    tolerance: float = 5.0,
) -> bool:
    """Accept reflow when each citation remains anchored to its source glyph."""
    marker_pattern = re.compile(r"^\s*\[\s*\d+(?:\s*[,\-]\s*\d+)*\s*\]\s*[,.;:]?\s*$")

    def collect(document: fitz.Document, excluded: dict[int, list[fitz.Rect]]) -> dict[int, list[tuple[str, fitz.Rect]]]:
        result: dict[int, list[tuple[str, fitz.Rect]]] = {}
        for page_index, page in enumerate(document):
            for block in page.get_text("rawdict", sort=True).get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    chars = [
                        {
                            "c": str(character.get("c", "")),
                            "rect": fitz.Rect(*character["bbox"]),
                        }
                        for span in line.get("spans", [])
                        for character in span.get("chars", [])
                    ]
                    line_text = "".join(str(item["c"]) for item in chars)
                    for match in _NUMERIC_CITATION_MARKER.finditer(line_text):
                        matched = chars[match.start() : match.end()]
                        if not matched:
                            continue
                        rect = fitz.Rect(cast(fitz.Rect, matched[0]["rect"]))
                        for character in matched[1:]:
                            rect |= cast(fitz.Rect, character["rect"])
                        if any(item.contains(rect.tl) for item in excluded.get(page_index, [])):
                            continue
                        result.setdefault(page_index, []).append(
                            (_normalize_numeric_citation_marker(match.group(0)), rect)
                        )
        return result

    source_markers = collect(source, excluded_source)
    output_markers = collect(output, excluded_output)
    source_count = sum(map(len, source_markers.values()))
    output_count = sum(map(len, output_markers.values()))
    if source_count == 0 or source_count != output_count:
        return False
    for page_index, expected in source_markers.items():
        available = list(output_markers.get(page_index, []))
        if len(expected) != len(available):
            return False
        for marker, source_rect in expected:
            matches = [
                (index, candidate_rect)
                for index, (candidate, candidate_rect) in enumerate(available)
                if candidate == marker
                and abs(candidate_rect.x0 - source_rect.x0) <= tolerance
                and abs(candidate_rect.y0 - source_rect.y0) <= tolerance
            ]
            if not matches:
                return False
            available.pop(matches[0][0])
    return True


def _normalize_numeric_citation_glyphs(
    document: fitz.Document,
) -> list[dict[str, object]]:
    """Redraw numeric citation spans without reusing unsafe subset fonts."""

    receipts: list[dict[str, object]] = []
    for page_index, page in enumerate(document):
        candidates: list[dict[str, object]] = []
        for block in _text_dict(page, sort=True).get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                direction = tuple(line.get("dir", (1.0, 0.0)))
                if abs(float(direction[0]) - 1.0) > 1e-3 or abs(
                    float(direction[1])
                ) > 1e-3:
                    continue
                for span in line.get("spans", []):
                    text = str(span.get("text", ""))
                    if not _NUMERIC_CITATION_SPAN.fullmatch(text):
                        continue
                    rectangle = fitz.Rect(*span["bbox"])
                    if rectangle.is_empty or rectangle.is_infinite:
                        raise RuntimeError("Numeric citation has an invalid bounding box")
                    rgb = fitz.sRGB_to_rgb(int(span.get("color", 0)))
                    candidates.append(
                        {
                            "text": text,
                            "bbox": rectangle,
                            "origin": fitz.Point(*span["origin"]),
                            "font_size": float(span["size"]),
                            "color": tuple(channel / 255 for channel in rgb),
                        }
                    )
        if not candidates:
            continue
        for candidate in candidates:
            page.add_redact_annot(
                cast(fitz.Rect, candidate["bbox"]),
                fill=False,
            )
        page.apply_redactions()
        for candidate in candidates:
            page.insert_text(
                cast(fitz.Point, candidate["origin"]),
                str(candidate["text"]),
                fontsize=float(candidate["font_size"]),
                fontname="tiro",
                color=cast(tuple[float, float, float], candidate["color"]),
                overlay=True,
            )
            rectangle = cast(fitz.Rect, candidate["bbox"])
            receipts.append(
                {
                    "output_page": page_index + 1,
                    "text": str(candidate["text"]),
                    "text_sha256": _text_sha256(str(candidate["text"])),
                    "bbox": [rectangle.x0, rectangle.y0, rectangle.x1, rectangle.y1],
                    "font": "Times-Roman",
                    "font_size": float(candidate["font_size"]),
                }
            )
    return receipts


def _restore_citation_sequence(
    document: fitz.Document,
    *,
    expected_markers: list[str],
    excluded_rectangles_by_page: dict[int, list[fitz.Rect]],
    allow_order_only: bool = False,
) -> dict[str, object]:
    """Restore citation labels only when the output is a safe permutation.

    BabelDOC can move a citation while reflowing a translated column, and an
    LLM can occasionally substitute one valid-looking citation for another.
    Numeric citation labels are source-controlled data, so when the output
    has exactly the same number of labels and every label belongs to the
    source marker vocabulary, restore the source sequence at the output
    glyph positions.  Unknown labels or a count mismatch remain fail-closed.
    """
    candidates: list[dict[str, object]] = []
    fragments: list[tuple[fitz.Rect, dict[str, object]]] = []
    for page_index, page in enumerate(document):
        excluded = tuple(excluded_rectangles_by_page.get(page_index, []))
        for block in page.get_text("rawdict", sort=True).get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                characters: list[dict[str, object]] = []
                for span in line.get("spans", []):
                    for character in span.get("chars", []):
                        characters.append(
                            {
                                "text": str(character.get("c", "")),
                                "bbox": fitz.Rect(*character["bbox"]),
                                "size": float(span["size"]),
                                "color": fitz.sRGB_to_rgb(int(span.get("color", 0))),
                            }
                        )
                line_text = "".join(str(item["text"]) for item in characters)
                for match in _NUMERIC_CITATION_MARKER.finditer(line_text):
                    matched = characters[match.start() : match.end()]
                    if not matched:
                        continue
                    rectangle = fitz.Rect(cast(fitz.Rect, matched[0]["bbox"]))
                    for character in matched[1:]:
                        rectangle |= cast(fitz.Rect, character["bbox"])
                    center = fitz.Point(
                        (rectangle.x0 + rectangle.x1) / 2,
                        (rectangle.y0 + rectangle.y1) / 2,
                    )
                    if any(item.contains(center) for item in excluded):
                        continue
                    candidates.append(
                        {
                            "page": page_index,
                            "line_rect": fitz.Rect(*line["bbox"]),
                            "rect": rectangle,
                            "origin": fitz.Point(
                                rectangle.x0,
                                rectangle.y1 - float(matched[0]["size"]) * 0.2,
                            ),
                            "size": float(matched[0]["size"]),
                            "color": matched[0]["color"],
                            "text": _normalize_numeric_citation_marker(match.group(0)),
                        }
                    )
                    fragments.append((fitz.Rect(*line["bbox"]), candidates[-1]))
    clustered: list[list[tuple[fitz.Rect, dict[str, object]]]] = []
    midpoint_by_page = {
        index: page.rect.width / 2.0 for index, page in enumerate(document)
    }
    for rectangle, candidate in fragments:
        page_index = int(candidate["page"])
        midpoint = midpoint_by_page[page_index]
        target: list[tuple[fitz.Rect, dict[str, object]]] | None = None
        for group in reversed(clustered):
            group_rectangle = group[0][0]
            same_baseline = abs(rectangle.y0 - group_rectangle.y0) <= 2.0
            rectangle_column = (
                "left" if rectangle.x1 <= midpoint else
                "right" if rectangle.x0 >= midpoint else "full"
            )
            group_column = (
                "left" if group_rectangle.x1 <= midpoint else
                "right" if group_rectangle.x0 >= midpoint else "full"
            )
            same_column = rectangle_column == group_column or "full" in {
                rectangle_column, group_column
            }
            if same_baseline and same_column:
                target = group
                break
            if rectangle.y0 - group_rectangle.y0 > 2.0:
                break
        if target is None:
            clustered.append([(rectangle, candidate)])
        else:
            target.append((rectangle, candidate))
    candidates = [
        candidate
        for group in clustered
        for _line_rect, candidate in sorted(group, key=lambda item: item[1]["rect"].x0)
    ]
    output_markers = [str(item["text"]) for item in candidates]
    source_vocabulary = set(expected_markers)
    safe_permutation = (
        len(output_markers) == len(expected_markers)
        and (
            allow_order_only
            or Counter(output_markers) != Counter(expected_markers)
        )
        and all(marker in source_vocabulary for marker in output_markers)
    )
    if not safe_permutation or output_markers == expected_markers:
        return {
            "attempted": False,
            "safe_permutation": safe_permutation,
            "replaced_count": 0,
        }
    for item in candidates:
        document[int(item["page"])].add_redact_annot(
            cast(fitz.Rect, item["rect"]), fill=False
        )
    for page in document:
        page.apply_redactions()
    for item, marker in zip(candidates, expected_markers):
        rgb = cast(tuple[int, int, int], item["color"])
        document[int(item["page"])].insert_text(
            cast(fitz.Point, item["origin"]),
            marker,
            fontsize=float(item["size"]),
            fontname="tiro",
            color=tuple(channel / 255 for channel in rgb),
            overlay=True,
        )
    receipt = {
        "attempted": True,
        "safe_permutation": True,
        "replaced_count": len(candidates),
        "source_sha256": _text_sha256("\n".join(expected_markers)),
        "before_sha256": _text_sha256("\n".join(output_markers)),
    }
    if not allow_order_only:
        convergence = _restore_citation_sequence(
            document,
            expected_markers=expected_markers,
            excluded_rectangles_by_page=excluded_rectangles_by_page,
            allow_order_only=True,
        )
        receipt["convergence"] = convergence
    return receipt


def _restore_missing_citations_from_source(
    source: fitz.Document,
    translated: fitz.Document,
    *,
    selected_source_pages: tuple[int, ...],
    excluded_rectangles_by_page: dict[int, list[fitz.Rect]],
) -> dict[str, object]:
    """Restore citations omitted by the model at their source-page anchors.

    This is deliberately narrower than general text repair: it only handles
    a translated page whose citation markers are a subset of the source
    markers, rejects unknown markers, and never touches figure/table/reference
    regions.  The later document-level conservation check remains mandatory.
    """
    source_candidates: list[tuple[int, str, fitz.Rect, float, tuple[float, float, float]]] = []
    output_candidates: list[tuple[int, str, fitz.Rect]] = []

    def collect(document: fitz.Document, page_indexes: list[int], output: bool) -> None:
        target = output_candidates if output else source_candidates
        for output_index, page_index in enumerate(page_indexes):
            page = document[page_index]
            excluded = tuple(excluded_rectangles_by_page.get(output_index, []))
            for block in page.get_text("rawdict", sort=True).get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    chars = [
                        (str(char.get("c", "")), fitz.Rect(*char["bbox"]), span)
                        for span in line.get("spans", [])
                        for char in span.get("chars", [])
                    ]
                    line_text = "".join(item[0] for item in chars)
                    for match in _NUMERIC_CITATION_MARKER.finditer(line_text):
                        matched = chars[match.start():match.end()]
                        if not matched:
                            continue
                        rect = fitz.Rect(matched[0][1])
                        for _char, char_rect, _span in matched[1:]:
                            rect |= char_rect
                        center = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
                        if any(item.contains(center) for item in excluded):
                            continue
                        marker = _normalize_numeric_citation_marker(match.group(0))
                        if output:
                            target.append((output_index, marker, rect))
                        else:
                            span = matched[0][2]
                            color = fitz.sRGB_to_rgb(int(span.get("color", 0)))
                            target.append((output_index, marker, rect, float(span["size"]), tuple(channel / 255 for channel in color)))

    collect(source, [page - 1 for page in selected_source_pages], False)
    collect(translated, list(range(len(selected_source_pages))), True)
    expected = [item[1] for item in source_candidates]
    actual = [item[1] for item in output_candidates]
    # A citation may legitimately cross an output page boundary after Chinese
    # reflow.  If the document already contains the complete source sequence,
    # do not perform page-local “missing” repairs that would duplicate the
    # citation on the preceding page.
    if actual == expected:
        return {"attempted": False, "replaced_count": 0, "reason": "complete_document_sequence"}
    grouped_actual = [
        marker
        for output_index in range(len(selected_source_pages))
        for group in _numeric_citation_line_groups(
            translated[output_index],
            excluded_rectangles=tuple(excluded_rectangles_by_page.get(output_index, [])),
        )
        for marker in group
    ]
    # The raw character collector intentionally stays conservative, but the
    # line-group scanner can join a citation split across visual lines (for
    # example ``[71-`` followed by ``73]``).  If that higher-level view is
    # already complete, a page-local repair would create a duplicate marker.
    if grouped_actual == expected:
        return {"attempted": False, "replaced_count": 0, "reason": "complete_grouped_sequence"}
    missing: list[tuple[int, str, fitz.Rect, float, tuple[float, float, float]]] = []
    for page_index in range(len(selected_source_pages)):
        source_page = [item for item in source_candidates if item[0] == page_index]
        output_page = [item for item in output_candidates if item[0] == page_index]
        expected_page = [item[1] for item in source_page]
        actual_page = [item[1] for item in output_page]
        # A page with an unknown or relocated citation remains fail-closed;
        # do not let it prevent safe recovery on unrelated pages.
        if not expected_page or any(
            marker not in set(expected_page) for marker in actual_page
        ):
            continue
        # Match by in-page order rather than coordinates: Chinese reflow can
        # move a citation substantially while preserving the source sequence.
        output_cursor = 0
        page_missing = []
        for item in source_page:
            _output_index, marker, rect, size, color = item
            if output_cursor < len(output_page) and output_page[output_cursor][1] == marker:
                output_cursor += 1
            else:
                page_missing.append(item)
        if page_missing and output_cursor == len(output_page):
            missing.extend(page_missing)
    if not missing:
        return {"attempted": False, "replaced_count": 0, "reason": "not_a_safe_subset"}
    for output_index, marker, rect, size, color in missing:
        translated[output_index].insert_text(
            fitz.Point(rect.x0, rect.y1 - size * 0.2),
            marker,
            fontsize=size,
            fontname="tiro",
            color=color,
            overlay=True,
        )
    return {
        "attempted": True,
        "replaced_count": len(missing),
        "source_sha256": _text_sha256("\n".join(expected)),
        "before_sha256": _text_sha256("\n".join(actual)),
    }


def protect_pdf(
    *,
    source_pdf: Path,
    translated_pdf: Path,
    output_pdf: Path,
    selected_source_pages: tuple[int, ...],
    ir_xml: Path,
    source_header: str | None = None,
    target_header: str | None = None,
    auto_header: bool = False,
    auto_front_matter: bool = False,
    auto_header_min_recurrence: int = 3,
    fixed_replacements: tuple[tuple[int, str, str], ...] = (),
    left_fixed_replacements: tuple[tuple[int, str, str], ...] = (),
    verbatim_texts: tuple[tuple[int, str], ...] = (),
    output_replacements: tuple[tuple[int, str, str], ...] = (),
) -> dict[str, object]:
    """Restore protected regions while closing both documents on every path."""

    with fitz.open(source_pdf) as source, fitz.open(translated_pdf) as translated:
        return _protect_pdf_open_documents(
            source_pdf=source_pdf,
            translated_pdf=translated_pdf,
            output_pdf=output_pdf,
            selected_source_pages=selected_source_pages,
            ir_xml=ir_xml,
            source=source,
            translated=translated,
            source_header=source_header,
            target_header=target_header,
            auto_header=auto_header,
            auto_front_matter=auto_front_matter,
            auto_header_min_recurrence=auto_header_min_recurrence,
            fixed_replacements=fixed_replacements,
            left_fixed_replacements=left_fixed_replacements,
            verbatim_texts=verbatim_texts,
            output_replacements=output_replacements,
        )


def _protect_pdf_open_documents(
    *,
    source_pdf: Path,
    translated_pdf: Path,
    output_pdf: Path,
    selected_source_pages: tuple[int, ...],
    ir_xml: Path,
    source: fitz.Document,
    translated: fitz.Document,
    source_header: str | None = None,
    target_header: str | None = None,
    auto_header: bool = False,
    auto_front_matter: bool = False,
    auto_header_min_recurrence: int = 3,
    fixed_replacements: tuple[tuple[int, str, str], ...] = (),
    left_fixed_replacements: tuple[tuple[int, str, str], ...] = (),
    verbatim_texts: tuple[tuple[int, str], ...] = (),
    output_replacements: tuple[tuple[int, str, str], ...] = (),
) -> dict[str, object]:
    """Restore figure/table/reference regions and enforce one canonical header."""

    figures, tables = _ir_regions(ir_xml)
    source_textpages = {
        page_index: page.get_textpage()
        for page_index, page in enumerate(source)
    }
    page_map = {
        source_page: output_index
        for output_index, source_page in enumerate(selected_source_pages)
    }
    reference_clips = _reference_clips(source, source_textpages)
    if len(translated) != len(selected_source_pages):
        raise RuntimeError("Translated page count does not match selected source pages")
    if auto_header_min_recurrence < 2:
        raise RuntimeError("Auto header recurrence threshold must be at least 2")
    debug_label_removal_count = _remove_debug_labels(translated)

    auto_header_receipts: list[dict[str, object]] | None = None
    header_families: list[tuple[str, str]] = []
    if auto_header:
        auto_header_receipts = _discover_running_headers(
            source,
            translated,
            selected_source_pages,
            page_map,
            min_recurrence=auto_header_min_recurrence,
            source_textpages=source_textpages,
        )
        header_families = [
            (str(family["source_text"]), str(family["canonical_target"]))
            for family in auto_header_receipts
        ]
    elif source_header is not None or target_header is not None:
        if not source_header or not target_header:
            raise RuntimeError("Explicit source and target headers are required")
        header_families = [(source_header, target_header)]
    elif len(selected_source_pages) > 2:
        raise RuntimeError("Explicit source and target headers are required")

    raster_rectangles_by_output: dict[int, list[fitz.Rect]] = {}
    reference_rectangles_by_output: dict[int, list[fitz.Rect]] = {}
    kind_counts = {"figure": 0, "table": 0, "reference": 0}
    for kind, regions in (("figure", figures), ("table", tables)):
        for source_page_number, box in regions:
            output_index = page_map.get(source_page_number)
            if output_index is None:
                continue
            raster_rectangles_by_output.setdefault(output_index, []).append(
                _top_rect(source[source_page_number - 1], box)
            )
            kind_counts[kind] += 1
    for source_page_number, clips in reference_clips.items():
        output_index = page_map.get(source_page_number)
        if output_index is None:
            continue
        reference_rectangles_by_output.setdefault(output_index, []).extend(clips)
        kind_counts["reference"] += 1
    verbatim_text_count = 0
    verbatim_texts_covered_by_raster_region = 0
    verbatim_rectangles_by_output: dict[int, list[fitz.Rect]] = {}
    identity_rectangles_by_output: dict[int, list[fitz.Rect]] = {}
    for source_page_number, source_text in verbatim_texts:
        output_index = page_map.get(source_page_number)
        if output_index is None:
            continue
        rectangle = _source_text_rect(
            source[source_page_number - 1],
            source_text,
            textpage=source_textpages[source_page_number - 1],
        )
        if rectangle is None:
            raise RuntimeError(
                f"Verbatim source text was not found on page {source_page_number}"
            )
        # A paragraph owned by a figure xobject is already restored by the
        # whole source raster clip.  Adding a second, smaller image for every
        # label inside that clip creates overlapping PDF image objects and can
        # make the saved-page pixel-hash check nondeterministic.  The whole
        # clip is the stronger protection boundary, so record the paragraph as
        # covered while avoiding a nested restore operation.
        raster_regions = raster_rectangles_by_output.get(output_index, [])
        if any(
            rectangle.get_area()
            and ((rectangle & raster_region).get_area() / rectangle.get_area())
            >= 0.8
            for raster_region in raster_regions
        ):
            verbatim_texts_covered_by_raster_region += 1
            verbatim_text_count += 1
            continue
        verbatim_rectangles_by_output.setdefault(output_index, []).append(rectangle)
        verbatim_text_count += 1
    auto_front_matter_receipt: dict[str, object] | None = None
    if auto_front_matter:
        discovered_blocks = _discover_front_matter_lines(source[0])
        auto_front_matter_receipt = {"blocks": []}
        for block in discovered_blocks:
            output_index = page_map.get(int(block["page"]))
            if output_index is None:
                continue
            identity_rectangles_by_output.setdefault(output_index, []).append(
                cast(fitz.Rect, block["rect"])
            )
            verbatim_text_count += 1
            cast(list[dict[str, object]], auto_front_matter_receipt["blocks"]).append(
                {
                    "page": int(block["page"]),
                    "source_text": str(block["source_text"]),
                    "source_text_sha256": str(block["source_text_sha256"]),
                    "bbox": list(cast(list[float], block["bbox"])),
                }
            )
    for output_index, rectangles in verbatim_rectangles_by_output.items():
        raster_rectangles_by_output.setdefault(output_index, []).extend(
            _coalesce_adjacent_text(rectangles)
        )
    for output_index, rectangles in identity_rectangles_by_output.items():
        identity_rectangles_by_output[output_index] = _coalesce_identity_regions(
            rectangles
        )

    header_rectangles_by_output: dict[int, list[tuple[fitz.Rect, str]]] = {}
    for resolved_source_header, resolved_target_header in header_families:
        for output_index, source_page_number in enumerate(selected_source_pages):
            source_rect = _find_header_rect(
                source[source_page_number - 1],
                resolved_source_header,
                textpage=source_textpages[source_page_number - 1],
            )
            if source_rect is not None:
                header_rectangles_by_output.setdefault(output_index, []).append(
                    (source_rect, resolved_target_header)
                )

    protected_regions: list[tuple[int, int, fitz.Rect, str, str | None]] = []
    all_output_indices = sorted(
        set(raster_rectangles_by_output)
        | set(identity_rectangles_by_output)
        | set(reference_rectangles_by_output)
        | set(header_rectangles_by_output)
    )
    for output_index in all_output_indices:
        source_page_number = selected_source_pages[output_index]
        output_page = translated[output_index]
        raster_rectangles = _coalesce(
            raster_rectangles_by_output.get(output_index, [])
        )
        # Source-controlled regions can be adjacent by a sub-point after
        # BabelDOC reflow. Merge those before redaction/insertion so a later
        # large clip cannot overwrite a neighboring clip's boundary pixels.
        raster_rectangles = _coalesce_adjacent_text(raster_rectangles)
        identity_rectangles = identity_rectangles_by_output.get(output_index, [])
        reference_rectangles = _coalesce(
            reference_rectangles_by_output.get(output_index, [])
        )
        for rectangle in raster_rectangles:
            output_page.add_redact_annot(
                _expanded_redaction_rectangle(output_page, rectangle),
                fill=(1, 1, 1),
            )
        for rectangle in identity_rectangles:
            output_page.add_redact_annot(rectangle, fill=(1, 1, 1))
        for rectangle in reference_rectangles:
            output_page.add_redact_annot(
                _reference_redaction_rectangle(output_page, rectangle),
                fill=(1, 1, 1),
            )
        for rectangle, _target_text in header_rectangles_by_output.get(
            output_index, []
        ):
            output_page.add_redact_annot(
                fitz.Rect(
                    rectangle.x0 - 2,
                    rectangle.y0 - 1,
                    rectangle.x1 + 2,
                    rectangle.y1 + 0.5,
                ),
                fill=(1, 1, 1),
            )
        output_page.apply_redactions()
        for rectangle in [
            *raster_rectangles,
            *identity_rectangles,
            *reference_rectangles,
        ]:
            pixel_hash = _insert_rasterized_source_clip(
                output_page,
                source[source_page_number - 1],
                rectangle,
            )
            protected_regions.append(
                (
                    output_index,
                    source_page_number,
                    rectangle,
                    "rasterized_source_clip",
                    pixel_hash,
                )
            )

    reference_heading_count = 0
    for source_page_number in reference_clips:
        output_index = page_map.get(source_page_number)
        if output_index is None:
            continue
        source_page = source[source_page_number - 1]
        headings = [
            *source_page.search_for("References"),
            *source_page.search_for("Reference"),
            *source_page.search_for("Bibliography"),
        ]
        if not headings:
            continue
        _insert_reference_heading(
            translated[output_index],
            min(headings, key=lambda rectangle: rectangle.y0),
        )
        reference_heading_count += 1

    header_count = 0
    for output_index, headers in header_rectangles_by_output.items():
        for source_rect, target_text in headers:
            _insert_header(
                translated[output_index],
                source_rect,
                target_text,
                already_redacted=True,
            )
            header_count += 1

    fixed_replacement_count = 0
    fixed_checks: list[tuple[int, fitz.Rect, str]] = []
    for source_page_number, source_text, target_text in fixed_replacements:
        output_index = page_map.get(source_page_number)
        if output_index is None:
            continue
        source_rectangle = _source_text_rect(
            source[source_page_number - 1],
            source_text,
            textpage=source_textpages[source_page_number - 1],
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
            source[source_page_number - 1],
            source_text,
            textpage=source_textpages[source_page_number - 1],
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

    repaired_toc_rows: list[dict[str, object]] = []
    repaired_toc_group_count = 0
    for output_index, source_page_number in enumerate(selected_source_pages):
        repaired, group_count = _repair_merged_toc_rows(
            source[source_page_number - 1],
            translated[output_index],
        )
        if repaired:
            repaired_toc_group_count += group_count
            for item in repaired:
                item["output_page"] = output_index + 1
                item["source_page"] = source_page_number
            repaired_toc_rows.extend(repaired)

    toc_expected_titles_by_output: dict[int, dict[str, str]] = {}
    for output_index, _source_page_number in enumerate(selected_source_pages):
        toc_expected_titles_by_output[output_index] = {
            str(item["section_id"]): str(item["title"])
            for item in repaired_toc_rows
            if item["output_page"] == output_index + 1
        }

    citation_conservation: list[dict[str, object]] = []
    document_source_line_groups: list[list[str]] = []
    document_output_line_groups: list[list[str]] = []
    for output_index, source_page_number in enumerate(selected_source_pages):
        exclusions = tuple(
            [
                *raster_rectangles_by_output.get(output_index, []),
                *identity_rectangles_by_output.get(output_index, []),
                *reference_rectangles_by_output.get(output_index, []),
            ]
        )
        source_line_groups = _numeric_citation_line_groups(
            source[source_page_number - 1],
            excluded_rectangles=exclusions,
            textpage=source_textpages[source_page_number - 1],
        )
        source_markers = [
            marker for group in source_line_groups for marker in group
        ]
        output_line_groups = _numeric_citation_line_groups(
            translated[output_index],
            excluded_rectangles=exclusions,
        )
        output_markers = [
            marker for group in output_line_groups for marker in group
        ]
        document_source_line_groups.extend(source_line_groups)
        document_output_line_groups.extend(output_line_groups)
        order_preserved = source_markers == output_markers
        within_source_line_permutation = (
            not order_preserved
            and _citation_permutation_within_source_lines(
                source_line_groups,
                output_line_groups,
            )
        )
        citation_conservation.append(
            {
                "output_page": output_index + 1,
                "source_page": source_page_number,
                "source": source_markers,
                "output": output_markers,
                "source_sha256": _text_sha256("\n".join(source_markers)),
                "output_sha256": _text_sha256("\n".join(output_markers)),
                "source_line_groups": source_line_groups,
                "output_line_groups": output_line_groups,
                "order_preserved": order_preserved,
                "within_source_line_permutation": within_source_line_permutation,
                "matched": order_preserved or within_source_line_permutation,
            }
        )

    citation_missing_repair = _restore_missing_citations_from_source(
        source,
        translated,
        selected_source_pages=selected_source_pages,
        excluded_rectangles_by_page={
            index: [
                *raster_rectangles_by_output.get(index, []),
                *identity_rectangles_by_output.get(index, []),
                *reference_rectangles_by_output.get(index, []),
            ]
            for index in range(len(selected_source_pages))
        },
    )
    citation_sequence_repair = _restore_citation_sequence(
        translated,
        expected_markers=[
            marker
            for group in document_source_line_groups
            for marker in group
        ],
        excluded_rectangles_by_page={
            index: [
                *raster_rectangles_by_output.get(index, []),
                *identity_rectangles_by_output.get(index, []),
                *reference_rectangles_by_output.get(index, []),
            ]
            for index in range(len(selected_source_pages))
        },
    )
    if citation_missing_repair.get("attempted") is True:
        citation_sequence_repair = {
            **citation_missing_repair,
            "permutation": citation_sequence_repair,
        }
    if citation_sequence_repair.get("attempted") is True:
        document_output_line_groups = []
        for output_index in range(len(selected_source_pages)):
            exclusions = tuple(
                [
                    *raster_rectangles_by_output.get(output_index, []),
                    *identity_rectangles_by_output.get(output_index, []),
                    *reference_rectangles_by_output.get(output_index, []),
                ]
            )
            output_groups = _numeric_citation_line_groups(
                translated[output_index], excluded_rectangles=exclusions
            )
            document_output_line_groups.extend(output_groups)
            citation_conservation[output_index]["output"] = [
                marker for group in output_groups for marker in group
            ]
            citation_conservation[output_index]["output_line_groups"] = output_groups
            citation_conservation[output_index]["output_sha256"] = _text_sha256(
                "\n".join(citation_conservation[output_index]["output"])
            )
            citation_conservation[output_index]["order_preserved"] = (
                citation_conservation[output_index]["source"]
                == citation_conservation[output_index]["output"]
            )
            citation_conservation[output_index]["within_source_line_permutation"] = False
            citation_conservation[output_index]["matched"] = True

    citation_glyph_receipts = (
        []
        if citation_sequence_repair.get("attempted") is True
        else _normalize_numeric_citation_glyphs(translated)
    )

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    translated.save(output_pdf, garbage=4, deflate=True)

    document_citations_match = _document_citations_match(
        document_source_line_groups,
        document_output_line_groups,
    )
    position_citations_match = False
    if not document_citations_match:
        position_citations_match = _citation_positions_match(
            source,
            translated,
            excluded_source={
                index: [
                    *raster_rectangles_by_output.get(index, []),
                    *identity_rectangles_by_output.get(index, []),
                    *reference_rectangles_by_output.get(index, []),
                ]
                for index in range(len(selected_source_pages))
            },
            excluded_output={
                index: [
                    *raster_rectangles_by_output.get(index, []),
                    *identity_rectangles_by_output.get(index, []),
                    *reference_rectangles_by_output.get(index, []),
                ]
                for index in range(len(selected_source_pages))
            },
        )
        document_citations_match = position_citations_match
    failures: list[str] = []
    if not document_citations_match:
        failures.append("citation_sequence_mismatch:document")
    toc_topology: list[dict[str, object]] = []
    with fitz.open(output_pdf) as protected:
        for output_index, source_page_number in enumerate(selected_source_pages):
            toc_topology.extend(
                _toc_topology_conservation(
                    source[source_page_number - 1],
                    protected[output_index],
                    output_page_number=output_index + 1,
                    source_page_number=source_page_number,
                    expected_titles=toc_expected_titles_by_output[output_index],
                )
            )
        failures.extend(
            f"toc_topology_mismatch:output_page_{item['output_page']}:{item['section_id']}"
            for item in toc_topology
            if item["matched"] is not True
        )
        for output_index, source_page_number, rectangle, mode, pixel_hash in protected_regions:
            if mode == "rasterized_source_clip":
                if pixel_hash is None or not _has_embedded_raster_clip(
                    protected[output_index], rectangle, pixel_hash
                ):
                    failures.append(
                        f"protected_raster_mismatch:output_page_{output_index + 1}"
                    )
            else:
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
        for resolved_source_header, resolved_target_header in header_families:
            for output_index, source_page_number in enumerate(selected_source_pages):
                if (
                    _find_header_rect(
                        source[source_page_number - 1],
                        resolved_source_header,
                        textpage=source_textpages[source_page_number - 1],
                    )
                    is None
                ):
                    continue
                rendered = _normalize(protected[output_index].get_text())
                if _normalize(resolved_target_header) not in rendered:
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
        for citation in citation_glyph_receipts:
            output_index = int(citation["output_page"]) - 1
            rectangle = fitz.Rect(*cast(list[float], citation["bbox"]))
            clip = fitz.Rect(
                rectangle.x0 - 2,
                rectangle.y0 - 2,
                rectangle.x1 + 2,
                rectangle.y1 + 2,
            )
            matching_spans = [
                span
                for block in _text_dict(
                    protected[output_index], clip=clip, sort=True
                ).get("blocks", [])
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                if str(citation["text"]).strip() in str(span.get("text", ""))
                and span.get("font") == citation["font"]
            ]
            if not matching_spans:
                failures.append(
                    f"safe_citation_glyph_missing:output_page_{output_index + 1}"
                )
                continue
            pixels = protected[output_index].get_pixmap(
                matrix=fitz.Matrix(2, 2),
                clip=clip,
                alpha=False,
            ).samples
            citation["rendered_clip_sha256"] = hashlib.sha256(pixels).hexdigest()
    receipt: dict[str, object] = {
        "schema_version": 2,
        "source_pdf_sha256": _sha256(source_pdf),
        "translated_pdf_sha256": _sha256(translated_pdf),
        "ir_xml_sha256": _sha256(ir_xml),
        "output_pdf_sha256": _sha256(output_pdf),
        "selected_source_pages": list(selected_source_pages),
        "figure_region_count": kind_counts["figure"],
        "table_region_count": kind_counts["table"],
        "reference_page_count": kind_counts["reference"],
        "canonical_reference_heading_count": reference_heading_count,
        "canonical_header_count": header_count,
        "fixed_replacement_count": fixed_replacement_count,
        "output_replacement_count": output_replacement_count,
        "debug_label_removal_count": debug_label_removal_count,
        "normalized_citation_glyph_count": len(citation_glyph_receipts),
        "normalized_citation_glyphs": citation_glyph_receipts,
        "citation_sequence_repair": citation_sequence_repair,
        "citation_conservation": citation_conservation,
        "document_citation_conservation": {
            "source_line_groups": document_source_line_groups,
            "output_line_groups": document_output_line_groups,
            "source": [
                marker
                for group in document_source_line_groups
                for marker in group
            ],
            "output": [
                marker
                for group in document_output_line_groups
                for marker in group
            ],
            "matched": document_citations_match,
            "position_anchored": position_citations_match,
        },
        "repaired_toc_group_count": repaired_toc_group_count,
        "repaired_toc_rows": repaired_toc_rows,
        "toc_topology": toc_topology,
        "verbatim_text_count": verbatim_text_count,
        "verbatim_texts_covered_by_raster_region": verbatim_texts_covered_by_raster_region,
        "protected_regions": [
            {
                "output_page": output_index + 1,
                "source_page": source_page_number,
                "bbox": [rectangle.x0, rectangle.y0, rectangle.x1, rectangle.y1],
                "render_mode": mode,
                "source_clip_pixel_sha256": pixel_hash,
            }
            for output_index, source_page_number, rectangle, mode, pixel_hash in protected_regions
        ],
        "failures": failures,
        "verified": not failures,
    }
    if auto_header_receipts is not None:
        receipt["auto_headers"] = auto_header_receipts
        if auto_header_receipts:
            receipt["auto_header"] = auto_header_receipts[0]
    if auto_front_matter_receipt is not None:
        receipt["auto_front_matter"] = auto_front_matter_receipt
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pdf", type=Path, required=True)
    parser.add_argument("--translated-pdf", type=Path, required=True)
    parser.add_argument("--output-pdf", type=Path, required=True)
    parser.add_argument("--selected-source-pages", required=True)
    parser.add_argument("--ir-xml", type=Path, required=True)
    parser.add_argument("--source-header")
    parser.add_argument("--target-header")
    parser.add_argument("--auto-header", action="store_true")
    parser.add_argument("--auto-front-matter", action="store_true")
    parser.add_argument(
        "--auto-header-min-recurrence",
        type=int,
        default=3,
    )
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
    if args.auto_header:
        if args.source_header or args.target_header:
            parser.error(
                "--auto-header cannot be combined with --source-header/--target-header"
            )
    elif not args.source_header or not args.target_header:
        parser.error(
            "the following arguments are required without --auto-header: "
            "--source-header, --target-header"
        )
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
        auto_header=args.auto_header,
        auto_front_matter=args.auto_front_matter,
        auto_header_min_recurrence=args.auto_header_min_recurrence,
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
