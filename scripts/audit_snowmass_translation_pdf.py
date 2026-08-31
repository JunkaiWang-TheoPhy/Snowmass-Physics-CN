#!/usr/bin/env python3
"""Audit one packaged Snowmass translation PDF and optionally render a contact sheet."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageDraw

try:
    from snowmass_publication_qc import contains_model_meta_response
except ModuleNotFoundError:
    from scripts.snowmass_publication_qc import contains_model_meta_response


RESIDUE_PATTERNS = (
    ("placeholder", re.compile(r"(?:\{v\d+|\[\[SM_)")),
    ("cid", re.compile(r"cid:", re.IGNORECASE)),
    ("replacement_character", re.compile("�")),
)
_LATIN_FRAGMENT_RE = re.compile(r"^[A-Za-z]{1,12}[.,]?$")
_PUNCTUATION_FRAGMENT_RE = re.compile(r"^[.,;:]$")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_MIXED_FRAGMENT_RE = re.compile(
    # A single Latin token beside Chinese is often a unit, acronym, or
    # proper name (for example ``1-MeV-neq 通量``).  Require a two-word
    # English fragment before treating it as leaked prose.
    r"(?:[\u3400-\u9fff]\s+[a-z]{2,}\s+[a-z]{2,}\b|"
    r"\b[a-z]{2,}\s+[a-z]{2,}\s+[\u3400-\u9fff])"
)
_ENGLISH_PROSE_RE = re.compile(
    # Require a genuinely contiguous English clause.  Isolated terminology
    # in Chinese parentheticals (for example ``standard model``) is valid;
    # leaked English sentences such as ``provide compelling science reach``
    # must still fail closed.
    r"\b[a-z]{2,}\b(?:[^A-Za-z\u3400-\u9fff]+[a-z]{2,}\b){3,}"
)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _is_contact_metadata(text: str) -> bool:
    """Do not classify author contact lines as leaked English prose."""
    if not _EMAIL_RE.search(text):
        return False
    remainder = _EMAIL_RE.sub(" ", text)
    return not re.search(r"\b[a-z]{2,}\b(?:\s+[^A-Za-z\u3400-\u9fff]+[a-z]{2,}\b){2,}", remainder, re.I)


def secondary_extractor_identity() -> dict[str, str]:
    """Return the exact Poppler extractor identity used by structural QC."""

    executable = shutil.which("pdftotext")
    if executable is None:
        raise RuntimeError("secondary PDF text extractor is unavailable")
    try:
        result = subprocess.run(
            [executable, "-v"], capture_output=True, text=True, check=False
        )
    except OSError as error:
        raise RuntimeError("secondary PDF text extractor is unavailable") from error
    version_output = (result.stderr or result.stdout).splitlines()
    if result.returncode != 0 or not version_output:
        raise RuntimeError("secondary PDF text extractor version is unavailable")
    return {
        "executable": str(Path(executable).resolve()),
        "version": version_output[0].strip(),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _render_contact_sheet(document: fitz.Document, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    thumbnails: list[Image.Image] = []
    matrix = fitz.Matrix(90 / 72, 90 / 72)
    for page_number, page in enumerate(document, 1):
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        image.thumbnail((180, 255))
        canvas = Image.new("RGB", (190, 280), "white")
        canvas.paste(image, ((190 - image.width) // 2, 18))
        ImageDraw.Draw(canvas).text((6, 4), str(page_number), fill="black")
        thumbnails.append(canvas)
    columns = 6
    rows = (len(thumbnails) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * 190, rows * 280), (225, 225, 225))
    for index, image in enumerate(thumbnails):
        sheet.paste(image, ((index % columns) * 190, (index // columns) * 280))
    sheet.save(output, quality=90)


def _secondary_page_texts(pdf_path: Path) -> list[str]:
    identity = secondary_extractor_identity()
    try:
        result = subprocess.run(
            [identity["executable"], "-layout", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise RuntimeError("secondary PDF text extractor is unavailable") from error
    if result.returncode != 0:
        raise RuntimeError("secondary PDF text extractor failed")
    pages = result.stdout.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def _covered_area(
    rectangles: list[tuple[float, float, float, float]], bounds: fitz.Rect
) -> float:
    clipped = [fitz.Rect(*rectangle) & bounds for rectangle in rectangles]
    clipped = [rectangle for rectangle in clipped if not rectangle.is_empty]
    x_values = sorted({value for rectangle in clipped for value in (rectangle.x0, rectangle.x1)})
    area = 0.0
    for left, right in zip(x_values, x_values[1:]):
        intervals = sorted(
            (rectangle.y0, rectangle.y1)
            for rectangle in clipped
            if rectangle.x0 < right and rectangle.x1 > left
        )
        covered_height = 0.0
        if intervals:
            current_top, current_bottom = intervals[0]
            for top, bottom in intervals[1:]:
                if top <= current_bottom:
                    current_bottom = max(current_bottom, bottom)
                else:
                    covered_height += current_bottom - current_top
                    current_top, current_bottom = top, bottom
            covered_height += current_bottom - current_top
        area += (right - left) * covered_height
    return area


def _has_visible_page_content(page: fitz.Page) -> bool:
    """Distinguish an image/vector-only page from a genuinely blank page.

    Some source papers render References or scanned figures without an
    extractable text layer.  Those pages still require visual review, but
    must not be classified as blank solely because text extraction returns
    zero characters.
    """

    pixmap = page.get_pixmap(matrix=fitz.Matrix(0.25, 0.25), alpha=False)
    samples = pixmap.samples
    if not samples:
        return False
    channels = pixmap.n
    non_white = 0
    pixels = len(samples) // channels
    for offset in range(0, len(samples), channels):
        if min(samples[offset : offset + min(channels, 3)]) < 245:
            non_white += 1
    return pixels > 0 and non_white / pixels >= 0.002


def audit_pdf(
    pdf_path: str | Path,
    *,
    source_pdf: str | Path | None = None,
    expected_pages: int | None = None,
    contact_sheet_path: str | Path | None = None,
    minimum_extractable_characters: int = 5,
    ignored_text_regions: dict[int, list[tuple[float, float, float, float]]]
    | None = None,
    protection_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    pdf_path = Path(pdf_path)
    failures: list[str] = []
    out_of_bounds: list[dict[str, Any]] = []
    residue: list[dict[str, Any]] = []
    low_text_pages: list[int] = []
    isolated_latin_edge_words: list[dict[str, Any]] = []
    mixed_script_bottom_fragments: list[dict[str, Any]] = []
    cross_page_english_fragments: list[dict[str, Any]] = []
    english_prose_residue: list[dict[str, Any]] = []
    secondary_text_layer_excess: list[dict[str, Any]] = []
    secondary_only_latin_tokens: list[dict[str, Any]] = []
    secondary_extractor: dict[str, str] | None = None
    primary_page_text_lengths: list[int] = []
    ignored_text_regions = ignored_text_regions or {}
    source_page_texts: list[str] = []
    source_pdf_sha256: str | None = None
    if source_pdf is not None:
        source_path = Path(source_pdf)
        try:
            with fitz.open(source_path) as source_document:
                source_page_texts = [
                    page.get_text("text") for page in source_document
                ]
            source_pdf_sha256 = _sha256(source_path)
        except (OSError, RuntimeError, ValueError):
            failures.append("unreadable_source_pdf")
    try:
        document = fitz.open(pdf_path)
    except (OSError, RuntimeError, ValueError) as error:
        return {
            "schema_version": 1,
            "pdf_path": pdf_path.name,
            "pdf_sha256": _sha256(pdf_path) if pdf_path.is_file() else None,
            "page_count": None,
            "expected_pages": expected_pages,
            "low_text_pages": [],
            "out_of_bounds": [],
            "residue": [],
            "cross_page_english_fragments": [],
            "english_prose_residue": [],
            "secondary_text_layer_excess": [],
            "contact_sheet_path": None,
            "contact_sheet_sha256": None,
            "protection_receipt_sha256": protection_receipt_sha256,
            "failures": [f"unreadable_pdf:{type(error).__name__}"],
            "ok": False,
        }
    try:
        page_count = len(document)
        if expected_pages is not None and page_count != expected_pages:
            failures.append(f"page_count_mismatch:{page_count}!={expected_pages}")
        for page_number, page in enumerate(document, 1):
            text = page.get_text("text")
            source_words = {
                token.casefold()
                for token in re.findall(
                    r"[A-Za-z]{3,}",
                    source_page_texts[page_number - 1]
                    if page_number <= len(source_page_texts)
                    else "",
                )
            }
            extractable = len(re.sub(r"\s+", "", text))
            primary_page_text_lengths.append(extractable)
            protected_coverage = (
                _covered_area(ignored_text_regions.get(page_number, []), page.rect)
                / page.rect.get_area()
                if page.rect.get_area()
                else 0.0
            )
            if (
                extractable < minimum_extractable_characters
                and protected_coverage < 0.5
                and not _has_visible_page_content(page)
            ):
                low_text_pages.append(page_number)
                failures.append(f"low_text_page:{page_number}")
            if contains_model_meta_response(text):
                failures.append(f"model_meta_response:page_{page_number}")
            for label, pattern in RESIDUE_PATTERNS:
                if pattern.search(text):
                    residue.append({"page": page_number, "kind": label})
                    failures.append(f"residue:{label}:page_{page_number}")
            bounds = page.rect
            blocks = page.get_text("blocks")
            for block in blocks:
                x0, y0, x1, y1, block_text = block[:5]
                prose_text = _URL_RE.sub(" ", str(block_text))
                center_x = (x0 + x1) / 2
                center_y = (y0 + y1) / 2
                ignored = any(
                    left <= center_x <= right and top <= center_y <= bottom
                    for left, top, right, bottom in ignored_text_regions.get(page_number, [])
                )
                if (
                    not ignored
                    and (page_number > 1 or y0 >= bounds.y1 * 0.35)
                    and not _is_contact_metadata(prose_text)
                    and _ENGLISH_PROSE_RE.search(prose_text)
                ):
                    english_prose_residue.append(
                        {
                            "page": page_number,
                            "bbox": [x0, y0, x1, y1],
                            "text": re.sub(r"\s+", " ", str(block_text)).strip()[:240],
                        }
                    )
                    failures.append(f"english_prose_residue:page_{page_number}")
                if (
                    not ignored
                    and y0 >= bounds.y1 * 0.82
                    and _CJK_RE.search(str(block_text))
                    and _MIXED_FRAGMENT_RE.search(str(block_text))
                    and len(re.findall(r"\b[a-z]{2,}\b", str(block_text), re.I)) >= 3
                    and not _URL_RE.search(str(block_text))
                ):
                    mixed_script_bottom_fragments.append(
                        {
                            "page": page_number,
                            "bbox": [x0, y0, x1, y1],
                            "text": re.sub(r"\s+", " ", str(block_text)).strip()[:160],
                        }
                    )
                    failures.append(f"mixed_script_bottom_fragment:page_{page_number}")
                if (
                    not ignored
                    and y0 >= bounds.y1 * 0.78
                    and _CJK_RE.search(str(block_text))
                    and not _URL_RE.search(str(block_text))
                ):
                    short_latin = [
                        token.casefold()
                        for token in re.findall(r"\b[A-Za-z]{3,}\b", str(block_text))
                    ]
                    unknown_fragments = [
                        token for token in short_latin if token not in source_words
                    ]
                    if (
                        source_words
                        and len(unknown_fragments) >= 2
                        and sum(map(len, unknown_fragments)) <= 30
                    ):
                        cross_page_english_fragments.append(
                            {
                                "page": page_number,
                                "bbox": [x0, y0, x1, y1],
                                "tokens": unknown_fragments,
                                "text": re.sub(r"\s+", " ", str(block_text)).strip()[:200],
                            }
                        )
                        failures.append(f"cross_page_english_fragment:page_{page_number}")
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    line_text = "".join(
                        str(span.get("text") or "") for span in line.get("spans", [])
                    ).strip()
                    x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
                    if (
                        (
                            _LATIN_FRAGMENT_RE.fullmatch(line_text)
                            and (
                                y0 >= bounds.y1 * 0.82
                                or (
                                    x0 >= bounds.x1 * 0.80
                                    and y0 >= bounds.y1 * 0.65
                                )
                            )
                            or (
                                _PUNCTUATION_FRAGMENT_RE.fullmatch(line_text)
                                and y0 >= bounds.y1 * 0.82
                            )
                        )
                    ):
                        center_x = (x0 + x1) / 2
                        center_y = (y0 + y1) / 2
                        if any(
                            left <= center_x <= right and top <= center_y <= bottom
                            for left, top, right, bottom in ignored_text_regions.get(
                                page_number, []
                            )
                        ):
                            continue
                        detail = {
                            "page": page_number,
                            "word": line_text,
                            "bbox": [x0, y0, x1, y1],
                        }
                        source_text = (
                            source_page_texts[page_number - 1]
                            if page_number <= len(source_page_texts)
                            else ""
                        )
                        if (
                            line_text.casefold() in source_text.casefold()
                            and line_text.casefold() not in {"the", "and", "for"}
                        ):
                            continue
                        isolated_latin_edge_words.append(detail)
                        failures.append(
                            f"isolated_latin_edge_word:{line_text.casefold()}:page_{page_number}"
                        )
            for block in blocks:
                x0, y0, x1, y1 = block[:4]
                if (
                    x0 < bounds.x0 - 1
                    or y0 < bounds.y0 - 1
                    or x1 > bounds.x1 + 1
                    or y1 > bounds.y1 + 1
                ):
                    detail = {
                        "page": page_number,
                        "bbox": [x0, y0, x1, y1],
                    }
                    out_of_bounds.append(detail)
                    failures.append(f"out_of_bounds_text:page_{page_number}")
        if contact_sheet_path is not None:
            _render_contact_sheet(document, Path(contact_sheet_path))
        try:
            secondary_extractor = secondary_extractor_identity()
            secondary_pages = _secondary_page_texts(pdf_path)
        except RuntimeError as error:
            failures.append(str(error).replace(" ", "_"))
            secondary_pages = []
        if secondary_pages and len(secondary_pages) != page_count:
            failures.append(
                f"secondary_page_count_mismatch:{len(secondary_pages)}!={page_count}"
            )
        for page_number, secondary_text in enumerate(secondary_pages, 1):
            primary_length = primary_page_text_lengths[page_number - 1]
            secondary_length = len(re.sub(r"\s+", "", secondary_text))
            character_surplus = secondary_length - primary_length
            primary_tokens = Counter(
                token.casefold()
                for token in re.findall(r"[A-Za-z]{2,}", document[page_number - 1].get_text())
            )
            secondary_tokens = Counter(
                token.casefold()
                for token in re.findall(r"[A-Za-z]{2,}", secondary_text)
            )
            extra_tokens = list((secondary_tokens - primary_tokens).elements())
            if extra_tokens:
                secondary_only_latin_tokens.append(
                    {
                        "page": page_number,
                        "count": len(extra_tokens),
                        "sample": extra_tokens[:20],
                    }
                )
            if len(extra_tokens) >= 4 and character_surplus >= 15:
                secondary_text_layer_excess.append(
                    {
                        "page": page_number,
                        "primary_characters": primary_length,
                        "secondary_characters": secondary_length,
                        "secondary_character_surplus": character_surplus,
                        "secondary_only_latin_token_count": len(extra_tokens),
                    }
                )
                failures.append(f"secondary_text_layer_excess:page_{page_number}")
    finally:
        document.close()
    return {
        "schema_version": 1,
        "pdf_path": pdf_path.name,
        "pdf_sha256": _sha256(pdf_path),
        "source_pdf_sha256": source_pdf_sha256,
        "page_count": page_count,
        "expected_pages": expected_pages,
        "low_text_pages": low_text_pages,
        "out_of_bounds": out_of_bounds,
        "isolated_latin_edge_words": isolated_latin_edge_words,
        "mixed_script_bottom_fragments": mixed_script_bottom_fragments,
        "cross_page_english_fragments": cross_page_english_fragments,
        "english_prose_residue": english_prose_residue,
        "secondary_text_layer_excess": secondary_text_layer_excess,
        "secondary_only_latin_tokens": secondary_only_latin_tokens,
        "secondary_extractor": secondary_extractor,
        "residue": residue,
        "ignored_text_region_count": sum(map(len, ignored_text_regions.values())),
        "contact_sheet_path": (
            Path(contact_sheet_path).name if contact_sheet_path is not None else None
        ),
        "contact_sheet_sha256": (
            _sha256(Path(contact_sheet_path))
            if contact_sheet_path is not None and Path(contact_sheet_path).is_file()
            else None
        ),
        "protection_receipt_sha256": protection_receipt_sha256,
        "failures": list(dict.fromkeys(failures)),
        "ok": not failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--source-pdf", type=Path)
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--contact-sheet", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--protection-receipt", type=Path)
    arguments = parser.parse_args(argv)
    ignored_text_regions: dict[int, list[tuple[float, float, float, float]]] = {}
    protection_receipt_sha256: str | None = None
    if arguments.protection_receipt is not None:
        protection = json.loads(
            arguments.protection_receipt.read_text(encoding="utf-8")
        )
        if protection.get("verified") is not True:
            raise SystemExit(
                "Protection receipt must be verified before its regions can be ignored"
            )
        protection_receipt_sha256 = _sha256(arguments.protection_receipt)
        for region in protection.get("protected_regions", []):
            page = int(region["output_page"])
            ignored_text_regions.setdefault(page, []).append(
                tuple(map(float, region["bbox"]))
            )
    report = audit_pdf(
        arguments.pdf,
        source_pdf=arguments.source_pdf,
        expected_pages=arguments.expected_pages,
        contact_sheet_path=arguments.contact_sheet,
        ignored_text_regions=ignored_text_regions,
        protection_receipt_sha256=protection_receipt_sha256,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if arguments.report is not None:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
