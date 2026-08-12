#!/usr/bin/env python3
"""Typed, paragraph-scale protection for Snowmass translation units.

This module is deliberately independent of the legacy raw-LaTeX chunker.  It
protects inline literals after a document parser has already produced a
paragraph or other semantic unit.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import re


class StructureDensityError(RuntimeError):
    """A semantic unit contains too many immutable objects for one request."""


class StructureMismatchError(RuntimeError):
    """Protected nodes were lost, duplicated, or invented by a translation."""


@dataclass(frozen=True)
class ProtectedNode:
    token: str
    kind: str
    value: str


@dataclass(frozen=True)
class ProtectedUnit:
    text: str
    nodes: tuple[ProtectedNode, ...]


@dataclass(frozen=True)
class NumericComparison:
    values_equal: bool
    format_changed: bool
    missing_values: tuple[str, ...]
    added_values: tuple[str, ...]


_COMMENT_MARKER_RE = re.compile(r"\\(?P<edge>begin|end)\s*\{comment\}")
_NUMBER_RE = re.compile(
    r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.\d+)?(?:[eE][-+]?\d+)?%?"
)
_UNIT_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:\s*[×x]\s*[-+]?\d+(?:\.\d+)?)?"
    r"\s*(?:%|eV|keV|MeV|GeV|TeV|PeV|fb(?:-1)?|pb(?:-1)?|nb(?:-1)?|ab(?:-1)?|mm|cm|km|m|ns|ps|ms|s|Hz|kHz|MHz|GHz|K)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_COMPARE_NUMBER_RE = re.compile(
    r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.\d+)?(?:[eE][-+]?\d+)?%?s?"
)
_BARE_URL_RE = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_TOKEN_RE = re.compile(r"\[\[SMU_[0-9]{4}_[A-Z_]+_[0-9a-f]{10}\]\]")
_EXISTING_PROTECTED_TOKEN_RE = re.compile(
    r"\[\[(?:SM_[0-9]{4}_[0-9a-f]{5,10}|SMU_[0-9]{4}_[A-Z_]+_[0-9a-f]{10})\]\]"
)


def _remove_comment_environments(text: str) -> str:
    output: list[str] = []
    cursor = 0
    block_start: int | None = None
    depth = 0
    for match in _COMMENT_MARKER_RE.finditer(text):
        edge = match.group("edge")
        if edge == "begin":
            if depth == 0:
                output.append(text[cursor : match.start()])
                block_start = match.start()
            depth += 1
            continue
        if depth == 0:
            continue
        depth -= 1
        if depth == 0:
            cursor = match.end()
            block_start = None
    if depth:
        raise ValueError(f"Unclosed LaTeX comment environment at offset {block_start}")
    output.append(text[cursor:])
    return "".join(output)


def _strip_line_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        escaped = False
        cut = len(line)
        for index, char in enumerate(line):
            if char == "%" and not escaped:
                cut = index
                break
            if char == "\\":
                escaped = not escaped
            else:
                escaped = False
        content = line[:cut]
        if line.endswith("\n") and not content.endswith("\n"):
            content += "\n"
        lines.append(content)
    return "".join(lines)


def published_tex_body(text: str) -> str:
    """Return only TeX content that can contribute to the rendered paper."""

    return _strip_line_comments(_remove_comment_environments(text))


def _balanced_command_spans(text: str, command: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    marker = re.compile(rf"\\{re.escape(command)}(?![A-Za-z@])\s*\{{")
    for match in marker.finditer(text):
        depth = 1
        index = match.end()
        escaped = False
        while index < len(text):
            char = text[index]
            if char == "\\" and not escaped:
                escaped = True
                index += 1
                continue
            if not escaped:
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        spans.append((match.start(), index + 1))
                        break
            escaped = False
            index += 1
    return spans


def _candidate_spans(text: str) -> list[tuple[int, int, str]]:
    candidates: list[tuple[int, int, str, int]] = []
    for start, end in _balanced_command_spans(text, "url"):
        candidates.append((start, end, "tex_url", 0))
    for match in _BARE_URL_RE.finditer(text):
        candidates.append((match.start(), match.end(), "url", 1))
    for match in _EMAIL_RE.finditer(text):
        candidates.append((match.start(), match.end(), "email", 2))
    for match in _UNIT_VALUE_RE.finditer(text):
        candidates.append((match.start(), match.end(), "unit", 3))
    for match in _NUMBER_RE.finditer(text):
        candidates.append((match.start(), match.end(), "number", 4))

    accepted: list[tuple[int, int, str]] = []
    occupied = [(match.start(), match.end()) for match in _EXISTING_PROTECTED_TOKEN_RE.finditer(text)]
    for start, end, kind, _priority in sorted(candidates, key=lambda item: (item[0], item[3], -item[1])):
        if any(start < occupied_end and end > occupied_start for occupied_start, occupied_end in occupied):
            continue
        accepted.append((start, end, kind))
        occupied.append((start, end))
    return sorted(accepted)


def _node_token(index: int, kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"[[SMU_{index:04d}_{kind.upper()}_{digest}]]"


def protect_translation_unit(text: str, *, max_nodes: int = 40) -> ProtectedUnit:
    """Protect typed inline literals in one parser-produced semantic unit."""

    if max_nodes < 0:
        raise ValueError("max_nodes must be non-negative")
    spans = _candidate_spans(text)
    if len(spans) > max_nodes:
        raise StructureDensityError(
            f"Translation unit contains {len(spans)} protected nodes; limit is {max_nodes}"
        )

    nodes: list[ProtectedNode] = []
    pieces: list[str] = []
    cursor = 0
    for index, (start, end, kind) in enumerate(spans, 1):
        value = text[start:end]
        token = _node_token(index, kind, value)
        pieces.extend((text[cursor:start], token))
        nodes.append(ProtectedNode(token=token, kind=kind, value=value))
        cursor = end
    pieces.append(text[cursor:])
    return ProtectedUnit(text="".join(pieces), nodes=tuple(nodes))


def restore_translation_unit(text: str, nodes: tuple[ProtectedNode, ...]) -> str:
    expected = {node.token for node in nodes}
    observed = _TOKEN_RE.findall(text)
    unknown = set(observed) - expected
    if unknown:
        raise StructureMismatchError(f"Unknown protected nodes: {sorted(unknown)}")
    restored = text
    for node in nodes:
        count = restored.count(node.token)
        if count != 1:
            raise StructureMismatchError(
                f"Expected protected node {node.token} exactly once, found {count}"
            )
        restored = restored.replace(node.token, node.value)
    return restored


def _numeric_source_text(text: str) -> str:
    protected = protect_translation_unit(text, max_nodes=10_000)
    output = protected.text
    for node in protected.nodes:
        if node.kind not in {"number", "unit"}:
            output = output.replace(node.token, " ")
        else:
            output = output.replace(node.token, node.value)
    return output


def _canonical_number(value: str) -> str:
    if value.endswith("s"):
        value = value[:-1]
    percent = value.endswith("%")
    raw = value[:-1] if percent else value
    try:
        number = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return value
    if number == 0:
        canonical = "0"
    else:
        canonical = format(number.normalize(), "f")
    return canonical + ("%" if percent else "")


def _numeric_literals(text: str) -> tuple[tuple[str, str], ...]:
    source = _numeric_source_text(text)
    return tuple(
        (match.group(0), _canonical_number(match.group(0)))
        for match in _COMPARE_NUMBER_RE.finditer(source)
    )


def _counter_difference(left: Counter[str], right: Counter[str]) -> tuple[str, ...]:
    values: list[str] = []
    for value in sorted(left):
        values.extend([value] * max(0, left[value] - right[value]))
    return tuple(values)


def compare_numeric_literals(source: str, translated: str) -> NumericComparison:
    source_numbers = _numeric_literals(source)
    translated_numbers = _numeric_literals(translated)
    source_values = Counter(canonical for _raw, canonical in source_numbers)
    translated_values = Counter(canonical for _raw, canonical in translated_numbers)
    values_equal = source_values == translated_values
    raw_source = tuple(raw for raw, _canonical in source_numbers)
    raw_translated = tuple(raw for raw, _canonical in translated_numbers)
    return NumericComparison(
        values_equal=values_equal,
        format_changed=values_equal and raw_source != raw_translated,
        missing_values=_counter_difference(source_values, translated_values),
        added_values=_counter_difference(translated_values, source_values),
    )
