#!/usr/bin/env python3
"""Audit one packaged Snowmass translation PDF and optionally render a contact sheet."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
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
_LATIN_WORD_RE = re.compile(r"^[A-Za-z]{2,12}$")


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


def audit_pdf(
    pdf_path: str | Path,
    *,
    expected_pages: int | None = None,
    contact_sheet_path: str | Path | None = None,
    minimum_extractable_characters: int = 5,
    ignored_text_regions: dict[int, list[tuple[float, float, float, float]]]
    | None = None,
) -> dict[str, Any]:
    pdf_path = Path(pdf_path)
    failures: list[str] = []
    out_of_bounds: list[dict[str, Any]] = []
    residue: list[dict[str, Any]] = []
    low_text_pages: list[int] = []
    isolated_latin_edge_words: list[dict[str, Any]] = []
    ignored_text_regions = ignored_text_regions or {}
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
            "contact_sheet_path": None,
            "failures": [f"unreadable_pdf:{type(error).__name__}"],
            "ok": False,
        }
    try:
        page_count = len(document)
        if expected_pages is not None and page_count != expected_pages:
            failures.append(f"page_count_mismatch:{page_count}!={expected_pages}")
        for page_number, page in enumerate(document, 1):
            text = page.get_text("text")
            extractable = len(re.sub(r"\s+", "", text))
            if extractable < minimum_extractable_characters:
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
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    line_text = "".join(
                        str(span.get("text") or "") for span in line.get("spans", [])
                    ).strip()
                    x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
                    if (
                        _LATIN_WORD_RE.fullmatch(line_text)
                        and x0 >= bounds.x1 * 0.80
                        and y0 >= bounds.y1 * 0.65
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
    finally:
        document.close()
    return {
        "schema_version": 1,
        "pdf_path": pdf_path.name,
        "pdf_sha256": _sha256(pdf_path),
        "page_count": page_count,
        "expected_pages": expected_pages,
        "low_text_pages": low_text_pages,
        "out_of_bounds": out_of_bounds,
        "isolated_latin_edge_words": isolated_latin_edge_words,
        "residue": residue,
        "ignored_text_region_count": sum(map(len, ignored_text_regions.values())),
        "contact_sheet_path": (
            Path(contact_sheet_path).name if contact_sheet_path is not None else None
        ),
        "failures": list(dict.fromkeys(failures)),
        "ok": not failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--contact-sheet", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--protection-receipt", type=Path)
    arguments = parser.parse_args(argv)
    ignored_text_regions: dict[int, list[tuple[float, float, float, float]]] = {}
    if arguments.protection_receipt is not None:
        protection = json.loads(
            arguments.protection_receipt.read_text(encoding="utf-8")
        )
        if protection.get("verified") is not True:
            raise SystemExit(
                "Protection receipt must be verified before its regions can be ignored"
            )
        for region in protection.get("protected_regions", []):
            page = int(region["output_page"])
            ignored_text_regions.setdefault(page, []).append(
                tuple(map(float, region["bbox"]))
            )
    report = audit_pdf(
        arguments.pdf,
        expected_pages=arguments.expected_pages,
        contact_sheet_path=arguments.contact_sheet,
        ignored_text_regions=ignored_text_regions,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if arguments.report is not None:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
