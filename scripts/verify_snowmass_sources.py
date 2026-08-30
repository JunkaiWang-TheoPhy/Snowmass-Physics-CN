#!/usr/bin/env python3
"""Verify the local Snowmass PDF/e-print corpus and quarantine stale PDF parts."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path("output/snowmass2021_sources")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def move_stale_pdf_part(path: Path) -> str:
    relative = path.relative_to(ROOT / "papers")
    item_dir = relative.parent
    quarantine_dir = ROOT / "quarantine_pdf_responses" / item_dir
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    destination = quarantine_dir / "source.pdf-response.part.pdf"
    if destination.exists():
        index = 2
        while True:
            candidate = quarantine_dir / f"source.pdf-response.part.{index}.pdf"
            if not candidate.exists():
                destination = candidate
                break
            index += 1
    shutil.move(str(path), str(destination))
    return str(destination.relative_to(ROOT))


def pdf_check(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "ok": False, "error": "missing"}
    try:
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                return {"path": str(path), "ok": False, "error": "not_pdf"}
        result = subprocess.run(
            ["pdfinfo", str(path)], capture_output=True, text=True, timeout=180, check=False
        )
        if result.returncode != 0:
            return {"path": str(path), "ok": False, "error": result.stderr.strip()[-500:]}
        pages = None
        for line in result.stdout.splitlines():
            if line.startswith("Pages:"):
                pages = int(line.split(":", 1)[1].strip())
                break
        if pages is None or pages <= 0:
            return {"path": str(path), "ok": False, "error": "invalid_page_count", "pages": pages}
        # pdfinfo validates the trailer but may not dereference malformed
        # page objects. Exercise every page through MuPDF too, because the
        # production protection/QC path uses the same object graph.
        try:
            import fitz

            with fitz.open(path) as document:
                if len(document) != pages:
                    return {
                        "path": str(path),
                        "ok": False,
                        "error": f"mupdf_page_count_mismatch:{len(document)}!={pages}",
                        "pages": pages,
                    }
                for page in document:
                    page.get_text("text")
        except Exception as exc:  # noqa: BLE001 - report corrupt page objects
            return {"path": str(path), "ok": False, "error": f"mupdf:{type(exc).__name__}:{exc}", "pages": pages}
        return {"path": str(path), "ok": True, "pages": pages, "mupdf_checked": True}
    except Exception as exc:  # noqa: BLE001 - verifier must report, not abort
        return {"path": str(path), "ok": False, "error": repr(exc)}


def archive_check(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "ok": False, "error": "missing"}
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic[:2] != b"\x1f\x8b":
            return {"path": str(path), "ok": False, "error": f"unexpected_magic:{magic.hex()}"}
        result = subprocess.run(
            ["gzip", "-t", str(path)], capture_output=True, text=True, timeout=300, check=False
        )
        if result.returncode != 0:
            return {"path": str(path), "ok": False, "error": result.stderr.strip()[-500:]}
        return {"path": str(path), "ok": True, "format": "gzip"}
    except Exception as exc:  # noqa: BLE001 - verifier must report, not abort
        return {"path": str(path), "ok": False, "error": repr(exc)}


def main() -> int:
    manifest_path = ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest["records"]
    quarantined: list[str] = []

    for part in (ROOT / "papers").rglob("*.part"):
        try:
            with part.open("rb") as stream:
                is_pdf = stream.read(5) == b"%PDF-"
        except OSError:
            is_pdf = False
        if is_pdf:
            quarantined.append(move_stale_pdf_part(part))

    pdf_paths = [ROOT / record["pdf_path"] for record in records]
    archive_paths = [
        ROOT / record["directory"] / "source.tar.gz"
        for record in records
        if record.get("archive_status") == "complete"
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        pdf_results = list(executor.map(pdf_check, pdf_paths))
        archive_results = list(executor.map(archive_check, archive_paths))

    status_mismatches: list[dict[str, str]] = []
    for record, pdf_result in zip(records, pdf_results):
        status_path = ROOT / record["directory"] / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        pdf_path = ROOT / status["pdf_path"]
        if status.get("pdf_status") != "complete" or not pdf_result["ok"]:
            status_mismatches.append({"record_id": status["record_id"], "error": "PDF status/structure"})
        elif status.get("pdf_bytes") != pdf_path.stat().st_size or status.get("pdf_sha256") != sha256(pdf_path):
            status_mismatches.append({"record_id": status["record_id"], "error": "PDF hash/size mismatch"})
        if status.get("archive_status") == "not_available" and (ROOT / status["directory"] / "source.tar.gz").exists():
            status_mismatches.append({"record_id": status["record_id"], "error": "archive marked unavailable but present"})

    archive_failures = [result for result in archive_results if not result["ok"]]
    pdf_failures = [result for result in pdf_results if not result["ok"]]
    leftovers = [str(path.relative_to(ROOT)) for path in ROOT.rglob("*.part")]
    result = {
        "verified_at": now(),
        "records": len(records),
        "pdf_files": len(pdf_results),
        "pdf_failures": pdf_failures,
        "archives_expected": len(archive_paths),
        "archive_failures": archive_failures,
        "status_mismatches": status_mismatches,
        "quarantined_stale_pdf_parts": quarantined,
        "remaining_part_files": leftovers,
        "ok": not pdf_failures and not archive_failures and not status_mismatches and not leftovers,
    }
    output = ROOT / "verification.json"
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    print(json.dumps({key: value for key, value in result.items() if key not in {"pdf_failures", "archive_failures", "status_mismatches", "quarantined_stale_pdf_parts"}}, ensure_ascii=False, indent=2))
    if pdf_failures or archive_failures or status_mismatches or leftovers:
        print(f"FAIL pdf={len(pdf_failures)} archive={len(archive_failures)} status={len(status_mismatches)} part={len(leftovers)}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
