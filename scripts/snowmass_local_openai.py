#!/usr/bin/env python3
"""Loopback-only OpenAI chat client for zero-paid local Snowmass shadow runs."""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any
import urllib.request
from urllib.parse import urlparse

from snowmass_batch_budget import RequestLimitExceededError


class LocalOpenAIClient:
    is_zero_cost_local = True

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        model_manifest_sha256: str,
        max_calls: int,
        timeout_seconds: int = 900,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("local model endpoint must be an HTTP loopback address")
        if not model.strip():
            raise ValueError("local model identity must be non-empty")
        if not re.fullmatch(r"[0-9a-f]{64}", model_manifest_sha256):
            raise ValueError("local model manifest SHA-256 must be 64 lowercase hex characters")
        if timeout_seconds <= 0:
            raise ValueError("local model timeout must be positive")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls <= 0:
            raise ValueError("local model request cap must be a positive integer")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_manifest_sha256 = model_manifest_sha256
        self.timeout_seconds = timeout_seconds
        self.max_calls = max_calls
        self.local_model_calls = 0
        self.local_model_attempts = 0
        self._lock = threading.Lock()

    def complete(
        self,
        instructions: str,
        input_text: str,
        max_output_tokens: int,
    ) -> tuple[dict[str, Any], float]:
        with self._lock:
            if self.local_model_attempts >= self.max_calls:
                raise RequestLimitExceededError(
                    f"local model request cap exceeded: {self.max_calls}"
                )
            self.local_model_attempts += 1
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": input_text},
            ],
            "max_tokens": int(max_output_tokens),
            "temperature": 0.15,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as stream:
                raw = json.loads(stream.read().decode("utf-8"))
        except Exception as error:
            raise RuntimeError(f"local model request failed: {type(error).__name__}: {error}") from error
        if not isinstance(raw, dict) or raw.get("model") != self.model:
            observed = raw.get("model") if isinstance(raw, dict) else None
            raise RuntimeError(
                f"local model identity mismatch: expected {self.model!r}, observed {observed!r}"
            )
        choices = raw.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise RuntimeError("local model response must contain exactly one choice")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("local model response content is blank or invalid")
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        response = {
            "id": str(raw.get("id") or "local-model-response"),
            "status": "completed",
            "model": self.model,
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
        }
        with self._lock:
            self.local_model_calls += 1
        return response, round(time.monotonic() - started, 3)
