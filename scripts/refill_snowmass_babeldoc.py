#!/usr/bin/env python3
"""Refill verified Snowmass translations into persisted BabelDOC XML IR."""

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


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RIGHTS_MANIFEST = ROOT / "site/data/papers.json"
DEFAULT_GLOSSARY = ROOT / "translations/snowmass-global-glossary.json"
DEFAULT_HARD_CONSTRAINTS = ROOT / "translations/snowmass-hard-constraints.json"
REFILL_SCHEMA_VERSION = 9


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


def _normalized_phrase(value: str) -> str:
    return " ".join(value.split()).casefold()


def _term_match(text: str, term: str) -> re.Match[str] | None:
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    if term and term[0].isascii() and term[0].isalnum():
        escaped = r"(?<![A-Za-z0-9])" + escaped
    if term and term[-1].isascii() and term[-1].isalnum():
        escaped += r"(?![A-Za-z0-9])"
    return re.search(escaped, text, flags=re.IGNORECASE)


def _first_use_terms(
    translations: list[Any],
    glossary: list[dict[str, Any]],
    *,
    eligible_indices: list[int] | None = None,
) -> list[tuple[int, int, int, dict[str, Any]]]:
    candidates: list[tuple[int, int, int, dict[str, Any]]] = []
    for term in glossary:
        if term.get("first_use") is not True:
            continue
        source_term = str(term.get("source", "")).strip()
        target_term = str(term.get("target", "")).strip()
        if not source_term or not target_term or source_term == target_term:
            continue
        indices = eligible_indices if eligible_indices is not None else list(range(len(translations)))
        for translation_index in indices:
            translation = translations[translation_index]
            match = _term_match(translation.source_text, source_term)
            if match is not None:
                candidates.append(
                    (translation_index, match.start(), match.end(), term)
                )
                break

    accepted: list[tuple[int, int, int, dict[str, Any]]] = []
    for candidate in sorted(candidates, key=lambda item: (item[0], item[1], -(item[2] - item[1]))):
        index, start, end, _term = candidate
        if any(
            other_index == index and not (end <= other_start or start >= other_end)
            for other_index, other_start, other_end, _other_term in accepted
        ):
            continue
        accepted.append(candidate)
    return accepted


def _insert_first_use(text: str, term: dict[str, Any]) -> str:
    source_term = str(term["source"]).strip()
    target_term = str(term["target"]).strip()
    target_index = text.find(target_term)
    if target_index < 0:
        raise RuntimeError(f"locked first-use target is missing: {target_term}")
    suffix = text[target_index + len(target_term) :]
    existing = re.match(r"\s*[（(]([^）)]*)[）)]", suffix)
    if existing is not None and source_term.casefold() in existing.group(1).casefold():
        return text
    acronym = str(term.get("acronym", "")).strip()
    definition = source_term + (f"，{acronym}" if acronym else "")
    insertion_at = target_index + len(target_term)
    return text[:insertion_at] + f"（{definition}）" + text[insertion_at:]


def prepare_publication_translations(
    article_dir: Path,
    manifest: dict[str, Any],
    translations: list[Any],
    *,
    constraints: dict[str, Any],
    glossary: list[dict[str, Any]],
    figure_text_chunk_ids: set[str] | None = None,
    table_text_chunk_ids: set[str] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Create deterministic publication-stage chunks and enforce hard constraints."""

    if constraints.get("schema_version", 1) != 1:
        raise RuntimeError("Unsupported publication constraint schema")
    chunks = sorted(manifest.get("chunks", []), key=lambda item: item.get("order", 0))
    if len(chunks) != len(translations):
        raise RuntimeError("Publication chunks do not match refill translations")
    figure_text_chunk_ids = set(figure_text_chunk_ids or ())
    table_text_chunk_ids = set(table_text_chunk_ids or ())
    passthrough_chunk_ids = figure_text_chunk_ids | table_text_chunk_ids
    prepared_texts = [translation.translated_text for translation in translations]
    for index, (chunk, translation) in enumerate(zip(chunks, translations, strict=True)):
        if str(chunk["id"]) in passthrough_chunk_ids:
            prepared_texts[index] = translation.source_text
    exact_occurrences = 0
    for rule in constraints.get("exact_translations", []):
        source = str(rule.get("source", "")).strip()
        target = str(rule.get("target", "")).strip()
        if not source or not target:
            raise RuntimeError("Exact translation rule is incomplete")
        matched = 0
        for index, translation in enumerate(translations):
            if str(chunks[index]["id"]) in passthrough_chunk_ids:
                continue
            if _normalized_phrase(translation.source_text) != _normalized_phrase(source):
                continue
            trailing_newline = "\n" if translation.translated_text.endswith("\n") else ""
            prepared_texts[index] = target + trailing_newline
            matched += 1
        if matched == 0:
            raise RuntimeError(f"exact translation source was not found: {source}")
        exact_occurrences += matched

    eligible_first_use_indices: list[int] = []
    in_references = False
    for index, (chunk, translation) in enumerate(zip(chunks, translations, strict=True)):
        normalized_source = _normalized_phrase(translation.source_text).rstrip(":")
        if normalized_source in {"references", "bibliography"}:
            in_references = True
        if (
            not in_references
            and str(chunk.get("layout_label", "")) != "title"
            and str(chunk["id"]) not in passthrough_chunk_ids
        ):
            eligible_first_use_indices.append(index)
    first_use = _first_use_terms(
        translations,
        glossary,
        eligible_indices=eligible_first_use_indices,
    )
    for translation_index, _start, _end, term in first_use:
        prepared_texts[translation_index] = _insert_first_use(
            prepared_texts[translation_index], term
        )

    publication_dir = article_dir / "publication_chunks"
    prepared: list[Any] = []
    chunk_hashes: dict[str, str] = {}
    for chunk, translation, text in zip(chunks, translations, prepared_texts, strict=True):
        output = publication_dir / f"{chunk['id']}.md"
        temporary = output.with_name(output.name + ".tmp")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, output)
        chunk_hashes[str(chunk["id"])] = _sha256(output)
        prepared.append(
            BRIDGE.RefillTranslation(
                page_number=translation.page_number,
                paragraph_index=translation.paragraph_index,
                source_text=translation.source_text,
                translated_text=text,
            )
        )
    return prepared, {
        "ok": True,
        "exact_translation_occurrences": exact_occurrences,
        "first_use_terms": len(first_use),
        "figure_text_passthrough_units": len(figure_text_chunk_ids),
        "table_text_passthrough_units": len(table_text_chunk_ids),
        "publication_chunk_sha256": chunk_hashes,
    }


def _figure_text_chunk_ids(
    article_dir: Path, manifest: dict[str, Any]
) -> set[str]:
    """Resolve vector-figure text from persisted IR, never from wording heuristics."""

    return BRIDGE.resolve_figure_text_chunk_ids(article_dir, manifest)


def _table_text_chunk_ids(
    article_dir: Path, manifest: dict[str, Any]
) -> set[str]:
    """Resolve table body text from persisted page-layout geometry."""

    return BRIDGE.resolve_table_text_chunk_ids(article_dir, manifest)


def _load_constraints(
    article_dir: Path,
    record_id: str,
    *,
    policy_path: Path = DEFAULT_HARD_CONSTRAINTS,
) -> dict[str, Any]:
    path = article_dir / "hard_constraints.json"
    if path.is_file():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError(f"Hard constraints must be a JSON object: {path}")
        return value
    if not policy_path.is_file():
        return {"schema_version": 1, "record_id": record_id, "exact_translations": []}
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise RuntimeError(f"Invalid tracked hard constraint policy: {policy_path}")
    records = policy.get("records")
    if not isinstance(records, dict):
        raise RuntimeError(f"Tracked hard constraint policy has no records: {policy_path}")
    record_rules = records.get(record_id, {})
    if not isinstance(record_rules, dict):
        raise RuntimeError(f"Tracked hard constraints are invalid for {record_id}")
    return {
        "schema_version": 1,
        "record_id": record_id,
        "exact_translations": record_rules.get("exact_translations", []),
    }


def _load_glossary(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    terms = value.get("terms") if isinstance(value, dict) else None
    if not isinstance(terms, list) or not all(isinstance(term, dict) for term in terms):
        raise RuntimeError(f"Glossary must contain a terms list: {path}")
    return terms


def _reference_page_numbers(article_dir: Path, manifest: dict[str, Any]) -> set[int]:
    chunks = sorted(manifest.get("chunks", []), key=lambda item: item.get("order", 0))
    reference_page: int | None = None
    last_page = 0
    for chunk in chunks:
        page_number = int(chunk["page_number"])
        last_page = max(last_page, page_number)
        source_path = article_dir / str(chunk["source_file"])
        heading = _normalized_phrase(source_path.read_text(encoding="utf-8")).rstrip(":")
        if reference_page is None and heading in {"references", "bibliography"}:
            reference_page = page_number
    if reference_page is None:
        return set()
    return set(range(reference_page, last_page + 1))


def _verbatim_header_translation(
    article_dir: Path,
    manifest: dict[str, Any],
    reference_pages: set[int],
    constraints: dict[str, Any],
) -> dict[str, str] | None:
    if not reference_pages:
        return None
    by_source: dict[str, dict[str, Any]] = {}
    for chunk in manifest.get("chunks", []):
        page_number = int(chunk["page_number"])
        if page_number not in reference_pages:
            continue
        raw = (article_dir / str(chunk["source_file"])).read_text(encoding="utf-8")
        normalized = _normalized_phrase(raw)
        if len(normalized) > 200 or len(re.findall(r"[A-Za-z]", normalized)) < 4:
            continue
        item = by_source.setdefault(
            normalized,
            {"source": " ".join(raw.split()), "pages": set()},
        )
        item["pages"].add(page_number)
    repeated = {
        normalized: item
        for normalized, item in by_source.items()
        if item["pages"] == reference_pages
        and normalized not in {"references", "bibliography"}
    }
    if not repeated:
        return None
    exact = {
        _normalized_phrase(str(rule.get("source", ""))): str(rule.get("target", "")).strip()
        for rule in constraints.get("exact_translations", [])
    }
    matches = [
        {"source": item["source"], "target": exact[normalized]}
        for normalized, item in repeated.items()
        if exact.get(normalized)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Reference pages contain a repeated running header without one canonical exact translation"
        )
    return matches[0]


def _verbatim_section_heading_translations(
    constraints: dict[str, Any], reference_pages: set[int]
) -> list[dict[str, str]]:
    if not reference_pages:
        return []
    return [
        {"source": str(rule["source"]).strip(), "target": str(rule["target"]).strip()}
        for rule in constraints.get("exact_translations", [])
        if _normalized_phrase(str(rule.get("source", ""))).rstrip(":")
        in {"references", "bibliography"}
        and str(rule.get("target", "")).strip()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--article-dir", type=Path, required=True)
    parser.add_argument("--rights-manifest", type=Path, default=DEFAULT_RIGHTS_MANIFEST)
    parser.add_argument("--glossary", type=Path, default=DEFAULT_GLOSSARY)
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
    constraints = _load_constraints(args.article_dir, str(record_id))
    constraint_record = constraints.get("record_id")
    if constraint_record is not None and constraint_record != record_id:
        raise RuntimeError(
            f"Hard constraint record mismatch: expected {record_id}, got {constraint_record}"
        )
    glossary = _load_glossary(args.glossary)
    figure_text_chunk_ids = _figure_text_chunk_ids(args.article_dir, manifest)
    table_text_chunk_ids = _table_text_chunk_ids(args.article_dir, manifest)
    translations, publication_qc = prepare_publication_translations(
        args.article_dir,
        manifest,
        translations,
        constraints=constraints,
        glossary=glossary,
        figure_text_chunk_ids=figure_text_chunk_ids,
        table_text_chunk_ids=table_text_chunk_ids,
    )
    reference_pages = _reference_page_numbers(args.article_dir, manifest)
    verbatim_header = _verbatim_header_translation(
        args.article_dir,
        manifest,
        reference_pages,
        constraints,
    )
    verbatim_section_headings = _verbatim_section_heading_translations(
        constraints, reference_pages
    )
    signature_payload = {
        "refill_schema_version": REFILL_SCHEMA_VERSION,
        "babeldoc_version": BRIDGE.BABELDOC_VERSION,
        "ir_pipeline_version": BRIDGE.IR_PIPELINE_VERSION,
        "record_id": record_id,
        "source_pdf_sha256": _sha256(source_pdf),
        "ir_xml_sha256": _sha256(ir_xml),
        "chunks": chunk_hashes,
        "publication_chunks": publication_qc["publication_chunk_sha256"],
        "hard_constraints": constraints,
        "first_use_glossary": [term for term in glossary if term.get("first_use") is True],
        "verbatim_page_numbers": sorted(reference_pages),
        "verbatim_header_translation": verbatim_header,
        "verbatim_section_heading_translations": verbatim_section_headings,
        "figure_text_chunk_ids": sorted(figure_text_chunk_ids),
        "table_text_chunk_ids": sorted(table_text_chunk_ids),
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
        verbatim_page_numbers=reference_pages,
        verbatim_header_translation=verbatim_header,
        verbatim_section_heading_translations=verbatim_section_headings,
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
            "figure_text_verbatim_count": result.figure_text_verbatim_count,
            "table_text_verbatim_count": result.table_text_verbatim_count,
            "figure_region_count": rendered.figure_region_count,
            "figure_regions_verified": rendered.figure_regions_verified,
            "table_region_count": rendered.table_region_count,
            "table_regions_verified": rendered.table_regions_verified,
            "output_xml_file": output_xml.name,
            "output_xml_sha256": _sha256(output_xml),
            "mono_pdf_file": str(rendered.mono_pdf_path.relative_to(args.article_dir)),
            "mono_pdf_sha256": _sha256(rendered.mono_pdf_path),
            "dual_pdf_file": str(rendered.dual_pdf_path.relative_to(args.article_dir)),
            "dual_pdf_sha256": _sha256(rendered.dual_pdf_path),
            "chunks": chunk_hashes,
            "publication_qc": publication_qc,
            "reference_qc": {
                "page_numbers": list(rendered.verbatim_pages),
                "verified": rendered.verbatim_verified,
                "reference_numbers": rendered.reference_numbers,
                "canonical_header_occurrences": rendered.canonical_header_occurrences,
                "section_heading_occurrences": rendered.section_heading_occurrences,
            },
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
