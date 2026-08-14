#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_snowmass_local_shadow_readiness as readiness


class LocalShadowReadinessTests(unittest.TestCase):
    def test_missing_runtime_and_identity_fail_closed(self) -> None:
        with (
            mock.patch.object(readiness.platform, "machine", return_value="arm64"),
            mock.patch.object(readiness, "physical_memory_bytes", return_value=64 * 2**30),
            mock.patch.object(readiness.shutil, "disk_usage", return_value=(2**40, 0, 900 * 2**30)),
            mock.patch.object(readiness.shutil, "which", side_effect=lambda name: "/usr/sbin/lsof" if name == "lsof" else None),
            mock.patch.object(readiness.importlib.util, "find_spec", return_value=None),
        ):
            report = readiness.readiness_report(workspace=Path("."))

        self.assertFalse(report["ready"])
        self.assertIn("python_module_missing:mlx", report["blockers"])
        self.assertIn("local_model_manifest_not_configured", report["blockers"])
        self.assertIn("local_server_binary_not_configured", report["blockers"])

    def test_complete_attested_configuration_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            binary = root / "python"
            manifest.write_text("{}", encoding="utf-8")
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            with (
                mock.patch.object(readiness.platform, "machine", return_value="arm64"),
                mock.patch.object(readiness, "physical_memory_bytes", return_value=64 * 2**30),
                mock.patch.object(readiness.shutil, "disk_usage", return_value=(2**40, 0, 900 * 2**30)),
                mock.patch.object(readiness.shutil, "which", return_value="/usr/sbin/lsof"),
                mock.patch.object(readiness.importlib.util, "find_spec", return_value=object()),
                mock.patch.object(
                    readiness.local_attestation,
                    "verify_local_execution",
                    return_value={
                        "model_manifest_sha256": "a" * 64,
                        "model_root": "/models/qwen",
                        "server_binary_sha256": "b" * 64,
                        "server_command_sha256": "c" * 64,
                    },
                ),
            ):
                report = readiness.readiness_report(
                    workspace=root,
                    base_url="http://127.0.0.1:8000",
                    model="local/model",
                    model_manifest=manifest,
                    server_binary=binary,
                )

        self.assertTrue(report["ready"], report["blockers"])
        self.assertEqual(report["attestation"]["server_binary_sha256"], "b" * 64)


if __name__ == "__main__":
    unittest.main()
