#!/usr/bin/env python3
"""Run fail-closed visual/residue QA across packaged Snowmass translations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

try:
    from audit_snowmass_translation_pdf import audit_pdf
    from prepare_snowmass_babeldoc import safe_record_name
except ModuleNotFoundError:
    from scripts.audit_snowmass_translation_pdf import audit_pdf
    from scripts.prepare_snowmass_babeldoc import safe_record_name


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "site/data/papers.json"
DEFAULT_OUTPUT_ROOT = ROOT / "output/snowmass2021/babeldoc_production"
DEFAULT_QA_ROOT = ROOT / "output/snowmass2021/pdf_qa"


def _packaged_pdf(output_root: Path, record_id: str) -> Path:
    safe_name = safe_record_name(record_id)
    suffix = safe_name.removeprefix("arxiv_")
    return output_root / "papers" / safe_name / "packaged" / f"snowmass-{suffix}.zh-CN.pdf"


def audit_batch(
    records: Iterable[dict[str, Any]],
    *,
    output_root: str | Path,
    qa_root: str | Path,
) -> dict[str, Any]:
    records = list(records)
    output_root = Path(output_root)
    qa_root = Path(qa_root)
    results: list[dict[str, Any]] = []
    failures: list[str] = ["empty_batch"] if not records else []
    safe_names: dict[str, str] = {}
    for record in records:
        record_id = str(record.get("record_id") or "")
        safe_name = safe_record_name(record_id) if record_id else ""
        if not safe_name or safe_name in {".", ".."} or Path(safe_name).name != safe_name:
            failures.append(f"unsafe_record_path:{record_id}")
            continue
        previous = safe_names.get(safe_name)
        if previous is not None and previous != record_id:
            failures.append(f"record_path_collision:{previous}:{record_id}:{safe_name}")
        else:
            safe_names[safe_name] = record_id
    for record in records:
        record_id = str(record.get("record_id") or "")
        safe_name = safe_record_name(record_id) if record_id else "missing_record_id"
        if f"unsafe_record_path:{record_id}" in failures:
            continue
        if any(
            failure.startswith("record_path_collision:")
            and f":{record_id}:" in failure
            for failure in failures
        ):
            continue
        if record.get("publication_allowed") is not True:
            failures.append(f"publication_not_allowed:{record_id}")
            continue
        page_count = record.get("page_count")
        if not isinstance(page_count, int) or page_count <= 0:
            failures.append(f"invalid_source_page_count:{record_id}")
            continue
        pdf = _packaged_pdf(output_root, record_id)
        if not pdf.is_file():
            failures.append(f"missing_packaged_pdf:{record_id}")
            continue
        article_qa = qa_root / safe_name
        report = audit_pdf(
            pdf,
            expected_pages=page_count + 1,
            contact_sheet_path=article_qa / "contact-sheet.jpg",
        )
        article_qa.mkdir(parents=True, exist_ok=True)
        (article_qa / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = {"record_id": record_id, **report}
        results.append(result)
        if not report.get("ok"):
            failures.extend(
                f"{record_id}:{failure}" for failure in report.get("failures", [])
            )
    return {
        "schema_version": 1,
        "selected": len(records),
        "audited": len(results),
        "passed": sum(bool(item.get("ok")) for item in results),
        "failed": len(records) - sum(bool(item.get("ok")) for item in results),
        "results": results,
        "failures": failures,
        "ok": not failures and all(bool(item.get("ok")) for item in results),
    }


def _load_selected_records(manifest: Path, record_ids: list[str]) -> list[dict[str, Any]]:
    records = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("rights manifest must be a JSON list")
    by_id = {
        str(record.get("record_id")): record
        for record in records
        if isinstance(record, dict) and record.get("record_id")
    }
    missing = [record_id for record_id in record_ids if record_id not in by_id]
    if missing:
        raise ValueError("records missing from rights manifest: " + ", ".join(missing))
    return [by_id[record_id] for record_id in record_ids]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rights-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--qa-root", type=Path, default=DEFAULT_QA_ROOT)
    parser.add_argument("--record-id", action="append", required=True)
    parser.add_argument("--summary", type=Path)
    arguments = parser.parse_args(argv)
    records = _load_selected_records(arguments.rights_manifest, arguments.record_id)
    report = audit_batch(records, output_root=arguments.output_root, qa_root=arguments.qa_root)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if arguments.summary is not None:
        arguments.summary.parent.mkdir(parents=True, exist_ok=True)
        arguments.summary.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
