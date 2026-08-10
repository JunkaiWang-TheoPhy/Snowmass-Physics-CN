#!/usr/bin/env python3
"""Fail when tracked files cross the repository's public/private boundary."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_TRACKED_FILE_BYTES = 50 * 1024 * 1024

FORBIDDEN_TRACKED_PREFIXES = (
    "output/snowmass2021_sources/",
    "output/snowmass2021_translation/",
    "output/snowmass2021/rights/cache/",
    "tmp/",
)

PRIVATE_ALLOWED = {
    "private/.gitignore",
    "private/README.md",
    "private/schema.sql",
}

TEXT_PATTERNS = {
    "email address": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "API token": re.compile(r"(?:sk|rk|ghp|github_pat)-[A-Za-z0-9_\-]{12,}"),
    "private key": re.compile(r"BEGIN (?:RSA|OPENSSH|EC|PGP) PRIVATE KEY"),
}

PUBLIC_EMAIL_ALLOWLIST = {"wangtheophys@outlook.com"}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def scan() -> list[str]:
    errors: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(FORBIDDEN_TRACKED_PREFIXES):
            errors.append(f"forbidden tracked path: {relative}")
        if relative.startswith("private/") and relative not in PRIVATE_ALLOWED:
            errors.append(f"private data path is tracked: {relative}")
        if path.stat().st_size > MAX_TRACKED_FILE_BYTES:
            errors.append(f"tracked file exceeds 50 MiB: {relative}")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in TEXT_PATTERNS.items():
            for match in pattern.finditer(text):
                if label == "email address" and match.group(0).lower() in PUBLIC_EMAIL_ALLOWLIST:
                    continue
                errors.append(f"{label} found in {relative}:{text.count(chr(10), 0, match.start()) + 1}")
    return errors


def main() -> int:
    errors = scan()
    if errors:
        print("Public-tree audit failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Public-tree audit passed: no forbidden paths, unapproved contact emails, credentials, or oversized files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
