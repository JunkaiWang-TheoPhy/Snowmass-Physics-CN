#!/usr/bin/env python3
"""Tests for the fail-closed Snowmass publication orchestrator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("publish_snowmass_release.py")


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("Snowmass publication orchestrator is not implemented")
    spec = importlib.util.spec_from_file_location("publish_snowmass_release", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeRunner:
    def __init__(
        self,
        *,
        public_repo: Path,
        github_repo: str,
        github_tag: str,
        dirty_public_repo: bool = False,
        fail_step: str | None = None,
        existing_assets: dict[str, object] | None = None,
        changed_files: list[str] | None = None,
    ) -> None:
        self.public_repo = public_repo
        self.github_repo = github_repo
        self.github_tag = github_tag
        self.dirty_public_repo = dirty_public_repo
        self.fail_step = fail_step
        self.release_exists = existing_assets is not None
        self.release_draft = True
        self.calls: list[str] = []
        self.payloads: list[tuple[str, dict[str, object], Path | None]] = []
        self.assets: dict[str, dict[str, object]] = {}
        self.changed_files = changed_files
        for name, raw in (existing_assets or {}).items():
            if isinstance(raw, dict):
                size = int(raw["size"])
                digest = str(raw.get("digest", ""))
                state = str(raw.get("state", "uploaded"))
            else:
                size = int(raw)
                digest = ""
                state = "uploaded"
            self.assets[name] = {
                "name": name,
                "size": size,
                "digest": digest,
                "state": state,
                "browser_download_url": (
                    f"https://github.com/{github_repo}/releases/download/{github_tag}/{name}"
                ),
            }
        self.deploy_url = "https://snowmass-physics-cn.netlify.app"

    def __call__(self, step: str, payload: dict[str, object], cwd: Path | None = None) -> dict[str, object]:
        self.calls.append(step)
        self.payloads.append((step, dict(payload), cwd))
        if self.fail_step == step:
            raise RuntimeError(f"injected failure for {step}")
        if step == "github.release.get":
            return {
                "exists": self.release_exists,
                "is_draft": self.release_draft,
                "assets": sorted(self.assets.values(), key=lambda item: str(item["name"])),
            }
        if step == "github.release.create_draft":
            self.release_exists = True
            self.release_draft = True
            return {"exists": True, "is_draft": True}
        if step == "github.release.upload_asset":
            asset_name = str(payload["asset_name"])
            asset_path = Path(str(payload["asset_path"]))
            self.assets[asset_name] = {
                "name": asset_name,
                "size": asset_path.stat().st_size,
                "digest": f"sha256:{sha256_file(asset_path)}",
                "state": "uploaded",
                "browser_download_url": (
                    f"https://github.com/{self.github_repo}/releases/download/"
                    f"{self.github_tag}/{asset_name}"
                ),
            }
            return {"uploaded": asset_name}
        if step == "github.release.publish":
            self.release_exists = True
            self.release_draft = False
            return {"published": True}
        if step == "public_repo.assert_clean":
            return {"clean": not self.dirty_public_repo}
        if step == "public_repo.build_manifest":
            registry = json.loads(
                (self.public_repo / "translations" / "snowmass-publications.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest_path = self.public_repo / "site" / "data" / "papers.json"
            stats_path = self.public_repo / "site" / "data" / "stats.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            stats_path.write_text(
                json.dumps(
                    {"published_count": len(registry)},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return {"ok": True}
        if step == "public_repo.run_tests":
            return {"ok": True, "suites": payload.get("suites", [])}
        if step == "public_repo.changed_files":
            if self.changed_files is not None:
                return {"files": list(self.changed_files)}
            changed = []
            for relative in (
                "translations/snowmass-publications.json",
                "site/data/papers.json",
                "site/data/stats.json",
            ):
                if (self.public_repo / relative).exists():
                    changed.append(relative)
            return {"files": changed}
        if step == "public_repo.commit_push":
            return {
                "ok": True,
                "files": list(payload.get("files", [])),
                "commit_sha": "c" * 40,
            }
        if step == "public_repo.current_commit":
            return {"commit_sha": "c" * 40}
        if step == "netlify.deploy":
            return {
                "production_url": self.deploy_url,
                "unique_deploy_url": "https://deploy-preview.example.invalid",
                "message": payload.get("message"),
            }
        if step == "netlify.verify":
            manifest = json.loads(
                (self.public_repo / "site" / "data" / "papers.json").read_text(encoding="utf-8")
            )
            by_record_id = {record["record_id"]: record for record in manifest}
            planned = payload.get("planned_records", [])
            if not isinstance(planned, list):
                raise AssertionError("planned_records must be a list")
            for record in planned:
                online = by_record_id[str(record["record_id"])]
                if online["publication_translation_url"] != record["release_url"]:
                    raise RuntimeError("online manifest release URL mismatch")
                if online["publication_translation_sha256"] != record["packaged_pdf_sha256"]:
                    raise RuntimeError("online manifest hash mismatch")
                if int(online["publication_translation_size_bytes"]) != int(record["asset_size_bytes"]):
                    raise RuntimeError("online manifest size mismatch")
            return {
                "homepage_ok": True,
                "manifest_count": len(manifest),
                "download_count": int(payload["expected_homepage_download_count"]),
                "verified_records": [record["record_id"] for record in planned],
            }
        raise AssertionError(f"Unexpected step: {step}")


class PublishSnowmassReleaseTests(unittest.TestCase):
    def test_module_imports_under_macos_system_python(self) -> None:
        system_python = Path("/usr/bin/python3")
        if not system_python.is_file():
            self.skipTest("macOS system Python is unavailable")
        result = subprocess.run(
            [
                str(system_python),
                "-c",
                (
                    "import importlib.util; "
                    f"p={str(MODULE_PATH)!r}; "
                    "s=importlib.util.spec_from_file_location('release_compat', p); "
                    "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.receipts = self.root / "receipts"
        self.receipts.mkdir()
        self.evidence_root = self.root / "evidence"
        self.evidence_root.mkdir()
        self.rights_manifest = self.root / "rights.json"
        self.private_registry = self.root / "snowmass-publications.json"
        self.public_repo = self.root / "public"
        (self.public_repo / "translations").mkdir(parents=True)
        (self.public_repo / "site" / "data").mkdir(parents=True)
        (self.public_repo / "scripts").mkdir(parents=True)
        self.state_dir = self.root / "state"
        self.private_registry.write_text("[]\n", encoding="utf-8")
        self.public_repo.joinpath("translations", "snowmass-publications.json").write_text(
            "[]\n",
            encoding="utf-8",
        )
        self.public_repo.joinpath("scripts", "build_public_manifest.py").write_text(
            "# placeholder\n",
            encoding="utf-8",
        )
        self.write_rights_manifest()
        self.github_repo = "JunkaiWang-TheoPhy/Snowmass-Physics-CN"
        self.github_tag = "translations-2026-08-31-v1"
        self.netlify_site = "snowmass-physics-cn"
        self.default_limits = {
            "max_assets": 10,
            "expected_public_manifest_count": 1,
            "expected_homepage_download_count": 1,
        }

    def write_rights_manifest(self, *, publication_allowed: bool = True) -> None:
        self.rights_manifest.write_text(
            json.dumps(
                {
                    "papers": [
                        {
                            "record_id": "arxiv:2203.07506",
                            "publication_allowed": publication_allowed,
                        },
                        {
                            "record_id": "arxiv:2203.07564",
                            "publication_allowed": True,
                        },
                    ]
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def write_receipt(
        self,
        *,
        record_id: str = "arxiv:2203.07506",
        asset_name: str = "snowmass-2203.07506.zh-CN.pdf",
        packaging_contract_version: int = 4,
        qc_receipt_hashes: dict[str, str] | None = None,
        source_pdf_sha256: str | None = None,
    ) -> Path:
        article_dir = self.receipts / record_id.replace(":", "_")
        article_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = article_dir / asset_name
        pdf_path.write_bytes(b"%PDF-1.4\nplaceholder\n")
        protected_source_sha = source_pdf_sha256 or sha256_file(pdf_path)
        evidence = self.write_evidence_bundle(
            record_id=record_id,
            protected_pdf_sha256=protected_source_sha,
            qc_receipt_hashes=qc_receipt_hashes,
        )
        receipt_path = article_dir / f"{pdf_path.stem}.json"
        receipt = {
            "packaging_contract_version": packaging_contract_version,
            "record_id": record_id,
            "output_pdf_path": asset_name,
            "source_pdf_sha256": protected_source_sha,
            "packaged_pdf_sha256": sha256_file(pdf_path),
            "qc_receipt_hashes": qc_receipt_hashes or evidence["qc_receipt_hashes"],
            "version": "v1-production-20260831",
        }
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return receipt_path

    def build_plan(
        self,
        module,
        *receipt_paths: Path,
        limits: dict[str, object] | None = None,
        machine_model: str = "deepseek-v4-flash",
        translation_license: str = "CC-BY-4.0",
    ) -> dict[str, object]:
        return module.build_release_plan(
            receipt_paths=list(receipt_paths),
            rights_manifest_path=self.rights_manifest,
            private_registry_path=self.private_registry,
            public_repo_path=self.public_repo,
            github_repo=self.github_repo,
            github_tag=self.github_tag,
            netlify_site=self.netlify_site,
            state_dir=self.state_dir,
            evidence_root=self.evidence_root,
            machine_model=machine_model,
            translation_license=translation_license,
            limits=limits or dict(self.default_limits),
        )

    def read_private_registry(self) -> list[dict[str, object]]:
        return json.loads(self.private_registry.read_text(encoding="utf-8"))

    def evidence_bundle_for(self, record_id: str) -> dict[str, Path]:
        paper_root = self.evidence_root / "current-run" / "papers" / record_id.replace(":", "_")
        return {
            "paper_root": paper_root,
            "paper_seal_path": paper_root / "qc" / "paper-seal.json",
            "artifact_manifest_path": paper_root / "artifact-manifest.json",
            "environment_lock_path": paper_root / "environment-lock.json",
            "qc_dir": paper_root / "qc",
        }

    def write_evidence_bundle(
        self,
        *,
        record_id: str,
        protected_pdf_sha256: str,
        qc_receipt_hashes: dict[str, str] | None = None,
        run_name: str = "current-run",
    ) -> dict[str, object]:
        paper_root = self.evidence_root / run_name / "papers" / record_id.replace(":", "_")
        qc_dir = paper_root / "qc"
        qc_dir.mkdir(parents=True, exist_ok=True)
        artifact_manifest_path = paper_root / "artifact-manifest.json"
        artifact_manifest_path.write_text(
            json.dumps(
                {"record_id": record_id, "state": "visual_qc"},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        environment_lock_base = {
            "python": {"version": "3.12.13"},
            "contracts": {"model": "deepseek-v4-flash", "provider": "deepseek"},
        }
        environment_lock_sha = hashlib.sha256(
            json.dumps(environment_lock_base, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        environment_lock = dict(environment_lock_base)
        environment_lock["lock_sha256"] = environment_lock_sha
        environment_lock_path = paper_root / "environment-lock.json"
        environment_lock_path.write_text(
            json.dumps(environment_lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if qc_receipt_hashes is None:
            qc_receipt_hashes = {}
            for kind in ("semantic", "structural", "visual"):
                path = qc_dir / f"{kind}.json"
                path.write_text(
                    json.dumps(
                        {"kind": kind, "record_id": record_id},
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                qc_receipt_hashes[kind] = sha256_file(path)
        else:
            for kind in ("semantic", "structural", "visual"):
                path = qc_dir / f"{kind}.json"
                if kind not in qc_receipt_hashes:
                    continue
                path.write_text(
                    json.dumps(
                        {"kind": kind, "record_id": record_id, "expected_hash": qc_receipt_hashes[kind]},
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        paper_seal_path = qc_dir / "paper-seal.json"
        paper_seal = {
            "passed": True,
            "record_id": record_id,
            "protected_pdf_sha256": protected_pdf_sha256,
            "qc_receipt_hashes": dict(qc_receipt_hashes),
            "artifact_manifest_sha256": sha256_file(artifact_manifest_path),
            "environment_lock_sha256": environment_lock_sha,
            "environment_lock_file_sha256": sha256_file(environment_lock_path),
        }
        paper_seal_path.write_text(
            json.dumps(paper_seal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "paper_root": paper_root,
            "paper_seal_path": paper_seal_path,
            "artifact_manifest_path": artifact_manifest_path,
            "environment_lock_path": environment_lock_path,
            "qc_dir": qc_dir,
            "qc_receipt_hashes": dict(qc_receipt_hashes),
        }

    def test_plan_rejects_non_true_publication_rights(self) -> None:
        module = load_module()
        self.write_rights_manifest(publication_allowed=False)
        receipt_path = self.write_receipt()

        with self.assertRaisesRegex(ValueError, "publication_allowed must be literal true"):
            self.build_plan(module, receipt_path)

        self.assertFalse((self.state_dir / "release-plan.json").exists())

    def test_apply_revalidates_fingerprints_and_rejects_hash_mismatch_before_commands(self) -> None:
        module = load_module()
        receipt_path = self.write_receipt()
        plan = self.build_plan(module, receipt_path)
        tampered_pdf = receipt_path.parent / "snowmass-2203.07506.zh-CN.pdf"
        tampered_pdf.write_bytes(b"%PDF-1.4\ntampered\n")
        runner = FakeRunner(
            public_repo=self.public_repo,
            github_repo=self.github_repo,
            github_tag=self.github_tag,
        )

        with self.assertRaisesRegex(RuntimeError, "fingerprint mismatch"):
            module.apply_release_plan(plan_path=Path(str(plan["plan_path"])), command_runner=runner)

        self.assertEqual(runner.calls, [])

    def test_plan_requires_packaging_contract_v4_and_complete_qc_hashes(self) -> None:
        module = load_module()
        bad_version = self.write_receipt(packaging_contract_version=3)

        with self.assertRaisesRegex(ValueError, "packaging contract v4"):
            self.build_plan(module, bad_version)

        bad_qc = self.write_receipt(
            record_id="arxiv:2203.07564",
            asset_name="snowmass-2203.07564.zh-CN.pdf",
            qc_receipt_hashes={"semantic": "1" * 64, "structural": "2" * 64},
        )
        good = self.write_receipt()

        with self.assertRaisesRegex(ValueError, "semantic, structural, and visual"):
            self.build_plan(module, good, bad_qc)

    def test_plan_requires_nonempty_machine_model_and_translation_license(self) -> None:
        module = load_module()
        receipt = self.write_receipt()

        with self.assertRaisesRegex(ValueError, "machine_model"):
            self.build_plan(module, receipt, machine_model="")

        second = self.write_receipt(
            record_id="arxiv:2203.07564",
            asset_name="snowmass-2203.07564.zh-CN.pdf",
        )

        with self.assertRaisesRegex(ValueError, "translation_license"):
            self.build_plan(module, receipt, second, translation_license="  ")

    def test_plan_is_deterministic_and_contains_immutable_release_urls(self) -> None:
        module = load_module()
        second = self.write_receipt(
            record_id="arxiv:2203.07564",
            asset_name="snowmass-2203.07564.zh-CN.pdf",
        )
        first = self.write_receipt()

        plan_a = self.build_plan(module, second, first)
        first_bytes = (self.state_dir / "release-plan.json").read_bytes()
        plan_b = self.build_plan(module, first, second)
        second_bytes = (self.state_dir / "release-plan.json").read_bytes()

        self.assertEqual(plan_a, plan_b)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(
            [record["record_id"] for record in plan_a["records"]],
            ["arxiv:2203.07506", "arxiv:2203.07564"],
        )
        self.assertEqual(
            plan_a["records"][0]["release_url"],
            "https://github.com/JunkaiWang-TheoPhy/Snowmass-Physics-CN/releases/download/"
            "translations-2026-08-31-v1/snowmass-2203.07506.zh-CN.pdf",
        )
        self.assertEqual(plan_a["records"][0]["machine_model"], "deepseek-v4-flash")
        self.assertEqual(plan_a["records"][0]["translation_license"], "CC-BY-4.0")
        self.assertEqual(
            plan_a["records"][0]["evidence"]["paper_seal_path"],
            "current-run/papers/arxiv_2203.07506/qc/paper-seal.json",
        )

    def test_plan_rejects_missing_or_mutated_qc_evidence_receipt(self) -> None:
        module = load_module()

        with self.subTest(case="missing"):
            receipt_path = self.write_receipt()
            bundle = self.evidence_bundle_for("arxiv:2203.07506")
            (bundle["qc_dir"] / "semantic.json").unlink()
            with self.assertRaisesRegex(RuntimeError, "semantic evidence receipt"):
                self.build_plan(module, receipt_path)

        with self.subTest(case="mutated"):
            receipt_path = self.write_receipt()
            bundle = self.evidence_bundle_for("arxiv:2203.07506")
            (bundle["qc_dir"] / "semantic.json").write_text('{"mutated":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "semantic evidence hash mismatch"):
                self.build_plan(module, receipt_path)

    def test_plan_rejects_missing_or_mismatched_environment_lock_evidence(self) -> None:
        module = load_module()

        with self.subTest(case="missing"):
            receipt_path = self.write_receipt()
            bundle = self.evidence_bundle_for("arxiv:2203.07506")
            bundle["environment_lock_path"].unlink()
            with self.assertRaisesRegex(RuntimeError, "environment lock"):
                self.build_plan(module, receipt_path)

        with self.subTest(case="mismatched"):
            receipt_path = self.write_receipt()
            bundle = self.evidence_bundle_for("arxiv:2203.07506")
            payload = json.loads(bundle["environment_lock_path"].read_text(encoding="utf-8"))
            payload["lock_sha256"] = "0" * 64
            bundle["environment_lock_path"].write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "environment lock identity mismatch"):
                self.build_plan(module, receipt_path)

    def test_plan_rejects_artifact_manifest_evidence_mismatch(self) -> None:
        module = load_module()
        receipt_path = self.write_receipt()
        bundle = self.evidence_bundle_for("arxiv:2203.07506")
        bundle["artifact_manifest_path"].write_text('{"mutated":true}\n', encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "artifact manifest hash mismatch"):
            self.build_plan(module, receipt_path)

    def test_plan_rejects_ambiguous_matching_paper_seals(self) -> None:
        module = load_module()
        receipt_path = self.write_receipt()
        protected_hash = sha256_file(receipt_path.parent / "snowmass-2203.07506.zh-CN.pdf")
        self.write_evidence_bundle(
            record_id="arxiv:2203.07506",
            protected_pdf_sha256=protected_hash,
            run_name="current-run-copy",
        )

        with self.assertRaisesRegex(RuntimeError, "multiple matching paper seals"):
            self.build_plan(module, receipt_path)

    def test_plan_rejects_non_positive_or_non_finite_limits(self) -> None:
        module = load_module()
        receipt_path = self.write_receipt()

        with self.assertRaisesRegex(ValueError, "positive finite"):
            self.build_plan(
                module,
                receipt_path,
                limits={
                    "max_assets": 0,
                    "expected_public_manifest_count": 1,
                    "expected_homepage_download_count": 1,
                },
            )

        with self.assertRaisesRegex(ValueError, "positive finite"):
            self.build_plan(
                module,
                receipt_path,
                limits={
                    "max_assets": 1,
                    "expected_public_manifest_count": float("inf"),
                    "expected_homepage_download_count": 1,
                },
            )

    def test_apply_rejects_dirty_public_repo_before_release_or_registry_mutation(self) -> None:
        module = load_module()
        receipt_path = self.write_receipt()
        plan = self.build_plan(module, receipt_path)
        before_registry = self.private_registry.read_text(encoding="utf-8")
        runner = FakeRunner(
            public_repo=self.public_repo,
            github_repo=self.github_repo,
            github_tag=self.github_tag,
            dirty_public_repo=True,
        )

        with self.assertRaisesRegex(RuntimeError, "public repo is not clean"):
            module.apply_release_plan(plan_path=Path(str(plan["plan_path"])), command_runner=runner)

        state = module.read_release_status(state_dir=self.state_dir)
        self.assertEqual(runner.calls, ["public_repo.assert_clean"])
        self.assertEqual(state["completed_phases"], [])
        self.assertEqual(self.private_registry.read_text(encoding="utf-8"), before_registry)

    def test_apply_resumes_idempotently_and_writes_deterministic_release_seal(self) -> None:
        module = load_module()
        first = self.write_receipt()
        second = self.write_receipt(
            record_id="arxiv:2203.07564",
            asset_name="snowmass-2203.07564.zh-CN.pdf",
        )
        plan = self.build_plan(
            module,
            first,
            second,
            limits={
                "max_assets": 10,
                "expected_public_manifest_count": 2,
                "expected_homepage_download_count": 2,
            },
        )
        runner = FakeRunner(
            public_repo=self.public_repo,
            github_repo=self.github_repo,
            github_tag=self.github_tag,
            fail_step="public_repo.run_tests",
        )

        with self.assertRaisesRegex(RuntimeError, "public_repo.run_tests"):
            module.apply_release_plan(plan_path=Path(str(plan["plan_path"])), command_runner=runner)

        runner.fail_step = None
        result = module.apply_release_plan(plan_path=Path(str(plan["plan_path"])), command_runner=runner)
        seal_path = self.state_dir / "release-seal.json"
        first_seal = seal_path.read_bytes()
        rerun_calls = len(runner.calls)
        rerun_result = module.apply_release_plan(plan_path=Path(str(plan["plan_path"])), command_runner=runner)
        second_seal = seal_path.read_bytes()

        self.assertEqual(runner.calls.count("github.release.create_draft"), 1)
        self.assertEqual(runner.calls.count("github.release.upload_asset"), 2)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(rerun_result["status"], "complete")
        self.assertEqual(
            result["completed_phases"],
            list(module.RELEASE_PHASES),
        )
        self.assertEqual(first_seal, second_seal)
        self.assertEqual(len(runner.calls), rerun_calls)
        registry = self.read_private_registry()
        self.assertEqual(registry[0]["machine_model"], "deepseek-v4-flash")
        self.assertEqual(registry[0]["translation_license"], "CC-BY-4.0")
        seal = json.loads(first_seal.decode("utf-8"))
        self.assertEqual(seal["plan_sha256"], plan["plan_sha256"])
        self.assertRegex(seal["seal_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn(str(self.root), first_seal.decode("utf-8"))
        self.assertEqual(seal["public_commit_result"]["commit_sha"], "c" * 40)
        self.assertNotIn("deploy_output", seal["netlify_deploy_result"])
        self.assertEqual(
            seal["netlify_deploy_result"]["production_url"],
            runner.deploy_url,
        )

    def test_apply_rejects_existing_asset_with_digest_mismatch(self) -> None:
        module = load_module()
        receipt_path = self.write_receipt()
        plan = self.build_plan(module, receipt_path)
        runner = FakeRunner(
            public_repo=self.public_repo,
            github_repo=self.github_repo,
            github_tag=self.github_tag,
            existing_assets={
                "snowmass-2203.07506.zh-CN.pdf": {
                    "size": (receipt_path.parent / "snowmass-2203.07506.zh-CN.pdf").stat().st_size,
                    "digest": "sha256:" + "f" * 64,
                    "state": "uploaded",
                }
            },
        )

        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            module.apply_release_plan(plan_path=Path(str(plan["plan_path"])), command_runner=runner)

        self.assertEqual(runner.calls.count("github.release.upload_asset"), 0)
        self.assertEqual(
            runner.assets["snowmass-2203.07506.zh-CN.pdf"]["digest"],
            "sha256:" + "f" * 64,
        )

    def test_apply_allows_commit_noop_when_publication_files_are_unchanged(self) -> None:
        module = load_module()
        receipt_path = self.write_receipt()
        plan = self.build_plan(module, receipt_path)
        runner = FakeRunner(
            public_repo=self.public_repo,
            github_repo=self.github_repo,
            github_tag=self.github_tag,
            changed_files=[],
        )

        result = module.apply_release_plan(plan_path=Path(str(plan["plan_path"])), command_runner=runner)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(runner.calls.count("public_repo.commit_push"), 0)

    def test_apply_rejects_unrelated_public_repo_changes_at_commit_step(self) -> None:
        module = load_module()
        receipt_path = self.write_receipt()
        plan = self.build_plan(module, receipt_path)
        runner = FakeRunner(
            public_repo=self.public_repo,
            github_repo=self.github_repo,
            github_tag=self.github_tag,
            changed_files=[
                "translations/snowmass-publications.json",
                "site/data/papers.json",
                "site/data/stats.json",
                "README.md",
            ],
        )

        with self.assertRaisesRegex(RuntimeError, "changed file set mismatch"):
            module.apply_release_plan(plan_path=Path(str(plan["plan_path"])), command_runner=runner)

    def test_apply_preserves_last_completed_checkpoint_on_failure_and_status_is_read_only(self) -> None:
        module = load_module()
        receipt_path = self.write_receipt()
        plan = self.build_plan(module, receipt_path)
        runner = FakeRunner(
            public_repo=self.public_repo,
            github_repo=self.github_repo,
            github_tag=self.github_tag,
            fail_step="github.release.publish",
        )

        with self.assertRaisesRegex(RuntimeError, "github.release.publish"):
            module.apply_release_plan(plan_path=Path(str(plan["plan_path"])), command_runner=runner)

        state_path = self.state_dir / "release-state.json"
        before = state_path.read_text(encoding="utf-8")
        status = module.read_release_status(state_dir=self.state_dir)
        after = state_path.read_text(encoding="utf-8")

        self.assertEqual(before, after)
        self.assertEqual(status["completed_phases"], [
            "preflight_public_repo",
            "create_draft_release",
            "upload_assets",
            "verify_assets",
        ])
        self.assertEqual(status["pending_phases"][0], "publish_release")

    def test_shell_runner_uses_public_repo_build_script_and_all_public_suites(self) -> None:
        module = load_module()
        calls: list[tuple[list[str], Path | None]] = []

        def fake_run(argv, *, cwd, allow_failure=False):
            calls.append((list(argv), cwd))
            stdout = ""
            if list(argv)[:2] == ["netlify", "deploy"]:
                stdout = (
                    "Production URL: <https://snowmass-physics-cn.netlify.app>\n"
                    "Unique deploy URL: <https://abc123--snowmass-physics-cn.netlify.app>\n"
                )
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        runner = module.ShellCommandRunner(
            python_executable="python3",
            run_command=fake_run,
        )
        runner(
            "public_repo.build_manifest",
            {
                "public_repo_path": str(self.public_repo),
                "registry_path": str(self.public_repo / "translations" / "snowmass-publications.json"),
            },
            cwd=self.public_repo,
        )
        runner(
            "public_repo.run_tests",
            {"public_repo_path": str(self.public_repo)},
            cwd=self.public_repo,
        )
        runner(
            "netlify.deploy",
            {
                "public_repo_path": str(self.public_repo),
                "netlify_site": self.netlify_site,
                "github_tag": self.github_tag,
            },
            cwd=self.public_repo,
        )

        self.assertEqual(
            calls[0][0],
            [
                "python3",
                str(self.public_repo / "scripts" / "build_public_manifest.py"),
            ],
        )
        self.assertEqual(calls[0][1], self.public_repo)
        self.assertIn(["python3", "-m", "unittest", "scripts.test_public_manifest"], [call[0] for call in calls])
        self.assertIn(["python3", "-m", "unittest", "scripts.test_site_interface"], [call[0] for call in calls])
        self.assertIn(["python3", "-m", "unittest", "scripts.test_community_pages"], [call[0] for call in calls])
        deploy_call = calls[-1][0]
        self.assertIn("--no-build", deploy_call)
        self.assertIn("--message", deploy_call)

    def test_shell_runner_github_release_get_uses_api_and_treats_only_http_404_as_absent(self) -> None:
        module = load_module()
        calls: list[list[str]] = []

        def fake_run(argv, *, cwd, allow_failure=False):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 404: Not Found")

        runner = module.ShellCommandRunner(run_command=fake_run)
        result = runner(
            "github.release.get",
            {"github_repo": self.github_repo, "github_tag": self.github_tag},
        )

        self.assertEqual(
            calls[0],
            ["gh", "api", f"repos/{self.github_repo}/releases/tags/{self.github_tag}"],
        )
        self.assertFalse(result["exists"])

    def test_shell_runner_github_release_get_finds_draft_via_release_list(self) -> None:
        module = load_module()
        calls: list[list[str]] = []

        def fake_run(argv, *, cwd, allow_failure=False):
            command = list(argv)
            calls.append(command)
            if command[-1].endswith(f"/releases/tags/{self.github_tag}"):
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="HTTP 404: Not Found"
                )
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    [
                        {
                            "tag_name": self.github_tag,
                            "draft": True,
                            "assets": [],
                            "url": "https://api.github.test/releases/1",
                        }
                    ]
                ),
                stderr="",
            )

        runner = module.ShellCommandRunner(run_command=fake_run)
        result = runner(
            "github.release.get",
            {"github_repo": self.github_repo, "github_tag": self.github_tag},
        )

        self.assertTrue(result["exists"])
        self.assertTrue(result["is_draft"])
        self.assertEqual(result["assets"], [])
        self.assertEqual(
            calls[1],
            ["gh", "api", f"repos/{self.github_repo}/releases?per_page=100"],
        )

    def test_shell_runner_github_release_get_recovers_primary_transport_failure_from_list(self) -> None:
        module = load_module()

        def fake_run(argv, *, cwd, allow_failure=False):
            command = list(argv)
            if command[-1].endswith(f"/releases/tags/{self.github_tag}"):
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="Get https://api.github.test: EOF"
                )
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    [
                        {
                            "tag_name": self.github_tag,
                            "draft": True,
                            "assets": [],
                            "url": "https://api.github.test/releases/1",
                        }
                    ]
                ),
                stderr="",
            )

        result = module.ShellCommandRunner(run_command=fake_run)(
            "github.release.get",
            {"github_repo": self.github_repo, "github_tag": self.github_tag},
        )

        self.assertTrue(result["exists"])
        self.assertTrue(result["is_draft"])

    def test_release_asset_validation_accepts_github_draft_download_url(self) -> None:
        module = load_module()
        asset_name = "snowmass-2203.10060.zh-CN.pdf"
        digest = "a" * 64
        record = {
            "asset_name": asset_name,
            "asset_size_bytes": 123,
            "packaged_pdf_sha256": digest,
            "release_url": (
                f"https://github.com/{self.github_repo}/releases/download/"
                f"{self.github_tag}/{asset_name}"
            ),
        }
        asset = {
            "name": asset_name,
            "size": 123,
            "digest": f"sha256:{digest}",
            "state": "uploaded",
            "browser_download_url": (
                f"https://github.com/{self.github_repo}/releases/download/"
                f"untagged-55320a06fb1f8549579a/{asset_name}"
            ),
        }

        validated = module._validate_release_asset(record, asset)

        self.assertEqual(validated["browser_download_url"], asset["browser_download_url"])

    def test_shell_runner_http_verification_issues_requests_and_fails_on_manifest_mismatch(self) -> None:
        module = load_module()
        receipt_path = self.write_receipt()
        plan = self.build_plan(module, receipt_path)
        record = plan["records"][0]
        origin = "https://snowmass-physics-cn.netlify.app"
        online_manifest = [
            {
                "record_id": record["record_id"],
                "publication_translation_url": record["release_url"],
                "publication_translation_sha256": "0" * 64,
                "publication_translation_size_bytes": record["asset_size_bytes"],
            }
        ]
        calls: list[list[str]] = []

        def fake_run(argv, *, cwd, allow_failure=False):
            command = list(argv)
            calls.append(command)
            if command[:4] == ["curl", "-fsSL", "--max-time", "30"] and command[4] == origin:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if command[:4] == ["curl", "-fsSL", "--max-time", "30"] and command[4].endswith("/data/papers.json"):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(online_manifest, ensure_ascii=False),
                    stderr="",
                )
            if command[:4] == ["curl", "-fsSIL", "--max-time", "30"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=(
                        "HTTP/2 302\ncontent-length: 0\n\n"
                        f"HTTP/2 200\ncontent-length: {record['asset_size_bytes']}\n"
                    ),
                    stderr="",
                )
            raise AssertionError(command)

        runner = module.ShellCommandRunner(run_command=fake_run)
        with self.assertRaisesRegex(RuntimeError, "online manifest hash mismatch"):
            runner(
                "netlify.verify",
                {
                    "public_repo_path": str(self.public_repo),
                    "netlify_site": self.netlify_site,
                    "expected_public_manifest_count": 1,
                    "expected_homepage_download_count": 1,
                    "planned_records": plan["records"],
                },
                cwd=self.public_repo,
            )

        self.assertTrue(
            any(command[:4] == ["curl", "-fsSIL", "--max-time", "30"] for command in calls)
        )


if __name__ == "__main__":
    unittest.main()
