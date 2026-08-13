#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("snowmass_hpc_postprocess.py")
SPEC = importlib.util.spec_from_file_location("snowmass_hpc_postprocess", MODULE_PATH)
HPC = importlib.util.module_from_spec(SPEC) if SPEC and SPEC.loader else None
if HPC is not None and SPEC and SPEC.loader:
    SPEC.loader.exec_module(HPC)


class SnowmassHpcPostprocessTests(unittest.TestCase):
    def test_task_manifest_rejects_zero_or_missing_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tasks.json"
            path.write_text('{"schema_version":1,"tasks":[]}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "non-empty"):
                HPC.load_task_manifest(path)

    def test_task_selection_is_one_based_and_bounds_checked(self) -> None:
        manifest = {"tasks": [{"record_id": "a"}, {"record_id": "b"}]}
        self.assertEqual(HPC.select_task(manifest, 2)["record_id"], "b")
        with self.assertRaisesRegex(ValueError, "between 1 and 2"):
            HPC.select_task(manifest, 0)

    def test_verify_snapshot_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "paper" / "manifest.json"
            artifact.parent.mkdir()
            artifact.write_text("original", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "files": [{"path": "paper/manifest.json", "sha256": HPC.sha256(artifact)}],
                "tasks": [{"record_id": "a"}],
            }
            path = root / "tasks.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            HPC.verify_snapshot(root, manifest)
            artifact.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                HPC.verify_snapshot(root, manifest)

    def test_safe_relative_path_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, "escapes"):
                HPC.safe_path(root, "../secret")


if __name__ == "__main__":
    unittest.main()
