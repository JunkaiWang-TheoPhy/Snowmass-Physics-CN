#!/usr/bin/env python3
"""Compile article constraints into one immutable per-chunk execution plan."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


PLAN_FILE = "compiled_constraints.json"
VERBATIM_POLICIES = {"verbatim_figure_text", "verbatim_table_text", "verbatim_source"}


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _normalized(value: str) -> str:
    return " ".join(value.split()).casefold()


def _merge_exact_rules(baseline: list[dict[str, Any]], overrides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = [dict(rule) for rule in baseline]
    positions = {_normalized(str(rule.get("source", ""))): index for index, rule in enumerate(merged)}
    for rule in overrides:
        replacement = dict(rule)
        key = _normalized(str(replacement.get("source", "")))
        if key in positions:
            merged[positions[key]] = replacement
        elif key:
            positions[key] = len(merged)
            merged.append(replacement)
    return merged


def load_constraints(article_dir: Path, record_id: str, policy_path: Path) -> dict[str, Any]:
    policy = json.loads(Path(policy_path).read_text(encoding="utf-8")) if Path(policy_path).is_file() else {"schema_version": 1, "records": {}}
    if policy.get("schema_version") != 1 or not isinstance(policy.get("records"), dict):
        raise RuntimeError("invalid tracked hard constraint policy")
    record_rules = policy["records"].get(record_id, {})
    if not isinstance(record_rules, dict):
        raise RuntimeError(f"tracked hard constraints are invalid for {record_id}")
    local_path = Path(article_dir) / "hard_constraints.json"
    local = json.loads(local_path.read_text(encoding="utf-8")) if local_path.is_file() else {}
    if not isinstance(local, dict):
        raise RuntimeError("local hard constraints must be an object")
    forbidden: list[dict[str, Any]] = []
    for source in (policy, record_rules, local):
        rules = source.get("forbidden_translations", [])
        if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
            raise RuntimeError("forbidden_translations must be a list of objects")
        forbidden.extend(rule for rule in rules if rule not in forbidden)
    return {
        "schema_version": 1,
        "record_id": local.get("record_id", record_id),
        "exact_translations": _merge_exact_rules(
            list(record_rules.get("exact_translations", [])),
            list(local.get("exact_translations", [])),
        ),
        "forbidden_translations": forbidden,
    }


def _exact_translation(source: str, rules: list[dict[str, Any]]) -> str | None:
    source_key = _normalized(source)
    matches = [
        str(rule.get("target", ""))
        for rule in rules
        if _normalized(str(rule.get("source", ""))) == source_key
        and str(rule.get("target", "")).strip()
    ]
    if len(set(matches)) > 1:
        raise RuntimeError(f"ambiguous exact translations for source: {source.strip()}")
    if not matches:
        return None
    suffix = "\n" if source.endswith("\n") else ""
    return matches[0].strip() + suffix


def compile_constraint_plan(
    article_dir: Path,
    manifest: dict[str, Any],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    record_id = str(manifest.get("record_id") or "")
    if constraints.get("record_id", record_id) != record_id:
        raise RuntimeError("constraint record mismatch")
    directives: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    exact_rules = constraints.get("exact_translations", [])
    for chunk in manifest.get("chunks", []):
        chunk_id = str(chunk["id"])
        source_path = article_dir / str(chunk["source_file"])
        source = source_path.read_text(encoding="utf-8")
        hashes[chunk_id] = sha256(source_path)
        policy = str(chunk.get("translation_policy") or "translate")
        if policy in VERBATIM_POLICIES:
            directives[chunk_id] = {"policy": "verbatim_source", "reason": policy}
            continue
        fixed = _exact_translation(source, exact_rules)
        if fixed is not None:
            directives[chunk_id] = {
                "policy": "fixed_translation",
                "fixed_translation": fixed,
                "reason": "exact_translation",
            }
    payload = {
        "schema_version": 1,
        "record_id": record_id,
        "source_hashes": hashes,
        "chunk_directives": directives,
        "constraints_sha256": _json_sha256(constraints),
    }
    payload["plan_sha256"] = _json_sha256(payload)
    return payload


def write_constraint_plan(article_dir: Path, plan: dict[str, Any]) -> Path:
    path = Path(article_dir) / PLAN_FILE
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def load_constraint_plan(
    article_dir: Path,
    manifest: dict[str, Any],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    path = Path(article_dir) / PLAN_FILE
    if not path.is_file():
        raise RuntimeError(f"compiled constraint plan is missing: {path}")
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != 1 or plan.get("record_id") != manifest.get("record_id"):
        raise RuntimeError("invalid compiled constraint plan")
    recorded_plan_hash = plan.get("plan_sha256")
    unsigned_plan = dict(plan)
    unsigned_plan.pop("plan_sha256", None)
    if not isinstance(recorded_plan_hash, str) or recorded_plan_hash != _json_sha256(unsigned_plan):
        raise RuntimeError("compiled constraint plan hash mismatch")
    if plan.get("constraints_sha256") != _json_sha256(constraints):
        raise RuntimeError("compiled constraint plan constraints hash mismatch")
    if not isinstance(plan.get("source_hashes"), dict) or not isinstance(
        plan.get("chunk_directives"), dict
    ):
        raise RuntimeError("invalid compiled constraint plan payload")
    for chunk in manifest.get("chunks", []):
        chunk_id = str(chunk["id"])
        source = Path(article_dir) / str(chunk["source_file"])
        if plan.get("source_hashes", {}).get(chunk_id) != sha256(source):
            raise RuntimeError(f"compiled constraint plan is stale for {chunk_id}")
    expected = compile_constraint_plan(Path(article_dir), manifest, constraints)
    if plan != expected:
        raise RuntimeError(
            "compiled constraint plan hash does not match the canonical current plan"
        )
    return plan
