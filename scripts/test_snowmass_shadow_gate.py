#!/usr/bin/env python3
"""Zero-paid-call shadow release test for the Snowmass evidence chain."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts import snowmass_production_contract as contract
from scripts import snowmass_qc_contract as qc


class SnowmassShadowGateTests(unittest.TestCase):
    def test_complete_chain_is_publishable_and_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article = root / "paper"
            article.mkdir()
            rights = root / "papers.json"
            rights.write_text('[{"record_id":"arxiv:shadow","publication_allowed":true}]\n')
            font = root / "font.ttc"
            cover = root / "cover.png"
            font.write_bytes(b"font")
            cover.write_bytes(b"cover")
            environment = contract.build_environment_lock(
                root=root,
                python_executable="/verified/python",
                python_version="3.14",
                installed_packages={"babeldoc": "0.6.4"},
                babeldoc_version="0.6.4",
                ir_version="6",
                model="fixture-no-model-call",
                provider="offline-fixture",
                pricing_contract={"currency": "RMB", "maximum": 0},
                contract_versions={"production": 1, "qc": 1},
                font_paths=[font],
                cover_asset_paths=[cover],
                git_commit="fixture-commit",
                git_tree="fixture-tree",
            )
            manifest = article / "production_artifacts.json"
            contract.write_artifact_manifest(
                manifest_path=manifest,
                record_id="arxiv:shadow",
                publication_allowed=True,
                rights_manifest_path=rights,
                article_root=article,
                environment_lock=environment,
            )
            stages = [
                ("prepared", "prepared.json", "prepared", ()),
                ("revision_ready", "revision.md", "revision_ready", ("prepared",)),
                ("translated", "translation.md", "translated", ("revision_ready",)),
                ("rendered", "rendered.pdf", "rendered", ("translated",)),
            ]
            for artifact_id, filename, stage, parents in stages:
                (article / filename).write_bytes(artifact_id.encode())
                contract.record_artifact(
                    manifest_path=manifest,
                    article_root=article,
                    artifact_id=artifact_id,
                    relative_path=filename,
                    producer="offline-shadow",
                    artifact_type="fixture",
                    paper_stage=stage,
                    environment_lock=environment,
                    parents=parents,
                    contract_versions={"fixture": 1},
                )
            parent = "rendered"
            receipt_paths = []
            for kind, stage in (("semantic", "semantic_qc"), ("structural", "structural_qc"), ("visual", "visual_qc")):
                path = article / f"{kind}.json"
                qc.write_qc_receipt(
                    receipt_path=path,
                    article_root=article,
                    record_id="arxiv:shadow",
                    kind=kind,
                    target_artifact_id="rendered",
                    target_path=article / "rendered.pdf",
                    environment_lock_sha256=environment["lock_sha256"],
                    contract_version=1,
                    ok=True,
                    evidence_summary={"offline_shadow": True, "paid_calls": 0},
                )
                contract.record_artifact(
                    manifest_path=manifest,
                    article_root=article,
                    artifact_id=f"{kind}_qc",
                    relative_path=path.name,
                    producer="offline-shadow",
                    artifact_type="qc_receipt",
                    paper_stage=stage,
                    environment_lock=environment,
                    parents=(parent,),
                    contract_versions={"fixture": 1},
                )
                parent = f"{kind}_qc"
                receipt_paths.append(path)
            package = article / "package.pdf"
            package.write_bytes(b"package")
            contract.record_artifact(
                manifest_path=manifest,
                article_root=article,
                artifact_id="packaged",
                relative_path=package.name,
                producer="offline-shadow",
                artifact_type="package",
                paper_stage="packaged",
                environment_lock=environment,
                parents=(parent,),
                contract_versions={"fixture": 1},
            )

            receipt_gate = qc.validate_publishability_receipts(
                receipt_paths,
                article_root=article,
                expected_record_id="arxiv:shadow",
                current_environment_lock_sha256=environment["lock_sha256"],
                required_contract_version=1,
            )
            state = contract.derive_paper_state(
                manifest,
                article_root=article,
                current_environment_lock=environment,
                rights_manifest_path=rights,
            )
            self.assertTrue(receipt_gate["publishable"], receipt_gate["errors"])
            self.assertTrue(state["publishable"], state["errors"])

            package.write_bytes(b"tampered")
            tampered = contract.derive_paper_state(manifest, article_root=article)
            self.assertFalse(tampered["publishable"])
            self.assertEqual(tampered["state"], "quarantined")


if __name__ == "__main__":
    unittest.main()
