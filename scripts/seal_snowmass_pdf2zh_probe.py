#!/usr/bin/env python3
"""Seal a complete pdf2zh-next probe only when every receipt hash agrees."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Receipt is not a JSON object: {Path(path).name}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _positive_cap(value: Any, maximum: float, label: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > maximum:
        raise RuntimeError(f"{label} must be finite, positive, and <= {maximum}")
    return result


def seal_probe(
    *,
    finish_path: Path,
    ir_receipt_path: Path,
    protection_path: Path,
    semantic_path: Path,
    audit_path: Path,
    visual_path: Path,
) -> dict[str, Any]:
    finish = _load(finish_path)
    ir = _load(ir_receipt_path)
    protection = _load(protection_path)
    semantic = _load(semantic_path)
    audit = _load(audit_path)
    visual = _load(visual_path)
    if finish.get("status") != "translated_pending_qc":
        raise RuntimeError("translation finish receipt is not pending QC")
    budget = finish.get("budget") or {}
    _positive_cap(budget.get("project_max_cost_rmb"), 1000.0, "project budget")
    _positive_cap(budget.get("stage_max_cost_rmb"), 100.0, "stage budget")
    _positive_cap(budget.get("stage_max_api_calls"), 100000.0, "request cap")
    source_hash = str((finish.get("source") or {}).get("sha256") or "")
    raw_hash = str(
        (((finish.get("outputs") or {}).get("mono_pdf") or {}).get("sha256")) or ""
    )
    final_hash = str(protection.get("output_pdf_sha256") or "")
    if len(source_hash) != 64 or len(raw_hash) != 64 or len(final_hash) != 64:
        raise RuntimeError("probe hash chain contains an invalid SHA-256")
    if ir.get("zero_paid") is not True or ir.get("source_pdf_sha256") != source_hash:
        raise RuntimeError("zero-paid IR source hash does not match translation source")
    if protection.get("verified") is not True:
        raise RuntimeError("PDF protection receipt is not verified")
    if protection.get("source_pdf_sha256") != source_hash:
        raise RuntimeError("protection source hash does not match translation source")
    if protection.get("translated_pdf_sha256") != raw_hash:
        raise RuntimeError("protection input hash does not match raw translated PDF")
    if semantic.get("ok") is not True or semantic.get("failures"):
        raise RuntimeError("semantic PDF audit did not pass cleanly")
    if semantic.get("pdf_sha256") != final_hash:
        raise RuntimeError("semantic audit PDF hash does not match protected PDF")
    if audit.get("ok") is not True or audit.get("failures"):
        raise RuntimeError("structural PDF audit did not pass cleanly")
    if audit.get("pdf_sha256") != final_hash:
        raise RuntimeError("structural audit PDF hash does not match protected PDF")
    threshold = float(visual.get("threshold", 90))
    if visual.get("verdict") != "pass" or float(visual.get("score", 0)) < threshold:
        raise RuntimeError("visual verdict did not meet its threshold")
    if visual.get("pdf_sha256") != final_hash:
        raise RuntimeError("visual receipt PDF hash does not match protected PDF hash")
    receipt_paths = {
        "translation": Path(finish_path),
        "ir": Path(ir_receipt_path),
        "protection": Path(protection_path),
        "semantic": Path(semantic_path),
        "structural": Path(audit_path),
        "visual": Path(visual_path),
    }
    return {
        "schema_version": 1,
        "passed": True,
        "source_pdf_sha256": source_hash,
        "raw_translated_pdf_sha256": raw_hash,
        "protected_pdf_sha256": final_hash,
        "visual_score": float(visual["score"]),
        "visual_threshold": threshold,
        "receipt_hashes": {
            label: _sha256(path) for label, path in receipt_paths.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finish", type=Path, required=True)
    parser.add_argument("--ir", type=Path, required=True)
    parser.add_argument("--protection", type=Path, required=True)
    parser.add_argument("--semantic", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--visual", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = seal_probe(
        finish_path=args.finish,
        ir_receipt_path=args.ir,
        protection_path=args.protection,
        semantic_path=args.semantic,
        audit_path=args.audit,
        visual_path=args.visual,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
