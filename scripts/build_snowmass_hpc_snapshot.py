#!/usr/bin/env python3
"""Build an immutable, secret-free Snowmass HPC post-processing snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import package_snowmass_translation_pdf as packager
import prepare_snowmass_babeldoc as prepare
import snowmass_qc_contract as qc_contract
import snowmass_production_contract as production_contract
from run_snowmass_batch_production import load_publication_records


EXCLUDED_DIRS = {".babeldoc-refill-work", ".babeldoc-render-work", "rendered", "packaged"}
EXCLUDED_FILES = {"babeldoc_ir.json", "babeldoc_translated_ir.xml", "refill_status.json"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def chinese_title(article_dir: Path, manifest: dict) -> str:
    for chunk in sorted(manifest["chunks"], key=lambda item: item.get("order", 0)):
        if chunk.get("layout_label") == "title":
            text = (article_dir / chunk["output_file"]).read_text(encoding="utf-8").strip()
            if text:
                return " ".join(text.split())
    raise RuntimeError(f"translated title missing: {article_dir}")


def copy_article(source: Path, destination: Path) -> dict:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(*EXCLUDED_DIRS, *EXCLUDED_FILES),
    )
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_pdf = Path(manifest["source_pdf_path"])
    if not source_pdf.is_absolute():
        source_pdf = ROOT / source_pdf
    target_pdf = destination / "source.pdf"
    shutil.copy2(source_pdf, target_pdf)
    manifest["source_pdf_path"] = str(target_pdf.relative_to(destination.parents[1]))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--article-root", type=Path, default=ROOT / "output/snowmass2021/babeldoc_production/papers")
    parser.add_argument("--rights-manifest", type=Path, default=ROOT / "site/data/papers.json")
    parser.add_argument("--record-id", action="append", required=True)
    parser.add_argument("--translation-version", default="v3.0")
    parser.add_argument("--packaged-on", required=True)
    parser.add_argument("--cjk-font", type=Path, default=packager.SYSTEM_CJK_FONT)
    args = parser.parse_args(argv)

    allowed = {record["record_id"]: record for record in load_publication_records(args.rights_manifest)}
    missing = sorted(set(args.record_id) - set(allowed))
    if missing:
        raise RuntimeError("records outside publication rights gate: " + ", ".join(missing))
    if args.output_root.exists():
        raise FileExistsError(f"snapshot output already exists: {args.output_root}")
    root = args.output_root.resolve()
    (root / "papers").mkdir(parents=True)
    (root / "assets").mkdir()
    shutil.copy2(args.rights_manifest, root / "papers.json")
    shutil.copy2(ROOT / "site/assets/snowmass-mountain.png", root / "assets/mountain.png")
    shutil.copyfile(args.cjk_font, root / "assets/cjk-font.ttc")
    tasks = []
    for record_id in args.record_id:
        safe_id = prepare.safe_record_name(record_id)
        source_article = args.article_root / safe_id
        artifact_report = production_contract.validate_artifact_manifest(
            source_article / "production_artifacts.json",
            article_root=source_article,
            rights_manifest_path=args.rights_manifest,
        )
        if not artifact_report["ok"]:
            raise RuntimeError(
                f"HPC snapshot artifact contract failed for {record_id}: "
                + ", ".join(artifact_report["errors"])
            )
        environment_lock_sha256 = artifact_report["manifest"]["environment_lock_sha256"]
        source_qc_paths = [source_article / "qc" / f"{kind}.json" for kind in qc_contract.ALLOWED_KINDS]
        qc_gate = qc_contract.validate_publishability_receipts(
            source_qc_paths,
            article_root=source_article,
            expected_record_id=record_id,
            current_environment_lock_sha256=environment_lock_sha256,
            required_contract_version=1,
        )
        if not qc_gate["publishable"]:
            raise RuntimeError(
                f"HPC snapshot QC receipt gate failed for {record_id}: "
                + ", ".join(qc_gate["errors"])
            )
        qc_receipts = {
            report["kind"]: report["receipt"]["receipt_hash"] for report in qc_gate["receipts"]
        }
        destination = root / "papers" / safe_id
        manifest = copy_article(source_article, destination)
        qr_path = root / "assets" / f"{safe_id}.qr.png"
        packager._generate_paper_qr(packager._translation_page_url(allowed[record_id]), qr_path)
        tasks.append({
            "record_id": record_id,
            "record": allowed[record_id],
            "article_dir": f"papers/{safe_id}",
            "rights_manifest": "papers.json",
            "chinese_title": chinese_title(destination, manifest),
            "output_pdf": f"papers/{safe_id}/packaged/snowmass-{safe_id.removeprefix('arxiv_')}.zh-CN.pdf",
            "translation_version": args.translation_version,
            "packaged_on": args.packaged_on,
            "mountain_asset": "assets/mountain.png",
            "cjk_font": "assets/cjk-font.ttc",
            "paper_qr": f"assets/{safe_id}.qr.png",
            "qc_receipt_hashes": qc_receipts,
            "environment_lock_sha256": environment_lock_sha256,
        })
    tracked = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        tracked.append({"path": str(path.relative_to(root)), "sha256": sha256(path)})
    payload = {"schema_version": 1, "task_count": len(tasks), "files": tracked, "tasks": tasks}
    (root / "tasks.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
