#!/usr/bin/env python3
"""Run one immutable, rights-gated Snowmass post-processing task on HPC."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import package_snowmass_translation_pdf as packager
import refill_snowmass_babeldoc as refill
from run_snowmass_batch_production import evaluate_article_qc


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise RuntimeError("snapshot path must be non-empty and relative")
    resolved_root = Path(root).resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise RuntimeError(f"snapshot path escapes root: {relative}") from error
    return resolved


def load_task_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise RuntimeError("unsupported HPC task manifest schema")
    if not isinstance(manifest.get("tasks"), list) or not manifest["tasks"]:
        raise RuntimeError("HPC task manifest must contain a non-empty task list")
    return manifest


def select_task(manifest: dict[str, Any], index: int) -> dict[str, Any]:
    tasks = manifest["tasks"]
    if index < 1 or index > len(tasks):
        raise ValueError(f"task index must be between 1 and {len(tasks)}")
    return tasks[index - 1]


def verify_snapshot(root: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest.get("files", []):
        path = safe_path(root, str(entry.get("path") or ""))
        if not path.is_file():
            raise RuntimeError(f"snapshot file missing: {entry.get('path')}")
        if sha256(path) != entry.get("sha256"):
            raise RuntimeError(f"snapshot hash mismatch: {entry.get('path')}")


def run_task(root: Path, manifest: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    article_dir = safe_path(root, task["article_dir"])
    rights_manifest = safe_path(root, task["rights_manifest"])
    article_manifest_path = article_dir / "manifest.json"
    article_manifest = json.loads(article_manifest_path.read_text(encoding="utf-8"))
    article_manifest["source_pdf_path"] = str((article_dir / "source.pdf").resolve())
    article_manifest_path.write_text(
        json.dumps(article_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if refill.main(["--article-dir", str(article_dir), "--rights-manifest", str(rights_manifest)]) != 0:
        raise RuntimeError("BabelDOC refill/render failed")
    qc = evaluate_article_qc(article_dir)
    if not qc["ok"]:
        raise RuntimeError("publication QC failed: " + ", ".join(qc["failures"]))
    output = safe_path(root, task["output_pdf"])
    receipt = packager.package_translation_pdf(
        record=task["record"],
        chinese_title=task["chinese_title"],
        source_pdf_path=article_dir / "rendered/translated_mono.pdf",
        output_pdf_path=output,
        version=task["translation_version"],
        packaged_on=task["packaged_on"],
        mountain_svg_path=safe_path(root, task["mountain_asset"]),
        cjk_font_path=safe_path(root, task["cjk_font"]),
        paper_qr_image_path=safe_path(root, task["paper_qr"]),
    )
    result = {"record_id": task["record_id"], "status": "complete", "qc": qc, "receipt": receipt}
    result_path = output.with_suffix(".hpc-result.json")
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--task-manifest", type=Path, required=True)
    parser.add_argument("--task-index", type=int, default=int(os.environ.get("SLURM_ARRAY_TASK_ID", "0")))
    args = parser.parse_args(argv)
    manifest = load_task_manifest(args.task_manifest)
    verify_snapshot(args.snapshot_root, manifest)
    run_task(args.snapshot_root, manifest, select_task(manifest, args.task_index))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
