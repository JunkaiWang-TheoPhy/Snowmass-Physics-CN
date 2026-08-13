#!/usr/bin/env python3
"""Tests for the standalone Snowmass QC receipt contract."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("snowmass_qc_contract.py")


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("Snowmass QC contract module is not implemented")
    spec = importlib.util.spec_from_file_location("snowmass_qc_contract", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SnowmassQcContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.article = self.root / "paper"
        self.article.mkdir()
        self.packaged_dir = self.article / "packaged"
        self.packaged_dir.mkdir()
        self.receipts_dir = self.article / "qc"
        self.receipts_dir.mkdir()
        self.target_path = self.packaged_dir / "translated_dual.pdf"
        self.target_path.write_bytes(b"%PDF-1.7 translated-dual")
        self.second_target_path = self.packaged_dir / "translated_mono.pdf"
        self.second_target_path.write_bytes(b"%PDF-1.7 translated-mono")
        self.record_id = "arxiv:1234.5678"
        self.environment_lock_sha256 = "env-lock-sha256"
        self.other_environment_lock_sha256 = "other-env-lock-sha256"
        self.contract_version = 7

    def write_receipt(
        self,
        module,
        *,
        kind: str,
        filename: str | None = None,
        target_path: Path | None = None,
        target_artifact_id: str = "packaged-dual",
        environment_lock_sha256: str | None = None,
        ok: bool = True,
        evidence_summary: dict[str, object] | None = None,
    ) -> dict[str, object]:
        receipt_name = filename or f"{kind}.json"
        return module.write_qc_receipt(
            receipt_path=self.receipts_dir / receipt_name,
            article_root=self.article,
            record_id=self.record_id,
            kind=kind,
            target_artifact_id=target_artifact_id,
            target_path=target_path or self.target_path,
            environment_lock_sha256=(
                environment_lock_sha256 or self.environment_lock_sha256
            ),
            contract_version=self.contract_version,
            ok=ok,
            evidence_summary=evidence_summary
            or {"checked": [kind], "failures": [], "summary": f"{kind} ok"},
        )

    def validate_receipt(
        self,
        module,
        receipt_name: str,
        *,
        current_environment_lock_sha256: str | None = None,
        expected_kind: str | None = None,
        expected_target_artifact_id: str | None = None,
        required_contract_version: int | None = None,
    ) -> dict[str, object]:
        return module.validate_qc_receipt(
            self.receipts_dir / receipt_name,
            article_root=self.article,
            expected_record_id=self.record_id,
            expected_kind=expected_kind,
            expected_target_artifact_id=expected_target_artifact_id,
            current_environment_lock_sha256=current_environment_lock_sha256,
            required_contract_version=required_contract_version,
        )

    def test_write_qc_receipt_writes_hash_bound_receipt_for_supported_kind(self) -> None:
        module = load_module()

        receipt = self.write_receipt(module, kind="semantic")
        report = self.validate_receipt(
            module,
            "semantic.json",
            current_environment_lock_sha256=self.environment_lock_sha256,
            expected_kind="semantic",
            expected_target_artifact_id="packaged-dual",
            required_contract_version=self.contract_version,
        )

        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(receipt["record_id"], self.record_id)
        self.assertEqual(receipt["kind"], "semantic")
        self.assertEqual(receipt["target"]["relative_path"], "packaged/translated_dual.pdf")
        self.assertEqual(receipt["target"]["sha256"], sha256_file(self.target_path))
        self.assertIsInstance(receipt["receipt_hash"], str)

    def test_validate_qc_receipt_rejects_tampered_receipt_body(self) -> None:
        module = load_module()
        self.write_receipt(module, kind="semantic")
        receipt_path = self.receipts_dir / "semantic.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["evidence_summary"]["summary"] = "tampered"
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        report = self.validate_receipt(module, "semantic.json")

        self.assertFalse(report["ok"])
        self.assertIn("receipt_hash_mismatch", report["errors"])

    def test_validate_qc_receipt_rejects_target_path_escape(self) -> None:
        module = load_module()
        self.write_receipt(module, kind="semantic")
        receipt_path = self.receipts_dir / "semantic.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["target"]["relative_path"] = "../escaped.pdf"
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        report = self.validate_receipt(module, "semantic.json")

        self.assertFalse(report["ok"])
        self.assertIn("target_path_escape", report["errors"])

    def test_validate_qc_receipt_rejects_environment_drift(self) -> None:
        module = load_module()
        self.write_receipt(module, kind="semantic")

        report = self.validate_receipt(
            module,
            "semantic.json",
            current_environment_lock_sha256=self.other_environment_lock_sha256,
        )

        self.assertFalse(report["ok"])
        self.assertIn("environment_lock_sha256_drift", report["errors"])

    def test_validate_qc_receipt_rejects_target_drift(self) -> None:
        module = load_module()
        self.write_receipt(module, kind="semantic")
        self.target_path.write_bytes(b"%PDF-1.7 tampered-dual")

        report = self.validate_receipt(module, "semantic.json")

        self.assertFalse(report["ok"])
        self.assertIn("target_sha256_drift", report["errors"])

    def test_validate_qc_receipt_rejects_non_true_ok(self) -> None:
        module = load_module()
        self.write_receipt(module, kind="structural", ok=False)

        report = self.validate_receipt(module, "structural.json")

        self.assertFalse(report["ok"])
        self.assertIn("receipt_not_ok", report["errors"])

    def test_validate_publishability_receipts_returns_publishable_for_three_valid_kinds(self) -> None:
        module = load_module()
        self.write_receipt(module, kind="semantic")
        self.write_receipt(module, kind="structural")
        self.write_receipt(module, kind="visual")

        report = module.validate_publishability_receipts(
            [
                self.receipts_dir / "semantic.json",
                self.receipts_dir / "structural.json",
                self.receipts_dir / "visual.json",
            ],
            article_root=self.article,
            expected_record_id=self.record_id,
            current_environment_lock_sha256=self.environment_lock_sha256,
            required_contract_version=self.contract_version,
        )

        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(report["publishable"])
        self.assertEqual(report["target"]["artifact_id"], "packaged-dual")

    def test_validate_publishability_receipts_requires_all_three_kinds(self) -> None:
        module = load_module()
        self.write_receipt(module, kind="semantic")
        self.write_receipt(module, kind="structural")

        report = module.validate_publishability_receipts(
            [
                self.receipts_dir / "semantic.json",
                self.receipts_dir / "structural.json",
            ],
            article_root=self.article,
            expected_record_id=self.record_id,
        )

        self.assertFalse(report["ok"])
        self.assertFalse(report["publishable"])
        self.assertIn("missing_kind:visual", report["errors"])

    def test_validate_publishability_receipts_rejects_duplicate_kind(self) -> None:
        module = load_module()
        self.write_receipt(module, kind="semantic")
        self.write_receipt(module, kind="semantic", filename="semantic-duplicate.json")
        self.write_receipt(module, kind="structural")
        self.write_receipt(module, kind="visual")

        report = module.validate_publishability_receipts(
            [
                self.receipts_dir / "semantic.json",
                self.receipts_dir / "semantic-duplicate.json",
                self.receipts_dir / "structural.json",
                self.receipts_dir / "visual.json",
            ],
            article_root=self.article,
            expected_record_id=self.record_id,
        )

        self.assertFalse(report["ok"])
        self.assertFalse(report["publishable"])
        self.assertIn("duplicate_kind:semantic", report["errors"])

    def test_validate_publishability_receipts_rejects_target_mismatch_between_receipts(self) -> None:
        module = load_module()
        self.write_receipt(module, kind="semantic")
        self.write_receipt(
            module,
            kind="structural",
            filename="structural-other-target.json",
            target_path=self.second_target_path,
            target_artifact_id="packaged-mono",
        )
        self.write_receipt(module, kind="visual")

        report = module.validate_publishability_receipts(
            [
                self.receipts_dir / "semantic.json",
                self.receipts_dir / "structural-other-target.json",
                self.receipts_dir / "visual.json",
            ],
            article_root=self.article,
            expected_record_id=self.record_id,
        )

        self.assertFalse(report["ok"])
        self.assertFalse(report["publishable"])
        self.assertIn("target_mismatch_between_receipts", report["errors"])

    def test_validate_publishability_receipts_rejects_environment_mismatch_between_receipts(self) -> None:
        module = load_module()
        self.write_receipt(module, kind="semantic")
        self.write_receipt(
            module,
            kind="structural",
            filename="structural-other-env.json",
            environment_lock_sha256=self.other_environment_lock_sha256,
        )
        self.write_receipt(module, kind="visual")

        report = module.validate_publishability_receipts(
            [
                self.receipts_dir / "semantic.json",
                self.receipts_dir / "structural-other-env.json",
                self.receipts_dir / "visual.json",
            ],
            article_root=self.article,
            expected_record_id=self.record_id,
        )

        self.assertFalse(report["ok"])
        self.assertFalse(report["publishable"])
        self.assertIn("environment_lock_sha256_mismatch_between_receipts", report["errors"])


if __name__ == "__main__":
    unittest.main()
