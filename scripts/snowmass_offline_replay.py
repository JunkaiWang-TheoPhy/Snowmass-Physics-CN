#!/usr/bin/env python3
"""Fail-closed model-response replay for zero-paid Snowmass shadow runs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
from typing import Any

import run_snowmass_refined_translation as refined
import run_snowmass_translation as runner
import snowmass_style_batching as style_batching


class OfflineReplayMissError(RuntimeError):
    """Raised when a shadow request has no exact verified fixture response."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise OfflineReplayMissError(f"fixture path escapes article root: {relative}") from error
    return candidate


class OfflineReplayClient:
    """Replay exact historical model outputs without network or paid API use."""

    is_offline_replay = True

    def __init__(self, article_dir: Path) -> None:
        self.article_dir = Path(article_dir).resolve()
        self._paper_outputs: dict[str, str] = {}
        self._chunk_outputs: dict[str, str] = {}
        self._style_items: dict[str, dict[str, dict[str, str]]] = {}
        self._style_requests: dict[str, dict[str, str]] = {}
        self._evidence_paths: set[Path] = set()
        self._lock = threading.Lock()
        self.replay_calls = 0
        self.record_id = self._load_record_id()
        self._load_paper_outputs()
        self._load_chunk_outputs()
        self._load_style_requests()
        self.fixture_sha256 = self._fixture_hash()

    def _load_json(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise OfflineReplayMissError(f"invalid replay evidence: {path}") from error
        if not isinstance(payload, dict):
            raise OfflineReplayMissError(f"replay evidence is not an object: {path}")
        self._evidence_paths.add(path)
        return payload

    def _load_record_id(self) -> str:
        manifest = self._load_json(self.article_dir / "manifest.json")
        record_id = manifest.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise OfflineReplayMissError("fixture manifest has no record_id")
        return record_id

    def _artifact_text(self, relative: Any, expected_hash: Any) -> str:
        if not isinstance(relative, str) or not relative:
            raise OfflineReplayMissError("fixture artifact has no relative path")
        if not isinstance(expected_hash, str) or not expected_hash:
            raise OfflineReplayMissError(f"fixture artifact has no hash: {relative}")
        direct = _inside(self.article_dir, relative)
        candidates = [direct] if direct.is_file() else []
        if not candidates and Path(relative).parent == Path("."):
            candidates = [
                path for path in self.article_dir.rglob(Path(relative).name) if path.is_file()
            ]
        matches = [path for path in candidates if _sha256(path) == expected_hash]
        if len(matches) != 1:
            raise OfflineReplayMissError(
                f"fixture artifact is missing or ambiguous: {relative} ({expected_hash})"
            )
        self._evidence_paths.add(matches[0])
        return matches[0].read_text(encoding="utf-8")

    @staticmethod
    def _register(index: dict[str, str], key: str, text: str, label: str) -> None:
        existing = index.get(key)
        if existing is not None and existing != text:
            raise OfflineReplayMissError(f"conflicting replay outputs for {label}: {key}")
        index[key] = text

    def _load_paper_outputs(self) -> None:
        status = self._load_json(self.article_dir / "paper_status.json")
        if status.get("record_id") not in {None, self.record_id}:
            raise OfflineReplayMissError("paper status record_id does not match fixture manifest")
        phases = status.get("phases")
        if not isinstance(phases, dict):
            raise OfflineReplayMissError("paper status has no phases object")
        for name, phase in phases.items():
            if not isinstance(phase, dict):
                continue
            if phase.get("status") != "complete" or int(phase.get("max_output_tokens") or 0) <= 0:
                continue
            key = phase.get("input_hash")
            if not isinstance(key, str) or not key:
                raise OfflineReplayMissError(f"paper fixture phase has no input hash: {name}")
            try:
                text = self._artifact_text(phase.get("output_file"), phase.get("output_hash"))
            except OfflineReplayMissError:
                continue
            self._register(self._paper_outputs, key, text, f"paper phase {name}")

    def _load_chunk_outputs(self) -> None:
        status_dir = self.article_dir / "chunk_status"
        if not status_dir.is_dir():
            return
        for status_path in sorted(status_dir.glob("*.json")):
            status = self._load_json(status_path)
            chunk_id = str(status.get("chunk_id") or status_path.stem)
            stages = status.get("stages")
            if not isinstance(stages, dict):
                continue
            for stage_name, stage in stages.items():
                if not isinstance(stage, dict):
                    continue
                subrequests = stage.get("subrequests")
                if isinstance(subrequests, list):
                    for subrequest in subrequests:
                        if not isinstance(subrequest, dict) or subrequest.get("status") != "complete":
                            continue
                        key = subrequest.get("request_key")
                        if not isinstance(key, str) or not key:
                            continue
                        try:
                            text = self._artifact_text(
                                subrequest.get("output_file"), subrequest.get("output_hash")
                            )
                        except OfflineReplayMissError:
                            continue
                        self._register(
                            self._chunk_outputs,
                            key,
                            text,
                            f"{chunk_id}/{stage_name}",
                        )
                if (
                    stage.get("status") == "complete"
                    and stage.get("execution_policy") == "model_pipeline"
                ):
                    try:
                        text = self._artifact_text(
                            stage.get("output_file"), stage.get("output_hash")
                        )
                    except OfflineReplayMissError:
                        continue
                    item_key = stage.get("item_key")
                    request_key = stage.get("request_key")
                    if not isinstance(item_key, str) or not isinstance(request_key, str):
                        continue
                    self._style_items.setdefault(str(stage_name), {})[chunk_id] = {
                        "item_key": item_key,
                        "request_key": request_key,
                        "output": text,
                    }

    def _load_style_requests(self) -> None:
        path = self.article_dir / "style_batch_status.json"
        if not path.is_file():
            return
        status = self._load_json(path)
        stages = status.get("stages")
        if not isinstance(stages, dict):
            return
        for stage_name, stage_status in stages.items():
            requests = stage_status.get("requests") if isinstance(stage_status, dict) else None
            if not isinstance(requests, list):
                continue
            for request in requests:
                if (
                    not isinstance(request, dict)
                    or request.get("status") != "settled"
                    or request.get("recovery") is True
                ):
                    continue
                request_key = request.get("request_key")
                chunk_ids = request.get("chunk_ids")
                if not isinstance(request_key, str) or not isinstance(chunk_ids, list):
                    continue
                translations: dict[str, str] = {}
                for raw_chunk_id in chunk_ids:
                    chunk_id = str(raw_chunk_id)
                    item = self._style_items.get(str(stage_name), {}).get(chunk_id)
                    if item is None or item["request_key"] != request_key:
                        translations = {}
                        break
                    translations[chunk_id] = item["output"]
                if translations:
                    existing = self._style_requests.get(request_key)
                    if existing is not None and existing != translations:
                        raise OfflineReplayMissError(
                            f"conflicting style replay outputs: {request_key}"
                        )
                    self._style_requests[request_key] = translations

    def _fixture_hash(self) -> str:
        entries = []
        for path in sorted(self._evidence_paths):
            entries.append(
                {
                    "path": path.relative_to(self.article_dir).as_posix(),
                    "sha256": _sha256(path),
                }
            )
        payload = {
            "schema_version": 1,
            "record_id": self.record_id,
            "evidence": entries,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _response(text: str, identity: str) -> dict[str, Any]:
        return {
            "id": f"offline-replay-{identity[:24]}",
            "status": "completed",
            "model": runner.MODEL,
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {
                "input_tokens": 0,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 0,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 0,
            },
        }

    def complete(
        self, instructions: str, input_text: str, max_output_tokens: int
    ) -> tuple[dict[str, Any], float]:
        text: str | None = None
        identity = ""
        try:
            payload = json.loads(input_text)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if (
            isinstance(payload, dict)
            and payload.get("protocol") == style_batching.STYLE_BATCH_PROTOCOL
        ):
            stage = str(payload.get("stage") or "")
            chunks = payload.get("chunks")
            if not isinstance(chunks, list) or not stage:
                raise OfflineReplayMissError("invalid style-batch replay request")
            batch_items: list[style_batching.StyleBatchItem] = []
            for item in chunks:
                chunk_id = str(item.get("id") or "") if isinstance(item, dict) else ""
                fixture_item = self._style_items.get(stage, {}).get(chunk_id)
                if fixture_item is None or not isinstance(item, dict):
                    raise OfflineReplayMissError(
                        f"no exact fixture for style request: {stage}/{chunk_id}"
                    )
                batch_items.append(
                    style_batching.StyleBatchItem(
                        chunk_id=chunk_id,
                        protected_text=str(item.get("text") or ""),
                        source_hash="fixture-bound-by-item-key",
                        prior_hash="fixture-bound-by-item-key",
                        glossary_text=str(item.get("locked_terminology") or ""),
                        context=str(item.get("read_only_context") or ""),
                        item_key=fixture_item["item_key"],
                    )
                )
            request_key = style_batching.style_batch_request_key(
                batch=style_batching.StyleBatch(tuple(batch_items)),
                stage=stage,
                model=runner.MODEL,
                instructions=instructions,
                max_output_tokens=max_output_tokens,
            )
            fixture_outputs = self._style_requests.get(request_key)
            if fixture_outputs is None:
                raise OfflineReplayMissError(
                    f"no exact fixture for style request: {request_key}"
                )
            translations = {
                item.chunk_id: runner.protect_stage_text(fixture_outputs[item.chunk_id])[0]
                for item in batch_items
            }
            text = json.dumps({"translations": translations}, ensure_ascii=False)
            identity = request_key
        if text is None:
            paper_key = refined._paper_phase_input_hash(
                instructions, input_text, max_output_tokens
            )
            text = self._paper_outputs.get(paper_key)
            identity = paper_key
        if text is None:
            for stage in ("translate", "terminology", "anti_ai", "revision", "academic"):
                key = runner.request_key(
                    stage=stage,
                    model=runner.MODEL,
                    instructions=instructions,
                    input_text=input_text,
                    max_output_tokens=max_output_tokens,
                )
                if key in self._chunk_outputs:
                    text = self._chunk_outputs[key]
                    identity = key
                    break
        if text is None:
            raise OfflineReplayMissError(
                "no exact fixture for offline shadow model request"
            )
        with self._lock:
            self.replay_calls += 1
        return self._response(text, identity), 0.0
