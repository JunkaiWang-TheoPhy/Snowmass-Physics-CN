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
        self.article = Path(self.temporary.name).resolve()
        (self.article / "manifest.json").write_text(
            json.dumps({"record_id": "arxiv:test"}), encoding="utf-8"
        )

    def _write_manifest_with_chunk(
        self,
        *,
        chunk_id: str = "chunk0001",
        source_file: str = "sources/chunk0001.md",
        output_file: str = "outputs/chunk0001.md",
        source_text: str = "Source paragraph with $x$.\n",
    ) -> dict[str, str]:
        source_hash = _write(self.article / source_file, source_text)
        manifest = {
            "record_id": "arxiv:test",
            "chunks": [
                {
                    "id": chunk_id,
                    "source_file": source_file,
                    "output_file": output_file,
                    "source_hash": source_hash,
                }
            ],
        }
        (self.article / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return {
            "chunk_id": chunk_id,
            "source_file": source_file,
            "output_file": output_file,
            "source_hash": source_hash,
            "source_text": source_text,
        }

    def _write_prompt_phase(self, prompt_text: str) -> None:
        prompt_hash = _write(self.article / "02-prompt.md", prompt_text)
        (self.article / "paper_status.json").write_text(
            json.dumps(
                {
                    "record_id": "arxiv:test",
                    "phases": {
                        "prompt": {
                            "status": "complete",
                            "input_hash": "prompt-input",
                            "output_file": "02-prompt.md",
                            "output_hash": prompt_hash,
                            "max_output_tokens": 1,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    def _write_chunk_stage(
        self,
        *,
        chunk_id: str,
        source_file: str,
        source_hash: str,
        stage: str,
        output_text: str,
    ) -> str:
        output_path = runner.stage_output_path(
            self.article, chunk_id, "outputs/chunk0001.md", stage
        )
        output_hash = _write(output_path, output_text)
        (self.article / "chunk_status").mkdir(exist_ok=True)
        (self.article / "chunk_status" / f"{chunk_id}.json").write_text(
            json.dumps(
                {
                    "chunk_id": chunk_id,
                    "source_file": source_file,
                    "source_hash": source_hash,
                    "stages": {
                        stage: {
                            "status": "complete",
                            "execution_policy": "model_pipeline",
                            "output_file": output_path.relative_to(self.article).as_posix(),
                            "output_hash": output_hash,
                            "qc": {"ok": True},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return output_hash

    @staticmethod
    def _style_batch_payload(
        *,
        stage: str,
        chunk_id: str,
        text: str,
        context: str,
    ) -> str:
        return json.dumps(
            {
                "protocol": style_batching.STYLE_BATCH_PROTOCOL,
                "stage": stage,
                "chunks": [
                    {
                        "id": chunk_id,
                        "text": text,
                        "locked_terminology": "",
                        "read_only_context": context,
                    }
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
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

    def test_replays_legacy_translate_checkpoint_for_current_batch_request(self) -> None:
        source = "Source $x$.\n"
        translated = "译文 $x$。\n"
        source_hash = _write(self.article / "chunk0001.md", source)
        translated_hash = _write(self.article / "stage1_chunk0001.md", translated)
        prompt = "locked paper context"
        prompt_hash = _write(self.article / "02-prompt.md", prompt)
        (self.article / "manifest.json").write_text(
            json.dumps(
                {
                    "record_id": "arxiv:test",
                    "chunks": [
                        {
                            "id": "chunk0001",
                            "source_file": "chunk0001.md",
                            "source_hash": source_hash,
                            "output_file": "output_chunk0001.md",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.article / "paper_status.json").write_text(
            json.dumps(
                {
                    "record_id": "arxiv:test",
                    "phases": {
                        "prompt": {
                            "status": "complete",
                            "output_file": "02-prompt.md",
                            "output_hash": prompt_hash,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.article / "chunk_status").mkdir()
        (self.article / "chunk_status/chunk0001.json").write_text(
            json.dumps(
                {
                    "chunk_id": "chunk0001",
                    "stages": {
                        "translate": {
                            "status": "complete",
                            "output_file": "stage1_chunk0001.md",
                            "output_hash": translated_hash,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        payload = {
            "protocol": style_batching.STYLE_BATCH_PROTOCOL,
            "stage": "translate",
            "chunks": [
                {
                    "id": "chunk0001",
                    "text": runner.protect_stage_text(source)[0],
                    "locked_terminology": "",
                    "read_only_context": prompt,
                }
            ],
        }

        client = replay.OfflineReplayClient(self.article)
        response, _ = client.complete(
            "current batch instructions",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            100,
        )
        translations = json.loads(
            runner.validate_response(response, runner.MODEL).text
        )["translations"]
        self.assertEqual(
            translations["chunk0001"], runner.protect_stage_text(translated)[0]
        )

        changed = json.loads(json.dumps(payload))
        changed["chunks"][0]["text"] = "drifted input"
        with self.assertRaisesRegex(replay.OfflineReplayMissError, "no exact fixture"):
            client.complete(
                "current batch instructions",
                json.dumps(changed, ensure_ascii=False, sort_keys=True),
                100,
            )

    def test_legacy_batch_replay_rejects_tampered_output_hash(self) -> None:
        source_hash = _write(self.article / "chunk0001.md", "source")
        output_hash = _write(self.article / "stage1_chunk0001.md", "translated")
        prompt_hash = _write(self.article / "02-prompt.md", "context")
        (self.article / "manifest.json").write_text(
            json.dumps(
                {
                    "record_id": "arxiv:test",
                    "chunks": [{
                        "id": "chunk0001", "source_file": "chunk0001.md",
                        "source_hash": source_hash, "output_file": "output_chunk0001.md",
                    }],
                }
            ), encoding="utf-8",
        )
        (self.article / "paper_status.json").write_text(
            json.dumps({"record_id": "arxiv:test", "phases": {"prompt": {
                "status": "complete", "output_file": "02-prompt.md",
                "output_hash": prompt_hash,
            }}}), encoding="utf-8",
        )
        (self.article / "chunk_status").mkdir()
        (self.article / "chunk_status/chunk0001.json").write_text(
            json.dumps({"chunk_id": "chunk0001", "stages": {"translate": {
                "status": "complete", "output_file": "stage1_chunk0001.md",
                "output_hash": output_hash,
            }}}), encoding="utf-8",
        )
        (self.article / "stage1_chunk0001.md").write_text("tampered", encoding="utf-8")
        payload = {"protocol": style_batching.STYLE_BATCH_PROTOCOL, "stage": "translate", "chunks": [{
            "id": "chunk0001", "text": runner.protect_stage_text("source")[0],
            "locked_terminology": "", "read_only_context": "context",
        }]}

        client = replay.OfflineReplayClient(self.article)
        with self.assertRaisesRegex(replay.OfflineReplayMissError, "no exact fixture"):
            client.complete("instructions", json.dumps(payload), 100)

    def test_unknown_request_fails_closed(self) -> None:
        (self.article / "paper_status.json").write_text(
            json.dumps({"record_id": "arxiv:test", "phases": {}}), encoding="utf-8"
        )
        client = replay.OfflineReplayClient(self.article)

        with self.assertRaisesRegex(replay.OfflineReplayMissError, "no exact fixture"):
            client.complete("unknown", "request", 10)

    def test_replays_translate_style_batch_from_verified_chunk_checkpoint(self) -> None:
        fixture = self._write_manifest_with_chunk()
        prompt_text = "# Paper Translation Brief\nUse exact terminology.\n"
        translate_output = "译文段落 $x$。\n"
        self._write_prompt_phase(prompt_text)
        self._write_chunk_stage(
            chunk_id=fixture["chunk_id"],
            source_file=fixture["source_file"],
            source_hash=fixture["source_hash"],
            stage="translate",
            output_text=translate_output,
        )
        client = replay.OfflineReplayClient(self.article)

        response, _ = client.complete(
            "STYLE-BATCH JSON PROTOCOL",
            self._style_batch_payload(
                stage="translate",
                chunk_id=fixture["chunk_id"],
                text=runner.protect_stage_text(fixture["source_text"])[0],
                context=prompt_text,
            ),
            100,
        )

        translations = json.loads(runner.validate_response(response, runner.MODEL).text)[
            "translations"
        ]
        self.assertEqual(
            translations[fixture["chunk_id"]],
            runner.protect_stage_text(translate_output)[0],
        )

    def test_replays_terminology_style_batch_from_verified_chunk_checkpoint(self) -> None:
        fixture = self._write_manifest_with_chunk()
        translate_output = "第一版译文 $x$。\n"
        terminology_output = "统一术语后的译文 $x$。\n"
        self._write_prompt_phase("# Paper Translation Brief\nPrompt.\n")
        self._write_chunk_stage(
            chunk_id=fixture["chunk_id"],
            source_file=fixture["source_file"],
            source_hash=fixture["source_hash"],
            stage="translate",
            output_text=translate_output,
        )
        output_path = runner.stage_output_path(
            self.article, fixture["chunk_id"], fixture["output_file"], "terminology"
        )
        output_hash = _write(output_path, terminology_output)
        (self.article / "chunk_status" / f"{fixture['chunk_id']}.json").write_text(
            json.dumps(
                {
                    "chunk_id": fixture["chunk_id"],
                    "source_file": fixture["source_file"],
                    "source_hash": fixture["source_hash"],
                    "stages": {
                        "translate": {
                            "status": "complete",
                            "execution_policy": "model_pipeline",
                            "output_file": "stage1_chunk0001.md",
                            "output_hash": hashlib.sha256(
                                translate_output.encode()
                            ).hexdigest(),
                            "qc": {"ok": True},
                        },
                        "terminology": {
                            "status": "complete",
                            "execution_policy": "model_pipeline",
                            "output_file": output_path.relative_to(self.article).as_posix(),
                            "output_hash": output_hash,
                            "qc": {"ok": True},
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        client = replay.OfflineReplayClient(self.article)

        response, _ = client.complete(
            "STYLE-BATCH JSON PROTOCOL",
            self._style_batch_payload(
                stage="terminology",
                chunk_id=fixture["chunk_id"],
                text=runner.protect_stage_text(translate_output)[0],
                context="",
            ),
            100,
        )

        translations = json.loads(runner.validate_response(response, runner.MODEL).text)[
            "translations"
        ]
        self.assertEqual(
            translations[fixture["chunk_id"]],
            runner.protect_stage_text(terminology_output)[0],
        )

    def test_translate_style_batch_fails_closed_when_input_text_drifts(self) -> None:
        fixture = self._write_manifest_with_chunk()
        self._write_prompt_phase("# Paper Translation Brief\nUse exact terminology.\n")
        self._write_chunk_stage(
            chunk_id=fixture["chunk_id"],
            source_file=fixture["source_file"],
            source_hash=fixture["source_hash"],
            stage="translate",
            output_text="译文段落 $x$。\n",
        )
        client = replay.OfflineReplayClient(self.article)

        with self.assertRaisesRegex(replay.OfflineReplayMissError, "no exact fixture"):
            client.complete(
                "STYLE-BATCH JSON PROTOCOL",
                self._style_batch_payload(
                    stage="translate",
                    chunk_id=fixture["chunk_id"],
                    text="漂移后的输入 [[SM_0000_X]]。\n",
                    context="# Paper Translation Brief\nUse exact terminology.\n",
                ),
                100,
            )

    def test_translate_style_batch_fails_closed_when_output_hash_drifts(self) -> None:
        fixture = self._write_manifest_with_chunk()
        prompt_text = "# Paper Translation Brief\nUse exact terminology.\n"
        self._write_prompt_phase(prompt_text)
        self._write_chunk_stage(
            chunk_id=fixture["chunk_id"],
            source_file=fixture["source_file"],
            source_hash=fixture["source_hash"],
            stage="translate",
            output_text="译文段落 $x$。\n",
        )
        output_path = runner.stage_output_path(
            self.article, fixture["chunk_id"], fixture["output_file"], "translate"
        )
        output_path.write_text("被篡改的译文。\n", encoding="utf-8")
        client = replay.OfflineReplayClient(self.article)

        with self.assertRaisesRegex(replay.OfflineReplayMissError, "no exact fixture"):
            client.complete(
                "STYLE-BATCH JSON PROTOCOL",
                self._style_batch_payload(
                    stage="translate",
                    chunk_id=fixture["chunk_id"],
                    text=runner.protect_stage_text(fixture["source_text"])[0],
                    context=prompt_text,
                ),
                100,
            )

    def test_non_translate_style_batch_does_not_fallback_to_chunk_checkpoint(self) -> None:
        fixture = self._write_manifest_with_chunk()
        self._write_prompt_phase("# Paper Translation Brief\nPrompt.\n")
        self._write_chunk_stage(
            chunk_id=fixture["chunk_id"],
            source_file=fixture["source_file"],
            source_hash=fixture["source_hash"],
            stage="academic",
            output_text="学术化译文 $x$。\n",
        )
        client = replay.OfflineReplayClient(self.article)

        with self.assertRaisesRegex(replay.OfflineReplayMissError, "no exact fixture"):
            client.complete(
                "STYLE-BATCH JSON PROTOCOL",
                self._style_batch_payload(
                    stage="academic",
                    chunk_id=fixture["chunk_id"],
                    text=runner.protect_stage_text(fixture["source_text"])[0],
                    context="",
                ),
                100,
            )


if __name__ == "__main__":
    unittest.main()
