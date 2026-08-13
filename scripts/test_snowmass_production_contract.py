#!/usr/bin/env python3
"""Tests for the Snowmass production artifact contract."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("snowmass_production_contract.py")


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("Snowmass production contract module is not implemented")
    spec = importlib.util.spec_from_file_location("snowmass_production_contract", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SnowmassProductionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.article = self.root / "paper"
        self.article.mkdir()
        self.manifest_path = self.article / "artifact_manifest.json"
        self.rights_manifest = self.root / "papers.json"
        self.font_asset = self.root / "font.ttc"
        self.cover_asset = self.root / "cover.png"
        self.qr_asset = self.root / "qr.png"
        self.font_asset.write_bytes(b"font-bytes")
        self.cover_asset.write_bytes(b"cover-bytes")
        self.qr_asset.write_bytes(b"qr-bytes")
        self.write_rights_manifest()

    def write_rights_manifest(self, *, record_id: str = "arxiv:a", publication_allowed: bool = True) -> None:
        self.rights_manifest.write_text(
            json.dumps(
                [{"record_id": record_id, "publication_allowed": publication_allowed}],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def build_lock(self, module, **overrides):
        options = {
            "root": self.root,
            "python_executable": "/opt/snowmass/bin/python3",
            "python_version": "3.12.7",
            "installed_packages": {
                "babeldoc": "0.6.4",
                "pymupdf": "1.24.9",
                "pypdf": "5.8.0",
            },
            "babeldoc_version": "0.6.4",
            "ir_version": "snowmass-ir-v1",
            "model": "deepseek-v4-flash",
            "provider": "deepseek",
            "pricing_contract": {
                "source": "https://api-docs.deepseek.com/quick_start/pricing/",
                "verified_at": "2026-08-10",
                "currency": "USD",
                "input_cache_hit": 0.0028,
                "input_cache_miss": 0.14,
                "output": 0.28,
            },
            "contract_versions": {
                "translation_qc": 5,
                "packaging": 3,
            },
            "font_paths": [self.font_asset],
            "cover_asset_paths": [self.cover_asset, self.qr_asset],
            "git_commit": "abc1234",
            "git_tree": "def5678",
        }
        options.update(overrides)
        return module.build_environment_lock(**options)

    def initialize_manifest(self, module, environment_lock, **overrides):
        options = {
            "manifest_path": self.manifest_path,
            "record_id": "arxiv:a",
            "publication_allowed": True,
            "rights_manifest_path": self.rights_manifest,
            "article_root": self.article,
            "environment_lock": environment_lock,
        }
        options.update(overrides)
        return module.write_artifact_manifest(**options)

    def record_artifact(
        self,
        module,
        environment_lock,
        *,
        artifact_id: str,
        relative_path: str,
        producer: str,
        artifact_type: str,
        paper_stage: str,
        parents=(),
        contract_versions: dict[str, object] | None = None,
    ):
        return module.record_artifact(
            manifest_path=self.manifest_path,
            article_root=self.article,
            artifact_id=artifact_id,
            relative_path=relative_path,
            producer=producer,
            artifact_type=artifact_type,
            paper_stage=paper_stage,
            environment_lock=environment_lock,
            parents=parents,
            contract_versions=contract_versions or {"contract": 1},
        )

    def test_build_environment_lock_is_deterministic_and_hashes_assets(self) -> None:
        module = load_module()

        first = self.build_lock(module)
        second = self.build_lock(module)

        self.assertEqual(first["lock_sha256"], second["lock_sha256"])
        self.assertEqual(first["python"]["version"], "3.12.7")
        self.assertEqual(first["assets"]["fonts"][0]["sha256"], sha256_file(self.font_asset))
        self.assertEqual(first["assets"]["cover_assets"][1]["sha256"], sha256_file(self.qr_asset))

    def test_record_artifact_rejects_path_escape_and_duplicate_ids(self) -> None:
        module = load_module()
        environment_lock = self.build_lock(module)
        self.initialize_manifest(module, environment_lock)
        workspace = self.article / "workspace.json"
        workspace.write_text('{"ok":true}\n', encoding="utf-8")

        record = self.record_artifact(
            module,
            environment_lock,
            artifact_id="prepared",
            relative_path="workspace.json",
            producer="prepare",
            artifact_type="workspace",
            paper_stage="prepared",
        )

        self.assertEqual(record["artifact_id"], "prepared")
        self.assertEqual(record["sha256"], sha256_file(workspace))
        with self.assertRaisesRegex(RuntimeError, "escapes article directory"):
            self.record_artifact(
                module,
                environment_lock,
                artifact_id="escape",
                relative_path="../escape.json",
                producer="prepare",
                artifact_type="workspace",
                paper_stage="prepared",
            )
        with self.assertRaisesRegex(ValueError, "Duplicate artifact_id"):
            self.record_artifact(
                module,
                environment_lock,
                artifact_id="prepared",
                relative_path="workspace.json",
                producer="prepare",
                artifact_type="workspace",
                paper_stage="prepared",
            )

    def test_validate_artifact_manifest_fails_closed_on_missing_or_tampered_parent(self) -> None:
        module = load_module()
        environment_lock = self.build_lock(module)
        self.initialize_manifest(module, environment_lock)
        source = self.article / "prepared.json"
        translated = self.article / "translated.json"
        source.write_text('{"stage":"prepared"}\n', encoding="utf-8")
        translated.write_text('{"stage":"translated"}\n', encoding="utf-8")
        self.record_artifact(
            module,
            environment_lock,
            artifact_id="prepared",
            relative_path="prepared.json",
            producer="prepare",
            artifact_type="workspace",
            paper_stage="prepared",
        )
        self.record_artifact(
            module,
            environment_lock,
            artifact_id="revision_ready",
            relative_path="translated.json",
            producer="refine",
            artifact_type="revision",
            paper_stage="revision_ready",
            parents=["prepared"],
        )

        good = module.validate_artifact_manifest(
            self.manifest_path,
            article_root=self.article,
            current_environment_lock=environment_lock,
            rights_manifest_path=self.rights_manifest,
        )
        self.assertTrue(good["ok"], good["errors"])

        source.write_text('{"stage":"tampered"}\n', encoding="utf-8")
        tampered = module.validate_artifact_manifest(
            self.manifest_path,
            article_root=self.article,
            current_environment_lock=environment_lock,
            rights_manifest_path=self.rights_manifest,
        )
        self.assertFalse(tampered["ok"])
        self.assertIn("artifact_hash_mismatch:prepared", tampered["errors"])
        self.assertIn("parent_hash_mismatch:revision_ready:prepared", tampered["errors"])

        source.write_text('{"stage":"prepared"}\n', encoding="utf-8")
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"][1]["parents"] = [{"artifact_id": "missing", "sha256": "deadbeef"}]
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        missing = module.validate_artifact_manifest(
            self.manifest_path,
            article_root=self.article,
            current_environment_lock=environment_lock,
            rights_manifest_path=self.rights_manifest,
        )
        self.assertFalse(missing["ok"])
        self.assertIn("missing_parent:revision_ready:missing", missing["errors"])

    def test_validate_recomputes_embedded_environment_lock_hash(self) -> None:
        module = load_module()
        environment_lock = self.build_lock(module)
        self.initialize_manifest(module, environment_lock)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["environment_lock"]["python"]["version"] = "tampered"
        self.manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

        report = module.validate_artifact_manifest(self.manifest_path, article_root=self.article)

        self.assertFalse(report["ok"])
        self.assertIn("stored_environment_lock_content_mismatch", report["errors"])

    def test_validate_artifact_manifest_fails_closed_on_environment_and_rights_drift(self) -> None:
        module = load_module()
        environment_lock = self.build_lock(module)
        self.initialize_manifest(module, environment_lock)
        artifact = self.article / "prepared.json"
        artifact.write_text('{"stage":"prepared"}\n', encoding="utf-8")
        self.record_artifact(
            module,
            environment_lock,
            artifact_id="prepared",
            relative_path="prepared.json",
            producer="prepare",
            artifact_type="workspace",
            paper_stage="prepared",
        )

        drifted_environment = self.build_lock(module, python_version="3.12.8")
        drift_report = module.validate_artifact_manifest(
            self.manifest_path,
            article_root=self.article,
            current_environment_lock=drifted_environment,
            rights_manifest_path=self.rights_manifest,
        )
        self.assertFalse(drift_report["ok"])
        self.assertIn("environment_lock_drift", drift_report["errors"])

        drifted_rights = self.root / "papers-drifted.json"
        drifted_rights.write_text(
            json.dumps([{"record_id": "arxiv:a", "publication_allowed": True, "page_count": 99}], indent=2) + "\n",
            encoding="utf-8",
        )
        rights_report = module.validate_artifact_manifest(
            self.manifest_path,
            article_root=self.article,
            current_environment_lock=environment_lock,
            rights_manifest_path=drifted_rights,
        )
        self.assertFalse(rights_report["ok"])
        self.assertIn("rights_manifest_sha256_drift", rights_report["errors"])

    def test_manifest_requires_live_allowed_rights_record(self) -> None:
        module = load_module()
        environment_lock = self.build_lock(module)
        self.write_rights_manifest(publication_allowed=False)
        with self.assertRaisesRegex(ValueError, "Live publication rights"):
            self.initialize_manifest(module, environment_lock)
        self.write_rights_manifest(record_id="arxiv:other")
        with self.assertRaisesRegex(ValueError, "Live publication rights"):
            self.initialize_manifest(module, environment_lock)

    def test_incomplete_environment_lock_is_rejected(self) -> None:
        module = load_module()
        environment_lock = self.build_lock(module, model=None)
        with self.assertRaisesRegex(ValueError, "environment_contracts_incomplete"):
            self.initialize_manifest(module, environment_lock)

    def test_derive_state_quarantines_ambiguous_equal_rank_tips(self) -> None:
        module = load_module()
        environment_lock = self.build_lock(module)
        self.initialize_manifest(module, environment_lock)
        prepared = self.article / "prepared.json"
        prepared.write_text("{}\n", encoding="utf-8")
        self.record_artifact(module, environment_lock, artifact_id="prepared", relative_path="prepared.json", producer="prepare", artifact_type="workspace", paper_stage="prepared")
        for suffix in ("a", "b"):
            revised = self.article / f"revised-{suffix}.json"
            revised.write_text(f'{{"tip":"{suffix}"}}\n', encoding="utf-8")
            self.record_artifact(module, environment_lock, artifact_id=f"revised-{suffix}", relative_path=revised.name, producer="refine", artifact_type="revision", paper_stage="revision_ready", parents=["prepared"])

        state = module.derive_paper_state(self.manifest_path, article_root=self.article)

        self.assertFalse(state["ok"])
        self.assertEqual(state["state"], "quarantined")
        self.assertIn("ambiguous_state_tips:revised-a,revised-b", state["errors"])

    def test_derive_paper_state_returns_packaged_for_intact_chain(self) -> None:
        module = load_module()
        environment_lock = self.build_lock(module)
        self.initialize_manifest(module, environment_lock)

        chain = [
            ("prepared", "prepared.json", "prepare", "workspace", "prepared", ()),
            ("revised", "revised.json", "refine", "revision", "revision_ready", ("prepared",)),
            ("translated", "translated.json", "translate", "translation", "translated", ("revised",)),
            ("rendered", "rendered.pdf", "render", "render", "rendered", ("translated",)),
            ("semantic", "semantic.json", "audit", "qc_receipt", "semantic_qc", ("rendered",)),
            ("structural", "structural.json", "audit", "qc_receipt", "structural_qc", ("semantic",)),
            ("visual", "visual.json", "audit", "qc_receipt", "visual_qc", ("structural",)),
            ("packaged", "packaged.pdf", "package", "package", "packaged", ("visual",)),
        ]
        for artifact_id, relative_path, producer, artifact_type, paper_stage, parents in chain:
            path = self.article / relative_path
            path.write_bytes(f"{artifact_id}\n".encode("utf-8"))
            self.record_artifact(
                module,
                environment_lock,
                artifact_id=artifact_id,
                relative_path=relative_path,
                producer=producer,
                artifact_type=artifact_type,
                paper_stage=paper_stage,
                parents=parents,
                contract_versions={"stage_contract": 1},
            )

        state = module.derive_paper_state(
            self.manifest_path,
            article_root=self.article,
            current_environment_lock=environment_lock,
            rights_manifest_path=self.rights_manifest,
        )

        self.assertTrue(state["ok"], state["errors"])
        self.assertEqual(state["state"], "packaged")
        self.assertTrue(state["publishable"])
        self.assertEqual(state["record_id"], "arxiv:a")
        self.assertEqual(state["artifact_id"], "packaged")


if __name__ == "__main__":
    unittest.main()
