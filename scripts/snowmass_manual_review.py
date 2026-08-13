#!/usr/bin/env python3
"""Build a resumable queue for revision findings that require semantic review."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REVIEW_REASON = "revision_literal_rebinding_requires_manual_review"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect_manual_review_queue(output_root: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for status_path in sorted(Path(output_root).glob("papers/*/chunk_status/*.json")):
        status = json.loads(status_path.read_text(encoding="utf-8"))
        revision = status.get("stages", {}).get("revision", {})
        if not isinstance(revision, dict):
            continue
        if revision.get("decision", {}).get("reason") != REVIEW_REASON:
            continue
        article_dir = status_path.parent.parent
        chunk_id = str(status.get("chunk_id") or status_path.stem)
        source_file = str(status.get("source_file") or f"{chunk_id}.md")
        source = (article_dir / source_file).read_text(encoding="utf-8")
        terminology = (article_dir / f"stage2_{chunk_id}.md").read_text(encoding="utf-8")
        critique_path = article_dir / "04-critique.md"
        finding_prefix = re.compile(
            rf"^\s*(?:[-*+]\s*)?{re.escape(chunk_id)}\s*:",
            flags=re.I,
        )
        critique = [
            line
            for line in critique_path.read_text(encoding="utf-8").splitlines()
            if finding_prefix.match(line)
        ] if critique_path.is_file() else []
        items.append(
            {
                "record_id": str(status.get("record_id") or ""),
                "chunk_id": chunk_id,
                "source_file": source_file,
                "source_sha256": _sha256(source),
                "source": source,
                "terminology_draft": terminology,
                "terminology_draft_sha256": _sha256(terminology),
                "critique": critique,
                "resolution": None,
                "resolution_policy": (
                    "Add a source-exact rule to translations/snowmass-hard-constraints.json; "
                    "the next checkpointed run will recompile constraints and remove this item."
                ),
            }
        )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "unresolved_count": len(items),
        "items": items,
    }


def write_queue(output_root: Path, destination: Path) -> dict[str, Any]:
    queue = collect_manual_review_queue(output_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return queue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("output/snowmass2021/babeldoc_production"),
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("output/snowmass2021/production_control/manual-review-queue.json"),
    )
    args = parser.parse_args()
    queue = write_queue(args.output_root, args.destination)
    print(json.dumps({"destination": str(args.destination), "unresolved_count": queue["unresolved_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
