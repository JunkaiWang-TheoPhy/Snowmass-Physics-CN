#!/usr/bin/env python3
"""Behavior tests for the cross-paper Snowmass RMB budget."""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import threading
import unittest


MODULE_PATH = Path(__file__).with_name("snowmass_batch_budget.py")


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("persistent batch budget is not implemented")
    spec = importlib.util.spec_from_file_location("snowmass_batch_budget", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BudgetValidationTests(unittest.TestCase):
    def test_budget_must_be_finite_positive_and_within_authorized_maximum(self) -> None:
        module = load_module()
        for value in (0.0, -1.0, math.nan, math.inf, -math.inf, 1000.01):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.validate_budget(value, label="project", maximum=1000.0)
        self.assertEqual(module.validate_budget(1000.0, label="project", maximum=1000.0), 1000.0)


class PersistentBudgetGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.control = Path(self.temporary.name)

    def guard(self, *, run_id: str, project: float = 1.0, stage: float = 1.0, historical: float = 0.0):
        module = load_module()
        return module.PersistentBudgetGuard(
            self.control,
            project_max_cost_rmb=project,
            stage_max_cost_rmb=stage,
            run_id=run_id,
            usd_cny_rate=7.2,
            historical_spent_rmb=historical,
        )

    def test_initialized_project_cap_cannot_be_raised(self) -> None:
        self.guard(run_id="first", project=5.0, stage=1.0)
        with self.assertRaisesRegex(ValueError, "cannot be raised"):
            self.guard(run_id="second", project=6.0, stage=1.0)
        lowered = self.guard(run_id="third", project=4.0, stage=1.0)
        self.assertEqual(lowered.snapshot()["project_max_cost_rmb"], 4.0)

    def test_legacy_partial_initialization_recovers_from_historical_scan(self) -> None:
        module = load_module()
        (self.control / "budget_config.json").write_text(
            json.dumps(
                {
                    "schema_version": module.SCHEMA_VERSION,
                    "project_max_cost_rmb": 5.0,
                    "authorized_max_cost_rmb": module.AUTHORIZED_PROJECT_MAX_RMB,
                    "usd_cny_rate": 7.2,
                }
            ),
            encoding="utf-8",
        )

        recovered = self.guard(run_id="resume", project=5.0, stage=1.0, historical=0.75)

        self.assertAlmostEqual(recovered.snapshot()["project_spent_rmb"], 0.75)
        config = json.loads((self.control / "budget_config.json").read_text(encoding="utf-8"))
        self.assertTrue(config["ledger_initialized"])

    def test_initialized_config_with_deleted_ledger_fails_closed(self) -> None:
        self.guard(run_id="first", project=5.0, stage=1.0)
        (self.control / "budget_ledger.jsonl").unlink()

        with self.assertRaisesRegex(RuntimeError, "missing"):
            self.guard(run_id="resume", project=5.0, stage=1.0, historical=0.75)

    def test_historical_spend_is_imported_once(self) -> None:
        first = self.guard(run_id="first", historical=0.25)
        second = self.guard(run_id="second", historical=99.0)
        self.assertAlmostEqual(first.snapshot()["project_spent_rmb"], 0.25)
        self.assertAlmostEqual(second.snapshot()["project_spent_rmb"], 0.25)

    def test_two_guards_share_reservations_and_cannot_jointly_overspend(self) -> None:
        first = self.guard(run_id="shared", project=0.08, stage=0.08)
        second = self.guard(run_id="shared", project=0.08, stage=0.08)
        reservation = first.reserve("a" * 1000, 20000)
        with self.assertRaises(load_module().BudgetExceededError):
            second.reserve("b" * 1000, 20000)
        first.settle(
            reservation,
            {"input_tokens": 100, "cached_tokens": 0, "output_tokens": 50},
        )
        self.assertEqual(second.snapshot()["active_reservations"], 0)

    def test_dead_process_reservation_is_conservatively_recovered(self) -> None:
        guard = self.guard(run_id="crashed", project=1.0, stage=1.0)
        reservation = guard.reserve("source", 4096)
        ledger = self.control / "budget_ledger.jsonl"
        events = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        for event in events:
            if event.get("reservation_id") == reservation:
                event["owner_pid"] = 999_999_999
        ledger.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )

        recovered = self.guard(run_id="resume", project=1.0, stage=1.0)
        snapshot = recovered.snapshot()
        self.assertEqual(snapshot["active_reservations"], 0)
        self.assertGreater(snapshot["project_spent_rmb"], 0)

    def test_shared_stop_signal_blocks_new_reservations(self) -> None:
        module = load_module()
        stopped = threading.Event()
        guard = module.PersistentBudgetGuard(
            self.control,
            project_max_cost_rmb=1.0,
            stage_max_cost_rmb=1.0,
            run_id="run",
            usd_cny_rate=7.2,
            stop_event=stopped,
        )
        stopped.set()
        with self.assertRaises(module.ProductionStoppedError):
            guard.reserve("source", 4096)

    def test_snapshot_reports_stage_usage_and_uncertain_requests(self) -> None:
        guard = self.guard(run_id="metrics", project=1.0, stage=1.0)
        settled = guard.reserve("source", 4096)
        guard.settle(
            settled,
            {
                "input_tokens": 120,
                "cached_tokens": 20,
                "output_tokens": 40,
                "total_tokens": 160,
            },
        )
        uncertain = guard.reserve("source", 4096)
        guard.commit_estimate(uncertain)

        usage = guard.snapshot()["stage_usage"]

        self.assertEqual(usage["api_calls"], 2)
        self.assertEqual(usage["settled_calls"], 1)
        self.assertEqual(usage["uncertain_calls"], 1)
        self.assertEqual(usage["input_tokens"], 120)
        self.assertEqual(usage["cached_tokens"], 20)
        self.assertEqual(usage["output_tokens"], 40)

    def test_resolved_uncertainty_keeps_cost_audit_but_closes_gate_risk(self) -> None:
        guard = self.guard(run_id="replay", project=1.0, stage=1.0)
        uncertain = guard.reserve(
            "source",
            4096,
            uncertainty_key="arxiv:test:chunk0001:translate",
        )
        charged = guard.commit_estimate(uncertain)

        replay = guard.reserve(
            "source",
            4096,
            uncertainty_key="arxiv:test:chunk0001:translate",
        )
        guard.settle(
            replay,
            {
                "input_tokens": 120,
                "cached_tokens": 20,
                "output_tokens": 40,
                "total_tokens": 160,
            },
        )
        self.assertTrue(guard.resolve_uncertain("arxiv:test:chunk0001:translate"))

        snapshot = guard.snapshot()
        usage = snapshot["stage_usage"]
        self.assertGreaterEqual(snapshot["stage_spent_rmb"], charged)
        self.assertEqual(usage["uncertain_calls"], 1)
        self.assertEqual(usage["unresolved_uncertain_calls"], 0)

        events = [
            json.loads(line)
            for line in (self.control / "budget_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(sum(event["kind"] == "commit_estimate" for event in events), 1)
        self.assertEqual(sum(event["kind"] == "resolve_uncertain" for event in events), 1)

    def test_resolving_unknown_uncertainty_is_idempotent_and_writes_nothing(self) -> None:
        guard = self.guard(run_id="replay", project=1.0, stage=1.0)

        self.assertFalse(guard.resolve_uncertain("missing-risk"))
        self.assertEqual(
            (self.control / "budget_ledger.jsonl").read_text(encoding="utf-8"),
            "",
        )

    def test_legacy_uncertainty_can_be_closed_by_reservation_id(self) -> None:
        guard = self.guard(run_id="legacy", project=1.0, stage=1.0)
        reservation = guard.reserve("source", 4096)
        guard.commit_estimate(reservation)

        self.assertTrue(
            guard.resolve_uncertain(
                "arxiv:test:chunk0001:translate:legacy",
                reservation_id=reservation,
            )
        )
        self.assertEqual(
            guard.snapshot()["stage_usage"]["unresolved_uncertain_calls"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
