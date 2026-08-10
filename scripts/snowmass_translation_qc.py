#!/usr/bin/env python3
"""Pure deterministic QC helpers for Snowmass translation stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from collections import Counter
import re
from typing import Any

from snowmass_pipeline import protected_literals


_SENTINEL_RE = re.compile(r"\[\[SM_[0-9]{4}_[0-9a-f]{5,10}\]\]")
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
_CITATION_RE = re.compile(r"\[(?:\d+(?:\s*[-,]\s*\d+)*)\]|arXiv:\d{4}\.\d{4,5}(?:v\d+)?")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?%?")
_UNIT_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"[-+]?\d+(?:\.\d+)?"
    r"(?:\s*[×x]\s*[-+]?\d+(?:\.\d+)?)?"
    r"\s*(?:%|eV|keV|MeV|GeV|TeV|PeV|fb(?:-1)?|pb(?:-1)?|nb(?:-1)?|ab(?:-1)?|mm|cm|m|km|ns|ps|ms|s|Hz|kHz|MHz|GHz|K)(?![A-Za-z0-9_])"
)
_ACRONYM_RE = re.compile(r"[A-Z][A-Z0-9/+.\-]{1,}")
_ANTI_AI_MARKERS = (
    "总而言之",
    "综上所述",
    "值得注意的是",
    "需要指出的是",
    "不难看出",
)


@dataclass(frozen=True)
class QCReport:
    ok: bool
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StageDecision:
    should_call_model: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": "call_model" if self.should_call_model else "copy_prior_text",
            "reason": self.reason,
        }


def _normalize_terms(glossary: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    terms: list[tuple[str, str]] = []
    for term in glossary:
        source = str(term.get("source", "")).strip()
        target = str(term.get("target", "")).strip()
        if source and target:
            terms.append((source, target))
    return tuple(terms)


def _is_permitted_acronym(source_term: str, target_term: str) -> bool:
    return source_term == target_term or bool(_ACRONYM_RE.fullmatch(source_term))


def _contains_term(text: str, term: str) -> bool:
    if not term:
        return False
    if _ACRONYM_RE.fullmatch(term):
        return term in text
    return term.casefold() in text.casefold()


def _sentinel_sequence_is_valid(text: str, mapping: dict[str, str]) -> bool:
    expected = list(mapping)
    observed = _SENTINEL_RE.findall(text)
    if not _same_multiset(expected, observed):
        return False
    for sentinel in expected:
        if text.count(sentinel) != 1:
            return False
    return True


def _locked_term_failures(source: str, translated: str, glossary: list[dict[str, Any]]) -> tuple[str, ...]:
    failures: list[str] = []
    for source_term, target_term in _normalize_terms(glossary):
        if not _contains_term(source, source_term):
            continue
        if _contains_term(translated, target_term):
            continue
        if _is_permitted_acronym(source_term, target_term) and _contains_term(translated, source_term):
            continue
        failures.append("locked_terms_mismatch")
        break
    return tuple(failures)


def _extract_urls(text: str) -> tuple[str, ...]:
    return tuple(match.rstrip(".,;:!?。；：！？") for match in _URL_RE.findall(text))


def _extract_unit_values(text: str) -> tuple[str, ...]:
    values: list[str] = []
    for match in _UNIT_VALUE_RE.findall(text):
        if re.fullmatch(r"[12]\d{3}s", match):
            continue
        values.append(re.sub(r"\s+", "", match))
    return tuple(values)


def _same_multiset(left: tuple[str, ...] | list[str], right: tuple[str, ...] | list[str]) -> bool:
    return Counter(left) == Counter(right)


def validate_chunk(source: str, translated: str, mapping: dict[str, str], glossary: list[dict[str, Any]]) -> QCReport:
    failures: list[str] = []

    if not _same_multiset(tuple(_NUMBER_RE.findall(source)), tuple(_NUMBER_RE.findall(translated))):
        failures.append("numbers_mismatch")
    if not _same_multiset(_extract_unit_values(source), _extract_unit_values(translated)):
        failures.append("units_mismatch")
    if not _same_multiset(_extract_urls(source), _extract_urls(translated)):
        failures.append("urls_mismatch")
    if not _same_multiset(tuple(_CITATION_RE.findall(source)), tuple(_CITATION_RE.findall(translated))):
        failures.append("citations_mismatch")
    if not _same_multiset(protected_literals(source), protected_literals(translated)):
        failures.append("protected_literals_mismatch")
    if not _sentinel_sequence_is_valid(translated, mapping):
        failures.append("sentinels_mismatch")
    failures.extend(_locked_term_failures(source, translated, glossary))

    return QCReport(ok=not failures, failures=tuple(failures))


def stage_decision(stage: str, text: str, glossary: list[dict[str, Any]]) -> StageDecision:
    if stage == "terminology":
        for source_term, target_term in _normalize_terms(glossary):
            if _contains_term(text, target_term):
                continue
            if _contains_term(text, source_term) and not _is_permitted_acronym(source_term, target_term):
                return StageDecision(True, "terminology_locked_term_conflict")
        return StageDecision(False, "terminology_noop_no_locked_term_conflicts")

    if stage == "anti_ai":
        if any(marker in text for marker in _ANTI_AI_MARKERS):
            return StageDecision(True, "anti_ai_marker_detected")
        return StageDecision(False, "anti_ai_noop_no_markers")

    return StageDecision(True, "stage_requires_model")
