#!/usr/bin/env python3
"""Fail-closed staged production control for the pinned pdf2zh-next route."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import run_snowmass_batch_production as batch_production
    import run_snowmass_pdf2zh_next_ab as ab_runner
    import seal_snowmass_pdf2zh_next_paper as paper_sealer
except ModuleNotFoundError:
    from scripts import run_snowmass_batch_production as batch_production
    from scripts import run_snowmass_pdf2zh_next_ab as ab_runner
    from scripts import seal_snowmass_pdf2zh_next_paper as paper_sealer


ROOT = Path(__file__).resolve().parents[1]
STAGES = (
    "deepseek_probe",
    "pilot5",
    "pilot10",
    "pilot25",
    "batch50",
    "remainder",
)
PREVIOUS_STAGE = {
    "pilot5": "deepseek_probe",
    "pilot10": "pilot5",
    "pilot25": "pilot10",
    "batch50": "pilot25",
    "remainder": "batch50",
}
FORMAL_PROBE_RECORD_ID = "arxiv:2203.06843"
SCHEMA_VERSION = 1
PINNED_PYTHON = Path(
    "/Users/Zhuanz/.local/share/snowmass-tools/pdf2zh-next-2.9.0/bin/python"
)


@dataclass(frozen=True)
class PlanArgs:
    stage: str
    rights_manifest: Path
    source_manifest: Path
    pdf_root: Path
    glossary_json: Path
    output_root: Path
    project_control_dir: Path
    project_max_cost_rmb: float
    stage_max_cost_rmb: float
    stage_max_api_calls: int
    pages: str = "all"
    qps: int = 1
    pool_max_workers: int = 1
    supplemental_glossary_json: Path | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _translation_contract_sha256() -> str:
    """Fingerprint local translation/QC code that can change launch validity."""

    paths = (
        Path(ab_runner.__file__).resolve(),
        Path(paper_sealer.__file__).resolve(),
        ROOT / "scripts/protect_snowmass_pdf2zh_output.py",
    )
    return _json_hash({str(path): _sha256(path) for path in paths})


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"invalid {label}: {type(error).__name__}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {label}: expected an object")
    return value


def _current_environment_lock() -> dict[str, Any]:
    if not PINNED_PYTHON.is_file():
        raise RuntimeError("pinned pdf2zh-next Python runtime is unavailable")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "environment-lock.json"
        result = subprocess.run(
            [
                str(PINNED_PYTHON),
                str(Path(paper_sealer.__file__).resolve()),
                "environment-lock",
                "--output",
                str(path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("production environment lock generation failed")
        return _load_json(path, label="current environment lock")


def _validate_caps(args: PlanArgs) -> tuple[float, float, int]:
    project, stage = ab_runner.validate_budgets(
        args.project_max_cost_rmb, args.stage_max_cost_rmb
    )
    request_cap = ab_runner.validate_request_cap(args.stage_max_api_calls)
    if stage > project:
        raise ValueError("stage budget must not exceed project budget")
    if args.qps <= 0 or args.pool_max_workers <= 0:
        raise ValueError("local concurrency values must be positive")
    if args.pool_max_workers > 2:
        raise ValueError("paper concurrency must not exceed 2")
    return project, stage, request_cap


def _source_records(path: Path) -> dict[str, dict[str, Any]]:
    manifest = _load_json(path, label="source manifest")
    records = manifest.get("records")
    if not isinstance(records, list):
        raise RuntimeError("source manifest records are missing")
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("record_id"), str):
            raise RuntimeError("source manifest record is invalid")
        record_id = ab_runner.normalize_record_id(record["record_id"])
        if record_id in by_id:
            raise RuntimeError(f"duplicate source identity: {record_id}")
        by_id[record_id] = record
    return by_id


def _source_pdf(args: PlanArgs, record_id: str, identity: Mapping[str, Any]) -> Path:
    candidate = identity.get("pdf_source")
    if isinstance(candidate, str) and candidate:
        path = Path(candidate)
        if not path.is_absolute():
            rooted = ROOT / path
            if rooted.is_file():
                return rooted
    filename = record_id.replace(":", "_").replace("/", "_") + ".pdf"
    return args.pdf_root / filename


def _request_allocations(
    total: int, records: Sequence[Mapping[str, Any]]
) -> list[int]:
    count = len(records)
    if count <= 0:
        raise RuntimeError("stage cohort is empty")
    if total < count:
        raise ValueError("request cap must allow at least one request per paper")
    weights = [max(5, int(record.get("page_count") or 0)) for record in records]
    remaining = total - count
    weight_total = sum(weights)
    exact_shares = [remaining * weight / weight_total for weight in weights]
    allocations = [1 + math.floor(share) for share in exact_shares]
    remainder = total - sum(allocations)
    order = sorted(
        range(count),
        key=lambda index: (-(exact_shares[index] % 1), index),
    )
    for index in order[:remainder]:
        allocations[index] += 1
    return allocations


@contextmanager
def _paper_launch_lock(article: Path):
    path = article / "run" / "launch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(str(os.getpid()))
            stream.flush()
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _paper_pages(args: PlanArgs, record: Mapping[str, Any]) -> str:
    if args.pages != "all":
        return args.pages
    page_count = int(record.get("page_count") or 0)
    if page_count <= 0:
        raise RuntimeError(f"page count is missing for {record.get('record_id')}")
    return f"1-{page_count}"


def _select_paid_stage_records(
    records: list[dict[str, Any]], stage: str
) -> list[dict[str, Any]]:
    """Reuse legacy stratification but fold optional shadow back into remainder."""

    selected = list(batch_production.select_stage_records(records, stage))
    if stage == "remainder":
        selected_ids = {str(record["record_id"]) for record in selected}
        selected.extend(
            record
            for record in batch_production.select_stage_records(records, "shadow")
            if str(record["record_id"]) not in selected_ids
        )
    return selected


def _plan_payload_hash(plan: Mapping[str, Any]) -> str:
    return _json_hash(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )


def _validate_plan(path: Path) -> dict[str, Any]:
    plan = _load_json(path, label="stage plan")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("stage plan schema mismatch")
    if plan.get("plan_sha256") != _plan_payload_hash(plan):
        raise RuntimeError("stage plan content hash mismatch")
    controller_contracts = plan.get("controller_contracts")
    expected_contracts = {
        str(Path(module.__file__).resolve()): _sha256(Path(module.__file__).resolve())
        for module in (batch_production, ab_runner, paper_sealer)
    }
    expected_contracts[str(Path(__file__).resolve())] = _sha256(
        Path(__file__).resolve()
    )
    if controller_contracts != expected_contracts:
        raise RuntimeError("stage plan controller contract drift")
    environment_entry = plan.get("environment_lock")
    if not isinstance(environment_entry, dict):
        raise RuntimeError("stage plan environment lock binding is missing")
    environment_path = Path(str(environment_entry.get("path") or ""))
    if not environment_path.is_file() or _sha256(
        environment_path
    ) != environment_entry.get("sha256"):
        raise RuntimeError("stage plan environment lock file hash mismatch")
    stored_environment = _load_json(environment_path, label="stored environment lock")
    current_environment = _current_environment_lock()
    if stored_environment.get("lock_sha256") != environment_entry.get(
        "lock_sha256"
    ) or current_environment.get("lock_sha256") != environment_entry.get("lock_sha256"):
        raise RuntimeError("stage plan environment lock drift")
    for key in ("rights_manifest", "source_manifest", "glossary_json"):
        entry = plan.get(key)
        if not isinstance(entry, dict):
            raise RuntimeError(f"stage plan {key} binding is missing")
        path_value = Path(str(entry.get("path") or ""))
        if not path_value.is_file() or _sha256(path_value) != entry.get("sha256"):
            raise RuntimeError(f"stage plan {key} hash mismatch")
    for paper in plan.get("papers") or []:
        source = Path(str(paper.get("source_pdf") or ""))
        preflight = Path(str(paper.get("preflight_path") or ""))
        if not source.is_file() or _sha256(source) != paper.get("source_sha256"):
            raise RuntimeError(
                f"planned source hash mismatch: {paper.get('record_id')}"
            )
        if not preflight.is_file() or _sha256(preflight) != paper.get(
            "preflight_sha256"
        ):
            raise RuntimeError(
                f"planned preflight hash mismatch: {paper.get('record_id')}"
            )
    return plan


def _same_plan_request(existing: Mapping[str, Any], args: PlanArgs) -> bool:
    expected = {
        "stage": args.stage,
        "rights_manifest": str(args.rights_manifest.resolve()),
        "source_manifest": str(args.source_manifest.resolve()),
        "glossary_json": str(args.glossary_json.resolve()),
        "project_control_dir": str(args.project_control_dir.resolve()),
        "project_max_cost_rmb": float(args.project_max_cost_rmb),
        "stage_max_cost_rmb": float(args.stage_max_cost_rmb),
        "stage_max_api_calls": int(args.stage_max_api_calls),
        "pages": args.pages,
        "qps": args.qps,
        "pool_max_workers": args.pool_max_workers,
    }
    actual = dict(existing.get("request") or {})
    return actual == expected


def _adapter_command(config: ab_runner.RunConfig, *, preflight_only: bool) -> list[str]:
    if not PINNED_PYTHON.is_file():
        raise RuntimeError("pinned pdf2zh-next Python runtime is unavailable")
    command = [
        str(PINNED_PYTHON),
        str(Path(ab_runner.__file__).resolve()),
        "--record-id",
        config.record_id,
        "--source-pdf",
        str(config.source_pdf),
        "--rights-manifest",
        str(config.rights_manifest),
        "--source-manifest",
        str(config.source_manifest),
        "--glossary-json",
        str(config.glossary_json),
        "--output-root",
        str(config.output_root),
        "--pages",
        config.pages,
        "--project-max-cost-rmb",
        str(config.project_max_cost_rmb),
        "--stage-max-cost-rmb",
        str(config.stage_max_cost_rmb),
        "--stage-max-api-calls",
        str(config.stage_max_api_calls),
        "--qps",
        str(config.qps),
        "--pool-max-workers",
        str(config.pool_max_workers),
        "--project-control-dir",
        str(config.project_control_dir),
    ]
    if config.supplemental_glossary_json is not None:
        command.extend(
            ["--supplemental-glossary-json", str(config.supplemental_glossary_json)]
        )
    if preflight_only:
        command.append("--preflight-only")
    return command


def _run_adapter_subprocess(
    config: ab_runner.RunConfig, *, preflight_only: bool
) -> dict[str, Any]:
    result = subprocess.run(
        _adapter_command(config, preflight_only=preflight_only),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "pinned pdf2zh-next adapter failed: "
            + (
                result.stderr.strip().splitlines()[-1]
                if result.stderr.strip()
                else "unknown"
            )
        )
    receipt_path = config.output_root / (
        "preflight.json" if preflight_only else "finish.json"
    )
    return _load_json(receipt_path, label="adapter receipt")


def _default_preflight_runner(config: ab_runner.RunConfig) -> Mapping[str, Any]:
    return _run_adapter_subprocess(config, preflight_only=True)


def plan_stage(
    args: PlanArgs,
    *,
    preflight_runner: Callable[[ab_runner.RunConfig], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write or validate one immutable, entirely zero-paid stage plan."""

    if args.stage not in STAGES:
        raise ValueError(f"unsupported paid production stage: {args.stage}")
    project_budget, stage_budget, stage_request_cap = _validate_caps(args)
    for path, label in (
        (args.rights_manifest, "rights manifest"),
        (args.source_manifest, "source manifest"),
        (args.glossary_json, "glossary"),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{label} is unavailable: {path}")
    stage_dir = args.output_root / "stages" / args.stage
    plan_path = stage_dir / "plan.json"
    if plan_path.is_file():
        existing = _validate_plan(plan_path)
        if not _same_plan_request(existing, args):
            raise RuntimeError("immutable stage plan collision")
        return existing

    environment = _current_environment_lock()
    environment_path = stage_dir / "environment-lock.json"
    _atomic_json(environment_path, environment)

    eligible = batch_production.load_publication_records(args.rights_manifest)
    selected = _select_paid_stage_records(eligible, args.stage)
    eligible_by_id = {
        ab_runner.normalize_record_id(str(record["record_id"])): record
        for record in eligible
    }
    if args.stage == "deepseek_probe" and not selected:
        formal = eligible_by_id.get(FORMAL_PROBE_RECORD_ID)
        selected = [formal] if formal is not None else []
    elif args.stage != "deepseek_probe" and not selected:
        limit = batch_production.STAGE_LIMITS.get(args.stage, len(eligible))
        selected = [
            record
            for record in eligible
            if ab_runner.normalize_record_id(str(record["record_id"]))
            != FORMAL_PROBE_RECORD_ID
        ][:limit]
    selected_ids = [
        ab_runner.normalize_record_id(str(item["record_id"])) for item in selected
    ]
    if args.stage == "deepseek_probe" and selected_ids != [FORMAL_PROBE_RECORD_ID]:
        raise RuntimeError(f"formal deepseek probe must be {FORMAL_PROBE_RECORD_ID}")
    allocations = _request_allocations(stage_request_cap, selected)
    source_by_id = _source_records(args.source_manifest)
    preflight_runner = preflight_runner or _default_preflight_runner
    papers: list[dict[str, Any]] = []
    projection_cost = 0.0
    projection_requests = 0
    for index, (record, request_allocation) in enumerate(zip(selected, allocations)):
        record_id = selected_ids[index]
        identity = source_by_id.get(record_id)
        if identity is None:
            raise RuntimeError(f"trusted source identity is missing: {record_id}")
        source_pdf = _source_pdf(args, record_id, identity)
        if not source_pdf.is_file():
            raise FileNotFoundError(f"source PDF is unavailable: {source_pdf}")
        expected_hash = identity.get("pdf_sha256")
        if identity.get("pdf_status") != "complete" or expected_hash != _sha256(
            source_pdf
        ):
            raise RuntimeError(f"trusted source PDF hash mismatch: {record_id}")
        expected_size = int(identity.get("pdf_bytes") or -1)
        if expected_size != source_pdf.stat().st_size:
            raise RuntimeError(f"trusted source PDF size mismatch: {record_id}")
        article = (
            args.output_root
            / "stages"
            / args.stage
            / "papers"
            / record_id.replace(":", "_")
        )
        run_dir = article / "run"
        paper_budget = stage_budget * request_allocation / stage_request_cap
        config = ab_runner.RunConfig(
            record_id=record_id,
            source_pdf=source_pdf,
            rights_manifest=args.rights_manifest,
            source_manifest=args.source_manifest,
            glossary_json=args.glossary_json,
            supplemental_glossary_json=args.supplemental_glossary_json,
            output_root=run_dir,
            pages=_paper_pages(args, record),
            project_max_cost_rmb=project_budget,
            stage_max_cost_rmb=paper_budget,
            stage_max_api_calls=request_allocation,
            qps=args.qps,
            pool_max_workers=args.pool_max_workers,
            project_control_dir=args.project_control_dir,
        )
        preflight = dict(preflight_runner(config))
        if preflight.get("status") not in {None, "preflight_passed"}:
            raise RuntimeError(f"paper preflight failed: {record_id}")
        projection = preflight.get("projection")
        if not isinstance(projection, dict):
            raise RuntimeError(f"paper projection is missing: {record_id}")
        projected_requests = int(projection.get("request_cap") or 0)
        projected_cost = float(projection.get("max_cost_rmb") or 0)
        if projected_requests <= 0 or projected_requests > request_allocation:
            raise ValueError(f"paper projection exceeds request cap: {record_id}")
        if (
            not math.isfinite(projected_cost)
            or projected_cost <= 0
            or projected_cost > paper_budget
        ):
            raise ValueError(
                f"paper projection exceeds stage budget allocation: {record_id}"
            )
        run_dir.mkdir(parents=True, exist_ok=True)
        preflight_path = run_dir / "planned-preflight.json"
        _atomic_json(preflight_path, preflight)
        projection_cost += projected_cost
        projection_requests += projected_requests
        papers.append(
            {
                "record_id": record_id,
                "article_dir": str(article.resolve()),
                "run_dir": str(run_dir.resolve()),
                "source_pdf": str(source_pdf.resolve()),
                "source_sha256": _sha256(source_pdf),
                "source_bytes": source_pdf.stat().st_size,
                "pages": config.pages,
                "page_count": int(record.get("page_count") or 0),
                "request_cap": request_allocation,
                "stage_max_cost_rmb": paper_budget,
                "projection": projection,
                "preflight_path": str(preflight_path.resolve()),
                "preflight_sha256": _sha256(preflight_path),
                "translation_contract_sha256": _translation_contract_sha256(),
                "finish_sha256": None,
            }
        )
    if projection_requests > stage_request_cap:
        raise ValueError("aggregate request cap exceeds the stage request cap")
    if projection_cost > stage_budget:
        raise ValueError("aggregate projection exceeds the stage budget")
    project_commitment = ab_runner.read_project_commitment(args.project_control_dir)
    if project_commitment + projection_cost > project_budget:
        raise ValueError("aggregate projection exceeds the remaining project budget")

    request = {
        "stage": args.stage,
        "rights_manifest": str(args.rights_manifest.resolve()),
        "source_manifest": str(args.source_manifest.resolve()),
        "glossary_json": str(args.glossary_json.resolve()),
        "project_control_dir": str(args.project_control_dir.resolve()),
        "project_max_cost_rmb": project_budget,
        "stage_max_cost_rmb": stage_budget,
        "stage_max_api_calls": stage_request_cap,
        "pages": args.pages,
        "qps": args.qps,
        "pool_max_workers": args.pool_max_workers,
    }
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "planned_zero_paid",
        "stage": args.stage,
        "stage_dir": str(stage_dir.resolve()),
        "plan_path": str(plan_path.resolve()),
        "eligible_record_count": len(eligible),
        "record_ids": selected_ids,
        "cohort_sha256": _json_hash(selected_ids),
        "controller_contracts": {
            str(Path(module.__file__).resolve()): _sha256(
                Path(module.__file__).resolve()
            )
            for module in (batch_production, ab_runner, paper_sealer)
        }
        | {str(Path(__file__).resolve()): _sha256(Path(__file__).resolve())},
        "environment_lock": {
            "path": str(environment_path.resolve()),
            "sha256": _sha256(environment_path),
            "lock_sha256": environment["lock_sha256"],
        },
        "rights_manifest": {
            "path": str(args.rights_manifest.resolve()),
            "sha256": _sha256(args.rights_manifest),
        },
        "source_manifest": {
            "path": str(args.source_manifest.resolve()),
            "sha256": _sha256(args.source_manifest),
        },
        "glossary_json": {
            "path": str(args.glossary_json.resolve()),
            "sha256": _sha256(args.glossary_json),
        },
        "request": request,
        "project_control_dir": str(args.project_control_dir.resolve()),
        "launch_projection": {
            "paper_count": len(papers),
            "request_cap": projection_requests,
            "max_cost_rmb": projection_cost,
            "stage_request_cap": stage_request_cap,
            "stage_max_cost_rmb": stage_budget,
            "project_max_cost_rmb": project_budget,
            "project_commitment_before_rmb": project_commitment,
        },
        "papers": papers,
        "zero_paid": True,
    }
    previous = PREVIOUS_STAGE.get(args.stage)
    if previous is not None:
        previous_seal = args.output_root / "stages" / previous / "stage-seal.json"
        if previous_seal.is_file():
            _atomic_json(
                stage_dir / "previous-stage-seal.json",
                _load_json(previous_seal, label="previous stage seal"),
            )
    plan["plan_sha256"] = _plan_payload_hash(plan)
    _atomic_json(plan_path, plan)
    return plan


def paper_launch_fingerprint(paper: Mapping[str, Any]) -> str:
    return _json_hash(
        {
            key: paper.get(key)
            for key in (
                "record_id",
                "source_sha256",
                "preflight_sha256",
                "pages",
                "request_cap",
                "stage_max_cost_rmb",
                "translation_contract_sha256",
            )
        }
    )


def _quarantine(article: Path, paper: Mapping[str, Any], error: BaseException) -> None:
    _atomic_json(
        article / "qc" / "launch-quarantine.json",
        {
            "schema_version": 1,
            "active": True,
            "record_id": paper["record_id"],
            "fingerprint": paper_launch_fingerprint(paper),
            "error_type": type(error).__name__,
            "error": str(error),
        },
    )


def _same_quarantine(article: Path, paper: Mapping[str, Any]) -> bool:
    path = article / "qc" / "launch-quarantine.json"
    if not path.is_file():
        return False
    value = _load_json(path, label="launch quarantine")
    return value.get("active") is True and value.get(
        "fingerprint"
    ) == paper_launch_fingerprint(paper)


def _finish_state(paper: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
    finish_path = Path(str(paper["run_dir"])) / "finish.json"
    if not finish_path.is_file():
        return "missing", None
    finish = _load_json(finish_path, label="translation finish")
    if (finish.get("source") or {}).get("sha256") != paper.get("source_sha256"):
        return "mismatch", finish
    if finish.get("status") != "translated_pending_qc":
        return "mismatch", finish
    output = (finish.get("outputs") or {}).get("mono_pdf") or {}
    if output:
        raw = Path(str(paper["run_dir"])) / "rendered" / str(output.get("path") or "")
        if not raw.is_file() or _sha256(raw) != output.get("sha256"):
            return "mismatch", finish
    return "valid", finish


def _config_from_plan(
    plan: Mapping[str, Any], paper: Mapping[str, Any]
) -> ab_runner.RunConfig:
    request = plan["request"]
    return ab_runner.RunConfig(
        record_id=str(paper["record_id"]),
        source_pdf=Path(str(paper["source_pdf"])),
        rights_manifest=Path(str(plan["rights_manifest"]["path"])),
        source_manifest=Path(str(plan["source_manifest"]["path"])),
        glossary_json=Path(str(plan["glossary_json"]["path"])),
        output_root=Path(str(paper["run_dir"])),
        pages=str(paper["pages"]),
        project_max_cost_rmb=float(request["project_max_cost_rmb"]),
        stage_max_cost_rmb=float(paper["stage_max_cost_rmb"]),
        stage_max_api_calls=int(paper["request_cap"]),
        qps=int(request["qps"]),
        pool_max_workers=int(request["pool_max_workers"]),
        project_control_dir=Path(str(plan["project_control_dir"])),
    )


def _default_paid_runner(config: ab_runner.RunConfig) -> Mapping[str, Any]:
    return _run_adapter_subprocess(config, preflight_only=False)


def _default_prepare_runner(**arguments: Any) -> Mapping[str, Any]:
    if not PINNED_PYTHON.is_file():
        raise RuntimeError("pinned pdf2zh-next Python runtime is unavailable")
    command = [
        str(PINNED_PYTHON),
        str(Path(paper_sealer.__file__).resolve()),
        "prepare",
        "--article",
        str(arguments["article"]),
        "--source-pdf",
        str(arguments["source_pdf"]),
        "--raw-pdf",
        str(arguments["raw_pdf"]),
        "--glossary-csv",
        str(arguments["glossary_csv"]),
        "--pages",
        ",".join(map(str, arguments["selected_source_pages"])),
    ]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            "per-paper QC preparation failed: "
            + (
                result.stderr.strip().splitlines()[-1]
                if result.stderr.strip()
                else "unknown"
            )
        )
    return json.loads(result.stdout)


def _launch_one(
    plan: Mapping[str, Any],
    paper: Mapping[str, Any],
    paid_runner: Callable[[ab_runner.RunConfig], Mapping[str, Any]],
    prepare_runner: Callable[..., Mapping[str, Any]],
) -> str:
    article = Path(str(paper["article_dir"]))
    with _paper_launch_lock(article) as acquired:
        if not acquired:
            return "duplicate_active"
        return _launch_one_locked(plan, paper, paid_runner, prepare_runner)


def _launch_one_locked(
    plan: Mapping[str, Any],
    paper: Mapping[str, Any],
    paid_runner: Callable[[ab_runner.RunConfig], Mapping[str, Any]],
    prepare_runner: Callable[..., Mapping[str, Any]],
) -> str:
    article = Path(str(paper["article_dir"]))
    if _same_quarantine(article, paper):
        return "quarantined"
    finish_state, finish = _finish_state(paper)
    review_request = article / "qc" / "visual-review-request.json"
    if finish_state == "valid" and review_request.is_file():
        return "reused"
    if finish_state == "mismatch":
        error = RuntimeError("existing finish does not match the planned input")
        _quarantine(article, paper, error)
        return "quarantined"
    launched = False
    try:
        if finish_state == "missing":
            finish = dict(paid_runner(_config_from_plan(plan, paper)))
            launched = True
        assert finish is not None
        output = (finish.get("outputs") or {}).get("mono_pdf") or {}
        raw = Path(str(paper["run_dir"])) / "rendered" / str(output.get("path") or "")
        if not raw.is_file() or _sha256(raw) != output.get("sha256"):
            raise RuntimeError(
                "paid translation did not produce a hash-matching mono PDF"
            )
        result = prepare_runner(
            record_id=str(paper["record_id"]),
            article=article,
            source_pdf=Path(str(paper["source_pdf"])),
            raw_pdf=raw,
            glossary_csv=Path(str(paper["run_dir"])) / "locked-glossary.csv",
            selected_source_pages=tuple(
                ab_runner.parse_selected_pages(
                    str(paper["pages"]), int(paper["page_count"])
                )
            ),
        )
        if result.get("status") != "awaiting_visual_review":
            raise RuntimeError("paper QC did not stop at visual review")
        return "launched" if launched else "prepared"
    except Exception as error:  # noqa: BLE001 - quarantine must capture every paper-local failure
        _quarantine(article, paper, error)
        return "quarantined"


def launch_stage(
    *,
    plan_path: Path,
    paid_runner: Callable[[ab_runner.RunConfig], Mapping[str, Any]] | None = None,
    prepare_runner: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    plan = _validate_plan(plan_path)
    _require_previous_stage_seal(plan)
    paid_runner = paid_runner or _default_paid_runner
    prepare_runner = prepare_runner or _default_prepare_runner
    workers = min(2, max(1, int(plan["request"]["pool_max_workers"])))
    outcomes: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_launch_one, plan, paper, paid_runner, prepare_runner)
            for paper in plan["papers"]
        ]
        for future in as_completed(futures):
            outcomes.append(future.result())
    return {
        "stage": plan["stage"],
        "launched_count": outcomes.count("launched"),
        "prepared_from_finish_count": outcomes.count("prepared"),
        "reused_count": outcomes.count("reused"),
        "quarantined_count": outcomes.count("quarantined"),
        "duplicate_active_count": outcomes.count("duplicate_active"),
        "awaiting_visual_review": outcomes.count("launched")
        + outcomes.count("prepared"),
    }


def resume_stage(plan_path: Path) -> dict[str, Any]:
    plan = _validate_plan(plan_path)
    outcomes: list[str] = []
    for paper in plan["papers"]:
        article = Path(str(paper["article_dir"]))
        if _same_quarantine(article, paper):
            outcomes.append("quarantined")
            continue
        state, _finish = _finish_state(paper)
        if state == "mismatch":
            _quarantine(article, paper, RuntimeError("resume input hash mismatch"))
            outcomes.append("quarantined")
        elif (
            state == "valid"
            and (article / "qc" / "visual-review-request.json").is_file()
        ):
            outcomes.append("reused")
        else:
            outcomes.append("pending")
    return {
        "stage": plan["stage"],
        "reused_count": outcomes.count("reused"),
        "pending_count": outcomes.count("pending"),
        "quarantined_count": outcomes.count("quarantined"),
    }


def status_stage(plan_path: Path) -> dict[str, Any]:
    plan = _validate_plan(plan_path)
    outcomes: list[str] = []
    for paper in plan["papers"]:
        article = Path(str(paper["article_dir"]))
        if _same_quarantine(article, paper):
            outcomes.append("quarantined")
            continue
        state, _finish = _finish_state(paper)
        if (article / "qc" / "paper-seal.json").is_file():
            outcomes.append("sealed")
        elif (
            state == "valid"
            and (article / "qc" / "visual-review-request.json").is_file()
        ):
            outcomes.append("awaiting_visual_review")
        else:
            outcomes.append(state)
    return {
        "stage": plan["stage"],
        "sealed_count": outcomes.count("sealed"),
        "awaiting_visual_review": outcomes.count("awaiting_visual_review"),
        "missing_count": outcomes.count("missing"),
        "mismatch_count": outcomes.count("mismatch"),
        "quarantined_count": outcomes.count("quarantined"),
    }


def _validate_stage_seal(value: Mapping[str, Any], *, expected_stage: str) -> None:
    expected_hash = value.get("stage_seal_sha256")
    payload = {key: item for key, item in value.items() if key != "stage_seal_sha256"}
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("passed") is not True
        or value.get("stage") != expected_stage
        or not isinstance(expected_hash, str)
        or expected_hash != _json_hash(payload)
    ):
        raise RuntimeError("previous stage seal is invalid")


def _require_previous_stage_seal(plan: Mapping[str, Any]) -> str | None:
    previous = PREVIOUS_STAGE.get(str(plan["stage"]))
    if previous is None:
        return None
    path = Path(str(plan["stage_dir"])) / "previous-stage-seal.json"
    if not path.is_file():
        raise RuntimeError("previous stage seal is required")
    value = _load_json(path, label="previous stage seal")
    _validate_stage_seal(value, expected_stage=previous)
    return _sha256(path)


def _validate_paper_seal(
    article: Path, paper: Mapping[str, Any], seal: Mapping[str, Any]
) -> None:
    required = (
        "environment_lock_sha256",
        "environment_lock_file_sha256",
        "artifact_manifest_sha256",
        "qc_receipt_hashes",
        "evidence_hashes",
    )
    missing = [field for field in required if field not in seal]
    if missing:
        raise RuntimeError(f"paper seal evidence is incomplete: {missing[0]}")
    if not isinstance(seal.get("environment_lock_sha256"), str):
        raise RuntimeError("paper seal evidence is incomplete: environment lock")
    live_files = {
        "environment_lock_file_sha256": article / "environment-lock.json",
        "artifact_manifest_sha256": article / "artifact-manifest.json",
    }
    for field, path in live_files.items():
        if not path.is_file() or _sha256(path) != seal.get(field):
            raise RuntimeError(f"paper seal live hash mismatch: {field}")
    qc_hashes = seal.get("qc_receipt_hashes")
    if not isinstance(qc_hashes, dict) or set(qc_hashes) != {
        "semantic",
        "structural",
        "visual",
    }:
        raise RuntimeError("paper seal evidence is incomplete: QC receipts")
    for kind, expected in qc_hashes.items():
        path = article / "qc" / f"{kind}.json"
        if not path.is_file() or _sha256(path) != expected:
            raise RuntimeError(f"paper seal QC hash mismatch: {kind}")
    evidence_hashes = seal.get("evidence_hashes")
    evidence_paths = {
        "finish": article / "run" / "finish.json",
        "preflight": Path(str(paper["preflight_path"])),
        "glossary": article / "run" / "locked-glossary.csv",
        "protection": article / "qc" / "protection.json",
        "semantic": article / "qc" / "semantic-report.json",
        "structural": article / "qc" / "structural-report.json",
        "visual": article / "qc" / "visual-review.json",
        "contact_sheet": article / "qc" / "contact-sheet.jpg",
    }
    if not isinstance(evidence_hashes, dict) or set(evidence_paths) - set(
        evidence_hashes
    ):
        raise RuntimeError("paper seal evidence is incomplete: evidence hashes")
    for kind, path in evidence_paths.items():
        if not path.is_file() or _sha256(path) != evidence_hashes.get(kind):
            raise RuntimeError(f"paper seal evidence hash mismatch: {kind}")


def promote_stage(plan_path: Path) -> dict[str, Any]:
    plan = _validate_plan(plan_path)
    stage_dir = Path(str(plan["stage_dir"]))
    previous = PREVIOUS_STAGE.get(str(plan["stage"]))
    previous_hash = _require_previous_stage_seal(plan)
    seals: dict[str, str] = {}
    for paper in plan["papers"]:
        path = Path(str(paper["article_dir"])) / "qc" / "paper-seal.json"
        if (
            not path.is_file()
            or path.stat().st_mtime_ns < Path(plan_path).stat().st_mtime_ns
        ):
            raise RuntimeError(f"fresh paper seal is missing: {paper['record_id']}")
        seal = _load_json(path, label="paper seal")
        if (
            seal.get("passed") is not True
            or seal.get("state") != "visual_qc"
            or ab_runner.normalize_record_id(str(seal.get("record_id") or ""))
            != paper["record_id"]
        ):
            raise RuntimeError(f"paper seal did not pass: {paper['record_id']}")
        sealed_source = seal.get("source_pdf_sha256", seal.get("source_sha256"))
        if sealed_source != paper["source_sha256"]:
            raise RuntimeError(f"paper seal source hash mismatch: {paper['record_id']}")
        _validate_paper_seal(Path(str(paper["article_dir"])), paper, seal)
        seals[str(paper["record_id"])] = _sha256(path)
    stage_seal: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "passed": True,
        "stage": plan["stage"],
        "plan_sha256": plan["plan_sha256"],
        "cohort_sha256": plan["cohort_sha256"],
        "paper_seal_sha256s": seals,
        "previous_stage": previous,
        "previous_stage_seal_sha256": previous_hash,
    }
    stage_seal["stage_seal_sha256"] = _json_hash(stage_seal)
    _atomic_json(stage_dir / "stage-seal.json", stage_seal)
    return stage_seal


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--stage", choices=STAGES, required=True)
    plan.add_argument(
        "--rights-manifest", type=Path, default=ROOT / "site/data/papers.json"
    )
    plan.add_argument(
        "--source-manifest",
        type=Path,
        default=ROOT / "output/snowmass2021_sources/manifest.json",
    )
    plan.add_argument("--pdf-root", type=Path, default=ROOT / "tmp/pdfs/snowmass2021")
    plan.add_argument(
        "--glossary-json",
        type=Path,
        default=ROOT / "translations/snowmass-global-glossary.json",
    )
    plan.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "output/snowmass2021/pdf2zh_next_production",
    )
    plan.add_argument(
        "--project-control-dir",
        type=Path,
        default=ROOT / "output/snowmass2021/production_control",
    )
    plan.add_argument("--project-max-cost-rmb", type=float, required=True)
    plan.add_argument("--stage-max-cost-rmb", type=float, required=True)
    plan.add_argument("--stage-max-api-calls", type=int, required=True)
    plan.add_argument("--pages", default="all")
    plan.add_argument("--qps", type=int, default=1)
    plan.add_argument("--pool-max-workers", type=int, default=1)
    for name in ("launch", "status", "resume", "promote"):
        command = commands.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        values = vars(arguments).copy()
        values.pop("command")
        result = plan_stage(PlanArgs(**values))
    elif arguments.command == "launch":
        result = launch_stage(plan_path=arguments.plan)
    elif arguments.command == "status":
        result = status_stage(arguments.plan)
    elif arguments.command == "resume":
        result = resume_stage(arguments.plan)
    else:
        result = promote_stage(arguments.plan)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
