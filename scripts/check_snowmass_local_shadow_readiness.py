#!/usr/bin/env python3
"""Report whether this machine can launch an attested zero-paid Snowmass shadow."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import snowmass_local_attestation as local_attestation


MINIMUM_MEMORY_BYTES = 48 * 2**30
MINIMUM_FREE_DISK_BYTES = 40 * 2**30
REQUIRED_MODULES = ("mlx", "mlx_lm")


def physical_memory_bytes() -> int:
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    page_count = int(os.sysconf("SC_PHYS_PAGES"))
    return page_size * page_count


def readiness_report(
    *,
    workspace: Path,
    base_url: str | None = None,
    model: str | None = None,
    model_manifest: Path | None = None,
    server_binary: Path | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    memory = physical_memory_bytes()
    free_disk = int(shutil.disk_usage(workspace)[2])
    checks = {
        "architecture": platform.machine(),
        "physical_memory_bytes": memory,
        "free_disk_bytes": free_disk,
        "lsof": shutil.which("lsof"),
        "python_modules": {
            name: importlib.util.find_spec(name) is not None for name in REQUIRED_MODULES
        },
    }
    blockers: list[str] = []
    if checks["architecture"] != "arm64":
        blockers.append("architecture_not_arm64")
    if memory < MINIMUM_MEMORY_BYTES:
        blockers.append("physical_memory_below_48_gib")
    if free_disk < MINIMUM_FREE_DISK_BYTES:
        blockers.append("free_disk_below_40_gib")
    if checks["lsof"] is None:
        blockers.append("command_missing:lsof")
    blockers.extend(
        f"python_module_missing:{name}"
        for name, available in checks["python_modules"].items()
        if not available
    )
    local_values = (base_url, model, model_manifest, server_binary)
    labels = (
        "local_openai_base_url_not_configured",
        "local_model_not_configured",
        "local_model_manifest_not_configured",
        "local_server_binary_not_configured",
    )
    blockers.extend(label for label, value in zip(labels, local_values) if value is None)
    attestation: dict[str, str] | None = None
    if all(value is not None for value in local_values):
        try:
            attestation = local_attestation.verify_local_execution(
                base_url=str(base_url),
                model=str(model),
                model_manifest=Path(model_manifest),
                server_binary=Path(server_binary),
            )
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
            blockers.append(f"local_execution_attestation_failed:{type(error).__name__}:{error}")
    return {
        "schema_version": 1,
        "ready": not blockers,
        "checks": checks,
        "attestation": attestation,
        "blockers": blockers,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=SCRIPT_DIR.parent)
    parser.add_argument("--local-openai-base-url")
    parser.add_argument("--local-model")
    parser.add_argument("--local-model-manifest", type=Path)
    parser.add_argument("--local-server-binary", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = readiness_report(
        workspace=args.workspace,
        base_url=args.local_openai_base_url,
        model=args.local_model,
        model_manifest=args.local_model_manifest,
        server_binary=args.local_server_binary,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
