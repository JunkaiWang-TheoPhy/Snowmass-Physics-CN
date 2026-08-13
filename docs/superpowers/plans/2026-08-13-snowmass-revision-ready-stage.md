# Snowmass `revision_ready` Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a rights-gated, budgeted, request-capped, resumable production stage that stops after verified revision checkpoints so style-pass projection can be computed safely.

**Architecture:** Split the refined article workflow at the existing `revision_merge` boundary. Add a pure checkpoint projection for the pre-style work, then route `revision_ready` through the production runner without invoking the style projection gate. Preserve every existing full-workflow path unchanged.

**Tech Stack:** Python 3, `unittest`, existing DeepSeek client, `PersistentBudgetGuard`, JSON checkpoints, BabelDOC preparation.

## Global Constraints

- Only records with live `publication_allowed: true` may run.
- Project budget must be finite, positive, and at most RMB 1000; stage budget must be finite and positive.
- Stage request cap must be a finite positive integer; `0` never means unlimited.
- No restricted SCNet node may be contacted.
- No style, render, QC, package, or publish action may occur in `revision_ready`.
- Completed checkpoints are reused only after identity and hash validation.
- Uncertain paid calls fail closed under the existing replay contract.

---

### Task 1: Refined runner stop boundary

**Files:**
- Modify: `scripts/run_snowmass_refined_translation.py`
- Modify: `scripts/test_run_snowmass_refined_translation.py`

**Interfaces:**
- Consumes: `run_refined_article(..., stop_after_revision: bool = False)`.
- Produces: `{record_id, status: "revision_ready", chunks}` when the new flag is true.

- [ ] Add a failing test that stubs `run_batched_final_style_passes` to raise and verifies `run_refined_article(..., stop_after_revision=True)` returns after persisting a valid `revision_merge`.
- [ ] Run the focused test and verify it fails because the parameter does not exist.
- [ ] Add the keyword-only flag, persist `paper_status.status = "revision_ready"`, and return immediately after `revision_merge`.
- [ ] Add CLI `--stop-after-revision` and prove it does not combine with `--style-projection-only`.
- [ ] Run all refined-runner tests and commit with Lore trailers.

### Task 2: Conservative pre-style request projection

**Files:**
- Modify: `scripts/run_snowmass_refined_translation.py`
- Modify: `scripts/test_run_snowmass_refined_translation.py`

**Interfaces:**
- Produces: `revision_ready_projection(article_dir) -> dict` with `projection_ready`, `projected_worst_case_api_calls`, per-stage missing counts, and identity diagnostics.

- [ ] Add failing tests for a fresh manifest, partial translate/terminology checkpoints, complete revision checkpoints, invalid record identity, and uncertain phases.
- [ ] Verify tests fail because the projection function does not exist.
- [ ] Compute the ceiling from missing analysis, translate, terminology, critique/shards, and revision work using existing manifest/chunk policies; passthrough/fixed stages contribute zero where deterministically known.
- [ ] Include conservative critique allowance derived from current shard limits and never undercount repair allowance.
- [ ] Prove completed valid checkpoints lower the ceiling and invalid/uncertain identities fail closed.
- [ ] Run focused tests and commit with Lore trailers.

### Task 3: Production-stage routing and launch gate

**Files:**
- Modify: `scripts/run_snowmass_batch_production.py`
- Modify: `scripts/test_run_snowmass_batch_production.py`

**Interfaces:**
- Extends `TERMINAL_STAGES` with `revision_ready`.
- Production preflight and launch consume `revision_ready_projection()` for this stage.

- [ ] Add failing tests proving revision-ready preflight accepts missing revision checkpoints and reports their conservative request ceiling.
- [ ] Add failing tests proving cap refusal occurs before budget reservation, API-key loading, and client construction.
- [ ] Add failing tests proving `_run_article` passes `stop_after_revision=True` and never refills/renders/packages.
- [ ] Route projection/gating by stage: `revision_ready` uses pre-style projection; all later stages retain exact style projection behavior.
- [ ] Return structured CLI exit code `2` for revision projection/cap refusal.
- [ ] Run batch-production and refined-runner tests; commit with Lore trailers.

### Task 4: Production verification and three-paper launch preparation

**Files:**
- Modify only if verification exposes a defect in Tasks 1–3.
- Record local preflight output under `output/snowmass2021/production_control/` without committing paper artifacts.

**Interfaces:**
- Consumes the new stage and produces an approved finite request/cost envelope for the three incomplete papers.

- [ ] Run the complete seven-module Snowmass suite and `py_compile` on all modified Python files.
- [ ] Run direct CLI `--help` and a three-paper `revision_ready --preflight-only` command with project cap RMB 1000, stage cap RMB 100, and finite request cap.
- [ ] Verify no API key/client is loaded during preflight and record exact missing-stage counts plus conservative request ceiling.
- [ ] Obtain an independent code review focused on undercounting, resume identity, uncertain replay, and accidental style invocation.
- [ ] Commit only verification-driven fixes; otherwise record the clean evidence in the execution ledger.
