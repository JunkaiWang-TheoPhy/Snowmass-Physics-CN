#!/usr/bin/env python3
"""Regression tests for the rights gate in the Snowmass translator."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run_snowmass_translation.py")
SPEC = importlib.util.spec_from_file_location("run_snowmass_translation", MODULE_PATH)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def completed_response(text: str = "译文\n", *, model: str = RUNNER.MODEL) -> dict[str, object]:
    return {
        "id": "resp_123",
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 1},
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 30,
        },
    }


class RightsGateTests(unittest.TestCase):
    def test_loader_accepts_only_explicit_publication_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / "papers.json"
            manifest_path.write_text(
                json.dumps(
                    [
                        {"record_id": "arxiv:allowed", "publication_allowed": True},
                        {"record_id": "arxiv:blocked", "publication_allowed": False},
                        {"record_id": "arxiv:unknown", "publication_allowed": None},
                        {"record_id": "arxiv:missing"},
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                RUNNER.load_allowed_record_ids(manifest_path),
                {"arxiv:allowed"},
            )

    def test_task_collection_excludes_records_outside_the_rights_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            papers = root / "papers"
            for slug, record_id in (
                ("arxiv_allowed", "arxiv:allowed"),
                ("arxiv_blocked", "arxiv:blocked"),
            ):
                article = papers / slug
                article.mkdir(parents=True)
                (article / "manifest.json").write_text(
                    json.dumps(
                        {
                            "chunks": [
                                {
                                    "id": "chunk0001",
                                    "order": 1,
                                    "source_file": "chunk0001.md",
                                    "output_file": "output_chunk0001.md",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                (article / "chunking_status.json").write_text(
                    json.dumps({"record_id": record_id}),
                    encoding="utf-8",
                )

            tasks = RUNNER.collect_tasks(
                root,
                max_articles=0,
                max_chunks=0,
                article_filter=None,
                allowed_record_ids={"arxiv:allowed"},
            )

            self.assertEqual(
                [(task["record_id"], task["chunk"]["id"]) for task in tasks],
                [("arxiv:allowed", "chunk0001")],
            )


class ResponseValidationTests(unittest.TestCase):
    def test_validate_response_accepts_completed_response(self) -> None:
        parsed = RUNNER.validate_response(completed_response("最终译文"), RUNNER.MODEL)

        self.assertEqual(parsed.text, "最终译文\n")
        self.assertEqual(parsed.response_id, "resp_123")
        self.assertEqual(parsed.model, RUNNER.MODEL)
        self.assertEqual(parsed.status, "completed")
        self.assertEqual(parsed.output_hash, hashlib.sha256("最终译文\n".encode("utf-8")).hexdigest())
        self.assertEqual(parsed.usage["total_tokens"], 30)

    def test_incomplete_response_is_not_accepted(self) -> None:
        with self.assertRaises(RUNNER.IncompleteResponseError):
            RUNNER.validate_response(
                {
                    "id": "resp_123",
                    "status": "incomplete",
                    "model": RUNNER.MODEL,
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
                RUNNER.MODEL,
            )

    def test_failed_response_is_not_accepted(self) -> None:
        with self.assertRaises(RUNNER.FailedResponseError):
            RUNNER.validate_response(
                {
                    "id": "resp_123",
                    "status": "failed",
                    "model": RUNNER.MODEL,
                    "error": {"message": "backend rejected the prompt"},
                },
                RUNNER.MODEL,
            )

    def test_wrong_model_is_not_accepted(self) -> None:
        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(completed_response(model="deepseek-chat"), RUNNER.MODEL)

    def test_completed_response_without_output_is_not_accepted(self) -> None:
        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(
                {
                    "id": "resp_123",
                    "status": "completed",
                    "model": RUNNER.MODEL,
                    "output": [],
                    "usage": {"total_tokens": 0},
                },
                RUNNER.MODEL,
            )

    def test_completed_response_with_malformed_output_is_not_accepted(self) -> None:
        response = completed_response()
        response["output"] = {"type": "message"}

        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(response, RUNNER.MODEL)

    def test_completed_response_with_malformed_content_is_not_accepted(self) -> None:
        response = completed_response()
        response["output"] = [{"type": "message", "content": ["not-an-object"]}]

        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(response, RUNNER.MODEL)

    def test_malformed_input_token_details_are_not_accepted(self) -> None:
        response = completed_response()
        response["usage"]["input_tokens_details"] = ["not-an-object"]

        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(response, RUNNER.MODEL)

    def test_malformed_output_token_details_are_not_accepted(self) -> None:
        response = completed_response()
        response["usage"]["output_tokens_details"] = "not-an-object"

        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.validate_response(response, RUNNER.MODEL)


class RequestKeyAndCheckpointTests(unittest.TestCase):
    def test_request_key_is_deterministic_for_same_payload(self) -> None:
        first = RUNNER.request_key(
            stage="translate",
            model=RUNNER.MODEL,
            instructions="translate carefully",
            input_text="source chunk",
            max_output_tokens=4096,
        )
        second = RUNNER.request_key(
            stage="translate",
            model=RUNNER.MODEL,
            instructions="translate carefully",
            input_text="source chunk",
            max_output_tokens=4096,
        )
        other = RUNNER.request_key(
            stage="translate",
            model=RUNNER.MODEL,
            instructions="translate carefully",
            input_text="source chunk",
            max_output_tokens=4097,
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_nonempty_stale_output_is_not_a_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "stage1_chunk0001.md"
            output.write_text("旧结果\n", encoding="utf-8")
            status = {
                "status": "complete",
                "request_key": "old-key",
                "output_hash": hashlib.sha256("旧结果\n".encode("utf-8")).hexdigest(),
            }

            self.assertFalse(RUNNER.checkpoint_is_valid(status, output, "new-key"))

    def test_output_hash_mismatch_is_not_a_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "stage1_chunk0001.md"
            output.write_text("当前结果\n", encoding="utf-8")
            status = {
                "status": "complete",
                "request_key": "expected-key",
                "output_hash": hashlib.sha256("旧结果\n".encode("utf-8")).hexdigest(),
            }

            self.assertFalse(RUNNER.checkpoint_is_valid(status, output, "expected-key"))

    def test_matching_request_key_hash_and_passing_qc_form_a_valid_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "stage1_chunk0001.md"
            output.write_text("当前结果\n", encoding="utf-8")
            status = {
                "status": "complete",
                "request_key": "expected-key",
                "output_hash": hashlib.sha256("当前结果\n".encode("utf-8")).hexdigest(),
                "qc": {"ok": True, "failures": []},
            }

            self.assertTrue(RUNNER.checkpoint_is_valid(status, output, "expected-key"))

    def test_matching_legacy_checkpoint_without_qc_is_not_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "stage1_chunk0001.md"
            output.write_text("旧版本结果\n", encoding="utf-8")
            status = {
                "status": "complete",
                "request_key": "expected-key",
                "output_hash": hashlib.sha256("旧版本结果\n".encode("utf-8")).hexdigest(),
            }

            self.assertFalse(RUNNER.checkpoint_is_valid(status, output, "expected-key"))


class ProcessChunkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.article_dir = self.root / "papers" / "arxiv_allowed"
        self.article_dir.mkdir(parents=True)
        self.task = {
            "article_dir": self.article_dir,
            "record_id": "arxiv:allowed",
            "chunk": {
                "id": "chunk0001",
                "source_file": "chunk0001.md",
                "output_file": "output_chunk0001.md",
                "source_hash": "source-hash",
            },
        }
        (self.article_dir / "chunk0001.md").write_text("Original source paragraph.\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_process_chunk_persists_request_key_and_output_hash_for_completed_stage(self) -> None:
        class FakeClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                return completed_response("阶段产物"), 0.75

        result = RUNNER.process_chunk(self.task, FakeClient(), [])
        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]

        self.assertEqual(result["status"], "complete")
        self.assertEqual(translate["status"], "complete")
        self.assertTrue(translate["request_key"])
        self.assertEqual(translate["output_hash"], hashlib.sha256("阶段产物\n".encode("utf-8")).hexdigest())
        self.assertEqual(translate["raw_response"]["status"], "completed")

    def test_process_chunk_reprocesses_legacy_checkpoint_without_qc(self) -> None:
        class InitialClient:
            calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                self.calls += 1
                return completed_response("阶段产物"), 0.1

        initial_client = InitialClient()
        RUNNER.process_chunk(self.task, initial_client, [])
        status_path = self.article_dir / "chunk_status" / "chunk0001.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["stages"]["translate"].pop("qc")
        status_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")

        class RepairClient:
            calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                self.calls += 1
                return completed_response("阶段产物"), 0.1

        repair_client = RepairClient()
        result = RUNNER.process_chunk(self.task, repair_client, [])
        repaired_status = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "complete")
        self.assertEqual(repair_client.calls, 1)
        self.assertEqual(repaired_status["stages"]["translate"]["qc"], {"ok": True, "failures": []})

    def test_process_chunk_reuses_modern_checkpoints_without_paid_calls(self) -> None:
        class InitialClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                return completed_response("阶段产物"), 0.1

        RUNNER.process_chunk(self.task, InitialClient(), [])

        class NoCallClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                raise AssertionError("modern checkpoints must resume without a paid API call")

        result = RUNNER.process_chunk(self.task, NoCallClient(), [])

        self.assertEqual(result["status"], "complete")

    def test_process_chunk_marks_ambiguous_transport_failure_uncertain_without_output(self) -> None:
        class FakeClient:
            calls = 0

            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                self.calls += 1
                raise RUNNER.AmbiguousTransportError("connection reset before response body completed")

        client = FakeClient()
        result = RUNNER.process_chunk(self.task, client, [])
        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]

        self.assertEqual(client.calls, 1)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(translate["status"], "uncertain")
        self.assertFalse((self.article_dir / "stage1_chunk0001.md").exists())

    def test_process_chunk_persists_raw_response_before_validation_failure(self) -> None:
        class FakeClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                return {
                    "id": "resp_bad",
                    "status": "incomplete",
                    "model": RUNNER.MODEL,
                    "incomplete_details": {"reason": "max_output_tokens"},
                }, 0.2

        with self.assertRaises(RUNNER.IncompleteResponseError):
            RUNNER.process_chunk(self.task, FakeClient(), [])

        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]

        self.assertEqual(translate["status"], "failed")
        self.assertEqual(translate["raw_response"]["status"], "incomplete")
        self.assertEqual(translate["response_id"], "resp_bad")

    def test_process_chunk_persists_coarse_metadata_for_malformed_usage_details(self) -> None:
        malformed = completed_response()
        malformed["id"] = "resp_malformed_usage"
        malformed["usage"]["input_tokens_details"] = ["not-an-object"]

        class FakeClient:
            def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, object], float]:
                return malformed, 0.2

        with self.assertRaises(RUNNER.ResponseValidationError):
            RUNNER.process_chunk(self.task, FakeClient(), [])

        status = json.loads((self.article_dir / "chunk_status" / "chunk0001.json").read_text(encoding="utf-8"))
        translate = status["stages"]["translate"]

        self.assertEqual(translate["status"], "failed")
        self.assertEqual(translate["raw_response"]["id"], "resp_malformed_usage")
        self.assertEqual(translate["raw_response"]["status"], "completed")
        self.assertEqual(translate["raw_response"]["usage"]["input_tokens"], 10)
        self.assertIsNone(translate["raw_response"]["usage"]["cached_tokens"])
        self.assertIn("input_tokens_details", translate["error"])


class DeepSeekClientRetryTests(unittest.TestCase):
    def test_client_does_not_retry_ambiguous_transport_failures(self) -> None:
        client = RUNNER.DeepSeekClient("test-key", max_retries=3)

        with (
            mock.patch.object(
                RUNNER.urllib.request,
                "urlopen",
                side_effect=RUNNER.urllib.error.URLError("connection reset"),
            ) as urlopen,
            mock.patch.object(RUNNER.time, "sleep") as sleep,
        ):
            with self.assertRaises(RUNNER.AmbiguousTransportError):
                client.complete("instructions", "input", 2048)

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_client_retries_retryable_http_errors_only_up_to_the_bound(self) -> None:
        client = RUNNER.DeepSeekClient("test-key", max_retries=2)
        response = io.BytesIO(b'{"error":"rate limited"}')
        http_error = RUNNER.urllib.error.HTTPError(
            RUNNER.API_URL,
            429,
            "Too Many Requests",
            hdrs=None,
            fp=response,
        )

        with (
            mock.patch.object(RUNNER.urllib.request, "urlopen", side_effect=[http_error, http_error, http_error]) as urlopen,
            mock.patch.object(RUNNER.time, "sleep") as sleep,
            mock.patch.object(RUNNER.random, "random", return_value=0.0),
        ):
            with self.assertRaises(RuntimeError):
                client.complete("instructions", "input", 2048)

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
