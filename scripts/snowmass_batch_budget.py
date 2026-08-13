#!/usr/bin/env python3
"""Crash-safe, cross-process RMB budget accounting for Snowmass production."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterator
import uuid

import run_snowmass_translation as runner


AUTHORIZED_PROJECT_MAX_RMB = 1000.0
SCHEMA_VERSION = 1
BudgetExceededError = runner.BudgetExceededError


class ProductionStoppedError(BudgetExceededError):
    """Raised before a paid request when another worker tripped a hard gate."""


class RequestLimitExceededError(BudgetExceededError):
    """Raised before a paid request that would exceed the finite call cap."""


def validate_budget(value: float, *, label: str, maximum: float | None = None) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} budget must be finite and greater than zero")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{label} budget must not exceed ¥{maximum:.2f}")
    return parsed


def validate_request_limit(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} request limit must be a finite positive integer")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{label} request limit must be a finite positive integer"
        ) from error
    if not math.isfinite(parsed) or parsed <= 0 or not parsed.is_integer():
        raise ValueError(f"{label} request limit must be a finite positive integer")
    return int(parsed)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class PersistentBudgetGuard:
    """BudgetGuard-compatible durable accounting shared by all paper workers."""

    def __init__(
        self,
        control_dir: Path,
        *,
        project_max_cost_rmb: float,
        stage_max_cost_rmb: float,
        stage_max_api_calls: int = 1000,
        run_id: str,
        usd_cny_rate: float,
        historical_spent_rmb: float = 0.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.control_dir = Path(control_dir)
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.control_dir / "budget_config.json"
        self.ledger_path = self.control_dir / "budget_ledger.jsonl"
        self.lock_path = self.control_dir / "budget.lock"
        self.project_max_cost_rmb = validate_budget(
            project_max_cost_rmb,
            label="project",
            maximum=AUTHORIZED_PROJECT_MAX_RMB,
        )
        self.stage_max_cost_rmb = validate_budget(
            stage_max_cost_rmb,
            label="stage",
            maximum=self.project_max_cost_rmb,
        )
        self.stage_max_api_calls = validate_request_limit(
            stage_max_api_calls, label="stage"
        )
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be non-empty")
        if not math.isfinite(usd_cny_rate) or usd_cny_rate <= 0:
            raise ValueError("usd_cny_rate must be finite and positive")
        if not math.isfinite(historical_spent_rmb) or historical_spent_rmb < 0:
            raise ValueError("historical_spent_rmb must be finite and non-negative")
        self.run_id = run_id
        self.usd_cny_rate = float(usd_cny_rate)
        self.stop_event = stop_event
        self._ledger_cache_events: list[dict[str, Any]] | None = None
        self._ledger_cache_offset = 0
        self._ledger_cache_identity: tuple[int, int] | None = None
        self._ledger_cache_mtime_ns = 0
        with self._locked():
            self._initialize_locked(float(historical_spent_rmb))
            self._recover_orphans_locked()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self.lock_path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _initialize_locked(self, historical_spent_rmb: float) -> None:
        if self.config_path.exists():
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            if config.get("schema_version") != SCHEMA_VERSION:
                raise RuntimeError("Unsupported Snowmass budget configuration schema")
            existing_cap = float(config["project_max_cost_rmb"])
            if self.project_max_cost_rmb > existing_cap + 1e-12:
                raise ValueError(
                    f"initialized project cap cannot be raised: "
                    f"¥{existing_cap:.6f} -> ¥{self.project_max_cost_rmb:.6f}"
                )
            if self.project_max_cost_rmb < existing_cap:
                config["project_max_cost_rmb"] = self.project_max_cost_rmb
                _atomic_json(self.config_path, config)
            if not self.ledger_path.exists():
                if config.get("ledger_initialized") is True:
                    raise RuntimeError("Snowmass budget ledger is missing after initialization")
                self._create_empty_ledger_locked()
                if historical_spent_rmb:
                    self._append_locked(
                        {
                            "event_id": uuid.uuid4().hex,
                            "kind": "historical_baseline",
                            "run_id": "__historical__",
                            "cost_rmb": historical_spent_rmb,
                        }
                    )
                config["ledger_initialized"] = True
                _atomic_json(self.config_path, config)
            elif config.get("ledger_initialized") is not True:
                # Migrate the pre-marker schema only after confirming its ledger exists.
                config["ledger_initialized"] = True
                _atomic_json(self.config_path, config)
        else:
            if not self.ledger_path.exists():
                self._create_empty_ledger_locked()
            events = self._events_locked()
            has_historical_baseline = any(
                event.get("kind") == "historical_baseline" for event in events
            )
            if historical_spent_rmb and not has_historical_baseline:
                self._append_locked(
                    {
                        "event_id": uuid.uuid4().hex,
                        "kind": "historical_baseline",
                        "run_id": "__historical__",
                        "cost_rmb": historical_spent_rmb,
                    }
                )
            _atomic_json(
                self.config_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "project_max_cost_rmb": self.project_max_cost_rmb,
                    "authorized_max_cost_rmb": AUTHORIZED_PROJECT_MAX_RMB,
                    "usd_cny_rate": self.usd_cny_rate,
                    "ledger_initialized": True,
                },
            )
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.project_max_cost_rmb = float(config["project_max_cost_rmb"])
        if self.stage_max_cost_rmb > self.project_max_cost_rmb:
            raise ValueError("stage budget cannot exceed the initialized project cap")

    def _create_empty_ledger_locked(self) -> None:
        descriptor = os.open(
            self.ledger_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _events_locked(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            raise RuntimeError("Snowmass budget ledger is missing")
        stat = self.ledger_path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if stat.st_size:
            with self.ledger_path.open("rb") as stream:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    raise RuntimeError("Corrupt budget ledger: incomplete final event")
        if (
            self._ledger_cache_events is not None
            and self._ledger_cache_identity == identity
            and stat.st_size >= self._ledger_cache_offset
        ):
            if (
                stat.st_size == self._ledger_cache_offset
                and stat.st_mtime_ns == self._ledger_cache_mtime_ns
            ):
                return list(self._ledger_cache_events)
            if stat.st_size > self._ledger_cache_offset:
                with self.ledger_path.open("rb") as stream:
                    stream.seek(self._ledger_cache_offset)
                    tail_bytes = stream.read()
                try:
                    tail = tail_bytes.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise RuntimeError("Corrupt budget ledger tail encoding") from error
                if tail and not tail.endswith("\n"):
                    raise RuntimeError("Corrupt budget ledger: incomplete final event")
                events = list(self._ledger_cache_events)
                first_line = len(events) + 1
                lines = tail.splitlines()
            else:
                events = []
                first_line = 1
                lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        else:
            events = []
            first_line = 1
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, first_line):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"Corrupt budget ledger line {line_number}") from error
            if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
                raise RuntimeError(f"Invalid budget ledger event on line {line_number}")
            events.append(event)
        final_stat = self.ledger_path.stat()
        self._ledger_cache_events = events
        self._ledger_cache_offset = final_stat.st_size
        self._ledger_cache_identity = (final_stat.st_dev, final_stat.st_ino)
        self._ledger_cache_mtime_ns = final_stat.st_mtime_ns
        return list(events)

    def _append_locked(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("schema_version", SCHEMA_VERSION)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        prior_stat = self.ledger_path.stat() if self.ledger_path.exists() else None
        descriptor = os.open(self.ledger_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        final_stat = self.ledger_path.stat()
        if (
            prior_stat is not None
            and self._ledger_cache_events is not None
            and self._ledger_cache_identity == (prior_stat.st_dev, prior_stat.st_ino)
            and self._ledger_cache_offset == prior_stat.st_size
        ):
            self._ledger_cache_events.append(payload)
            self._ledger_cache_offset = final_stat.st_size
            self._ledger_cache_identity = (final_stat.st_dev, final_stat.st_ino)
            self._ledger_cache_mtime_ns = final_stat.st_mtime_ns
        else:
            self._ledger_cache_events = None
            self._ledger_cache_offset = 0
            self._ledger_cache_identity = None
            self._ledger_cache_mtime_ns = 0

    @staticmethod
    def _state(events: list[dict[str, Any]]) -> tuple[float, dict[str, float], dict[str, dict[str, Any]]]:
        project_spent = 0.0
        run_spent: dict[str, float] = {}
        active: dict[str, dict[str, Any]] = {}
        for event in events:
            kind = event["kind"]
            run_id = str(event.get("run_id") or "")
            if kind == "reserve":
                active[str(event["reservation_id"])] = event
            elif kind in {"settle", "commit_estimate", "recover_orphan", "historical_baseline"}:
                cost = float(event.get("cost_rmb") or 0)
                project_spent += cost
                run_spent[run_id] = run_spent.get(run_id, 0.0) + cost
                reservation_id = event.get("reservation_id")
                if reservation_id:
                    active.pop(str(reservation_id), None)
        return project_spent, run_spent, active

    def _recover_orphans_locked(self) -> None:
        events = self._events_locked()
        _spent, _runs, active = self._state(events)
        for reservation_id, reservation in active.items():
            owner_pid = int(reservation.get("owner_pid") or 0)
            if _pid_is_alive(owner_pid):
                continue
            self._append_locked(
                {
                    "event_id": uuid.uuid4().hex,
                    "kind": "recover_orphan",
                    "run_id": str(reservation.get("run_id") or ""),
                    "reservation_id": reservation_id,
                    "cost_rmb": float(reservation["estimated_cost_rmb"]),
                    "owner_pid": owner_pid,
                    **(
                        {"uncertainty_key": reservation["uncertainty_key"]}
                        if reservation.get("uncertainty_key")
                        else {}
                    ),
                }
            )

    def _conservative_request_cost(self, input_text: str, max_output_tokens: int) -> float:
        input_ceiling = len(input_text.encode("utf-8")) + 4096
        output_ceiling = max(0, int(max_output_tokens))
        cost_usd = (
            input_ceiling * runner.INPUT_CACHE_MISS_USD_PER_MILLION
            + output_ceiling * runner.OUTPUT_USD_PER_MILLION
        ) / 1_000_000
        return cost_usd * self.usd_cny_rate

    def reserve(
        self,
        input_text: str,
        max_output_tokens: int,
        *,
        uncertainty_key: str | None = None,
    ) -> str:
        if self.stop_event is not None and self.stop_event.is_set():
            raise ProductionStoppedError("production stop signal is active")
        estimate = self._conservative_request_cost(input_text, max_output_tokens)
        with self._locked():
            if self.stop_event is not None and self.stop_event.is_set():
                raise ProductionStoppedError("production stop signal is active")
            self._recover_orphans_locked()
            events = self._events_locked()
            project_spent, run_spent, active = self._state(events)
            stage_completed_calls = sum(
                event.get("run_id") == self.run_id
                and event.get("kind") in {"settle", "commit_estimate", "recover_orphan"}
                for event in events
            )
            stage_active_calls = sum(
                event.get("run_id") == self.run_id for event in active.values()
            )
            if stage_completed_calls + stage_active_calls >= self.stage_max_api_calls:
                raise RequestLimitExceededError(
                    "stage request cap would be exceeded: "
                    f"{stage_completed_calls + stage_active_calls + 1} "
                    f"> {self.stage_max_api_calls}"
                )
            project_reserved = sum(float(event["estimated_cost_rmb"]) for event in active.values())
            run_reserved = sum(
                float(event["estimated_cost_rmb"])
                for event in active.values()
                if event.get("run_id") == self.run_id
            )
            projected_project = project_spent + project_reserved + estimate
            projected_stage = run_spent.get(self.run_id, 0.0) + run_reserved + estimate
            if projected_project > self.project_max_cost_rmb + 1e-12:
                raise BudgetExceededError(
                    f"project budget would be exceeded: ¥{projected_project:.6f} "
                    f"> ¥{self.project_max_cost_rmb:.6f}"
                )
            if projected_stage > self.stage_max_cost_rmb + 1e-12:
                raise BudgetExceededError(
                    f"stage budget would be exceeded: ¥{projected_stage:.6f} "
                    f"> ¥{self.stage_max_cost_rmb:.6f}"
                )
            reservation_id = uuid.uuid4().hex
            event = {
                "event_id": uuid.uuid4().hex,
                "kind": "reserve",
                "run_id": self.run_id,
                "reservation_id": reservation_id,
                "estimated_cost_rmb": estimate,
                "owner_pid": os.getpid(),
            }
            if uncertainty_key is not None:
                if not isinstance(uncertainty_key, str) or not uncertainty_key.strip():
                    raise ValueError("uncertainty_key must be a non-empty string")
                event["uncertainty_key"] = uncertainty_key
            self._append_locked(event)
            return reservation_id

    def _complete(
        self,
        reservation_id: str,
        *,
        kind: str,
        cost_rmb: float | None,
        usage: dict[str, Any] | None = None,
    ) -> float:
        with self._locked():
            events = self._events_locked()
            _project, _runs, active = self._state(events)
            reservation = active.get(reservation_id)
            if reservation is None:
                raise KeyError(f"unknown or completed reservation: {reservation_id}")
            charged = (
                float(reservation["estimated_cost_rmb"])
                if cost_rmb is None or cost_rmb <= 0
                else float(cost_rmb)
            )
            event = {
                "event_id": uuid.uuid4().hex,
                "kind": kind,
                "run_id": str(reservation["run_id"]),
                "reservation_id": reservation_id,
                "cost_rmb": charged,
            }
            if reservation.get("uncertainty_key"):
                event["uncertainty_key"] = reservation["uncertainty_key"]
            if usage is not None:
                event["usage"] = {
                    key: max(0, int(usage.get(key) or 0))
                    for key in ("input_tokens", "cached_tokens", "output_tokens", "total_tokens")
                }
            self._append_locked(event)
            return charged

    def settle(self, reservation: str, usage: dict[str, Any]) -> None:
        self._complete(
            reservation,
            kind="settle",
            cost_rmb=runner.estimate_cost_rmb(usage, self.usd_cny_rate),
            usage=usage,
        )

    def commit_estimate(self, reservation: str) -> float:
        return self._complete(reservation, kind="commit_estimate", cost_rmb=None)

    def resolve_uncertain(
        self,
        uncertainty_key: str,
        *,
        reservation_id: str | None = None,
    ) -> bool:
        """Close an ambiguity risk without changing its conservative charge."""

        if not isinstance(uncertainty_key, str) or not uncertainty_key.strip():
            raise ValueError("uncertainty_key must be a non-empty string")
        with self._locked():
            events = self._events_locked()
            matching = [
                event
                for event in events
                if event.get("run_id") == self.run_id
                and event.get("kind") in {"commit_estimate", "recover_orphan"}
                and (
                    event.get("uncertainty_key") == uncertainty_key
                    or (
                        reservation_id is not None
                        and event.get("reservation_id") == reservation_id
                    )
                )
            ]
            opened = bool(matching)
            resolved = any(
                event.get("run_id") == self.run_id
                and event.get("kind") == "resolve_uncertain"
                and (
                    event.get("uncertainty_key") == uncertainty_key
                    or (
                        reservation_id is not None
                        and event.get("reservation_id") == reservation_id
                    )
                )
                for event in events
            )
            if not opened or resolved:
                return False
            event = {
                "event_id": uuid.uuid4().hex,
                "kind": "resolve_uncertain",
                "run_id": self.run_id,
                "uncertainty_key": uncertainty_key,
            }
            if reservation_id is not None:
                event["reservation_id"] = reservation_id
            self._append_locked(event)
            return True

    def unresolved_uncertain_cost(self, uncertainty_key: str) -> float:
        """Return already committed conservative cost for one open risk."""

        with self._locked():
            events = self._events_locked()
            if any(
                event.get("run_id") == self.run_id
                and event.get("kind") == "resolve_uncertain"
                and event.get("uncertainty_key") == uncertainty_key
                for event in events
            ):
                return 0.0
            return sum(
                float(event.get("cost_rmb") or 0)
                for event in events
                if event.get("run_id") == self.run_id
                and event.get("kind") in {"commit_estimate", "recover_orphan"}
                and event.get("uncertainty_key") == uncertainty_key
            )

    def snapshot(self) -> dict[str, Any]:
        with self._locked():
            self._recover_orphans_locked()
            project_spent, run_spent, active = self._state(self._events_locked())
            project_reserved = sum(float(event["estimated_cost_rmb"]) for event in active.values())
            stage_reserved = sum(
                float(event["estimated_cost_rmb"])
                for event in active.values()
                if event.get("run_id") == self.run_id
            )
            stage_spent = run_spent.get(self.run_id, 0.0)
            stage_events = [
                event
                for event in self._events_locked()
                if event.get("run_id") == self.run_id
                and event.get("kind") in {"settle", "commit_estimate", "recover_orphan"}
            ]
            uncertainty_events = [
                event
                for event in stage_events
                if event.get("kind") in {"commit_estimate", "recover_orphan"}
            ]
            resolved_keys = {
                str(event["uncertainty_key"])
                for event in self._events_locked()
                if event.get("run_id") == self.run_id
                and event.get("kind") == "resolve_uncertain"
                and event.get("uncertainty_key")
            }
            resolved_reservations = {
                str(event["reservation_id"])
                for event in self._events_locked()
                if event.get("run_id") == self.run_id
                and event.get("kind") == "resolve_uncertain"
                and event.get("reservation_id")
            }
            unresolved_uncertain_calls = sum(
                (
                    event.get("uncertainty_key")
                    and str(event["uncertainty_key"]) not in resolved_keys
                )
                or (
                    not event.get("uncertainty_key")
                    and str(event.get("reservation_id") or "") not in resolved_reservations
                )
                for event in uncertainty_events
            )
            stage_usage = {
                "api_calls": len(stage_events),
                "settled_calls": sum(event.get("kind") == "settle" for event in stage_events),
                "uncertain_calls": len(uncertainty_events),
                "unresolved_uncertain_calls": unresolved_uncertain_calls,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }
            for event in stage_events:
                usage = event.get("usage")
                if not isinstance(usage, dict):
                    continue
                for key in ("input_tokens", "cached_tokens", "output_tokens", "total_tokens"):
                    stage_usage[key] += max(0, int(usage.get(key) or 0))
            return {
                "project_max_cost_rmb": self.project_max_cost_rmb,
                "stage_max_cost_rmb": self.stage_max_cost_rmb,
                "stage_max_api_calls": self.stage_max_api_calls,
                "usd_cny_rate": self.usd_cny_rate,
                "project_spent_rmb": project_spent,
                "project_reserved_rmb": project_reserved,
                "project_remaining_rmb": max(0.0, self.project_max_cost_rmb - project_spent - project_reserved),
                "stage_spent_rmb": stage_spent,
                "stage_reserved_rmb": stage_reserved,
                "stage_remaining_rmb": max(0.0, self.stage_max_cost_rmb - stage_spent - stage_reserved),
                "stage_remaining_api_calls": max(
                    0, self.stage_max_api_calls - len(stage_events) - sum(
                        event.get("run_id") == self.run_id for event in active.values()
                    )
                ),
                "active_reservations": len(active),
                "stage_usage": stage_usage,
            }
