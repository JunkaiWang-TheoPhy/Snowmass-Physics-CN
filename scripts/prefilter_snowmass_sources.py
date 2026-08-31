#!/usr/bin/env python3
"""Zero-cost source-PDF triage for a small, low-layout-risk pilot.

This is a scheduling aid, not a publication gate. Figure/plot/table interiors
remain source-language passthrough in the production pipeline; the metrics here
only estimate placement and QC risk before spending API calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import fitz


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def inspect_pdf(path: Path) -> dict[str, Any]:
    with fitz.open(path) as document:
        pages = len(document)
        images = 0
        drawings = 0
        words = 0
        numeric_citations = 0
        citation_ranges = 0
        reference_pages = 0
        contents_pages = 0
        contents_entries = 0
        for page in document:
            text = page.get_text("text")
            images += len(page.get_images(full=True))
            drawings += len(page.get_drawings())
            words += len(page.get_text("words"))
            citations = re.findall(r"\[(?:\d+)(?:\s*[-–—,]\s*\d+)*\]", text)
            numeric_citations += len(citations)
            citation_ranges += sum(1 for citation in citations if re.search(r"[-–—]", citation))
            if "references" in text.casefold() or "bibliography" in text.casefold():
                reference_pages += 1
            if re.search(r"^\s*(?:contents|table of contents)\s*$", text, re.I | re.M):
                contents_pages += 1
                contents_entries += len(re.findall(r"^\s*\d+(?:\.\d+)*\s+\S+", text, re.M))
    return {
        "pages": pages,
        "images": images,
        "drawings": drawings,
        "words": words,
        "numeric_citations": numeric_citations,
        "citation_ranges": citation_ranges,
        "reference_pages": reference_pages,
        "contents_pages": contents_pages,
        "contents_entries": contents_entries,
        "sha256": sha256(path),
    }


def classify_risk(metrics: dict[str, Any]) -> str:
    """Assign a scheduling tier without weakening the low-risk gate."""

    if (
        metrics["pages"] <= 10
        and metrics["images"] == 0
        and metrics["drawings"] <= 200
        and metrics["numeric_citations"] <= 10
        and metrics["citation_ranges"] <= 1
        and metrics["reference_pages"] <= 2
        and metrics["contents_pages"] == 0
    ):
        return "low_risk"
    if (
        metrics["pages"] <= 20
        and metrics["images"] == 0
        and metrics["drawings"] <= 50
        and metrics["numeric_citations"] <= 80
        and metrics["citation_ranges"] <= 5
        and metrics["reference_pages"] <= 3
        and metrics["contents_pages"] == 0
    ):
        return "text_only_medium"
    if (
        metrics["pages"] <= 20
        and metrics["images"] <= 2
        and metrics["drawings"] <= 500
        and metrics["numeric_citations"] <= 80
        and metrics["citation_ranges"] <= 5
        and metrics["reference_pages"] <= 3
        and metrics["contents_pages"] == 0
    ):
        return "figure_passthrough_medium"
    if (
        metrics["pages"] <= 40
        and metrics["images"] == 0
        and metrics["drawings"] == 0
        and metrics["numeric_citations"] <= 150
        and metrics["citation_ranges"] <= 10
        and metrics["reference_pages"] <= 5
        and metrics["contents_pages"] == 0
    ):
        return "text_only_long"
    if (
        metrics["pages"] <= 30
        and metrics["images"] == 0
        and metrics["drawings"] <= 500
        and metrics["numeric_citations"] <= 150
        and metrics["citation_ranges"] <= 10
        and metrics["reference_pages"] <= 5
        and metrics["contents_pages"] == 0
    ):
        return "vector_passthrough_long"
    return "complex_or_unclassified"


def prefilter(*, rights_path: Path, source_manifest_path: Path, pdf_root: Path) -> dict[str, Any]:
    rights = json.loads(rights_path.read_text(encoding="utf-8"))
    source = json.loads(source_manifest_path.read_text(encoding="utf-8"))["records"]
    source_by_id = {record["record_id"]: record for record in source}
    rows: list[dict[str, Any]] = []
    for record in rights:
        if record.get("publication_allowed") is not True:
            continue
        record_id = record["record_id"]
        source_record = source_by_id.get(record_id)
        row: dict[str, Any] = {
            "record_id": record_id,
            "publication_allowed": record.get("publication_allowed"),
            "eligible": False,
        }
        if not source_record:
            row["reasons"] = ["missing_source_record"]
            rows.append(row)
            continue
        pdf = pdf_root / source_record["pdf_path"]
        if not pdf.is_file():
            row["reasons"] = ["missing_source_pdf"]
            rows.append(row)
            continue
        try:
            metrics = inspect_pdf(pdf)
        except Exception as exc:  # noqa: BLE001 - triage must fail closed per record
            row["reasons"] = [f"pdf_inspection:{type(exc).__name__}"]
            rows.append(row)
            continue
        reasons = []
        if metrics["pages"] > 10:
            reasons.append("over_10_pages")
        if metrics["reference_pages"] > 2:
            reasons.append("reference_tail_over_2_pages")
        if metrics["images"] > 12:
            reasons.append("many_embedded_images")
        if metrics["drawings"] > 200:
            reasons.append("dense_vector_graphics")
        if metrics["numeric_citations"] > 10:
            reasons.append("many_numeric_citations")
        if metrics["citation_ranges"] > 1:
            reasons.append("complex_citation_ranges")
        if metrics["contents_pages"] > 0:
            reasons.append("contents_page_requires_toc_lane")
        row.update(metrics, eligible=not reasons, reasons=reasons)
        row["risk_tier"] = classify_risk(metrics)
        rows.append(row)
    candidates = [row for row in rows if row["eligible"]]
    candidates.sort(key=lambda row: (row["pages"], row["images"] + row["drawings"], row["reference_pages"], row["record_id"]))
    return {
        "schema_version": 3,
        "purpose": "zero_cost_low_layout_risk_pilot_prefilter",
        "policy": {
            "figure_interior_text": "source_verbatim",
            "captions_outside_figures": "translatable",
            "thresholds": {"pages_max": 10, "reference_pages_max": 2, "images_max": 12, "drawings_max": 200, "numeric_citations_max": 10, "citation_ranges_max": 1, "contents_pages_max": 0},
            "risk_tiers": {
                "low_risk": "the existing eligible thresholds",
                "text_only_medium": {"pages_max": 20, "images": 0, "drawings_max": 50, "numeric_citations_max": 80, "citation_ranges_max": 5, "reference_pages_max": 3, "contents_pages_max": 0},
                "figure_passthrough_medium": {"pages_max": 20, "images_max": 2, "drawings_max": 500, "numeric_citations_max": 80, "citation_ranges_max": 5, "reference_pages_max": 3, "contents_pages_max": 0, "figure_text": "source_verbatim"},
                "text_only_long": {"pages_max": 40, "images": 0, "drawings": 0, "numeric_citations_max": 150, "citation_ranges_max": 10, "reference_pages_max": 5, "contents_pages_max": 0},
                "vector_passthrough_long": {"pages_max": 30, "images": 0, "drawings_max": 500, "numeric_citations_max": 150, "citation_ranges_max": 10, "reference_pages_max": 5, "contents_pages_max": 0, "figure_text": "source_verbatim"},
            },
        },
        "eligible_count": sum(1 for row in rows if row.get("eligible")),
        "publication_allowed_count": len(rows),
        "candidates": candidates,
        "candidates_by_risk_tier": {
            tier: [row for row in rows if row.get("risk_tier") == tier]
            for tier in ("low_risk", "text_only_medium", "figure_passthrough_medium", "text_only_long", "vector_passthrough_long")
        },
        "records": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rights", type=Path, default=Path("site/data/papers.json"))
    parser.add_argument("--source-manifest", type=Path, default=Path("output/snowmass2021_sources/manifest.json"))
    parser.add_argument("--pdf-root", type=Path, default=Path("output/snowmass2021_sources"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = prefilter(rights_path=args.rights, source_manifest_path=args.source_manifest, pdf_root=args.pdf_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"eligible_count": result["eligible_count"], "publication_allowed_count": result["publication_allowed_count"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
