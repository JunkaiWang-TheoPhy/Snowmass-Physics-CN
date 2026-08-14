#!/usr/bin/env python3
"""Fail-closed filesystem and process attestation for a local model server."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any
from urllib.parse import urlparse


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def build_model_manifest(*, model_root: Path, model: str, output: Path) -> dict[str, Any]:
    root = Path(model_root).resolve()
    output_path = Path(output).resolve()
    if not root.is_dir() or not model.strip():
        raise ValueError("model root and model identity must be valid")
    try:
        output_path.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("model manifest must be stored outside the model root")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"model inventory contains a symlink: {path}")
        if path.is_file():
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    if not files:
        raise RuntimeError("model root contains no files")
    payload = {
        "schema_version": 1,
        "model": model,
        "model_root": str(root),
        "files": files,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _safe_model_file(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise RuntimeError("local model manifest contains an unsafe file path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise RuntimeError("local model manifest file escapes model root") from error
    return path


def verify_model_manifest(path: Path, *, expected_model: str) -> dict[str, str]:
    manifest_path = Path(path).resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("model") != expected_model:
        raise RuntimeError("local model manifest identity mismatch")
    root_value = payload.get("model_root")
    root = Path(root_value).resolve() if isinstance(root_value, str) else Path()
    if not root.is_dir():
        raise RuntimeError("local model manifest root is missing")
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("local model manifest has no files")
    declared: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("local model manifest file entry is invalid")
        relative = entry.get("path")
        file_path = _safe_model_file(root, relative)
        if relative in declared or not file_path.is_file() or file_path.is_symlink():
            raise RuntimeError("local model manifest file inventory is invalid")
        declared.add(str(relative))
        if file_path.stat().st_size != entry.get("size") or sha256_file(file_path) != entry.get("sha256"):
            raise RuntimeError(f"local model file identity mismatch: {relative}")
    observed = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file() and not item.is_symlink()
    }
    if observed != declared:
        raise RuntimeError("local model manifest inventory does not match model root")
    return {
        "model_manifest_sha256": sha256_file(manifest_path),
        "model_root": str(root),
    }


def attest_local_server(
    *,
    base_url: str,
    server_binary: Path,
    required_model_root: str,
) -> dict[str, str]:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("local server attestation requires an HTTP loopback endpoint")
    port = parsed.port
    if port is None:
        raise RuntimeError("local server endpoint must include an explicit port")
    binary = Path(server_binary).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError("local server binary is missing or not executable")
    listener = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids = sorted({line[1:] for line in listener.stdout.splitlines() if line.startswith("p")})
    if listener.returncode != 0 or len(pids) != 1 or not pids[0].isdigit():
        raise RuntimeError("local model endpoint does not have one attestable listener")
    pid = pids[0]
    executable = subprocess.run(
        ["lsof", "-a", "-p", pid, "-d", "txt", "-Fn"],
        capture_output=True,
        text=True,
        check=False,
    )
    executable_paths = [line[1:] for line in executable.stdout.splitlines() if line.startswith("n")]
    if executable.returncode != 0 or str(binary) not in {str(Path(item).resolve()) for item in executable_paths}:
        raise RuntimeError("listening process executable does not match the attested server binary")
    command = subprocess.run(
        ["ps", "-p", pid, "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    command_text = command.stdout.strip()
    if command.returncode != 0 or required_model_root not in command_text:
        raise RuntimeError("listening process command is not bound to the attested model root")
    return {
        "server_binary_sha256": sha256_file(binary),
        "server_command_sha256": hashlib.sha256(command_text.encode()).hexdigest(),
    }


def verify_local_execution(
    *,
    base_url: str,
    model: str,
    model_manifest: Path,
    server_binary: Path,
) -> dict[str, str]:
    model_identity = verify_model_manifest(model_manifest, expected_model=model)
    server_identity = attest_local_server(
        base_url=base_url,
        server_binary=server_binary,
        required_model_root=model_identity["model_root"],
    )
    return {**model_identity, **server_identity}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    build_model_manifest(model_root=args.model_root, model=args.model, output=args.output)
    print(json.dumps({"manifest": str(args.output), "sha256": sha256_file(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
