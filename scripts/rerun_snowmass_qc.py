#!/usr/bin/env python3
"""Re-run per-paper PDF QC from existing translation receipts without API work."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


PINNED_PYTHON = Path(
    "/Users/Zhuanz/.local/share/snowmass-tools/pdf2zh-next-2.9.0/bin/python"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    for paper in plan["papers"]:
        article = Path(paper["article_dir"])
        run = Path(paper["run_dir"])
        finish_path = run / "finish.json"
        if not finish_path.is_file():
            print(f"{paper['record_id']} SKIP no finish", flush=True)
            continue
        finish = json.loads(finish_path.read_text(encoding="utf-8"))
        raw = run / "rendered" / str(finish["outputs"]["mono_pdf"]["path"])
        command = [
            str(PINNED_PYTHON),
            "scripts/seal_snowmass_pdf2zh_next_paper.py",
            "prepare",
            "--article", str(article),
            "--source-pdf", str(paper["source_pdf"]),
            "--raw-pdf", str(raw),
            "--glossary-csv", str(run / "locked-glossary.csv"),
            "--pages", str(paper["pages"]),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        print(f"{paper['record_id']} rc={result.returncode}", flush=True)
        if result.stdout:
            print(result.stdout[-600:], flush=True)
        if result.stderr:
            print(f"ERR {result.stderr[-600:]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
