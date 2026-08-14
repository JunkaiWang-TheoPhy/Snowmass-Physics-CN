#!/usr/bin/env python3
"""Tests for the loopback-only local model client."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import snowmass_local_openai as local_openai


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class LocalOpenAIClientTests(unittest.TestCase):
    def test_remote_endpoint_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            local_openai.LocalOpenAIClient(
                base_url="https://example.com",
                model="local/model",
                model_manifest_sha256="a" * 64,
                max_calls=1,
            )

    def test_chat_completion_is_normalized_without_credentials(self) -> None:
        observed = {}

        def urlopen(request, timeout):
            observed["url"] = request.full_url
            observed["authorization"] = request.headers.get("Authorization")
            observed["payload"] = json.loads(request.data)
            observed["timeout"] = timeout
            return _Response(
                json.dumps(
                    {
                        "id": "chat-1",
                        "model": "local/model",
                        "choices": [{"message": {"content": "本地译文"}}],
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 7,
                            "total_tokens": 18,
                        },
                    }
                ).encode()
            )

        client = local_openai.LocalOpenAIClient(
            base_url="http://127.0.0.1:8000",
            model="local/model",
            model_manifest_sha256="a" * 64,
            max_calls=1,
        )
        with mock.patch.object(local_openai.urllib.request, "urlopen", side_effect=urlopen):
            response, latency = client.complete("system", "input", 64)

        self.assertEqual(observed["url"], "http://127.0.0.1:8000/v1/chat/completions")
        self.assertIsNone(observed["authorization"])
        self.assertEqual(observed["payload"]["model"], "local/model")
        self.assertEqual(response["model"], "local/model")
        self.assertEqual(response["usage"]["input_tokens"], 11)
        self.assertEqual(response["usage"]["output_tokens"], 7)
        self.assertEqual(client.local_model_calls, 1)
        self.assertEqual(client.local_model_attempts, 1)
        self.assertGreaterEqual(latency, 0)

    def test_model_identity_mismatch_fails_closed(self) -> None:
        client = local_openai.LocalOpenAIClient(
            base_url="http://localhost:8000",
            model="expected/model",
            model_manifest_sha256="b" * 64,
            max_calls=1,
        )
        response = _Response(
            json.dumps(
                {
                    "id": "chat-1",
                    "model": "wrong/model",
                    "choices": [{"message": {"content": "text"}}],
                    "usage": {},
                }
            ).encode()
        )
        with (
            mock.patch.object(local_openai.urllib.request, "urlopen", return_value=response),
            self.assertRaisesRegex(RuntimeError, "model identity mismatch"),
        ):
            client.complete("system", "input", 64)
        self.assertEqual(client.local_model_attempts, 1)
        self.assertEqual(client.local_model_calls, 0)

    def test_request_cap_is_enforced_before_network_access(self) -> None:
        client = local_openai.LocalOpenAIClient(
            base_url="http://localhost:8000",
            model="local/model",
            model_manifest_sha256="b" * 64,
            max_calls=1,
        )
        client.local_model_attempts = 1
        with (
            mock.patch.object(local_openai.urllib.request, "urlopen") as urlopen,
            self.assertRaisesRegex(
                local_openai.RequestLimitExceededError,
                "request cap exceeded",
            ),
        ):
            client.complete("system", "input", 64)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
