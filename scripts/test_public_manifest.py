"""Tests for the public Snowmass manifest.

These tests intentionally inspect only the redacted public artifact.  Private
contacts and authorization evidence are not valid inputs to this test suite.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from scripts.build_public_manifest import _safe_public_record


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "site" / "data" / "papers.json"
STATS_PATH = ROOT / "site" / "data" / "stats.json"

REQUIRED_FIELDS = {
    "paper_id",
    "record_id",
    "title",
    "authors_as_listed",
    "frontiers",
    "topics",
    "source_url",
    "source_license",
    "source_license_url",
    "permits_adaptation",
    "license_decision",
    "translation_status",
    "translation_license",
    "human_reviewers",
    "authorization_status",
    "publication_allowed",
    "publication_basis",
    "publication_conditions",
    "publication_translation_url",
    "publication_translation_sha256",
    "publication_translation_size_bytes",
    "translation_version",
    "translation_published_at",
    "public_updated_at",
}

PRIVATE_DATA_PATTERNS = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?:sk|rk|ghp|github_pat)-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"BEGIN (?:RSA|OPENSSH|EC|PGP) PRIVATE KEY"),
)


def _load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        raise AssertionError(f"public manifest missing: {MANIFEST_PATH}")
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


class PublicManifestTests(unittest.TestCase):
    def test_blocked_record_cannot_inherit_public_translation_fields(self) -> None:
        record = _safe_public_record(
            {
                "record_id": "arxiv:blocked",
                "publication_allowed": False,
                "permits_adaptation": False,
            },
            {},
            {},
            {},
            {
                "publication_translation_url": "https://example.invalid/blocked.pdf",
                "publication_translation_sha256": "a" * 64,
                "publication_translation_size_bytes": 123,
                "translation_version": "v1",
                "translation_published_at": "2026-08-31",
            },
        )
        self.assertIsNone(record["publication_translation_url"])
        self.assertIsNone(record["publication_translation_sha256"])
        self.assertIsNone(record["publication_translation_size_bytes"])
        self.assertIsNone(record["translation_version"])
        self.assertIsNone(record["translation_published_at"])

    def test_record_count(self) -> None:
        records = _load_manifest()
        self.assertEqual(len(records), 541)
        self.assertEqual(len({record["record_id"] for record in records}), 541)

    def test_required_fields(self) -> None:
        for record in _load_manifest():
            self.assertTrue(REQUIRED_FIELDS.issubset(record), record.get("record_id"))
            self.assertIsInstance(record["frontiers"], list)
            self.assertIsInstance(record["topics"], list)
            self.assertIsInstance(record["human_reviewers"], list)
            self.assertIn(record["translation_status"], {
                "not-started",
                "machine-draft",
                "human-review",
                "published",
                "superseded",
                "withdrawn",
            })
            self.assertIn(record["authorization_status"], {
                "not-reviewed",
                "license-cleared",
                "needs-permission",
                "contacted",
                "response-pending",
                "permission-granted",
                "permission-denied",
                "unclear",
                "withdrawn",
            })

    def test_no_private_data(self) -> None:
        public_text = MANIFEST_PATH.read_text(encoding="utf-8")
        public_text += STATS_PATH.read_text(encoding="utf-8")
        for pattern in PRIVATE_DATA_PATTERNS:
            self.assertIsNone(pattern.search(public_text), pattern.pattern)

    def test_publication_gate(self) -> None:
        for record in _load_manifest():
            if record["publication_allowed"]:
                self.assertIn(record["publication_basis"], {
                    "source-license",
                    "permission-granted",
                }, record["record_id"])
                self.assertTrue(
                    record["permits_adaptation"]
                    or record["authorization_status"] == "permission-granted",
                    record["record_id"],
                )
            else:
                self.assertNotEqual(record["publication_basis"], "permission-granted")

    def test_deterministic_order(self) -> None:
        records = _load_manifest()
        keys = [(record["title"].casefold(), record["record_id"].casefold()) for record in records]
        self.assertEqual(keys, sorted(keys))

    def test_stats_match_manifest(self) -> None:
        records = _load_manifest()
        stats = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(stats["catalog_count"], len(records))
        self.assertEqual(stats["authorization_counts"]["license-cleared"], sum(
            record["authorization_status"] == "license-cleared" for record in records
        ))
        self.assertEqual(stats["publication_counts"]["allowed"], sum(
            bool(record["publication_allowed"]) for record in records
        ))

    def test_legacy_pilot_without_current_receipt_is_not_advertised(self) -> None:
        record = next(
            item for item in _load_manifest()
            if item["record_id"] == "arxiv:2203.07506"
        )
        self.assertIsNone(record["publication_translation_url"])
        self.assertIsNone(record["publication_translation_sha256"])
        self.assertIsNone(record["publication_translation_size_bytes"])
        self.assertIsNone(record["translation_version"])
        self.assertIsNone(record["translation_published_at"])

    def test_legacy_layout_fix_without_contract_v4_receipt_is_not_advertised(self) -> None:
        record = next(
            item for item in _load_manifest()
            if item["record_id"] == "arxiv:2203.07564"
        )
        self.assertTrue(record["publication_allowed"])
        self.assertIsNone(record["publication_translation_url"])
        self.assertIsNone(record["publication_translation_sha256"])
        self.assertIsNone(record["publication_translation_size_bytes"])
        self.assertIsNone(record["translation_version"])
        self.assertIsNone(record["translation_published_at"])

    def test_all_public_translation_downloads_use_release_assets(self) -> None:
        published = [
            record for record in _load_manifest()
            if record.get("publication_translation_url")
        ]
        self.assertTrue(published)
        expected_prefix = (
            "https://github.com/JunkaiWang-TheoPhy/Snowmass-Physics-CN/releases/download/"
        )
        for record in published:
            self.assertTrue(
                record["publication_translation_url"].startswith(expected_prefix),
                record["record_id"],
            )


if __name__ == "__main__":
    unittest.main()
