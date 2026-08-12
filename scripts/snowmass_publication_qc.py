#!/usr/bin/env python3
"""Fail-closed quality gates for Snowmass publication artifacts."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata

import fitz


def _normalized_qc_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def validate_pdf_forbidden_translations(pdf_path: Path, policy_path: Path) -> None:
    """Reject known mistranslations and PDFs whose text cannot be checked."""

    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    rules = policy.get("forbidden_translations", []) if isinstance(policy, dict) else None
    if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
        raise ValueError(f"invalid forbidden translation policy: {policy_path}")

    document = fitz.open(pdf_path)
    extractable_characters = 0
    try:
        for page_index, page in enumerate(document):
            raw_text = page.get_text("text")
            extractable_characters += len(re.sub(r"\s+", "", raw_text))
            text = _normalized_qc_text(raw_text)
            for rule in rules:
                forbidden = str(rule.get("text", "")).strip()
                replacement = str(rule.get("replacement", "")).strip()
                if not forbidden or not replacement:
                    raise ValueError("forbidden translation rule is incomplete")
                if _normalized_qc_text(forbidden) in text:
                    raise ValueError(
                        f"forbidden translation on source PDF page {page_index + 1}: "
                        f"{forbidden}; use {replacement}"
                    )
        if rules and extractable_characters == 0:
            raise ValueError(
                "source PDF has no extractable text for forbidden translation QC"
            )
    finally:
        document.close()
