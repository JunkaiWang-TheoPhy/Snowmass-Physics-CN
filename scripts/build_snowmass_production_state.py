#!/usr/bin/env python3

"""Build a deterministic canonical state index for pdf2zh-next production runs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional


CURRENT_RUN_RE = re.compile(r"^pdf2zh_next_production_probe_current_v(\d+)$")
SEAL_FIELDS = ("paper-seal.json",)
QUARANTINE_FIELDS = ("quarantine.json", "launch-quarantine.json")
STATUS_FIELDS = ("finish.json", "status.json")
PLAN_FIELDS = ("plan.json",)


def _error(message: str) -> None:
    raise ValueError(message)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {path}") from exc


def _validate_record_id(value: Any) -> str:
    if not isinstance(value, str):
        _error("record_id must be a string")
    if value.strip() != value or not value:
        _error(f"malformed record_id: {value!r}")
    if value.startswith("arxiv:") and not value.split(":", 1)[1]:
        _error("empty arxiv record_id")
    return value


def _load_record_list(path: Path, *, label: str) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
        records = payload["records"]
    else:
        _error(f"{label} must be a JSON array or object with a records array")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for entry in records:
        if not isinstance(entry, dict):
            _error(f"{label} entries must be objects")
        record_id = _validate_record_id(entry.get("record_id"))
        if record_id in seen:
            _error(f"duplicate record_id: {record_id}")
        seen.add(record_id)
        copied = dict(entry)
        copied["record_id"] = record_id
        normalized.append(copied)
    return normalized


def _load_publication_registry(path: Optional[Path]) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    return {
        entry["record_id"]: entry
        for entry in _load_record_list(path, label="publication registry")
    }


def _load_source_prefilter(
    path: Optional[Path],
    *,
    rights_records: dict[str, dict[str, Any]],
) -> dict[str, str]:
    if path is None:
        return {}
    payload = _load_json(path)
    if not isinstance(payload, dict):
        _error("source prefilter must be a JSON object")
    if payload.get("schema_version") != 3:
        _error("source prefilter schema_version must be 3")
    tiers = payload.get("candidates_by_risk_tier")
    if not isinstance(tiers, dict):
        _error("source prefilter candidates_by_risk_tier must be an object")
    result: dict[str, str] = {}
    for tier, entries in sorted(tiers.items()):
        if not isinstance(tier, str) or not tier:
            _error("source prefilter risk tier must be a non-empty string")
        if not isinstance(entries, list):
            _error("source prefilter tier entries must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                _error("source prefilter entries must be objects")
            record_id = _validate_record_id(entry.get("record_id"))
            rights_entry = rights_records.get(record_id)
            if (
                rights_entry is None
                or rights_entry.get("publication_allowed") is not True
            ):
                _error(
                    f"source prefilter record_id is not rights-allowed: {record_id}"
                )
            prior_tier = result.get(record_id)
            if prior_tier is not None and prior_tier != tier:
                _error(f"source prefilter duplicate record_id: {record_id}")
            result[record_id] = tier
    return result


def _collect_run_dirs(runs_root: Path) -> list[tuple[int, Path]]:
    run_dirs: list[tuple[int, Path]] = []
    for child in sorted(runs_root.iterdir(), key=lambda item: item.name):
        match = CURRENT_RUN_RE.fullmatch(child.name)
        if match and child.is_dir():
            run_dirs.append((int(match.group(1)), child))
    return run_dirs


def _safe_relpath(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path, base)).as_posix()


def _path_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _load_plan_records(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        _error(f"plan must be a JSON object: {path}")
    stage = payload.get("stage")
    if not isinstance(stage, str) or not stage:
        _error(f"plan stage must be a non-empty string: {path}")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    record_ids = payload.get("record_ids", [])
    if record_ids is None:
        record_ids = []
    if not isinstance(record_ids, list):
        _error(f"plan record_ids must be a list: {path}")
    for value in record_ids:
        record_id = _validate_record_id(value)
        if record_id in seen:
            continue
        seen.add(record_id)
        normalized.append({"record_id": record_id, "stage": stage, "path": path})
    papers = payload.get("papers", [])
    if papers is None:
        papers = []
    if not isinstance(papers, list):
        _error(f"plan papers must be a list: {path}")
    for paper in papers:
        if not isinstance(paper, dict):
            _error(f"plan papers entries must be objects: {path}")
        record_id = _validate_record_id(paper.get("record_id"))
        if record_id in seen:
            continue
        seen.add(record_id)
        normalized.append({"record_id": record_id, "stage": stage, "path": path})
    return normalized


def _resolve_publication_receipt_path(
    entry: dict[str, Any],
    *,
    registry_path: Path,
    repo_root: Path,
    registry_hash: str,
) -> Optional[Path]:
    for key in ("publication_receipt_path", "published_receipt_path", "receipt_path"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            candidate = (registry_path.parent / value).resolve()
            if not _path_within_root(candidate, repo_root):
                _error("publication receipt path escapes repo root")
            return candidate
    record_id = entry["record_id"]
    short_id = record_id.split(":", 1)[1] if ":" in record_id else record_id
    receipts_root = repo_root / "site" / "pdfs" / "arxiv" / short_id
    if not receipts_root.is_dir():
        return None
    matches: list[Path] = []
    pattern = f"snowmass-{short_id}.zh-CN.json"
    for candidate in sorted(receipts_root.rglob(pattern)):
        receipt = _load_json(candidate)
        if not isinstance(receipt, dict):
            continue
        if (
            receipt.get("record_id") == record_id
            and receipt.get("packaged_pdf_sha256") == registry_hash
        ):
            matches.append(candidate.resolve())
    if len(matches) == 1:
        return matches[0]
    return None


def _collect_evidence(
    run_dirs: Iterable[tuple[int, Path]]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    plans: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for revision, run_dir in run_dirs:
        for plan_path in sorted(run_dir.glob("stages/*/plan.json")):
            for plan_record in _load_plan_records(plan_path):
                plans[plan_record["record_id"]].append(
                    {
                        "kind": "plan.json",
                        "revision": revision,
                        "path": plan_record["path"],
                        "payload": {"record_id": plan_record["record_id"]},
                        "stage": plan_record["stage"],
                    }
                )
        for file_name in SEAL_FIELDS + QUARANTINE_FIELDS + STATUS_FIELDS:
            for path in sorted(run_dir.rglob(file_name)):
                payload = _load_json(path)
                if not isinstance(payload, dict):
                    _error(f"evidence must be a JSON object: {path}")
                record_id = _validate_record_id(payload.get("record_id"))
                evidence[record_id].append(
                    {
                        "kind": file_name,
                        "revision": revision,
                        "path": path,
                        "payload": payload,
                    }
                )
    return evidence, plans


def _latest_revision(entries: Iterable[dict[str, Any]]) -> Optional[int]:
    revisions = [entry["revision"] for entry in entries]
    return max(revisions) if revisions else None


def _revision_entries(
    entries: list[dict[str, Any]], revision: int
) -> list[dict[str, Any]]:
    return [entry for entry in entries if entry["revision"] == revision]


def _state_from_revision(
    evidence_entries: list[dict[str, Any]],
    plan_entries: list[dict[str, Any]],
) -> tuple[str, Optional[dict[str, Any]]]:
    seals = [
        entry
        for entry in evidence_entries
        if entry["kind"] == "paper-seal.json"
        and entry["payload"].get("passed") is True
    ]
    active_quarantines = [
        entry
        for entry in evidence_entries
        if entry["kind"] in QUARANTINE_FIELDS
        and entry["payload"].get("active") is True
    ]
    if seals and active_quarantines:
        return "ambiguous", seals[-1]
    if active_quarantines:
        return "quarantined", active_quarantines[-1]
    if seals:
        return "sealed", seals[-1]
    finishes = [entry for entry in evidence_entries if entry["kind"] == "finish.json"]
    if finishes:
        return "incomplete", finishes[-1]
    statuses = [entry for entry in evidence_entries if entry["kind"] == "status.json"]
    if statuses:
        return "incomplete", statuses[-1]
    if plan_entries:
        return "planned", sorted(plan_entries, key=lambda item: str(item["path"]))[-1]
    return "candidate", None


def _published_state(
    *,
    base_state: str,
    seal_entry: Optional[dict[str, Any]],
    registry_entry: Optional[dict[str, Any]],
    registry_path: Optional[Path],
    repo_root: Path,
) -> tuple[str, Optional[Path]]:
    if registry_entry is None:
        return base_state, None
    if base_state != "sealed" or seal_entry is None or registry_path is None:
        return "publication_mismatch", None
    registry_hash = registry_entry.get("publication_translation_sha256")
    if not isinstance(registry_hash, str) or not registry_hash:
        registry_hash = registry_entry.get("sha256")
    if not isinstance(registry_hash, str) or not registry_hash:
        return "publication_mismatch", None
    receipt_path = _resolve_publication_receipt_path(
        registry_entry,
        registry_path=registry_path,
        repo_root=repo_root,
        registry_hash=registry_hash,
    )
    if receipt_path is None or not receipt_path.is_file():
        return "publication_mismatch", receipt_path
    receipt = _load_json(receipt_path)
    if not isinstance(receipt, dict):
        return "publication_mismatch", receipt_path
    if receipt.get("record_id") != registry_entry["record_id"]:
        return "publication_mismatch", receipt_path
    if receipt.get("packaged_pdf_sha256") != registry_hash:
        return "publication_mismatch", receipt_path
    if receipt.get("source_pdf_sha256") != seal_entry["payload"].get("protected_pdf_sha256"):
        return "publication_mismatch", receipt_path
    return "published", receipt_path


def _build_index(
    *,
    runs_root: Path,
    rights_manifest: Path,
    publication_registry: Optional[Path],
    source_prefilter: Optional[Path],
) -> dict[str, Any]:
    rights_records_list = _load_record_list(rights_manifest, label="rights manifest")
    rights_records = {entry["record_id"]: entry for entry in rights_records_list}
    publication_records = _load_publication_registry(publication_registry)
    source_prefilter_records = _load_source_prefilter(
        source_prefilter, rights_records=rights_records
    )
    run_dirs = _collect_run_dirs(runs_root)
    evidence_map, plan_map = _collect_evidence(run_dirs)

    base_paths = [runs_root.resolve(), rights_manifest.resolve()]
    if publication_registry is not None:
        base_paths.append(publication_registry.resolve())
    if source_prefilter is not None:
        base_paths.append(source_prefilter.resolve())
    repo_root = Path(os.path.commonpath([str(path) for path in base_paths]))

    records: list[dict[str, Any]] = []
    state_counts: Counter[str] = Counter()
    candidate_exclusion_ids: list[str] = []
    untouched_prefilter_candidate_ids: list[str] = []
    untouched_prefilter_candidate_counts_by_tier: Counter[str] = Counter()
    summary = {
        "passed_seal_count": 0,
        "current_quarantined_count": 0,
        "planned_count": 0,
        "planned_or_incomplete_count": 0,
        "published_count": 0,
        "publication_mismatch_count": 0,
        "untouched_prefilter_candidate_count": 0,
        "untouched_prefilter_candidate_counts_by_tier": {},
        "rights_allowed_unstarted_count": 0,
        "rights_blocked_count": 0,
    }

    for rights_entry in sorted(rights_records_list, key=lambda item: item["record_id"]):
        record_id = rights_entry["record_id"]
        record_evidence = sorted(
            evidence_map.get(record_id, []),
            key=lambda item: (item["revision"], item["kind"], str(item["path"])),
        )
        record_plans = sorted(
            plan_map.get(record_id, []),
            key=lambda item: (item["revision"], item["stage"], str(item["path"])),
        )
        state: str
        winning_revision = _latest_revision(record_evidence + record_plans)
        winning_evidence: Optional[dict[str, Any]] = None
        latest_plan = record_plans[-1] if record_plans else None
        if rights_entry.get("publication_allowed") is not True:
            state = "rights_blocked"
        elif winning_revision is None:
            state = "candidate"
        else:
            state, winning_evidence = _state_from_revision(
                _revision_entries(record_evidence, winning_revision),
                _revision_entries(record_plans, winning_revision),
            )
        publication_receipt_path: Optional[Path] = None
        if state == "sealed" or record_id in publication_records:
            state, publication_receipt_path = _published_state(
                base_state=state,
                seal_entry=winning_evidence,
                registry_entry=publication_records.get(record_id),
                registry_path=publication_registry.resolve() if publication_registry else None,
                repo_root=repo_root,
            )

        evidence_paths = [
            _safe_relpath(entry["path"], repo_root)
            for entry in record_evidence + record_plans
        ]
        if publication_receipt_path is not None and publication_receipt_path.is_file():
            evidence_paths.append(_safe_relpath(publication_receipt_path, repo_root))
        evidence_counts = {
            "paper_seal": sum(
                1 for entry in record_evidence if entry["kind"] == "paper-seal.json"
            ),
            "active_quarantine": sum(
                1
                for entry in record_evidence
                if entry["kind"] in QUARANTINE_FIELDS
                and entry["payload"].get("active") is True
            ),
            "inactive_quarantine": sum(
                1
                for entry in record_evidence
                if entry["kind"] in QUARANTINE_FIELDS
                and entry["payload"].get("active") is False
            ),
            "finish": sum(1 for entry in record_evidence if entry["kind"] == "finish.json"),
            "status": sum(1 for entry in record_evidence if entry["kind"] == "status.json"),
            "plan": len(record_plans),
            "publication_receipt": 1 if publication_receipt_path is not None else 0,
        }
        latest_plan_path = (
            _safe_relpath(latest_plan["path"], repo_root) if latest_plan is not None else None
        )
        winning_evidence_path = None
        if publication_receipt_path is not None and publication_receipt_path.is_file():
            winning_evidence_path = _safe_relpath(publication_receipt_path, repo_root)
        elif winning_evidence is not None:
            winning_evidence_path = _safe_relpath(winning_evidence["path"], repo_root)
        record_payload = {
            "record_id": record_id,
            "publication_allowed": rights_entry.get("publication_allowed") is True,
            "current_revision": winning_revision,
            "state": state,
            "risk_tier": source_prefilter_records.get(record_id),
            "evidence_counts": evidence_counts,
            "evidence_paths": sorted(evidence_paths),
            "latest_plan_stage": latest_plan["stage"] if latest_plan is not None else None,
            "latest_plan_path": latest_plan_path,
            "winning_evidence_path": winning_evidence_path,
        }
        records.append(record_payload)
        state_counts[state] += 1
        if rights_entry.get("publication_allowed") is not True:
            summary["rights_blocked_count"] += 1
        elif state == "candidate":
            summary["rights_allowed_unstarted_count"] += 1
            risk_tier = source_prefilter_records.get(record_id)
            if risk_tier is not None:
                untouched_prefilter_candidate_ids.append(record_id)
                untouched_prefilter_candidate_counts_by_tier[risk_tier] += 1
        else:
            candidate_exclusion_ids.append(record_id)
        if state in {"sealed", "published", "publication_mismatch"}:
            summary["passed_seal_count"] += 1
        if state == "quarantined":
            summary["current_quarantined_count"] += 1
        if state == "planned":
            summary["planned_count"] += 1
        if state in {"planned", "incomplete"}:
            summary["planned_or_incomplete_count"] += 1
        if state == "published":
            summary["published_count"] += 1
        if state == "publication_mismatch":
            summary["publication_mismatch_count"] += 1
    summary["untouched_prefilter_candidate_count"] = len(
        untouched_prefilter_candidate_ids
    )
    summary["untouched_prefilter_candidate_counts_by_tier"] = dict(
        sorted(untouched_prefilter_candidate_counts_by_tier.items())
    )

    return {
        "schema_version": 1,
        "current_revision": max((revision for revision, _ in run_dirs), default=None),
        "records": records,
        "record_count": len(records),
        "state_counts": dict(sorted(state_counts.items())),
        "candidate_exclusion_ids": sorted(candidate_exclusion_ids),
        "untouched_prefilter_candidate_ids": sorted(
            untouched_prefilter_candidate_ids
        ),
        "summary": summary,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--rights-manifest", type=Path, required=True)
    parser.add_argument("--publication-registry", type=Path)
    parser.add_argument("--source-prefilter", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    payload = _build_index(
        runs_root=args.runs_root,
        rights_manifest=args.rights_manifest,
        publication_registry=args.publication_registry,
        source_prefilter=args.source_prefilter,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
