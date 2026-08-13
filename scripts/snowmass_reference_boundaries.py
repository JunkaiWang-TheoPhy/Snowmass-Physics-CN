#!/usr/bin/env python3
"""Shared bibliography-boundary detection for translation and PDF refill."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any


REFERENCE_HEADINGS = {"references", "bibliography"}
ACKNOWLEDGMENT_HEADINGS = {"acknowledgments", "acknowledgements"}


def _normalized(text: str) -> str:
    return " ".join(text.split()).rstrip(":").casefold()


def _bibliography_like(text: str) -> bool:
    compact = " ".join(text.split())
    signals = (
        r"\barxiv\s*:",
        r"\bdoi\s*:",
        r"\b(?:19|20)\d{2}\b",
        r"\b(?:journal|proceedings|phys\.\s*rev\.|jhep|nature|science|"
        r"astroph(?:ys)?\.\s*j\.|astron\.\s*j\.|apj|mnras|jcap)\b",
        r"[“\"][^”\"]{8,}[”\"]",
    )
    return sum(bool(re.search(pattern, compact, flags=re.I)) for pattern in signals) >= 2


def reference_boundary(
    article_dir: Path,
    chunks: list[dict[str, Any]],
) -> dict[str, set[str] | set[int]]:
    """Locate the real bibliography and distinguish mixed from verbatim pages."""

    ordered = sorted(chunks, key=lambda item: item.get("order", 0))
    texts = [
        (article_dir / str(chunk["source_file"])).read_text(encoding="utf-8")
        for chunk in ordered
    ]
    heading_indexes = [
        index for index, text in enumerate(texts) if _normalized(text) in REFERENCE_HEADINGS
    ]
    max_page_number = max(
        (int(chunk.get("page_number") or 0) for chunk in ordered),
        default=0,
    )
    start: int | None = None
    heading_index: int | None = None
    for index in reversed(heading_indexes):
        heading_page_number = int(ordered[index].get("page_number") or 0)
        if max_page_number >= 5 and heading_page_number <= 2:
            # Long papers commonly list "References" in an opening table of
            # contents.  It cannot be the terminal bibliography boundary.
            continue
        sample = texts[index + 1 : index + 9]
        first_bibliography_offset = next(
            (offset for offset, text in enumerate(sample) if _bibliography_like(text)),
            None,
        )
        intervening_title = (
            first_bibliography_offset is not None
            and any(
                str(ordered[index + 1 + offset].get("layout_label") or "") == "title"
                for offset in range(first_bibliography_offset)
            )
        )
        if (
            sample
            and not intervening_title
            and sum(_bibliography_like(text) for text in sample) >= min(2, len(sample))
        ):
            start = index + 1
            heading_index = index
            break
        if index == len(ordered) - 1:
            start = len(ordered)
            heading_index = index
            break

    if start is None:
        acknowledgment_indexes = [
            index
            for index, text in enumerate(texts)
            if _normalized(text) in ACKNOWLEDGMENT_HEADINGS
        ]
        if acknowledgment_indexes:
            for index in range(acknowledgment_indexes[-1] + 1, len(ordered)):
                if _bibliography_like(texts[index]):
                    start = index
                    break

    if start is None:
        return {
            "chunk_ids": set(),
            "region_chunk_ids": set(),
            "check_page_numbers": set(),
            "verbatim_page_numbers": set(),
        }

    end = len(ordered)
    for index in range(start, len(ordered)):
        if (
            str(ordered[index].get("layout_label") or "") == "title"
            and not _bibliography_like(texts[index])
        ):
            end = index
            break

    entry_indexes = set(range(start, end))
    region_indexes = set(entry_indexes)
    if heading_index is not None:
        region_indexes.add(heading_index)
    chunk_ids = {str(ordered[index]["id"]) for index in entry_indexes}
    region_chunk_ids = {str(ordered[index]["id"]) for index in region_indexes}
    check_pages = {int(ordered[index]["page_number"]) for index in region_indexes}
    page_chunk_ids: dict[int, set[str]] = {}
    for chunk in ordered:
        page_chunk_ids.setdefault(int(chunk["page_number"]), set()).add(str(chunk["id"]))
    verbatim_pages = {
        page
        for page in check_pages
        if page_chunk_ids.get(page) and page_chunk_ids[page] <= region_chunk_ids
    }
    return {
        "chunk_ids": chunk_ids,
        "region_chunk_ids": region_chunk_ids,
        "check_page_numbers": check_pages,
        "verbatim_page_numbers": verbatim_pages,
    }
