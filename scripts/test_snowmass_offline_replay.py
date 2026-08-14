#!/usr/bin/env python3
"""Tests for the fail-closed Snowmass offline shadow replay client."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_snowmass_refined_translation as refined
import run_snowmass_translation as runner
import snowmass_offline_replay as replay
import snowmass_style_batching as style_batching


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


class OfflineReplayClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.article = Path(self.temporary.name)
        (self.article / "manifest.json").write_text(
            json.dumps({"record_id": "arxiv:test"}), encoding="utf-8"
        )

    def test_replays_paper_and_chunk_requests_by_exact_identity(self) -> None:
        paper_text = "## Content Summary\n已验证分析。\n"
        paper_hash = _write(self.article / "01-analysis.md", paper_text)
        instructions = "paper instructions"
        input_text = "paper input"
        maximum = 100
        (self.article / "paper_status.json").write_text(
            json.dumps(
                {
                    "record_id": "arxiv:test",
                    "phases": {
                        "analysis": {
                            "status": "complete",
                            "input_hash": refined._paper_phase_input_hash(
                                instructions, input_text, maximum
                            ),
                            "output_file": "01-analysis.md",
                            "output_hash": paper_hash,
                            "max_output_tokens": maximum,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        stage = "translate"
        chunk_instructions = "chunk instructions"
        chunk_input = "chunk input"
        chunk_maximum = 200
        request_key = runner.request_key(
            stage=stage,
            model=runner.MODEL,
            instructions=chunk_instructions,
            input_text=chunk_input,
            max_output_tokens=chunk_maximum,
        )
        protected = "[[SM_0000_X]]译文\n"
        protected_hash = _write(
            self.article / "stage_subrequests/chunk0001_translate_0001.protected.md",
            protected,
        )
        (self.article / "chunk_status").mkdir()
        (self.article / "chunk_status/chunk0001.json").write_text(
            json.dumps(
                {
                    "chunk_id": "chunk0001",
                    "stages": {
                        stage: {
                            "subrequests": [
                                {
                                    "status": "complete",
                                    "request_key": request_key,
                                    "output_file": "stage_subrequests/chunk0001_translate_0001.protected.md",
                                    "output_hash": protected_hash,
                                }
                            ]
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        client = replay.OfflineReplayClient(self.article)
        paper_response, _ = client.complete(instructions, input_text, maximum)
        chunk_response, _ = client.complete(
            chunk_instructions, chunk_input, chunk_maximum
        )

        self.assertEqual(runner.validate_response(paper_response, runner.MODEL).text, paper_text)
        self.assertEqual(runner.validate_response(chunk_response, runner.MODEL).text, protected)
        self.assertEqual(client.replay_calls, 2)
        self.assertRegex(client.fixture_sha256, r"^[0-9a-f]{64}$")

    def test_replays_style_batch_with_protected_verified_outputs(self) -> None:
        (self.article / "paper_status.json").write_text(
            json.dumps({"record_id": "arxiv:test", "phases": {}}), encoding="utf-8"
        )
        output_text = "学术译文 $x$。\n"
        output_hash = _write(self.article / "output_chunk0001.md", output_text)
        (self.article / "chunk_status").mkdir()
        protected_input = "草稿 [[SM_0000_X]]。"
        item_key = "item-key-1"
        instructions = "STYLE-BATCH JSON PROTOCOL"
        maximum = 100
        batch_item = style_batching.StyleBatchItem(
            chunk_id="chunk0001",
            protected_text=protected_input,
            source_hash="source-hash",
            prior_hash="prior-hash",
            glossary_text="",
            context="",
            item_key=item_key,
        )
        request_key = style_batching.style_batch_request_key(
            batch=style_batching.StyleBatch((batch_item,)),
            stage="academic",
            model=runner.MODEL,
            instructions=instructions,
            max_output_tokens=maximum,
        )
        (self.article / "chunk_status/chunk0001.json").write_text(
            json.dumps(
                {
                    "chunk_id": "chunk0001",
                    "stages": {
                        "academic": {
                            "status": "complete",
                            "execution_policy": "model_pipeline",
                            "item_key": item_key,
                            "request_key": request_key,
                            "output_file": "output_chunk0001.md",
                            "output_hash": output_hash,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        payload = {
            "protocol": "snowmass-style-batch-v1",
            "stage": "academic",
            "chunks": [
                {
                    "id": "chunk0001",
                    "text": protected_input,
                    "locked_terminology": "",
                    "read_only_context": "",
                }
            ],
        }
        (self.article / "style_batch_status.json").write_text(
            json.dumps(
                {
                    "stages": {
                        "academic": {
                            "requests": [
                                {
                                    "status": "settled",
                                    "request_key": request_key,
                                    "chunk_ids": ["chunk0001"],
                                    "recovery": False,
                                }
                            ]
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        client = replay.OfflineReplayClient(self.article)
        response, _ = client.complete(
            instructions,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            maximum,
        )
        response_text = runner.validate_response(response, runner.MODEL).text
        translations = json.loads(response_text)["translations"]

        self.assertEqual(
            translations["chunk0001"], runner.protect_stage_text(output_text)[0]
        )

        changed_payload = json.loads(json.dumps(payload))
        changed_payload["chunks"][0]["text"] = "变化后的草稿 [[SM_0000_X]]。"
        with self.assertRaisesRegex(replay.OfflineReplayMissError, "no exact fixture"):
            client.complete(
                instructions,
                json.dumps(changed_payload, ensure_ascii=False, sort_keys=True),
                maximum,
            )

    def test_unknown_request_fails_closed(self) -> None:
        (self.article / "paper_status.json").write_text(
            json.dumps({"record_id": "arxiv:test", "phases": {}}), encoding="utf-8"
        )
        client = replay.OfflineReplayClient(self.article)

        with self.assertRaisesRegex(replay.OfflineReplayMissError, "no exact fixture"):
            client.complete("unknown", "request", 10)


if __name__ == "__main__":
    unittest.main()
