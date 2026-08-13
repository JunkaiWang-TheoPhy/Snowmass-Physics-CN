#!/usr/bin/env python3
"""Fail-closed quality gates for Snowmass publication artifacts."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata

import fitz


MODEL_META_RESPONSE_PATTERNS = (
    re.compile(r"好的[，,。\s]*我(?:已)?(?:理解|明白).{0,80}(?:要求|规则).{0,120}请提供.{0,80}(?:翻译|译文)"),
    re.compile(r"请提供.{0,80}(?:需要|待|要).{0,40}翻译(?:的)?(?:原文|文本|段落|内容)"),
    re.compile(
        r"\bi\s+(?:fully\s+)?(?:understand|have understood)\s+(?:your|the)?\s*"
        r"(?:requirements?|instructions?).{0,160}please\s+(?:provide|send)\s+"
        r"(?:the\s+)?(?:source\s+)?(?:text|passage|content)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:好的[，,。\s]*)?请提供(?:需要翻译的)?原文.{0,80}(?:翻译|译)", re.IGNORECASE),
    re.compile(r"^(?:以下|下面)(?:是|为).{0,20}(?:翻译|译文)(?:结果)?[：:]?\s*$"),
    re.compile(
        r"^(?:(?:sure|certainly)[,!]?\s*)?(?:here(?:'s|\s+is)|below\s+is)\s+(?:the\s+)?"
        r"translation[：:]?\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"\bas an ai (?:assistant|language model).{0,120}\b(?:cannot|can't|unable to)\s+translate\b", re.IGNORECASE),
)


def contains_model_meta_response(text: str) -> bool:
    compact_text = " ".join(text.split())
    return any(pattern.search(compact_text) for pattern in MODEL_META_RESPONSE_PATTERNS)


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
            if contains_model_meta_response(raw_text):
                raise ValueError(
                    f"model meta-response on source PDF page {page_index + 1}"
                )
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
