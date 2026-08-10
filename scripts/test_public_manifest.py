"""Tests for the public Snowmass manifest.

These tests intentionally inspect only the redacted public artifact.  Private
contacts and authorization evidence are not valid inputs to this test suite.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "site" / "data" / "papers.json"
STATS_PATH = ROOT / "site" / "data" / "stats.json"
TITLE_MAP_PATH = ROOT / "data" / "snowmass_title_zh.json"

REQUIRED_FIELDS = {
    "paper_id",
    "record_id",
    "title",
    "title_zh",
    "title_zh_status",
    "title_zh_model",
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
            self.assertTrue(record["title_zh"].strip(), record["record_id"])
            self.assertEqual(record["title_zh_status"], "machine-draft")
            self.assertTrue(record["title_zh_model"].strip(), record["record_id"])
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

    def test_all_records_have_distinct_machine_titles(self) -> None:
        records = _load_manifest()
        self.assertEqual(len(records), 541)
        for record in records:
            self.assertNotEqual(record["title_zh"].casefold(), record["title"].casefold(), record["record_id"])

    def test_title_mapping_matches_manifest(self) -> None:
        records = _load_manifest()
        payload = json.loads(TITLE_MAP_PATH.read_text(encoding="utf-8"))
        translations = payload["translations"]
        self.assertEqual(payload["machine_model"], "deepseek-v4-flash")
        self.assertEqual(len(translations), 541)
        self.assertEqual(
            {item["record_id"].casefold() for item in translations},
            {record["record_id"].casefold() for record in records},
        )

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


if __name__ == "__main__":
    unittest.main()
