#!/usr/bin/env python3
"""Fail closed on locked-term residue and known mistranslations in translated prose."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import fitz

DEFAULT_FORBIDDEN = ("偏袒", "天文物體")
REFERENCE_HEADING = re.compile(
    r"^\s*(?:References?|Bibliography|参考文献|文献)\s*[:：]?\s*$",
    re.IGNORECASE,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _ignored_regions(receipt_path: Path) -> dict[int, list[fitz.Rect]]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("verified") is not True:
        raise RuntimeError("protection receipt must be verified")
    regions: dict[int, list[fitz.Rect]] = {}
    for item in receipt.get("protected_regions", []):
        regions.setdefault(int(item["output_page"]), []).append(
            fitz.Rect(*map(float, item["bbox"]))
        )
    return regions


def audit_semantics(
    pdf_path: Path,
    *,
    glossary_csv: Path,
    protection_receipt: Path,
    forbidden: tuple[str, ...] = DEFAULT_FORBIDDEN,
    allowed_untranslated: tuple[str, ...] = (),
    allowed_untranslated_phrases: tuple[str, ...] = (),
) -> dict[str, Any]:
    ignored = _ignored_regions(protection_receipt)
    with glossary_csv.open(encoding="utf-8", newline="") as stream:
        glossary_rows = list(csv.DictReader(stream))
    allowed = {" ".join(value.casefold().split()) for value in allowed_untranslated}
    source_terms = tuple(
        row["source"].strip()
        for row in glossary_rows
        if row.get("source")
        and row.get("target")
        and row["source"].strip().casefold() != row["target"].strip().casefold()
        and re.search(r"[A-Za-z]", row["source"])
        and " ".join(row["source"].casefold().split()) not in allowed
    )
    failures: list[str] = []
    findings: list[dict[str, Any]] = []
    with fitz.open(pdf_path) as document:
        for page_number, page in enumerate(document, 1):
            blocks = page.get_text("blocks", sort=True)
            reference_y: float | None = None
            for block in blocks:
                block_text = str(block[4]).replace("\x03", " ").strip()
                if REFERENCE_HEADING.fullmatch(block_text):
                    reference_y = float(block[1]) if reference_y is None else min(reference_y, float(block[1]))
            for block in blocks:
                rectangle = fitz.Rect(*block[:4])
                center = ((rectangle.x0 + rectangle.x1) / 2, (rectangle.y0 + rectangle.y1) / 2)
                if any(region.contains(center) for region in ignored.get(page_number, [])):
                    continue
                text = str(block[4]).replace("\x03", " ")
                allowed_ranges = [
                    match.span()
                    for phrase in allowed_untranslated_phrases
                    if phrase
                    for match in re.finditer(
                        r"\s+".join(re.escape(part) for part in phrase.split()),
                        text,
                        re.IGNORECASE,
                    )
                ]
                checks = [("forbidden", value) for value in forbidden]
                if reference_y is None or rectangle.y0 < reference_y:
                    checks.extend(("untranslated_glossary", value) for value in source_terms)
                for kind, value in checks:
                    if not value:
                        continue
                    flags = re.IGNORECASE if re.search(r"[A-Za-z]", value) else 0
                    matches = list(
                        re.finditer(
                            rf"(?<![A-Za-z]){re.escape(value)}(?![A-Za-z])",
                            text,
                            flags,
                        )
                    )
                    if kind == "untranslated_glossary":
                        matches = [
                            match
                            for match in matches
                            if not any(
                                start <= match.start() and match.end() <= end
                                for start, end in allowed_ranges
                            )
                        ]
                    if matches:
                        findings.append(
                            {
                                "page": page_number,
                                "kind": kind,
                                "term": value,
                                "bbox": list(rectangle),
                            }
                        )
                        failures.append(f"{kind}:{value}:page_{page_number}")
    return {
        "schema_version": 1,
        "pdf_path": pdf_path.name,
        "pdf_sha256": _sha256(pdf_path),
        "glossary_sha256": _sha256(glossary_csv),
        "protection_receipt_sha256": _sha256(protection_receipt),
        "checked_glossary_terms": len(source_terms),
        "forbidden_terms": list(forbidden),
        "allowed_untranslated_terms": list(allowed_untranslated),
        "allowed_untranslated_phrases": list(allowed_untranslated_phrases),
        "findings": findings,
        "failures": list(dict.fromkeys(failures)),
        "ok": not failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--glossary-csv", type=Path, required=True)
    parser.add_argument("--protection-receipt", type=Path, required=True)
    parser.add_argument("--forbidden", action="append", default=[])
    parser.add_argument("--allow-untranslated", action="append", default=[])
    parser.add_argument("--allow-untranslated-phrase", action="append", default=[])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit_semantics(
        args.pdf,
        glossary_csv=args.glossary_csv,
        protection_receipt=args.protection_receipt,
        forbidden=tuple(dict.fromkeys((*DEFAULT_FORBIDDEN, *args.forbidden))),
        allowed_untranslated=tuple(dict.fromkeys(args.allow_untranslated)),
        allowed_untranslated_phrases=tuple(
            dict.fromkeys(args.allow_untranslated_phrase)
        ),
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
