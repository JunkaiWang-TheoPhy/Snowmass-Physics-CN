#!/usr/bin/env python3
"""Bridge BabelDOC paragraph IR into the translate-book file contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass, replace
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Iterable
import unicodedata


BABELDOC_VERSION = "0.6.4"
IR_PIPELINE_VERSION = 6
# Corrupt/pathological IR guard only. Model-facing structure density is handled
# separately by resumable subrequest segmentation in the translation runner.
MAX_STRUCTURE_COUNT = 512
TRANSLATE_POLICY = "translate"
VERBATIM_FIGURE_TEXT_POLICY = "verbatim_figure_text"
VERBATIM_TABLE_TEXT_POLICY = "verbatim_table_text"
TRANSLATION_POLICIES = frozenset(
    {TRANSLATE_POLICY, VERBATIM_FIGURE_TEXT_POLICY, VERBATIM_TABLE_TEXT_POLICY}
)
_CROSS_PAGE_AUXILIARY_WORDS = frozenset(
    {"am", "are", "be", "been", "being", "can", "could", "did", "do", "does", "had", "has", "have", "is", "may", "might", "must", "shall", "should", "was", "were", "will", "would"}
)


@dataclass(frozen=True)
class DocumentUnit:
    page_number: int
    paragraph_index: int
    layout_label: str
    text: str
    structure_count: int
    translation_policy: str = TRANSLATE_POLICY


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
    figure_text_verbatim_count: int = 0
    table_text_verbatim_count: int = 0
    cross_page_sentence_rebalance_count: int = 0
    same_line_fragment_merge_count: int = 0
    cross_page_line_fragment_carry_count: int = 0


@dataclass(frozen=True)
class FigureRegion:
    page_number: int
    xobj_id: int
    box: tuple[float, float, float, float]


@dataclass(frozen=True)
class TableRegion:
    page_number: int
    layout_id: int
    box: tuple[float, float, float, float]


@dataclass(frozen=True)
class RenderedPdfResult:
    mono_pdf_path: Path
    dual_pdf_path: Path
    verbatim_pages: tuple[int, ...] = ()
    verbatim_verified: bool = True
    reference_numbers: dict[str, Any] | None = None
    canonical_header_occurrences: int = 0
    section_heading_occurrences: int = 0
    figure_regions_verified: bool = True
    figure_region_count: int = 0
    table_regions_verified: bool = True
    table_region_count: int = 0
    reference_regions_verified: bool = True
    reference_region_count: int = 0


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


def materialize_lazy_passthrough_instructions(value: Any) -> Any:
    """Replace BabelDOC's lazy drawing wrappers before XML serialization.

    BabelDOC 0.6.4 handles these wrappers in its JSON encoder but passes them
    unchanged to xsdata's XML serializer.  Complex vector pages can therefore
    fail with ``No converter registered for LazyPassthroughInstruction``.
    Materializing the wrappers preserves the drawing instruction while making
    both serialized IR formats use the same concrete string value.
    """

    seen: set[int] = set()

    def visit(item: Any) -> Any:
        if item is None or isinstance(item, (str, bytes, int, float, bool)):
            return item
        if (
            type(item).__name__ == "LazyPassthroughInstruction"
            and callable(getattr(item, "materialize", None))
        ):
            return str(item.materialize())
        identity = id(item)
        if identity in seen:
            return item
        seen.add(identity)
        if isinstance(item, list):
            for index, child in enumerate(item):
                item[index] = visit(child)
            return item
        if isinstance(item, dict):
            for key, child in list(item.items()):
                item[key] = visit(child)
            return item
        if isinstance(item, tuple):
            return tuple(visit(child) for child in item)
        if is_dataclass(item) and not isinstance(item, type):
            for field in fields(item):
                child = getattr(item, field.name)
                replacement = visit(child)
                if replacement is not child:
                    object.__setattr__(item, field.name, replacement)
        return item

    return visit(value)


def resolve_figure_text_chunk_ids(
    article_dir: Path, manifest: dict[str, Any]
) -> set[str]:
    """Identify figure-owned chunks from policy metadata or legacy JSON IR."""

    chunks = list(manifest.get("chunks", []))
    selected = {
        str(chunk["id"])
        for chunk in chunks
        if chunk.get("translation_policy") == VERBATIM_FIGURE_TEXT_POLICY
    }
    ir_name = manifest.get("babeldoc_ir_json_file")
    if not isinstance(ir_name, str) or not ir_name:
        return selected
    ir_path = Path(article_dir) / ir_name
    if not ir_path.is_file():
        raise RuntimeError(f"BabelDOC JSON IR is missing: {ir_path}")
    document = json.loads(ir_path.read_text(encoding="utf-8"))
    pages = document.get("page")
    if not isinstance(pages, list):
        raise RuntimeError(f"BabelDOC JSON IR has no page list: {ir_path}")
    for chunk in chunks:
        try:
            paragraph = pages[int(chunk["page_number"]) - 1]["pdf_paragraph"][
                int(chunk["paragraph_index"])
            ]
        except (IndexError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"BabelDOC figure-text identity is invalid for {chunk.get('id')}"
            ) from exc
        if int(paragraph.get("xobj_id") or 0) != 0:
            selected.add(str(chunk["id"]))
    return selected


def _json_box(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, dict):
        return None
    box = value.get("box", value)
    if not isinstance(box, dict):
        return None
    try:
        return tuple(float(box[key]) for key in ("x", "y", "x2", "y2"))
    except (KeyError, TypeError, ValueError):
        return None


def _object_box(value: Any) -> tuple[float, float, float, float] | None:
    box = getattr(value, "box", None)
    if box is None:
        return None
    try:
        return (float(box.x), float(box.y), float(box.x2), float(box.y2))
    except (AttributeError, TypeError, ValueError):
        return None


def _box_center_is_inside(
    inner: tuple[float, float, float, float] | None,
    outer: tuple[float, float, float, float],
) -> bool:
    if inner is None:
        return False
    x, y, x2, y2 = inner
    cx, cy = (x + x2) / 2, (y + y2) / 2
    ox, oy, ox2, oy2 = outer
    return ox <= cx <= ox2 and oy <= cy <= oy2


def suppress_cross_page_auxiliary_orphans(
    document: Any,
    translations: Iterable[RefillTranslation],
) -> list[tuple[int, int, str]]:
    """Suppress a source-only auxiliary stranded at a right-edge page break."""

    translated = {
        (item.page_number, item.paragraph_index): item for item in translations
    }
    suppressed: list[tuple[int, int, str]] = []
    pages = list(getattr(document, "page", ()) or ())
    for page_index, page in enumerate(pages[:-1]):
        paragraphs = list(getattr(page, "pdf_paragraph", ()) or ())
        next_paragraphs = list(getattr(pages[page_index + 1], "pdf_paragraph", ()) or ())
        page_box = _object_box(page)
        if page_box is None:
            page_box = _object_box(getattr(page, "mediabox", None))
        if page_box is None or not next_paragraphs:
            continue
        page_width = page_box[2] - page_box[0]
        for paragraph_index, paragraph in enumerate(paragraphs):
            word = str(getattr(paragraph, "unicode", "") or "").strip()
            box = _object_box(paragraph)
            if (
                word.casefold() not in _CROSS_PAGE_AUXILIARY_WORDS
                or box is None
                or int(getattr(paragraph, "xobj_id", 0) or 0) != 0
                or box[0] < page_box[0] + page_width * 0.80
                or (page_index + 1, paragraph_index) in translated
            ):
                continue
            previous_translated = any(
                (page_index + 1, prior_index) in translated
                for prior_index in range(paragraph_index)
            )
            next_source = str(getattr(next_paragraphs[0], "unicode", "") or "").lstrip()
            next_translated = (page_index + 2, 0) in translated
            if not previous_translated or not next_translated or not re.match(r"[a-z]", next_source):
                continue
            paragraph.unicode = ""
            paragraph.pdf_paragraph_composition = []
            suppressed.append((page_index + 1, paragraph_index, word))
    return suppressed


def _page_box(page: Any) -> tuple[float, float, float, float] | None:
    return _object_box(page) or _object_box(getattr(page, "mediabox", None))


def rebalance_cross_page_sentence_fragments(
    document: Any,
    translations: Iterable[RefillTranslation],
) -> tuple[list[RefillTranslation], list[tuple[int, int, int, int]]]:
    """Move an unstructured Chinese sentence tail to its continuation page.

    PDF parsers split logical paragraphs at physical page boundaries.  Keeping
    that English split in Chinese can strand a clause at the bottom of a page.
    This transformation is deliberately limited to adjacent plain-text blocks
    with no BabelDOC placeholders, and preserves the concatenated translation
    byte-for-byte while moving only the final incomplete Chinese sentence.
    """

    adjusted = list(translations)
    by_identity = {
        (item.page_number, item.paragraph_index): index
        for index, item in enumerate(adjusted)
    }
    moved: list[tuple[int, int, int, int]] = []
    pages = list(getattr(document, "page", ()) or ())
    for page_index, page in enumerate(pages[:-1]):
        paragraphs = list(getattr(page, "pdf_paragraph", ()) or ())
        next_paragraphs = list(getattr(pages[page_index + 1], "pdf_paragraph", ()) or ())
        page_box = _page_box(page)
        next_page_box = _page_box(pages[page_index + 1])
        if not paragraphs or not next_paragraphs or page_box is None or next_page_box is None:
            continue
        previous_index = len(paragraphs) - 1
        while previous_index >= 0:
            paragraph = paragraphs[previous_index]
            if (
                str(getattr(paragraph, "layout_label", "") or "").casefold()
                not in {"abandon"}
                and str(getattr(paragraph, "unicode", "") or "").strip()
                and (page_index + 1, previous_index) in by_identity
            ):
                break
            previous_index -= 1
        if previous_index < 0:
            continue
        next_index = 0
        while next_index < len(next_paragraphs):
            paragraph = next_paragraphs[next_index]
            if (
                str(getattr(paragraph, "layout_label", "") or "").casefold()
                not in {"abandon"}
                and str(getattr(paragraph, "unicode", "") or "").strip()
                and (page_index + 2, next_index) in by_identity
            ):
                break
            next_index += 1
        if next_index >= len(next_paragraphs):
            continue
        previous = paragraphs[previous_index]
        following = next_paragraphs[next_index]
        previous_box = _object_box(previous)
        following_box = _object_box(following)
        if previous_box is None or following_box is None:
            continue
        page_height = page_box[3] - page_box[1]
        next_page_height = next_page_box[3] - next_page_box[1]
        if (
            previous_box[1] > page_box[1] + page_height * 0.25
            or following_box[3] < next_page_box[1] + next_page_height * 0.70
            or int(getattr(previous, "xobj_id", 0) or 0) != 0
            or int(getattr(following, "xobj_id", 0) or 0) != 0
            or str(getattr(previous, "layout_label", "") or "").casefold()
            != "plain text"
            or str(getattr(following, "layout_label", "") or "").casefold()
            != "plain text"
        ):
            continue
        previous_source = str(getattr(previous, "unicode", "") or "").strip()
        following_source = str(getattr(following, "unicode", "") or "").lstrip()
        if (
            not previous_source
            or not following_source
            or re.search(r"[.!?][\"')\]]*$", previous_source)
            or not re.match(r"[a-z]", following_source)
        ):
            continue
        previous_identity = (page_index + 1, previous_index)
        following_identity = (page_index + 2, next_index)
        if previous_identity not in by_identity or following_identity not in by_identity:
            continue
        previous_item = adjusted[by_identity[previous_identity]]
        following_item = adjusted[by_identity[following_identity]]
        combined_contract = "".join(
            (
                previous_item.source_text,
                following_item.source_text,
                previous_item.translated_text,
                following_item.translated_text,
            )
        )
        if _BABELDOC_PLACEHOLDER.search(combined_contract):
            continue
        boundaries = list(re.finditer(r"[。！？]", previous_item.translated_text))
        if not boundaries:
            continue
        split_at = boundaries[-1].end()
        prefix = previous_item.translated_text[:split_at]
        tail = previous_item.translated_text[split_at:]
        if not prefix or not tail.strip():
            continue
        adjusted[by_identity[previous_identity]] = replace(
            previous_item, translated_text=prefix
        )
        adjusted[by_identity[following_identity]] = replace(
            following_item,
            translated_text=tail + following_item.translated_text,
        )
        moved.append((*previous_identity, *following_identity))
    return adjusted, moved


def coalesce_same_baseline_line_fragments(
    document: Any,
) -> list[tuple[int, int, int]]:
    """Join parser-split fallback-line prefixes to the following line body."""

    merged: list[tuple[int, int, int]] = []
    for page_number, page in enumerate(getattr(document, "page", ()) or (), 1):
        paragraphs = list(getattr(page, "pdf_paragraph", ()) or ())
        for left_index, left in enumerate(paragraphs[:-1]):
            right_index = left_index + 1
            right = paragraphs[right_index]
            left_box = _object_box(left)
            right_box = _object_box(right)
            left_compositions = list(
                getattr(left, "pdf_paragraph_composition", ()) or ()
            )
            right_compositions = list(
                getattr(right, "pdf_paragraph_composition", ()) or ()
            )
            if (
                str(getattr(left, "layout_label", "") or "").casefold()
                != "fallback_line"
                or int(getattr(left, "xobj_id", 0) or 0) != 0
                or int(getattr(right, "xobj_id", 0) or 0) != 0
                or left_box is None
                or right_box is None
                or not left_compositions
                or not right_compositions
            ):
                continue
            left_height = left_box[3] - left_box[1]
            right_height = right_box[3] - right_box[1]
            vertical_overlap = min(left_box[3], right_box[3]) - max(
                left_box[1], right_box[1]
            )
            gap = right_box[0] - left_box[2]
            if (
                max(left_height, right_height) > 24
                or vertical_overlap < min(left_height, right_height) * 0.55
                or gap < -1
                or gap > 16
            ):
                continue
            left_text = str(getattr(left, "unicode", "") or "").rstrip()
            right_text = str(getattr(right, "unicode", "") or "").lstrip()
            separator = " " if left_text and right_text else ""
            if separator:
                for composition in reversed(left_compositions):
                    unicode_run = getattr(
                        composition, "pdf_same_style_unicode_characters", None
                    )
                    if unicode_run is None:
                        continue
                    run_text = str(getattr(unicode_run, "unicode", "") or "")
                    if run_text:
                        unicode_run.unicode = run_text.rstrip() + " "
                        break
            left.unicode = left_text + separator + right_text
            left.box.x = min(left_box[0], right_box[0])
            left.box.y = min(left_box[1], right_box[1])
            left.box.x2 = max(left_box[2], right_box[2])
            left.box.y2 = max(left_box[3], right_box[3])
            left.pdf_paragraph_composition = left_compositions + right_compositions
            right.unicode = ""
            right.pdf_paragraph_composition = []
            merged.append((page_number, left_index, right_index))
    return merged


def _is_table_owned_paragraph(page: Any, paragraph: Any) -> bool:
    return any(
        _box_center_is_inside(_object_box(paragraph), region_box)
        for _layout_id, region_box in _object_table_regions(page)
    )


def identify_cross_page_line_fragment_carries(
    document: Any,
) -> list[tuple[int, int, int, int]]:
    """Find short source-line chains that continue on the following page."""

    carries: list[tuple[int, int, int, int]] = []
    pages = list(getattr(document, "page", ()) or ())
    for page_index, page in enumerate(pages[:-1]):
        next_page = pages[page_index + 1]
        page_box = _page_box(page)
        next_page_box = _page_box(next_page)
        paragraphs = list(getattr(page, "pdf_paragraph", ()) or ())
        next_paragraphs = list(getattr(next_page, "pdf_paragraph", ()) or ())
        if page_box is None or next_page_box is None or not paragraphs or not next_paragraphs:
            continue

        eligible = [
            index
            for index, paragraph in enumerate(paragraphs)
            if str(getattr(paragraph, "layout_label", "") or "").casefold()
            in {"plain text", "fallback_line"}
            and int(getattr(paragraph, "xobj_id", 0) or 0) == 0
            and _object_box(paragraph) is not None
            and str(getattr(paragraph, "unicode", "") or "").strip()
            and not _is_table_owned_paragraph(page, paragraph)
        ]
        next_eligible = [
            index
            for index, paragraph in enumerate(next_paragraphs)
            if str(getattr(paragraph, "layout_label", "") or "").casefold()
            in {"plain text", "fallback_line"}
            and int(getattr(paragraph, "xobj_id", 0) or 0) == 0
            and _object_box(paragraph) is not None
            and str(getattr(paragraph, "unicode", "") or "").strip()
            and not _is_table_owned_paragraph(next_page, paragraph)
        ]
        if not eligible or not next_eligible:
            continue
        last_index = eligible[-1]
        last_box = _object_box(paragraphs[last_index])
        assert last_box is not None
        if last_box[3] - last_box[1] > 24:
            continue
        chain = [last_index]
        for candidate_index in reversed(eligible[:-1]):
            candidate_box = _object_box(paragraphs[candidate_index])
            if candidate_box is None or candidate_box[3] - candidate_box[1] > 24:
                break
            overlap = min(candidate_box[3], last_box[3]) - max(
                candidate_box[1], last_box[1]
            )
            if overlap < min(
                candidate_box[3] - candidate_box[1], last_box[3] - last_box[1]
            ) * 0.45:
                break
            chain.append(candidate_index)
        chain.sort()
        anchor_index = chain[0]
        anchor_box = _object_box(paragraphs[anchor_index])
        next_index = next_eligible[0]
        following_box = _object_box(next_paragraphs[next_index])
        assert anchor_box is not None and following_box is not None
        page_height = page_box[3] - page_box[1]
        next_height = next_page_box[3] - next_page_box[1]
        source = " ".join(
            str(getattr(paragraphs[index], "unicode", "") or "").strip()
            for index in chain
        ).strip()
        following_source = str(
            getattr(next_paragraphs[next_index], "unicode", "") or ""
        ).lstrip()
        if (
            anchor_box[1] > page_box[1] + page_height * 0.20
            or following_box[3] < next_page_box[1] + next_height * 0.70
            or not source
            or re.search(r"[.!?][\"')\]]*$", source)
            or not re.match(r"[a-z]", following_source)
        ):
            continue
        carries.append((page_index + 1, anchor_index, page_index + 2, next_index))
    return carries


def apply_cross_page_line_fragment_carries(
    document: Any,
    carries: Iterable[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Move translated compositions for a short trailing line to the next page."""

    applied: list[tuple[int, int, int, int]] = []
    pages = list(getattr(document, "page", ()) or ())
    superscripts = str.maketrans("−-+0123456789", "⁻⁻⁺⁰¹²³⁴⁵⁶⁷⁸⁹")

    for page_number, paragraph_index, next_page_number, next_paragraph_index in carries:
        source_page = pages[page_number - 1]
        destination_page = pages[next_page_number - 1]
        trailing = source_page.pdf_paragraph[paragraph_index]
        following = destination_page.pdf_paragraph[next_paragraph_index]
        trailing_compositions = list(
            getattr(trailing, "pdf_paragraph_composition", ()) or ()
        )
        following_compositions = list(
            getattr(following, "pdf_paragraph_composition", ()) or ()
        )
        if not trailing_compositions or not following_compositions:
            continue
        portable_trailing: list[Any] = []
        trailing_text = str(getattr(trailing, "unicode", "") or "").rstrip()
        # A carry is an optional layout optimization.  If any formula in the
        # trailing line is not a plain numeric superscript, leave the original
        # page split intact instead of failing the whole document refill.
        carry_is_portable = True
        for composition in trailing_compositions:
            formula = getattr(composition, "pdf_formula", None)
            if formula is None:
                portable_trailing.append(composition)
                continue
            formula_characters = list(getattr(formula, "pdf_character", ()) or ())
            formula_text = "".join(
                str(getattr(character, "char_unicode", "") or "")
                for character in formula_characters
            )
            if (
                not formula_text
                or any(character not in "−-+0123456789" for character in formula_text)
                or (getattr(formula, "pdf_curve", None) or getattr(formula, "pdf_form", None))
                or not _BABELDOC_PLACEHOLDER.search(trailing_text)
                or not any(
                    str(
                        getattr(
                            getattr(prior, "pdf_same_style_unicode_characters", None),
                            "unicode",
                            "",
                        )
                        or ""
                    ).strip()
                    for prior in portable_trailing
                )
            ):
                carry_is_portable = False
                break
            portable_trailing.append(composition)
        if not carry_is_portable:
            continue
        portable_trailing = []
        for composition in trailing_compositions:
            formula = getattr(composition, "pdf_formula", None)
            if formula is None:
                portable_trailing.append(composition)
                continue
            formula_characters = list(getattr(formula, "pdf_character", ()) or ())
            formula_text = "".join(
                str(getattr(character, "char_unicode", "") or "")
                for character in formula_characters
            )
            if (
                not formula_text
                or any(character not in "−-+0123456789" for character in formula_text)
                or (getattr(formula, "pdf_curve", None) or getattr(formula, "pdf_form", None))
            ):
                raise AssertionError("portable carry preflight was incomplete")
            superscript = formula_text.translate(superscripts)
            for prior in reversed(portable_trailing):
                unicode_run = getattr(
                    prior, "pdf_same_style_unicode_characters", None
                )
                if unicode_run is None:
                    continue
                run_text = str(getattr(unicode_run, "unicode", "") or "")
                if run_text:
                    unicode_run.unicode = run_text.rstrip() + superscript
                    break
            else:
                raise AssertionError("portable carry preflight missed adjacent text")
            trailing_text, replacement_count = _BABELDOC_PLACEHOLDER.subn(
                superscript, trailing_text, count=1
            )
            if replacement_count != 1:
                raise AssertionError("portable carry preflight missed placeholder")
        trailing_compositions = portable_trailing
        for composition in following_compositions:
            unicode_run = getattr(
                composition, "pdf_same_style_unicode_characters", None
            )
            if unicode_run is None:
                continue
            run_text = str(getattr(unicode_run, "unicode", "") or "")
            if run_text:
                unicode_run.unicode = " " + run_text.lstrip()
                break
        following_text = str(getattr(following, "unicode", "") or "").lstrip()
        following.unicode = trailing_text + " " + following_text
        following.pdf_paragraph_composition = (
            trailing_compositions + following_compositions
        )
        trailing.unicode = ""
        trailing.pdf_paragraph_composition = []
        applied.append(
            (page_number, paragraph_index, next_page_number, next_paragraph_index)
        )
    return applied


def _json_table_regions(page: dict[str, Any]) -> list[tuple[int, tuple[float, float, float, float]]]:
    regions: list[tuple[int, tuple[float, float, float, float]]] = []
    for layout in page.get("page_layout") or []:
        if str(layout.get("class_name", "")).casefold() != "table":
            continue
        box = _json_box(layout)
        if box is not None:
            regions.append((int(layout.get("id") or 0), box))
    return regions


def _object_table_regions(page: Any) -> list[tuple[int, tuple[float, float, float, float]]]:
    regions: list[tuple[int, tuple[float, float, float, float]]] = []
    for layout in page.page_layout or []:
        if str(layout.class_name or "").casefold() != "table":
            continue
        box = _object_box(layout)
        if box is not None:
            regions.append((int(layout.id or 0), box))
    return regions


def resolve_table_text_chunk_ids(
    article_dir: Path, manifest: dict[str, Any]
) -> set[str]:
    """Identify table-body chunks from policy metadata or page-layout geometry."""

    chunks = list(manifest.get("chunks", []))
    selected = {
        str(chunk["id"])
        for chunk in chunks
        if chunk.get("translation_policy") == VERBATIM_TABLE_TEXT_POLICY
    }
    ir_name = manifest.get("babeldoc_ir_json_file")
    if not isinstance(ir_name, str) or not ir_name:
        return selected
    ir_path = Path(article_dir) / ir_name
    if not ir_path.is_file():
        raise RuntimeError(f"BabelDOC JSON IR is missing: {ir_path}")
    document = json.loads(ir_path.read_text(encoding="utf-8"))
    pages = document.get("page")
    if not isinstance(pages, list):
        raise RuntimeError(f"BabelDOC JSON IR has no page list: {ir_path}")
    for chunk in chunks:
        try:
            page = pages[int(chunk["page_number"]) - 1]
            paragraph = page["pdf_paragraph"][int(chunk["paragraph_index"])]
        except (IndexError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"BabelDOC table-text identity is invalid for {chunk.get('id')}"
            ) from exc
        paragraph_box = _json_box(paragraph)
        if any(
            _box_center_is_inside(paragraph_box, region_box)
            for _layout_id, region_box in _json_table_regions(page)
        ):
            selected.add(str(chunk["id"]))
    return selected


def resolve_reference_regions(
    article_dir: Path,
    manifest: dict[str, Any],
    reference_chunk_ids: set[str],
) -> list[TableRegion]:
    """Resolve bibliography paragraph boxes for exact source-region restoration."""

    if not reference_chunk_ids:
        return []
    ir_name = manifest.get("babeldoc_ir_json_file")
    if not isinstance(ir_name, str) or not ir_name:
        raise RuntimeError("BabelDOC JSON IR is required for reference restoration")
    ir_path = Path(article_dir) / ir_name
    document = json.loads(ir_path.read_text(encoding="utf-8"))
    pages = document.get("page")
    if not isinstance(pages, list):
        raise RuntimeError(f"BabelDOC JSON IR has no page list: {ir_path}")
    regions: list[TableRegion] = []
    for chunk in manifest.get("chunks", []):
        if str(chunk.get("id")) not in reference_chunk_ids:
            continue
        page_number = int(chunk["page_number"])
        paragraph_index = int(chunk["paragraph_index"])
        try:
            paragraph = pages[page_number - 1]["pdf_paragraph"][paragraph_index]
        except (IndexError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"BabelDOC reference identity is invalid for {chunk.get('id')}"
            ) from exc
        box = _json_box(paragraph)
        if box is None:
            raise RuntimeError(
                f"BabelDOC reference paragraph has no box: {chunk.get('id')}"
            )
        x, y, x2, y2 = box
        regions.append(
            TableRegion(
                page_number,
                paragraph_index,
                (x - 12.0, y - 3.0, x2 + 12.0, y2 + 3.0),
            )
        )
    if len(regions) != len(reference_chunk_ids):
        raise RuntimeError("Not every reference chunk resolved to a BabelDOC region")
    return regions


def _normalized_page_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalized_figure_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def reference_entry_numbers(text: str) -> list[int]:
    """Return bibliography entry numbers after the last section heading."""

    headings = list(re.finditer(r"(?im)^\s*(?:references|bibliography)\s*$", text))
    body = text[headings[-1].end() :] if headings else text
    return [int(value) for value in re.findall(r"(?m)^\s*\[(\d+)\]", body)]


def figure_regions_from_document(document: Any) -> list[FigureRegion]:
    """Collect XObject bounds that own translatable figure text paragraphs."""

    regions: list[FigureRegion] = []
    for page_number, page in enumerate(document.page, 1):
        text_xobj_ids = {
            int(paragraph.xobj_id or 0)
            for paragraph in page.pdf_paragraph
            if int(paragraph.xobj_id or 0) != 0
        }
        xobjects = {int(xobj.xobj_id): xobj for xobj in page.pdf_xobject}
        for xobj_id in sorted(text_xobj_ids):
            xobj = xobjects.get(xobj_id)
            if xobj is None or xobj.box is None:
                raise RuntimeError(
                    f"Figure text XObject has no bounding box: {page_number}/{xobj_id}"
                )
            regions.append(
                FigureRegion(
                    page_number=page_number,
                    xobj_id=xobj_id,
                    box=(
                        float(xobj.box.x),
                        float(xobj.box.y),
                        float(xobj.box.x2),
                        float(xobj.box.y2),
                    ),
                )
            )
    return regions


def table_regions_from_document(document: Any) -> list[TableRegion]:
    """Collect high-level table bounds while excluding captions below the grid."""

    regions: list[TableRegion] = []
    for page_number, page in enumerate(document.page, 1):
        for layout_id, box in _object_table_regions(page):
            regions.append(TableRegion(page_number, layout_id, box))
    return regions


def restore_verbatim_regions(
    *,
    source_pdf: Path,
    mono_pdf: Path,
    dual_pdf: Path,
    figure_regions: Iterable[FigureRegion],
    table_regions: Iterable[TableRegion],
) -> dict[str, Any]:
    """Replace re-typeset figure/table areas with pristine source vectors.

    BabelDOC can reconstruct text inside an XObject even when that paragraph
    bypasses every model stage.  Redacting the reconstructed target area before
    inserting the clipped source page prevents both visual drift and hidden,
    duplicate extractable text.  Captions remain outside the IR-owned regions.
    """

    import pymupdf

    figures = list(figure_regions)
    tables = list(table_regions)
    if not figures and not tables:
        return {
            "verified": True,
            "figure_region_count": 0,
            "table_region_count": 0,
        }

    source_pdf = Path(source_pdf)
    mono_pdf = Path(mono_pdf)
    dual_pdf = Path(dual_pdf)
    mono_temporary = mono_pdf.with_name(mono_pdf.stem + ".regions.tmp.pdf")
    dual_temporary = dual_pdf.with_name(dual_pdf.stem + ".regions.tmp.pdf")

    boxes_by_page: dict[int, list[tuple[float, float, float, float]]] = {}
    for region in [*figures, *tables]:
        boxes_by_page.setdefault(region.page_number, []).append(region.box)

    def coalesce_clips(clips: Iterable[Any]) -> list[Any]:
        """Merge near-identical IR regions so source text is inserted only once."""

        merged: list[Any] = []
        for clip in clips:
            matched = False
            for index, existing in enumerate(merged):
                intersection = clip & existing
                smaller_area = min(float(clip.get_area()), float(existing.get_area()))
                if smaller_area and float(intersection.get_area()) / smaller_area >= 0.98:
                    merged[index] = clip | existing
                    matched = True
                    break
            if not matched:
                merged.append(clip)
        return merged

    try:
        with pymupdf.open(source_pdf) as source, pymupdf.open(mono_pdf) as mono, pymupdf.open(
            dual_pdf
        ) as dual:
            if mono.page_count != source.page_count or dual.page_count != source.page_count:
                raise RuntimeError("Cannot restore verbatim regions across unequal page counts")

            clips_by_page: dict[int, list[Any]] = {}
            for page_number, boxes in boxes_by_page.items():
                if page_number < 1 or page_number > source.page_count:
                    raise RuntimeError(
                        f"Verbatim region page is outside the source PDF: {page_number}"
                    )
                source_page = source[page_number - 1]
                mono_page = mono[page_number - 1]
                dual_page = dual[page_number - 1]
                if (
                    abs(source_page.rect.width - mono_page.rect.width) > 0.5
                    or abs(source_page.rect.height - mono_page.rect.height) > 0.5
                    or abs(dual_page.rect.width - 2 * source_page.rect.width) > 1.0
                    or abs(dual_page.rect.height - source_page.rect.height) > 0.5
                ):
                    raise RuntimeError(
                        f"Cannot map verbatim regions onto page {page_number} dimensions"
                    )

                unique: dict[tuple[float, float, float, float], Any] = {}
                for x, y, x2, y2 in boxes:
                    clip = pymupdf.Rect(
                        x,
                        source_page.rect.height - y2,
                        x2,
                        source_page.rect.height - y,
                    ) & source_page.rect
                    if clip.is_empty or clip.is_infinite:
                        raise RuntimeError(
                            f"Verbatim region is empty on page {page_number}: {(x, y, x2, y2)}"
                        )
                    key = tuple(round(float(value), 4) for value in clip)
                    unique[key] = clip
                clips_by_page[page_number] = coalesce_clips(unique.values())

            # Apply all redactions before inserting source clips.  Otherwise an
            # overlapping later redaction could erase a source clip already added.
            for page_number, clips in clips_by_page.items():
                mono_page = mono[page_number - 1]
                dual_page = dual[page_number - 1]
                midpoint = dual_page.rect.width / 2
                for clip in clips:
                    mono_page.add_redact_annot(clip, fill=(1, 1, 1))
                    dual_page.add_redact_annot(
                        pymupdf.Rect(
                            clip.x0 + midpoint,
                            clip.y0,
                            clip.x1 + midpoint,
                            clip.y1,
                        ),
                        fill=(1, 1, 1),
                    )
                mono_page.apply_redactions()
                dual_page.apply_redactions()

            for page_number, clips in clips_by_page.items():
                page_index = page_number - 1
                mono_page = mono[page_index]
                dual_page = dual[page_index]
                midpoint = dual_page.rect.width / 2
                for clip in clips:
                    mono_page.show_pdf_page(
                        clip,
                        source,
                        page_index,
                        clip=clip,
                        keep_proportion=False,
                        overlay=True,
                    )
                    dual_page.show_pdf_page(
                        pymupdf.Rect(
                            clip.x0 + midpoint,
                            clip.y0,
                            clip.x1 + midpoint,
                            clip.y1,
                        ),
                        source,
                        page_index,
                        clip=clip,
                        keep_proportion=False,
                        overlay=True,
                    )

            mono.save(mono_temporary, garbage=4, deflate=True)
            dual.save(dual_temporary, garbage=4, deflate=True)
        os.replace(mono_temporary, mono_pdf)
        os.replace(dual_temporary, dual_pdf)
    finally:
        mono_temporary.unlink(missing_ok=True)
        dual_temporary.unlink(missing_ok=True)

    with pymupdf.open(source_pdf) as source, pymupdf.open(mono_pdf) as mono, pymupdf.open(
        dual_pdf
    ) as dual:
        for page_number, boxes in boxes_by_page.items():
            source_page = source[page_number - 1]
            mono_page = mono[page_number - 1]
            dual_page = dual[page_number - 1]
            midpoint = dual_page.rect.width / 2
            source_clips = coalesce_clips(
                pymupdf.Rect(
                    x,
                    source_page.rect.height - y2,
                    x2,
                    source_page.rect.height - y,
                )
                & source_page.rect
                for x, y, x2, y2 in boxes
            )
            for source_clip in source_clips:
                dual_clip = pymupdf.Rect(
                    source_clip.x0 + midpoint,
                    source_clip.y0,
                    source_clip.x1 + midpoint,
                    source_clip.y1,
                )
                expected = _normalized_figure_text(source_page.get_text(clip=source_clip))
                mono_text = _normalized_figure_text(mono_page.get_text(clip=source_clip))
                dual_text = _normalized_figure_text(dual_page.get_text(clip=dual_clip))
                if not expected or mono_text != expected or dual_text != expected:
                    raise RuntimeError(
                        f"Verbatim region restoration self-check failed: page={page_number}"
                    )

    return {
        "verified": True,
        "figure_region_count": len(figures),
        "table_region_count": len(tables),
    }


def restore_verbatim_reference_regions(
    *,
    source_pdf: Path,
    mono_pdf: Path,
    dual_pdf: Path,
    regions: Iterable[TableRegion],
) -> dict[str, Any]:
    """Restore bibliography clips with exact pixels and a source-faithful text layer."""

    import pymupdf

    checked = list(regions)
    if not checked:
        return {"verified": True, "region_count": 0}
    mono_temporary = Path(mono_pdf).with_name(Path(mono_pdf).stem + ".refs.tmp.pdf")
    dual_temporary = Path(dual_pdf).with_name(Path(dual_pdf).stem + ".refs.tmp.pdf")
    try:
        with pymupdf.open(source_pdf) as source, pymupdf.open(mono_pdf) as mono, pymupdf.open(
            dual_pdf
        ) as dual:
            if mono.page_count != source.page_count or dual.page_count != source.page_count:
                raise RuntimeError("Cannot restore reference regions across unequal page counts")
            prepared_regions: list[tuple[int, Any, Any, bytes, str]] = []
            for region in checked:
                if region.page_number < 1 or region.page_number > source.page_count:
                    raise RuntimeError(
                        f"Reference region page is outside the source PDF: {region.page_number}"
                    )
                page_index = region.page_number - 1
                source_page = source[page_index]
                mono_page = mono[page_index]
                dual_page = dual[page_index]
                if (
                    abs(source_page.rect.width - mono_page.rect.width) > 0.5
                    or abs(source_page.rect.height - mono_page.rect.height) > 0.5
                    or abs(dual_page.rect.width - 2 * source_page.rect.width) > 1.0
                    or abs(dual_page.rect.height - source_page.rect.height) > 0.5
                ):
                    raise RuntimeError(
                        f"Cannot map reference regions onto page {region.page_number} dimensions"
                    )
                x, y, x2, y2 = region.box
                clip = pymupdf.Rect(
                    x, source_page.rect.height - y2, x2, source_page.rect.height - y
                ) & source_page.rect
                if clip.is_empty or clip.is_infinite:
                    raise RuntimeError(
                        f"Reference region is empty on page {region.page_number}"
                    )
                pixmap = source_page.get_pixmap(
                    matrix=pymupdf.Matrix(3, 3), clip=clip, alpha=False
                )
                image_bytes = pixmap.tobytes("png")
                midpoint = dual_page.rect.width / 2
                dual_clip = pymupdf.Rect(
                    clip.x0 + midpoint,
                    clip.y0,
                    clip.x1 + midpoint,
                    clip.y1,
                )
                source_text = source_page.get_text(clip=clip, sort=True).strip()
                if not source_text:
                    raise RuntimeError(
                        f"Reference region has no source text on page {region.page_number}"
                    )
                prepared_regions.append(
                    (page_index, clip, dual_clip, image_bytes, source_text)
                )

            affected_pages = {page_index for page_index, *_rest in prepared_regions}
            for page_index, clip, dual_clip, _image_bytes, _source_text in prepared_regions:
                mono[page_index].add_redact_annot(clip, fill=(1, 1, 1))
                dual[page_index].add_redact_annot(dual_clip, fill=(1, 1, 1))
            for page_index in affected_pages:
                mono[page_index].apply_redactions()
                dual[page_index].apply_redactions()

            for page_index, clip, dual_clip, image_bytes, source_text in prepared_regions:
                mono_page = mono[page_index]
                dual_page = dual[page_index]
                for target_page, target_clip in (
                    (mono_page, clip),
                    (dual_page, dual_clip),
                ):
                    target_page.show_pdf_page(
                        target_clip,
                        source,
                        page_index,
                        clip=clip,
                        keep_proportion=False,
                        overlay=True,
                    )
                    target_page.insert_image(target_clip, stream=image_bytes, overlay=True)
            mono.save(mono_temporary, garbage=4, deflate=True)
            dual.save(dual_temporary, garbage=4, deflate=True)
        os.replace(mono_temporary, mono_pdf)
        os.replace(dual_temporary, dual_pdf)
    finally:
        mono_temporary.unlink(missing_ok=True)
        dual_temporary.unlink(missing_ok=True)

    def has_expected_image(page: Any, clip: Any, digest: bytes) -> bool:
        for image_info in page.get_image_info(hashes=True, xrefs=True):
            image_box = pymupdf.Rect(image_info["bbox"])
            if (
                image_info.get("digest") == digest
                # The digest is the authoritative pixel check.  The source
                # paragraph box intentionally has horizontal/vertical safety
                # padding, so the extracted image bbox need not coincide with
                # every edge of that box.  Require containment instead of an
                # overly strict edge-distance match.
                and image_box.intersects(clip)
                and image_box.x0 >= clip.x0 - 0.5
                and image_box.y0 >= clip.y0 - 0.5
                and image_box.x1 <= clip.x1 + 0.5
                and image_box.y1 <= clip.y1 + 0.5
            ):
                return True
        return False

    with pymupdf.open(source_pdf) as source, pymupdf.open(mono_pdf) as mono, pymupdf.open(
        dual_pdf
    ) as dual:
        for region in checked:
            page_index = region.page_number - 1
            source_page = source[page_index]
            mono_page = mono[page_index]
            dual_page = dual[page_index]
            x, y, x2, y2 = region.box
            source_clip = pymupdf.Rect(
                x, source_page.rect.height - y2, x2, source_page.rect.height - y
            ) & source_page.rect
            dual_clip = pymupdf.Rect(
                source_clip.x0 + source_page.rect.width,
                source_clip.y0,
                source_clip.x1 + source_page.rect.width,
                source_clip.y1,
            )
            expected_digest = source_page.get_pixmap(
                matrix=pymupdf.Matrix(3, 3), clip=source_clip, alpha=False
            ).digest
            expected_text = _normalized_page_text(
                source_page.get_text(clip=source_clip, sort=True)
            )
            mono_text = _normalized_page_text(
                mono_page.get_text(clip=source_clip, sort=True)
            )
            dual_text = _normalized_page_text(
                dual_page.get_text(clip=dual_clip, sort=True)
            )
            if (
                not has_expected_image(mono_page, source_clip, expected_digest)
                or not has_expected_image(dual_page, dual_clip, expected_digest)
                or not expected_text
                or mono_text != expected_text
                or dual_text != expected_text
            ):
                raise RuntimeError(
                    f"Reference region restoration self-check failed: page={region.page_number}"
                )
    return {"verified": True, "region_count": len(checked)}


def verify_verbatim_figure_regions(
    *,
    source_pdf: Path,
    mono_pdf: Path,
    regions: Iterable[FigureRegion],
) -> dict[str, Any]:
    """Compare source and rendered text inside every figure-owned XObject bound."""

    import pymupdf

    checked = list(regions)
    with pymupdf.open(source_pdf) as source, pymupdf.open(mono_pdf) as mono:
        if source.page_count != mono.page_count:
            raise RuntimeError("Cannot verify figure regions across unequal page counts")
        for region in checked:
            if region.page_number < 1 or region.page_number > source.page_count:
                raise RuntimeError(
                    f"Figure region page is outside the source PDF: {region.page_number}"
                )
            source_page = source[region.page_number - 1]
            mono_page = mono[region.page_number - 1]
            x, y, x2, y2 = region.box
            clip = pymupdf.Rect(x, source_page.rect.height - y2, x2, source_page.rect.height - y)
            source_text = _normalized_figure_text(source_page.get_text(clip=clip))
            rendered_text = _normalized_figure_text(mono_page.get_text(clip=clip))
            if not source_text or source_text != rendered_text:
                raise RuntimeError(
                    "Figure region text self-check failed: "
                    f"page={region.page_number} xobj={region.xobj_id}"
                )
    return {"verified": True, "region_count": len(checked)}


def verify_verbatim_table_regions(
    *,
    source_pdf: Path,
    mono_pdf: Path,
    regions: Iterable[TableRegion],
) -> dict[str, Any]:
    """Require every table grid to retain the complete source-language cell text."""

    import pymupdf

    checked = list(regions)
    with pymupdf.open(source_pdf) as source, pymupdf.open(mono_pdf) as mono:
        if source.page_count != mono.page_count:
            raise RuntimeError("Cannot verify table regions across unequal page counts")
        for region in checked:
            if region.page_number < 1 or region.page_number > source.page_count:
                raise RuntimeError(
                    f"Table region page is outside the source PDF: {region.page_number}"
                )
            source_page = source[region.page_number - 1]
            mono_page = mono[region.page_number - 1]
            x, y, x2, y2 = region.box
            clip = pymupdf.Rect(x, source_page.rect.height - y2, x2, source_page.rect.height - y)
            source_text = _normalized_figure_text(source_page.get_text(clip=clip))
            rendered_text = _normalized_figure_text(mono_page.get_text(clip=clip))
            if not source_text or source_text != rendered_text:
                raise RuntimeError(
                    "Table region text self-check failed: "
                    f"page={region.page_number} layout={region.layout_id}"
                )
            source_words = source_page.get_text("words", clip=clip)
            rendered_words = mono_page.get_text("words", clip=clip)
            if len(source_words) != len(rendered_words):
                raise RuntimeError(
                    "Table region geometry self-check failed: "
                    f"page={region.page_number} layout={region.layout_id}"
                )
            center_drifts = sorted(
                max(
                    abs((source_word[0] + source_word[2]) / 2 - (rendered_word[0] + rendered_word[2]) / 2),
                    abs((source_word[1] + source_word[3]) / 2 - (rendered_word[1] + rendered_word[3]) / 2),
                )
                for source_word, rendered_word in zip(
                    source_words, rendered_words, strict=True
                )
            )
            if not center_drifts:
                raise RuntimeError(
                    "Table region geometry self-check failed: "
                    f"page={region.page_number} layout={region.layout_id}"
                )
            percentile_95 = center_drifts[max(0, (95 * len(center_drifts) - 1) // 100)]
            if percentile_95 > 6.0 or center_drifts[-1] > 8.0:
                raise RuntimeError(
                    "Table region geometry self-check failed: "
                    f"page={region.page_number} layout={region.layout_id} "
                    f"p95={percentile_95:.2f} max={center_drifts[-1]:.2f}"
                )
    return {"verified": True, "region_count": len(checked)}


def restore_verbatim_pages(
    *,
    source_pdf: Path,
    mono_pdf: Path,
    dual_pdf: Path,
    page_numbers: set[int],
    reference_check_page_numbers: set[int] | None = None,
    canonical_header: dict[str, str] | None = None,
    section_heading_translations: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Replace layout-sensitive passthrough pages with pristine source pages."""

    import pymupdf

    selected = sorted(set(page_numbers))
    check_pages = sorted(set(reference_check_page_numbers or page_numbers))
    if not selected and not check_pages:
        return {
            "page_numbers": [],
            "reference_check_page_numbers": [],
            "verified": True,
            "reference_numbers": {
                "count": 0,
                "first": None,
                "last": None,
                "sequential": True,
            },
        }
    source_pdf = Path(source_pdf)
    mono_pdf = Path(mono_pdf)
    dual_pdf = Path(dual_pdf)
    with pymupdf.open(source_pdf) as source, pymupdf.open(mono_pdf) as mono, pymupdf.open(
        dual_pdf
    ) as dual:
        if mono.page_count != source.page_count or dual.page_count != source.page_count:
            raise RuntimeError("Cannot restore verbatim pages across unequal page counts")
        if selected and (selected[0] < 1 or selected[-1] > source.page_count):
            raise RuntimeError("Verbatim page number is outside the source PDF")
        if check_pages and (check_pages[0] < 1 or check_pages[-1] > source.page_count):
            raise RuntimeError("Reference-check page number is outside the source PDF")

        canonical_header_occurrences = 0
        section_heading_occurrences = 0
        if canonical_header is not None:
            source_header = str(canonical_header.get("source", "")).strip()
            target_header = str(canonical_header.get("target", "")).strip()
            if not source_header or not target_header:
                raise RuntimeError("Canonical header rule is incomplete")
            header_font_size: float | None = None
            header_bbox: tuple[float, float, float, float] | None = None
            for page_index, page in enumerate(mono):
                if page_index + 1 in page_numbers:
                    continue
                for block in page.get_text("dict").get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            if span.get("text") == target_header:
                                header_font_size = float(span["size"])
                                header_bbox = tuple(float(value) for value in span["bbox"])
                                break
                        if header_font_size is not None:
                            break
                    if header_font_size is not None:
                        break
                if header_font_size is not None:
                    break
            for page_number in selected:
                page = source[page_number - 1]
                header_rects = [
                    rect
                    for rect in page.search_for(source_header)
                    if rect.y1 <= page.rect.height * 0.12
                ]
                if len(header_rects) != 1:
                    raise RuntimeError(
                        f"Expected one running header on page {page_number}, "
                        f"found {len(header_rects)}"
                    )
                rect = header_rects[0]
                page.add_redact_annot(rect, fill=(1, 1, 1))
                page.apply_redactions()
                header_box = pymupdf.Rect(
                    header_bbox[0] if header_bbox is not None else 0,
                    max(
                        0,
                        (header_bbox[1] - 1)
                        if header_bbox is not None
                        else rect.y0 - 1,
                    ),
                    page.rect.width,
                    min(
                        page.rect.height,
                        (
                            header_bbox[1]
                            if header_bbox is not None
                            else rect.y0
                        )
                        + (header_font_size or rect.height * 0.75) * 1.7,
                    ),
                )
                cached_fonts = sorted(
                    (Path.home() / ".cache/babeldoc/fonts").glob(
                        "LXGWWenKaiGB-Regular*.ttf"
                    )
                )
                font_options: dict[str, Any] = (
                    {
                        "fontname": "snowmass-running-header",
                        "fontfile": str(cached_fonts[0]),
                    }
                    if cached_fonts
                    else {"fontname": "china-s"}
                )
                remaining = page.insert_textbox(
                    header_box,
                    target_header,
                    fontsize=(
                        header_font_size
                        if header_font_size is not None
                        else max(6, min(10, rect.height * 0.75))
                    ),
                    align=(
                        pymupdf.TEXT_ALIGN_LEFT
                        if header_bbox is not None
                        else pymupdf.TEXT_ALIGN_CENTER
                    ),
                    color=(0, 0, 0),
                    **font_options,
                )
                if remaining < 0:
                    raise RuntimeError(
                        f"Canonical running header did not fit on page {page_number}"
                    )
                canonical_header_occurrences += 1

        for rule in section_heading_translations or []:
            source_heading = str(rule.get("source", "")).strip()
            target_heading = str(rule.get("target", "")).strip()
            if not source_heading or not target_heading:
                raise RuntimeError("Section heading translation rule is incomplete")
            matched_rects: list[tuple[Any, Any]] = []
            for page_number in selected:
                page = source[page_number - 1]
                matched_rects.extend(
                    (page, rect)
                    for rect in page.search_for(source_heading)
                    if rect.y1 <= page.rect.height * 0.30
                )
            if len(matched_rects) != 1:
                raise RuntimeError(
                    f"Expected one verbatim section heading {source_heading!r}, "
                    f"found {len(matched_rects)}"
                )
            page, rect = matched_rects[0]
            page.add_redact_annot(rect, fill=(1, 1, 1))
            page.apply_redactions()
            heading_box = pymupdf.Rect(
                rect.x0,
                max(0, rect.y0 - 1),
                min(page.rect.width, rect.x1 + 80),
                min(page.rect.height, rect.y1 + 4),
            )
            remaining = page.insert_textbox(
                heading_box,
                target_heading,
                fontname="china-s",
                fontsize=max(7, min(13, rect.height * 0.78)),
                align=pymupdf.TEXT_ALIGN_LEFT,
                color=(0, 0, 0),
            )
            if remaining < 0:
                raise RuntimeError(f"Translated section heading did not fit: {source_heading}")
            section_heading_occurrences += 1

        mono_rebuilt = pymupdf.open()
        dual_rebuilt = pymupdf.open()
        for page_index in range(source.page_count):
            page_number = page_index + 1
            if page_number not in page_numbers:
                mono_rebuilt.insert_pdf(mono, from_page=page_index, to_page=page_index)
                dual_rebuilt.insert_pdf(dual, from_page=page_index, to_page=page_index)
                continue

            mono_rebuilt.insert_pdf(source, from_page=page_index, to_page=page_index)
            dual_page = dual[page_index]
            rebuilt_page = dual_rebuilt.new_page(
                width=dual_page.rect.width,
                height=dual_page.rect.height,
            )
            midpoint = rebuilt_page.rect.width / 2
            rebuilt_page.show_pdf_page(
                pymupdf.Rect(0, 0, midpoint, rebuilt_page.rect.height),
                source,
                page_index,
            )
            rebuilt_page.show_pdf_page(
                pymupdf.Rect(midpoint, 0, rebuilt_page.rect.width, rebuilt_page.rect.height),
                source,
                page_index,
            )

        mono_temporary = mono_pdf.with_name(mono_pdf.stem + ".verbatim.tmp.pdf")
        dual_temporary = dual_pdf.with_name(dual_pdf.stem + ".verbatim.tmp.pdf")
        mono_rebuilt.save(mono_temporary, garbage=4, deflate=True)
        dual_rebuilt.save(dual_temporary, garbage=4, deflate=True)
        mono_rebuilt.close()
        dual_rebuilt.close()

    os.replace(mono_temporary, mono_pdf)
    os.replace(dual_temporary, dual_pdf)

    selected_source_text: list[str] = []
    selected_source_reference_text: list[str] = []
    with pymupdf.open(source_pdf) as source, pymupdf.open(mono_pdf) as mono:
        for page_number in check_pages:
            source_page = source[page_number - 1]
            selected_source_reference_text.append(source_page.get_text())
        for page_number in selected:
            source_page = source[page_number - 1]
            output_page = mono[page_number - 1]
            source_body = pymupdf.Rect(
                0,
                source_page.rect.height * 0.10,
                source_page.rect.width,
                source_page.rect.height,
            )
            output_body = pymupdf.Rect(
                0,
                output_page.rect.height * 0.10,
                output_page.rect.width,
                output_page.rect.height,
            )
            source_text = _normalized_page_text(source_page.get_text(clip=source_body))
            output_text = _normalized_page_text(output_page.get_text(clip=output_body))
            for rule in section_heading_translations or []:
                source_text = source_text.replace(str(rule["source"]), "", 1).strip()
                output_text = output_text.replace(str(rule["target"]), "", 1).strip()
            if source_text != output_text:
                raise RuntimeError(
                    f"Verbatim page self-check failed on page {page_number}: "
                    f"source={source_text!r}, output={output_text!r}"
                )
            selected_source_text.append(source_text)
    numbers = reference_entry_numbers("\n".join(selected_source_reference_text))
    sequential = not numbers or numbers == list(range(1, numbers[-1] + 1))
    if not sequential:
        raise RuntimeError("Reference numbering self-check failed")
    return {
        "page_numbers": selected,
        "reference_check_page_numbers": check_pages,
        "verified": True,
        "canonical_header_occurrences": canonical_header_occurrences,
        "section_heading_occurrences": section_heading_occurrences,
        "reference_numbers": {
            "count": len(numbers),
            "first": numbers[0] if numbers else None,
            "last": numbers[-1] if numbers else None,
            "sequential": sequential,
        },
    }


def _validate_units(units: Iterable[DocumentUnit]) -> list[DocumentUnit]:
    validated = list(units)
    for index, unit in enumerate(validated):
        if unit.page_number < 1:
            raise ValueError(f"unit {index} has invalid page_number {unit.page_number}")
        if unit.paragraph_index < 0:
            raise ValueError(f"unit {index} has invalid paragraph_index {unit.paragraph_index}")
        if unit.structure_count < 0:
            raise ValueError(f"unit {index} has invalid structure_count {unit.structure_count}")
        if unit.translation_policy not in TRANSLATION_POLICIES:
            raise ValueError(
                f"unit {index} has invalid translation_policy {unit.translation_policy!r}"
            )
        if not unit.text.strip():
            raise ValueError(f"unit {index} has blank text")
    return validated


def _existing_chunk_ids(
    article_dir: Path, record_id: str
) -> tuple[dict[tuple[int, int], str], int]:
    manifest_path = article_dir / "manifest.json"
    if not manifest_path.is_file():
        return {}, 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("record_id") != record_id:
        raise RuntimeError("Existing workspace belongs to a different record")
    identities: dict[tuple[int, int], str] = {}
    highest = 0
    for chunk in manifest.get("chunks", []):
        chunk_id = str(chunk.get("id", ""))
        match = re.fullmatch(r"chunk(\d+)", chunk_id)
        if match is None:
            raise RuntimeError(f"Existing workspace has invalid chunk id: {chunk_id}")
        identity = (int(chunk["page_number"]), int(chunk["paragraph_index"]))
        if identity in identities:
            raise RuntimeError(f"Existing workspace has duplicate unit identity: {identity}")
        identities[identity] = chunk_id
        highest = max(highest, int(match.group(1)))
    return identities, highest


def pre_translate_document_paragraph(
    il_translator: Any,
    paragraph: Any,
    tracker: Any,
    page_font_map: dict[str, Any],
    xobj_font_map: dict[int, dict[str, Any]],
) -> tuple[Any, Any]:
    """Include short table fragments without lowering the global text threshold."""

    config = il_translator.translation_config
    original_minimum = config.min_text_length
    if paragraph.layout_label == "fallback_line":
        config.min_text_length = 1
    try:
        return il_translator.pre_translate_paragraph(
            paragraph, tracker, page_font_map, xobj_font_map
        )
    finally:
        config.min_text_length = original_minimum


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

    existing_ids, highest_chunk_number = _existing_chunk_ids(article_dir, record_id)
    next_chunk_number = highest_chunk_number + 1
    chunks: list[dict[str, Any]] = []
    unit_records: list[dict[str, Any]] = []
    for order, unit in enumerate(validated, 1):
        identity = (unit.page_number, unit.paragraph_index)
        chunk_id = existing_ids.get(identity)
        if chunk_id is None:
            chunk_id = f"chunk{next_chunk_number:04d}"
            next_chunk_number += 1
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
                "translation_policy": unit.translation_policy,
            }
        )
        unit_records.append({"chunk_id": chunk_id, "unit_id": unit_id, **asdict(unit)})

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "record_id": record_id,
        "input_mode": "babeldoc_ir",
        "babeldoc_version": BABELDOC_VERSION,
        "ir_pipeline_version": IR_PIPELINE_VERSION,
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
            "ir_pipeline_version": IR_PIPELINE_VERSION,
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
    """Require every BabelDOC object marker identity exactly once."""

    from collections import Counter

    return Counter(_placeholder_sequence(source)) == Counter(
        _placeholder_sequence(translated)
    )


def require_verbatim_figure_text(
    xobj_id: int | None, source: str, translated: str
) -> None:
    """Fail closed when text owned by a PDF figure XObject was translated."""

    if int(xobj_id or 0) != 0 and _with_terminal_newline(source) != _with_terminal_newline(
        translated
    ):
        raise RuntimeError("Figure-internal text must remain verbatim")


def require_verbatim_table_text(
    is_table_text: bool, source: str, translated: str
) -> None:
    """Fail closed when text geometrically owned by a table grid was translated."""

    if is_table_text and _with_terminal_newline(source) != _with_terminal_newline(
        translated
    ):
        raise RuntimeError("Table-internal text must remain verbatim")


def normalize_document_ir_numeric_tokens(value: Any) -> None:
    """Repair xsdata's list[object] XML round-trip for CTM float tokens in place."""

    visited: set[int] = set()

    def visit(item: Any) -> None:
        if item is None or isinstance(item, (str, bytes, int, float, bool)):
            return
        identity = id(item)
        if identity in visited:
            return
        visited.add(identity)
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not is_dataclass(item):
            return
        for descriptor in fields(item):
            child = getattr(item, descriptor.name)
            if descriptor.name in {"ctm", "relocation_transform"} and isinstance(
                child, list
            ):
                setattr(item, descriptor.name, [float(token) for token in child])
            else:
                visit(child)

    visit(value)


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

    config = build_parse_only_config(source_pdf, working_dir=Path(working_dir), debug=False)
    config.lang_in = "en"
    config.lang_out = "zh"
    config.disable_rich_text_translate = True
    config.auto_extract_glossary = False
    config.progress_monitor.disable = True
    try:
        converter = XMLConverter()
        docs = converter.read_xml(str(ir_xml_path))
        normalize_document_ir_numeric_tokens(docs)
        suppress_cross_page_auxiliary_orphans(docs, requested)
        cross_page_line_carries = identify_cross_page_line_fragment_carries(docs)
        requested, cross_page_rebalances = rebalance_cross_page_sentence_fragments(
            docs, requested
        )
        il_translator = ILTranslator(_babeldoc_placeholder_translator(), config)
        figure_text_verbatim_count = 0
        table_text_verbatim_count = 0
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
            source_text, translate_input = pre_translate_document_paragraph(
                il_translator, paragraph, tracker, page_font_map, xobj_font_map
            )
            if source_text is None or translate_input is None:
                raise RuntimeError(
                    f"BabelDOC paragraph is not translatable: {item.page_number}/{item.paragraph_index}"
                )
            if _with_terminal_newline(source_text) != _with_terminal_newline(item.source_text):
                raise RuntimeError(
                    f"BabelDOC source changed before refill: {item.page_number}/{item.paragraph_index}"
                )
            require_verbatim_figure_text(
                paragraph.xobj_id,
                item.source_text,
                item.translated_text,
            )
            if int(paragraph.xobj_id or 0) != 0:
                figure_text_verbatim_count += 1
            is_table_text = any(
                _box_center_is_inside(_object_box(paragraph), region_box)
                for _layout_id, region_box in _object_table_regions(page)
            )
            require_verbatim_table_text(
                is_table_text,
                item.source_text,
                item.translated_text,
            )
            if is_table_text:
                table_text_verbatim_count += 1
            if not placeholder_sequence_matches(source_text, item.translated_text):
                raise RuntimeError(
                    "BabelDOC placeholder identity or count changed in translation: "
                    f"{item.page_number}/{item.paragraph_index}"
                )
            il_translator.post_translate_paragraph(
                paragraph,
                tracker,
                translate_input,
                item.translated_text,
            )
        same_line_merges = coalesce_same_baseline_line_fragments(docs)
        applied_cross_page_line_carries = apply_cross_page_line_fragment_carries(
            docs, cross_page_line_carries
        )
        output_xml.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_xml.with_name(output_xml.name + ".tmp")
        converter.write_xml(docs, str(temporary))
        os.replace(temporary, output_xml)
        return RefillResult(
            output_xml,
            len(requested),
            figure_text_verbatim_count=figure_text_verbatim_count,
            table_text_verbatim_count=table_text_verbatim_count,
            cross_page_sentence_rebalance_count=len(cross_page_rebalances),
            same_line_fragment_merge_count=len(same_line_merges),
            cross_page_line_fragment_carry_count=len(
                applied_cross_page_line_carries
            ),
        )
    finally:
        config.cleanup_temp_files()


def render_translated_document(
    ir_xml_path: Path,
    *,
    source_pdf: Path,
    working_dir: Path,
    output_dir: Path,
    verbatim_page_numbers: set[int] | None = None,
    reference_check_page_numbers: set[int] | None = None,
    verbatim_header_translation: dict[str, str] | None = None,
    verbatim_section_heading_translations: list[dict[str, str]] | None = None,
    reference_regions: Iterable[TableRegion] | None = None,
) -> RenderedPdfResult:
    """Typeset translated BabelDOC XML IR into stable mono and dual PDFs."""

    installed_version = metadata.version("babeldoc")
    if installed_version != BABELDOC_VERSION:
        raise RuntimeError(
            f"BabelDOC version mismatch: expected {BABELDOC_VERSION}, got {installed_version}"
        )
    from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater
    from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting
    from babeldoc.format.pdf.document_il.xml_converter import XMLConverter
    from babeldoc.format.pdf.high_level import fix_filter
    from babeldoc.format.pdf.high_level import fix_media_box
    from babeldoc.format.pdf.high_level import fix_null_page_content
    from babeldoc.format.pdf.high_level import fix_null_xref
    from babeldoc.format.pdf.high_level import open_pdf_with_save_fallback
    from babeldoc.format.pdf.high_level import save_pdf_with_same_path_fallback
    from babeldoc.format.pdf.parse_shared import build_parse_only_config
    from babeldoc.format.pdf.translation_config import WatermarkOutputMode

    ir_xml_path = Path(ir_xml_path)
    source_pdf = Path(source_pdf)
    output_dir = Path(output_dir)
    if not ir_xml_path.is_file():
        raise FileNotFoundError(ir_xml_path)
    if not source_pdf.is_file():
        raise FileNotFoundError(source_pdf)
    output_dir.mkdir(parents=True, exist_ok=True)

    config = build_parse_only_config(
        source_pdf,
        working_dir=Path(working_dir),
        debug=False,
    )
    config.lang_in = "en"
    config.lang_out = "zh"
    config.output_dir = output_dir
    config.no_mono = False
    config.no_dual = False
    config.use_alternating_pages_dual = False
    config.watermark_output_mode = WatermarkOutputMode.NoWatermark
    config.progress_monitor.disable = True

    prepared_pdf = Path(config.get_working_file_path("render_input.pdf"))
    doc_pdf = None
    try:
        doc_pdf = open_pdf_with_save_fallback(source_pdf, prepared_pdf)
        fix_null_page_content(doc_pdf)
        fix_filter(doc_pdf)
        fix_null_xref(doc_pdf)
        mediabox_data = fix_media_box(doc_pdf)
        doc_pdf = save_pdf_with_same_path_fallback(doc_pdf, prepared_pdf)

        docs = XMLConverter().read_xml(str(ir_xml_path))
        normalize_document_ir_numeric_tokens(docs)
        figure_regions = figure_regions_from_document(docs)
        table_regions = table_regions_from_document(docs)
        bibliography_regions = list(reference_regions or ())
        class SnowmassTypesetting(Typesetting):
            """Correct BabelDOC 0.6.4's current-unit double count in word lookahead."""

            def _get_width_before_next_break_point(
                self, typesetting_units: list[Any], scale: float
            ) -> float:
                if not typesetting_units or typesetting_units[0].can_break_line:
                    return 0
                total_width = 0.0
                # The caller already adds the current unit width. BabelDOC 0.6.4
                # starts this sum at the current unit too, which can leave the
                # first Latin letter on a CJK line and wrap the rest of the word.
                for unit in typesetting_units[1:]:
                    if unit.can_break_line:
                        break
                    total_width += unit.width
                return total_width * scale

        SnowmassTypesetting(config).typesetting_document(docs)
        result = PDFCreater(prepared_pdf, docs, config, mediabox_data).write(config)
        if result.mono_pdf_path is None or result.dual_pdf_path is None:
            raise RuntimeError("BabelDOC did not produce both mono and dual PDF outputs")

        mono_pdf = output_dir / "translated_mono.pdf"
        dual_pdf = output_dir / "translated_dual.pdf"
        _atomic_copy(Path(result.mono_pdf_path), mono_pdf)
        _atomic_copy(Path(result.dual_pdf_path), dual_pdf)
        verbatim_report = restore_verbatim_pages(
            source_pdf=source_pdf,
            mono_pdf=mono_pdf,
            dual_pdf=dual_pdf,
            page_numbers=set(verbatim_page_numbers or ()),
            reference_check_page_numbers=set(reference_check_page_numbers or ()),
            canonical_header=verbatim_header_translation,
            section_heading_translations=verbatim_section_heading_translations,
        )
        restore_verbatim_regions(
            source_pdf=source_pdf,
            mono_pdf=mono_pdf,
            dual_pdf=dual_pdf,
            figure_regions=figure_regions,
            table_regions=table_regions,
        )
        reference_region_report = restore_verbatim_reference_regions(
            source_pdf=source_pdf,
            mono_pdf=mono_pdf,
            dual_pdf=dual_pdf,
            regions=bibliography_regions,
        )
        figure_report = verify_verbatim_figure_regions(
            source_pdf=source_pdf,
            mono_pdf=mono_pdf,
            regions=figure_regions,
        )
        table_report = verify_verbatim_table_regions(
            source_pdf=source_pdf,
            mono_pdf=mono_pdf,
            regions=table_regions,
        )
        return RenderedPdfResult(
            mono_pdf,
            dual_pdf,
            verbatim_pages=tuple(verbatim_report["page_numbers"]),
            verbatim_verified=bool(verbatim_report["verified"]),
            reference_numbers=verbatim_report["reference_numbers"],
            canonical_header_occurrences=int(
                verbatim_report.get("canonical_header_occurrences", 0)
            ),
            section_heading_occurrences=int(
                verbatim_report.get("section_heading_occurrences", 0)
            ),
            figure_regions_verified=bool(figure_report["verified"]),
            figure_region_count=int(figure_report["region_count"]),
            table_regions_verified=bool(table_report["verified"]),
            table_region_count=int(table_report["region_count"]),
            reference_regions_verified=bool(reference_region_report["verified"]),
            reference_region_count=int(reference_region_report["region_count"]),
        )
    finally:
        if doc_pdf is not None:
            doc_pdf.close()
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
        debug=False,
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
        materialize_lazy_passthrough_instructions(docs)
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
                text, translate_input = pre_translate_document_paragraph(
                    il_translator, paragraph, tracker, page_font_map, xobj_font_map
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
                is_table_text = any(
                    _box_center_is_inside(_object_box(paragraph), region_box)
                    for _layout_id, region_box in _object_table_regions(page)
                )
                units.append(
                    DocumentUnit(
                        page_number=page_number,
                        paragraph_index=paragraph_index,
                        layout_label=str(paragraph.layout_label or ""),
                        text=text,
                        structure_count=structure_count,
                        translation_policy=(
                            VERBATIM_FIGURE_TEXT_POLICY
                            if int(paragraph.xobj_id or 0) != 0
                            else (
                                VERBATIM_TABLE_TEXT_POLICY
                                if is_table_text
                                else TRANSLATE_POLICY
                            )
                        ),
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
