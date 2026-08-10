#!/usr/bin/env python3
"""Checkpointed DeepSeek V4 Flash translation pipeline for Snowmass chunks."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from snowmass_translation_qc import stage_decision, validate_chunk


TRANSLATION_ROOT = Path("output/snowmass2021_translation")
RIGHTS_MANIFEST = Path("site/data/papers.json")
API_URL = "https://api.deepseek.com/responses"
MODEL = "deepseek-v4-flash"
STAGES = ("translate", "terminology", "anti_ai", "academic")
RETRYABLE_HTTP_CODES = {408, 409, 429, 500, 502, 503, 504}


class ResponseValidationError(RuntimeError):
    """The API returned a structurally invalid or unacceptable response."""


class IncompleteResponseError(ResponseValidationError):
    """The API reported an incomplete response."""


class FailedResponseError(ResponseValidationError):
    """The API reported a failed response."""


class AmbiguousTransportError(RuntimeError):
    """Transport failed before a trustworthy API response could be validated."""


class ParsedResponse:
    def __init__(
        self,
        *,
        text: str,
        response_id: str,
        model: str,
        status: str,
        usage: dict[str, Any],
        output_hash: str,
    ) -> None:
        self.text = text
        self.response_id = response_id
        self.model = model
        self.status = status
        self.usage = usage
        self.output_hash = output_hash


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def load_api_key() -> str:
    supplied = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if supplied:
        return supplied
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "codex_0805", "-s", "codex.deepseek.api", "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    key = result.stdout.strip()
    if result.returncode != 0 or not key:
        raise RuntimeError("DeepSeek Keychain credential is unavailable")
    return key


def load_glossary(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("terms", [])


def glossary_text(terms: list[dict[str, Any]]) -> str:
    lines = ["| Source term | Canonical Chinese | Note |", "|---|---|---|"]
    for term in terms:
        source = str(term.get("source", "")).replace("|", "\\|")
        target = str(term.get("target", "")).replace("|", "\\|")
        note = str(term.get("note", "")).replace("|", "\\|")
        lines.append(f"| {source} | {target} | {note} |")
    return "\n".join(lines)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_request_payload(instructions: str, input_text: str, max_output_tokens: int) -> dict[str, Any]:
    return {
        "model": MODEL,
        "instructions": instructions,
        "input": input_text,
        "reasoning": {"effort": "none"},
        "max_output_tokens": max_output_tokens,
        "temperature": 0.15,
        "text": {"format": {"type": "text"}},
    }


def request_key(
    *,
    stage: str,
    model: str,
    instructions: str,
    input_text: str,
    max_output_tokens: int,
) -> str:
    payload = {
        "stage": stage,
        **build_request_payload(instructions, input_text, max_output_tokens),
    }
    payload["model"] = model
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text_hash(serialized)


def response_usage(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage", {})
    if not isinstance(usage, dict):
        raise ResponseValidationError("DeepSeek response usage must be an object")
    input_details = usage.get("input_tokens_details")
    if input_details is not None and not isinstance(input_details, dict):
        raise ResponseValidationError("DeepSeek response usage.input_tokens_details must be an object")
    output_details = usage.get("output_tokens_details")
    if output_details is not None and not isinstance(output_details, dict):
        raise ResponseValidationError("DeepSeek response usage.output_tokens_details must be an object")
    return {
        "input_tokens": usage.get("input_tokens"),
        "cached_tokens": (input_details or {}).get("cached_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": (output_details or {}).get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def coarse_response_usage(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    return {
        "input_tokens": usage.get("input_tokens"),
        "cached_tokens": input_details.get("cached_tokens") if isinstance(input_details, dict) else None,
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None,
        "total_tokens": usage.get("total_tokens"),
    }


def response_metadata(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {"response_type": type(response).__name__}
    metadata: dict[str, Any] = {
        "id": str(response.get("id", "")),
        "status": response.get("status"),
        "model": response.get("model"),
        "incomplete_details": response.get("incomplete_details"),
        "error": response.get("error"),
        "usage": coarse_response_usage(response),
    }
    output = response.get("output")
    if isinstance(output, list):
        metadata["output_items"] = len(output)
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        metadata["output_text_length"] = len(output_text)
    return metadata


def extract_output(response: dict[str, Any]) -> str:
    texts: list[str] = []
    output = response.get("output", [])
    if output is None:
        output = []
    if not isinstance(output, list):
        raise ResponseValidationError("DeepSeek response output must be an array")
    for item in output:
        if not isinstance(item, dict):
            raise ResponseValidationError("DeepSeek response output items must be objects")
        if item.get("type") != "message":
            continue
        contents = item.get("content", [])
        if contents is None:
            contents = []
        if not isinstance(contents, list):
            raise ResponseValidationError("DeepSeek response message content must be an array")
        for content in contents:
            if not isinstance(content, dict):
                raise ResponseValidationError("DeepSeek response content items must be objects")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                texts.append(content["text"])
    if not texts and isinstance(response.get("output_text"), str):
        texts.append(response["output_text"])
    text = "\n".join(texts).strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1]).strip()
    if not text:
        raise ResponseValidationError("DeepSeek response contained no output_text")
    return text + "\n"


def validate_response(response: Any, expected_model: str) -> ParsedResponse:
    if not isinstance(response, dict):
        raise ResponseValidationError("DeepSeek response must be an object")
    status = response.get("status")
    model = str(response.get("model", ""))
    response_id = str(response.get("id", ""))

    if model != expected_model:
        raise ResponseValidationError(f"DeepSeek response used unexpected model: {model or '<missing>'}")
    if status == "incomplete":
        details = response.get("incomplete_details") or {}
        reason = details.get("reason", "unknown") if isinstance(details, dict) else "unknown"
        raise IncompleteResponseError(f"DeepSeek response incomplete: {reason}")
    if status == "failed":
        error = response.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("code") or "unknown")
        else:
            message = str(error or "unknown")
        raise FailedResponseError(f"DeepSeek response failed: {message}")
    if status != "completed":
        raise ResponseValidationError(f"DeepSeek response had unexpected status: {status!r}")
    text = extract_output(response)
    return ParsedResponse(
        text=text,
        response_id=response_id,
        model=model,
        status="completed",
        usage=response_usage(response),
        output_hash=text_hash(text),
    )


def checkpoint_is_valid(status: dict[str, Any], output_path: Path, expected_key: str) -> bool:
    if status.get("status") != "complete":
        return False
    if status.get("request_key") != expected_key:
        return False
    qc = status.get("qc")
    if not isinstance(qc, dict) or qc.get("ok") is not True:
        return False
    output_hash = status.get("output_hash")
    if not isinstance(output_hash, str) or not output_hash:
        return False
    if not nonempty(output_path):
        return False
    return text_hash(output_path.read_text(encoding="utf-8")) == output_hash


def stage_output_path(article_dir: Path, chunk_id: str, final_output_file: str, stage: str) -> Path:
    if stage == "translate":
        return article_dir / f"stage1_{chunk_id}.md"
    if stage == "terminology":
        return article_dir / f"stage2_{chunk_id}.md"
    if stage == "anti_ai":
        return article_dir / f"stage3_{chunk_id}.md"
    return article_dir / final_output_file


class DeepSeekClient:
    def __init__(self, api_key: str, max_retries: int = 5) -> None:
        self.api_key = api_key
        self.max_retries = max_retries

    def complete(self, instructions: str, input_text: str, max_output_tokens: int) -> tuple[dict[str, Any], float]:
        payload = build_request_payload(instructions, input_text, max_output_tokens)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            request = urllib.request.Request(
                API_URL,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Snowmass2021-local-translation/1.0",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=900) as response_stream:
                    response = json.loads(response_stream.read().decode("utf-8"))
                return response, round(time.monotonic() - started, 3)
            except urllib.error.HTTPError as error:
                error_body = error.read().decode("utf-8", errors="replace")
                last_error = f"HTTP {error.code}: {' '.join(error_body.split())[:500]}"
                if error.code not in RETRYABLE_HTTP_CODES or attempt >= self.max_retries:
                    raise RuntimeError(last_error) from error
            except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                raise AmbiguousTransportError(f"{type(error).__name__}: {error}") from error
            time.sleep(min(60, (2**attempt) + random.random() * 2))
        raise RuntimeError(last_error)


def stage_instructions(stage: str, glossary: str) -> str:
    common = """You are translating a high-energy physics or cosmology academic paper from English to Simplified Chinese.
The result is for scholarly readers. Preserve meaning exactly. Never add facts, explanations, examples, claims, citations, links, names, numbers, units, equations, symbols, or section order.
Preserve Markdown, LaTeX, inline math, citation markers, URLs, and line/block boundaries whenever possible.
Output only the complete revised Chinese text, with no preface, commentary, analysis, or quotation fences.
"""
    if stage == "translate":
        return common + f"""
Perform the first faithful translation pass. Use the following locked terminology table whenever a listed source term appears:
{glossary}
"""
    if stage == "terminology":
        return common + f"""
This is the terminology-unification pass. Revise the draft only where terminology, proper names, abbreviations, or recurring technical phrases are inconsistent with the locked table below. Keep already-correct sentences unchanged and do not stylistically rewrite the text.
Locked terminology:
{glossary}
"""
    if stage == "anti_ai":
        return common + """
This is the AI-mannerism cleanup pass. Remove formulaic AI phrasing, empty transitions, repetitive summaries, canned conclusions, excessive parallelism, and unnatural connective words. Keep the author's technical register and sentence logic. Do not shorten, summarize, embellish, or change terminology.
"""
    return common + """
This is the final Chinese naturalization and academic-polish pass. Make the Chinese read like careful human academic prose: precise, restrained, coherent, and idiomatic. Preserve the source's level of certainty and all technical content. Do not add interpretation or omit detail.
"""


def stage_input(stage: str, source: str, current: str, glossary: str) -> str:
    if stage == "translate":
        return source
    label = {
        "terminology": "DRAFT TRANSLATION",
        "anti_ai": "TERMINOLOGY-CORRECTED TRANSLATION",
        "academic": "AI-MANNERISM-CLEANED TRANSLATION",
    }[stage]
    return f"ORIGINAL SOURCE:\n---\n{source}\n---\n\n{label}:\n---\n{current}\n---\n\nLOCKED TERMINOLOGY:\n{glossary}\n"


def nonempty(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0 and bool(path.read_text(encoding="utf-8").strip())


def load_allowed_record_ids(path: Path) -> set[str]:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise RuntimeError(f"Rights manifest must be a JSON list: {path}")
    seen: set[str] = set()
    allowed: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RuntimeError(f"Rights manifest record {index} is not an object")
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise RuntimeError(f"Rights manifest record {index} has no record_id")
        if record_id in seen:
            raise RuntimeError(f"Duplicate record_id in rights manifest: {record_id}")
        seen.add(record_id)
        if record.get("publication_allowed") is True:
            allowed.add(record_id)
    if not allowed:
        raise RuntimeError(f"Rights manifest contains no explicitly allowed records: {path}")
    return allowed


def process_chunk(task: dict[str, Any], client: DeepSeekClient, terms: list[dict[str, Any]]) -> dict[str, Any]:
    article_dir = task["article_dir"]
    chunk = task["chunk"]
    chunk_id = chunk["id"]
    source_path = article_dir / chunk["source_file"]
    source = source_path.read_text(encoding="utf-8")
    mapping = chunk.get("mapping") or {}
    glossary = glossary_text(terms)
    status_path = article_dir / "chunk_status" / f"{chunk_id}.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {
            "schema_version": 1,
            "record_id": task["record_id"],
            "chunk_id": chunk_id,
            "source_file": chunk["source_file"],
            "source_hash": chunk.get("source_hash", ""),
            "stages": {},
        }
    except json.JSONDecodeError:
        status = {"schema_version": 1, "record_id": task["record_id"], "chunk_id": chunk_id, "stages": {}}

    current = source
    for stage in STAGES:
        output_path = stage_output_path(article_dir, chunk_id, chunk["output_file"], stage)
        instructions = stage_instructions(stage, glossary)
        input_text = stage_input(stage, source, current, glossary)
        # Keep long non-streaming responses below the local proxy's practical
        # response size while leaving enough headroom for Chinese output.
        max_output = max(4096, min(20000, int(max(len(current), 4000) * 0.8)))
        expected_key = request_key(
            stage=stage,
            model=MODEL,
            instructions=instructions,
            input_text=input_text,
            max_output_tokens=max_output,
        )
        stage_status = status.setdefault("stages", {}).setdefault(stage, {})
        if checkpoint_is_valid(stage_status, output_path, expected_key):
            current = output_path.read_text(encoding="utf-8")
            continue

        decision = stage_decision(stage, current, terms)
        qc_terms = [] if stage == "translate" else terms
        started = now()
        stage_status.update(
            {
                "started_at": started,
                "request_key": expected_key,
                "output_file": output_path.name,
                "decision": decision.to_dict(),
            }
        )
        for stale_field in ("finished_at", "error", "output_hash", "raw_response", "response_id", "usage", "qc", "max_output_tokens"):
            stage_status.pop(stale_field, None)

        if not decision.should_call_model:
            qc_report = validate_chunk(source, current, mapping, qc_terms)
            stage_status["qc"] = qc_report.to_dict()
            if not qc_report.ok:
                stage_status.update(
                    {
                        "status": "failed",
                        "finished_at": now(),
                        "error": f"QC failed: {', '.join(qc_report.failures)}",
                    }
                )
                status["status"] = "failed"
                status["updated_at"] = now()
                atomic_json(status_path, status)
                raise RuntimeError(stage_status["error"])

            atomic_text(output_path, current)
            stage_status.update(
                {
                    "status": "complete",
                    "finished_at": now(),
                    "output_hash": text_hash(current),
                }
            )
            atomic_json(status_path, status)
            continue

        stage_status.update(
            {
                "status": "running",
                "max_output_tokens": max_output,
            }
        )
        atomic_json(status_path, status)
        try:
            response, latency_seconds = client.complete(instructions, input_text, max_output)
        except AmbiguousTransportError as exc:
            stage_status.update({"status": "uncertain", "finished_at": now(), "error": str(exc)})
            status["status"] = "uncertain"
            status["updated_at"] = now()
            atomic_json(status_path, status)
            return {"record_id": task["record_id"], "chunk_id": chunk_id, "status": "uncertain"}
        except RuntimeError as exc:
            stage_status.update({"status": "failed", "finished_at": now(), "error": str(exc)})
            status["status"] = "failed"
            status["updated_at"] = now()
            atomic_json(status_path, status)
            raise

        stage_status["raw_response"] = response_metadata(response)
        stage_status["response_id"] = str(response.get("id", "")) if isinstance(response, dict) else ""
        atomic_json(status_path, status)

        try:
            parsed = validate_response(response, MODEL)
        except ResponseValidationError as exc:
            stage_status.update({"status": "failed", "finished_at": now(), "error": str(exc)})
            status["status"] = "failed"
            status["updated_at"] = now()
            atomic_json(status_path, status)
            raise

        qc_report = validate_chunk(source, parsed.text, mapping, qc_terms)
        stage_status["qc"] = qc_report.to_dict()
        if not qc_report.ok:
            stage_status.update(
                {
                    "status": "failed",
                    "finished_at": now(),
                    "error": f"QC failed: {', '.join(qc_report.failures)}",
                }
            )
            status["status"] = "failed"
            status["updated_at"] = now()
            atomic_json(status_path, status)
            raise RuntimeError(stage_status["error"])

        atomic_text(output_path, parsed.text)
        usage = dict(parsed.usage)
        usage["latency_seconds"] = latency_seconds
        stage_status.update(
            {
                "status": "complete",
                "finished_at": now(),
                "response_id": parsed.response_id,
                "usage": usage,
                "output_hash": parsed.output_hash,
            }
        )
        atomic_json(status_path, status)
        current = parsed.text

    meta_path = article_dir / f"{chunk['output_file'][:-3]}.meta.json"
    if not meta_path.exists():
        atomic_json(
            meta_path,
            {
                "schema_version": 1,
                "new_entities": [],
                "alias_hypotheses": [],
                "attribute_hypotheses": [],
                "used_term_sources": [],
                "conflicts": [],
            },
        )
    status["status"] = "complete"
    status["updated_at"] = now()
    atomic_json(status_path, status)
    return {"record_id": task["record_id"], "chunk_id": chunk_id, "status": "complete"}


def collect_tasks(
    root: Path,
    max_articles: int,
    max_chunks: int,
    article_filter: str | None,
    allowed_record_ids: set[str],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    article_dirs = sorted(path for path in (root / "papers").iterdir() if path.is_dir())
    if article_filter:
        article_dirs = [path for path in article_dirs if path.name == article_filter]
    selected_articles = 0
    for article_dir in article_dirs:
        manifest_path = article_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record_id = json.loads((article_dir / "chunking_status.json").read_text(encoding="utf-8"))["record_id"]
        if record_id not in allowed_record_ids:
            continue
        if max_articles and selected_articles >= max_articles:
            break
        selected_articles += 1
        for chunk in sorted(manifest.get("chunks", []), key=lambda item: item.get("order", 0)):
            tasks.append({"article_dir": article_dir, "record_id": record_id, "chunk": chunk})
    return tasks[:max_chunks] if max_chunks else tasks


def finalize_run_states(root: Path, completed: list[dict[str, Any]]) -> None:
    by_article: dict[Path, list[str]] = {}
    for item in completed:
        by_article.setdefault(item["article_dir"], []).append(item["chunk_id"])
    runner = Path("/Users/Zhuanz/.agents/skills/translate-book/scripts/run_state.py")
    for article_dir, chunk_ids in by_article.items():
        command = ["python3", str(runner), "record", str(article_dir), *sorted(set(chunk_ids))]
        subprocess.run(command, capture_output=True, text=True, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=TRANSLATION_ROOT)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--max-articles", type=int, default=0)
    parser.add_argument("--max-chunks", type=int, default=0)
    parser.add_argument("--article", default="")
    parser.add_argument("--rights-manifest", type=Path, default=RIGHTS_MANIFEST)
    args = parser.parse_args()
    if args.concurrency < 1 or args.concurrency > 64:
        parser.error("--concurrency must be between 1 and 64")
    terms = load_glossary(args.root / "global_glossary.json")
    allowed_record_ids = load_allowed_record_ids(args.rights_manifest)
    tasks = collect_tasks(
        args.root,
        args.max_articles,
        args.max_chunks,
        args.article or None,
        allowed_record_ids,
    )
    if not tasks:
        raise SystemExit("No chunk tasks found")
    api_key = load_api_key()
    client = DeepSeekClient(api_key)
    print(
        f"START chunks={len(tasks)} eligible_records={len(allowed_record_ids)} "
        f"concurrency={args.concurrency} stages={','.join(STAGES)}",
        flush=True,
    )
    completed: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        future_map = {executor.submit(process_chunk, task, client, terms): task for task in tasks}
        for index, future in enumerate(concurrent.futures.as_completed(future_map), 1):
            task = future_map[future]
            try:
                result = future.result()
                result["article_dir"] = task["article_dir"]
                if result["status"] == "complete":
                    completed.append(result)
                    print(
                        f"PROGRESS {index}/{len(tasks)} {result['record_id']} {result['chunk_id']} complete",
                        flush=True,
                    )
                elif result["status"] == "uncertain":
                    uncertain.append(result)
                    print(
                        f"UNCERTAIN {index}/{len(tasks)} {result['record_id']} {result['chunk_id']}",
                        flush=True,
                    )
                else:
                    failures.append(result)
            except Exception as exc:  # noqa: BLE001 - checkpointed retries happen on rerun
                failure = {"record_id": task["record_id"], "chunk_id": task["chunk"]["id"], "error": repr(exc)}
                failures.append(failure)
                print(f"FAIL {index}/{len(tasks)} {failure['record_id']} {failure['chunk_id']} {failure['error']}", flush=True)
    finalize_run_states(args.root, completed)
    summary = {
        "updated_at": now(),
        "total_chunks": len(tasks),
        "completed": len(completed),
        "uncertain": uncertain,
        "failed": failures,
        "stages": list(STAGES),
        "model": MODEL,
        "eligible_records": len(allowed_record_ids),
        "rights_manifest": str(args.rights_manifest),
    }
    atomic_json(args.root / "translation_summary.json", summary)
    printable = {key: value for key, value in summary.items() if key not in {"failed", "uncertain"}}
    printable["uncertain"] = len(uncertain)
    printable["failed"] = len(failures)
    print(json.dumps(printable, ensure_ascii=False, indent=2), flush=True)
    return 0 if not failures and not uncertain else 2


if __name__ == "__main__":
    raise SystemExit(main())
