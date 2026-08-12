# Snowmass Batch Production Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable rights-gated batch orchestrator with a crash-safe cross-paper RMB budget and automatic publication QC, then run a capped baseline and 10-paper pilot.

**Architecture:** A focused budget module owns the immutable project cap and append-only locked ledger. A separate batch CLI selects live eligible records, invokes existing idempotent prepare/refined/refill/package stages, persists per-paper state, and fails closed on budget or QC. Existing translation code gains only the minimum cost-efficiency and zero-budget corrections.

**Tech Stack:** Python 3 standard library, existing BabelDOC/PyMuPDF/PyPDF stack, `unittest`, DeepSeek V4 Flash, `codex-bg`.

## Global Constraints

- Only records whose live `publication_allowed` value is exactly `true` enter a queue.
- Project and stage RMB budgets are required finite positive values; the project cap is at most `¥1000` and can never be raised after initialization.
- Preserve all existing audit artifacts and original rendered PDFs.
- Stop dispatching new papers on any budget, permission, uncertain-request, structural, reference, figure, table or publication QC failure.
- Never connect to the restricted SCNet nodes.
- Add no dependency.

---

### Task 1: Persistent cross-paper budget

**Files:**
- Create: `scripts/snowmass_batch_budget.py`
- Create: `scripts/test_snowmass_batch_budget.py`
- Modify: `scripts/run_snowmass_translation.py`
- Modify: `scripts/test_run_snowmass_translation.py`

**Interfaces:**
- Produces: `validate_budget(value: float, *, label: str, maximum: float | None = None) -> float`
- Produces: `PersistentBudgetGuard(control_dir: Path, *, project_max_cost_rmb: float, stage_max_cost_rmb: float, run_id: str, usd_cny_rate: float, historical_spent_rmb: float = 0.0)`
- Compatible methods: `reserve`, `settle`, `commit_estimate`, `snapshot`.

- [ ] Write tests that reject zero/NaN/infinity/>1000, prevent cap increases, recover orphan reservations conservatively, and prevent two guards from jointly exceeding the cap.
- [ ] Run focused tests and observe the missing-module/behavior failures.
- [ ] Implement atomic config writes and `fcntl.flock` guarded JSONL transactions.
- [ ] Change the old runner CLI and in-memory `BudgetGuard` to require a positive cap.
- [ ] Run focused tests to green.

### Task 2: Rights-gated resumable batch state machine

**Files:**
- Create: `scripts/run_snowmass_batch_production.py`
- Create: `scripts/test_run_snowmass_batch_production.py`

**Interfaces:**
- Produces: `load_publication_records(path: Path) -> list[dict]`
- Produces: `select_stage_records(records, stage, explicit_ids=(), max_articles=None) -> list[dict]`
- Produces: `evaluate_article_qc(article_dir: Path) -> dict`
- Produces: `run_batch(config: BatchConfig, *, client=None) -> dict`

- [ ] Write tests for exact-true rights filtering, deterministic stage selection, explicit blocked IDs, required budgets, resume after interruption, stop-on-first-hard-failure and complete QC aggregation.
- [ ] Run focused tests and observe failures.
- [ ] Implement immutable run snapshot, atomic per-paper state, shared persistent guard, prepare/refined/refill/package adapters, and fail-closed summary.
- [ ] Persist token/call/source-character cost metrics and a machine-readable next-stage promotion gate.
- [ ] Run focused tests to green.

### Task 3: Cost-efficient quality stages

**Files:**
- Modify: `scripts/snowmass_translation_qc.py`
- Modify: `scripts/test_snowmass_translation_qc.py`
- Modify: `scripts/test_run_snowmass_refined_translation.py`

**Interfaces:**
- `stage_decision("anti_ai", text, glossary)` always retains the independent model pass.
- Existing no-actionable revision behavior remains mandatory and audited.

- [ ] Preserve tests proving anti-AI and academic remain independent model stages.
- [ ] Keep cost optimization at document scheduling and no-actionable revision boundaries.
- [ ] Verify refined artifacts and usage accounting remain complete.

### Task 4: Integrated verification and staged production

**Files:**
- Modify: `docs/superpowers/specs/2026-08-10-snowmass-production-translation-design.md`
- Create runtime artifacts only under ignored `output/snowmass2021/`.

- [ ] Run all Snowmass unit tests and compile checks.
- [ ] Run a no-API queue/rights/budget preflight and verify the live eligible count.
- [ ] Start baseline under a small stage cap with `codex-bg`; verify status, logs, budget snapshot and artifacts.
- [ ] If baseline QC is green, start `pilot10` with project cap `¥1000` and stage cap `¥50`; otherwise stop and report the gate failure.
- [ ] Commit verified source/docs/tests with a Lore-formatted commit.
