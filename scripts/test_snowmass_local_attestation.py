#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import snowmass_local_attestation as attestation


class LocalAttestationTests(unittest.TestCase):
    def test_builder_creates_a_verifiable_complete_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "model"
            root.mkdir()
            (root / "weights.bin").write_bytes(b"weights")
            manifest = Path(temporary) / "manifest.json"
            attestation.build_model_manifest(
                model_root=root,
                model="local/model",
                output=manifest,
            )
            result = attestation.verify_model_manifest(
                manifest,
                expected_model="local/model",
            )
            self.assertEqual(result["model_root"], str(root.resolve()))

    def test_manifest_hashes_real_complete_model_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "model"
            root.mkdir()
            weight = root / "weights.bin"
            weight.write_bytes(b"weights")
            manifest = Path(temporary) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "model": "local/model",
                        "model_root": str(root),
                        "files": [{
                            "path": "weights.bin",
                            "size": 7,
                            "sha256": hashlib.sha256(b"weights").hexdigest(),
                        }],
                    }
                ),
                encoding="utf-8",
            )
            result = attestation.verify_model_manifest(manifest, expected_model="local/model")
            self.assertEqual(result["model_root"], str(root.resolve()))
            weight.write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                attestation.verify_model_manifest(manifest, expected_model="local/model")

    def test_server_listener_must_match_binary_and_model_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "python"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            responses = [
                mock.Mock(returncode=0, stdout="p123\n"),
                mock.Mock(returncode=0, stdout=f"n{binary.resolve()}\n"),
                mock.Mock(returncode=0, stdout="python -m server /models/qwen\n"),
            ]
            with mock.patch.object(attestation.subprocess, "run", side_effect=responses):
                result = attestation.attest_local_server(
                    base_url="http://127.0.0.1:8000",
                    server_binary=binary,
                    required_model_root="/models/qwen",
                )
            self.assertEqual(result["server_binary_sha256"], attestation.sha256_file(binary))
