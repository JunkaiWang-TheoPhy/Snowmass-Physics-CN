#!/usr/bin/env python3
"""Build the redacted, deterministic manifest consumed by the public site.

The rights manifest is the source of truth for license and publication
decisions.  Citation, year, and PDF-length data are optional enrichments; a
missing enrichment is represented as JSON null rather than inferred.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RIGHTS = ROOT / "output/snowmass2021/rights/snowmass2021_rights_manifest.json"
DEFAULT_CATALOG = ROOT / "output/snowmass2021/snowmass2021_whitepapers.json"
DEFAULT_ANALYSIS = ROOT / "output/snowmass2021/analysis/enriched_papers.json"
DEFAULT_LENGTHS = ROOT / "output/snowmass2021/analysis/length_records.json"
DEFAULT_TRANSLATIONS = ROOT / "translations/snowmass-publications.json"
DEFAULT_OUT_DIR = ROOT / "site/data"


ADAPTATION_LICENSES = {
    "CC-BY-4.0",
    "CC-BY-SA-4.0",
    "CC-BY-NC-SA-4.0",
    "CC0-1.0",
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _by_record_id(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        key = str(record.get("record_id", "")).strip().casefold()
        if not key:
            continue
        if key in result:
            raise ValueError(f"duplicate record_id: {record['record_id']}")
        result[key] = record
    return result


def _as_number(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _source_conditions(source_license: str | None) -> list[str]:
    conditions: list[str] = []
    if source_license and source_license != "CC0-1.0":
        conditions.extend(["attribution", "indicate-changes"])
    if source_license in {"CC-BY-SA-4.0", "CC-BY-NC-SA-4.0"}:
        conditions.append("share-alike")
    if source_license == "CC-BY-NC-SA-4.0":
        conditions.append("non-commercial")
    return conditions


def _rights_public_state(rights: dict[str, Any]) -> dict[str, Any]:
    source_license = rights.get("source_license")
    permits_adaptation = rights.get("permits_adaptation")
    decision = rights.get("license_decision") or "manual-review"
    if permits_adaptation is True and source_license in ADAPTATION_LICENSES:
        authorization_status = "license-cleared"
        publication_allowed = True
        publication_basis = "source-license"
        conditions = _source_conditions(source_license)
    else:
        authorization_status = "needs-permission"
        publication_allowed = False
        publication_basis = "manual-hold"
        conditions = ["written-rightsholder-permission-required"]
        if decision == "hold-no-public-adaptation":
            conditions = ["no-public-adaptation-under-current-source-license"]
        elif decision == "hold-hal-authorization-manual-review":
            conditions = ["HAL-distribution-authorization-is-not-adaptation-permission"]
        elif decision == "hold-manual-license-review":
            conditions = ["verify-current-rightsholder-and-license-before-publication"]

    return {
        "authorization_status": authorization_status,
        "publication_allowed": publication_allowed,
        "publication_basis": publication_basis,
        "publication_conditions": conditions,
    }


def _safe_public_record(
    rights: dict[str, Any],
    catalog: dict[str, Any],
    analysis: dict[str, Any],
    lengths: dict[str, Any],
    translation: dict[str, Any],
) -> dict[str, Any]:
    state = _rights_public_state(rights)
    record_id = rights["record_id"]
    title = rights.get("title") or catalog.get("title") or analysis.get("title") or lengths.get("title") or record_id

    return {
        "paper_id": rights.get("paper_id", record_id),
        "record_id": record_id,
        "title": title,
        "authors_as_listed": rights.get("authors_as_listed") or catalog.get("authors_as_listed") or "",
        "frontiers": rights.get("frontiers") or catalog.get("frontiers") or [],
        "topics": rights.get("topics") or catalog.get("topics") or [],
        "source_url": rights.get("source_url") or catalog.get("external_url"),
        "source_version": rights.get("source_version"),
        "source_license": rights.get("source_license"),
        "source_license_url": rights.get("source_license_url"),
        "permits_adaptation": rights.get("permits_adaptation"),
        "license_decision": rights.get("license_decision"),
        "translation_status": translation.get("translation_status") or rights.get("translation_status", "not-started"),
        "translation_license": translation.get("translation_license") or rights.get("translation_license"),
        "machine_model": translation.get("machine_model") or rights.get("machine_model"),
        "human_reviewers": translation.get("human_reviewers") or rights.get("human_reviewers") or [],
        **state,
        "publication_translation_url": translation.get("publication_translation_url"),
        "publication_translation_sha256": translation.get("publication_translation_sha256"),
        "publication_translation_size_bytes": translation.get("publication_translation_size_bytes"),
        "translation_version": translation.get("translation_version"),
        "translation_published_at": translation.get("translation_published_at"),
        "public_updated_at": rights.get("license_checked_at"),
        "publication_year": analysis.get("publication_year"),
        "citation_count": _as_number(analysis.get("citation_count")),
        "citation_count_without_self_citations": _as_number(analysis.get("citation_count_without_self_citations")),
        "citations_per_year": _as_number(analysis.get("citations_per_year")),
        "impact_proxy_score_0_100": _as_number(analysis.get("impact_proxy_score_0_100")),
        "page_count": _as_number(lengths.get("page_count")),
        "unicode_token_count": _as_number(lengths.get("unicode_token_count")),
        "frontier_labels": analysis.get("frontier_labels") or [],
        "primary_arxiv_category": analysis.get("primary_arxiv_category") or None,
    }


def _percentile_summary(values: list[float | int]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "total": 0, "mean": None, "median": None, "p25": None, "p75": None}
    ordered = sorted(values)
    return {
        "count": len(values),
        "total": sum(values),
        "mean": round(statistics.mean(values), 2),
        "median": statistics.median(values),
        "p25": ordered[max(0, int(round((len(ordered) - 1) * 0.25)))],
        "p75": ordered[max(0, int(round((len(ordered) - 1) * 0.75)))],
    }


def _build_stats(records: list[dict[str, Any]], source_dates: list[str]) -> dict[str, Any]:
    frontier_counts: Counter[str] = Counter()
    year_counts: Counter[str] = Counter()
    license_counts: Counter[str] = Counter()
    authorization_counts: Counter[str] = Counter()
    translation_counts: Counter[str] = Counter()
    citation_values: list[float | int] = []
    page_values: list[float | int] = []
    token_values: list[float | int] = []

    for record in records:
        frontier_counts.update(record["frontiers"])
        year = record.get("publication_year")
        if year is not None:
            year_counts[str(year)] += 1
        license_counts[record["source_license"] or "unknown"] += 1
        authorization_counts[record["authorization_status"]] += 1
        translation_counts[record["translation_status"]] += 1
        if record.get("citation_count") is not None:
            citation_values.append(record["citation_count"])
        if record.get("page_count") is not None:
            page_values.append(record["page_count"])
        if record.get("unicode_token_count") is not None:
            token_values.append(record["unicode_token_count"])

    allowed = sum(bool(record["publication_allowed"]) for record in records)
    return {
        "schema_version": "1.0",
        "catalog_count": len(records),
        "source_snapshot_date": max(source_dates)[:10] if source_dates else None,
        "license_counts": dict(sorted(license_counts.items())),
        "authorization_counts": dict(sorted(authorization_counts.items())),
        "translation_counts": dict(sorted(translation_counts.items())),
        "publication_counts": {"allowed": allowed, "blocked_or_pending": len(records) - allowed},
        "frontier_counts": dict(sorted(frontier_counts.items())),
        "year_counts": dict(sorted(year_counts.items())),
        "page_summary": _percentile_summary(page_values),
        "citation_summary": _percentile_summary(citation_values),
        "unicode_token_summary": _percentile_summary(token_values),
        "data_notes": [
            "Citation fields are an INSPIRE-derived snapshot and are not an official impact ranking.",
            "Page and token fields are PDF extraction proxies that include references and extraction artifacts.",
            "Cross-Frontier placements are retained in each record; frontier counts are not mutually exclusive.",
        ],
    }


def build_manifest(
    rights_path: Path = DEFAULT_RIGHTS,
    catalog_path: Path = DEFAULT_CATALOG,
    analysis_path: Path = DEFAULT_ANALYSIS,
    lengths_path: Path = DEFAULT_LENGTHS,
    translations_path: Path = DEFAULT_TRANSLATIONS,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rights_records = _read_json(rights_path)
    catalog_records = _by_record_id(_read_json(catalog_path))
    analysis_records = _by_record_id(_read_json(analysis_path))
    length_records = _by_record_id(_read_json(lengths_path))
    translation_records = _by_record_id(_read_json(translations_path)) if translations_path.is_file() else {}

    source_dates: list[str] = []
    records: list[dict[str, Any]] = []
    for rights in rights_records:
        key = rights["record_id"].casefold()
        if rights.get("license_checked_at"):
            source_dates.append(rights["license_checked_at"])
        records.append(_safe_public_record(
            rights,
            catalog_records.get(key, {}),
            analysis_records.get(key, {}),
            length_records.get(key, {}),
            translation_records.get(key, {}),
        ))

    records.sort(key=lambda item: (item["title"].casefold(), item["record_id"].casefold()))
    stats = _build_stats(records, source_dates)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "papers.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return records, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rights", type=Path, default=DEFAULT_RIGHTS)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--analysis", type=Path, default=DEFAULT_ANALYSIS)
    parser.add_argument("--lengths", type=Path, default=DEFAULT_LENGTHS)
    parser.add_argument("--translations", type=Path, default=DEFAULT_TRANSLATIONS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    records, stats = build_manifest(
        rights_path=args.rights,
        catalog_path=args.catalog,
        analysis_path=args.analysis,
        lengths_path=args.lengths,
        translations_path=args.translations,
        out_dir=args.out_dir,
    )
    print(json.dumps({
        "records": len(records),
        "source_snapshot_date": stats["source_snapshot_date"],
        "publication_allowed": stats["publication_counts"]["allowed"],
        "needs_permission": stats["authorization_counts"].get("needs-permission", 0),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
