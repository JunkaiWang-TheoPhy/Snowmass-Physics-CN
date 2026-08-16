#!/usr/bin/env python3
"""Conservatively settle a dead pdf2zh-next stage from its API call ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_snowmass_pdf2zh_next_ab import (
    SharedProjectReservation,
    atomic_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--request-ledger", type=Path, required=True)
    parser.add_argument("--request-ledger-sha256", required=True)
    parser.add_argument("--project-max-cost-rmb", type=float, required=True)
    parser.add_argument("--receipt", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reservation = SharedProjectReservation(
        control_dir=args.control_dir,
        run_id=args.run_id,
        project_max_cost_rmb=args.project_max_cost_rmb,
    )
    summary = reservation.reconcile_terminated(
        request_ledger_path=args.request_ledger,
        expected_request_ledger_sha256=args.request_ledger_sha256,
    )
    receipt = {
        "schema_version": 1,
        "status": "reconciled_conservatively",
        "run_id": args.run_id,
        "reservation_id": reservation.reservation_id,
        **summary,
    }
    if args.receipt is not None:
        atomic_json(args.receipt, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
