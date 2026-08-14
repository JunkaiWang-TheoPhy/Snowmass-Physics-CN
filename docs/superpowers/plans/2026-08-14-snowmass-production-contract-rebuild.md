# Snowmass Production Contract Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. Every behavior change follows red-green-refactor.

**Goal:** Replace heuristic promotion and scattered completion evidence with a fail-closed, hash-bound production contract that can safely quarantine unknown PDF variants.

**Architecture:** Keep the existing translators and BabelDOC bridge as producers, but add one canonical evidence layer. Each paper receives an artifact manifest whose records bind producer, schema, parents, environment and content hashes. Batch promotion consumes only fresh evidence from the current run. Packaging requires semantic, structural and visual QC receipts; all missing or stale evidence is non-publishable.

**Tech Stack:** Python standard library, existing PyMuPDF/Pillow PDF tooling, unittest, existing persistent budget ledger.

## Global Constraints

- Only live `publication_allowed: true` records may enter any production state.
- Project API budget is finite, positive, and at most RMB 1000; zero never means unlimited.
- Existing source, translated, rendered and packaged artifacts remain auditable and are not deleted.
- Figure/table internal text remains byte-for-byte source text.
- References remain source-language verbatim while the section heading may use a locked translation.
- Resume and repackage operations may recover work but cannot count as fresh promotion evidence.
- Unknown or incomplete evidence is quarantined; it never silently falls back to publishable.
- No paid production resumes until the shadow gate passes.

---

### Task 1: Canonical artifact and environment evidence

**Files:**
- Create: `scripts/snowmass_production_contract.py`
- Create: `scripts/test_snowmass_production_contract.py`

**Produces:** `build_environment_lock()`, `write_artifact_manifest()`, `validate_artifact_manifest()`, `record_artifact()`, `derive_paper_state()`.

- [ ] Test that parent tampering, missing parents, path escape, duplicate artifact IDs, environment drift and rights-hash drift all fail closed.
- [ ] Test that an intact deterministic chain derives the expected paper state.
- [ ] Implement immutable artifact records with SHA-256, schema version, producer, parent IDs/hashes, environment-lock hash and contract versions.
- [ ] Lock Python executable/version, installed package versions, BabelDOC/IR versions, model/provider, pricing contract, font/cover assets and git tree identity.
- [ ] Verify tests and compile the module.

### Task 2: Fresh-evidence promotion and stage-aware projection

**Files:**
- Modify: `scripts/run_snowmass_batch_production.py`
- Modify: `scripts/test_run_snowmass_batch_production.py`

**Produces:** promotion metrics that separate `fresh_results` from recovered results; projections scoped to the next unpaid stage.

- [ ] Reproduce that ten recovered papers currently allow `pilot10 -> batch50` and lock the expected rejection.
- [ ] Reproduce that a prepared new paper currently reports zero calls and cannot launch; lock a finite positive translation projection.
- [ ] Exclude recovered/repackaged usage and results from cost/promotion evidence while retaining them in audit totals.
- [ ] Compute translation/revision projections before style artifacts exist, and compute style projection only after revision-ready.
- [x] Add gated stages `shadow`, `deepseek_probe`, `pilot5`, `pilot10`, `pilot25`, `batch50`, `remainder` without hard-coded eligible totals. The offline shadow may unlock only the one-paper paid DeepSeek probe.
- [ ] Require fresh sample counts and zero critical failures before each promotion.

### Task 3: Runtime and structural fail-closed repairs

**Files:**
- Modify: `scripts/run_snowmass_batch_production.py`
- Modify: `scripts/test_run_snowmass_batch_production.py`

- [ ] Lock and fix `#!/usr/bin/env python3` BabelDOC shebang resolution using `shlex.split` plus `shutil.which`.
- [ ] Reject unsafe chunk IDs before constructing `publication_chunks/<id>.md`.
- [ ] Replace zero figure/table-region truthiness with explicit `verified`, `not_applicable`, or `classification_failed` evidence.
- [ ] Make runtime preflight fail before paid work when Python, BabelDOC, font, QR renderer or filesystem locking is unavailable.

### Task 4: Semantic, structural and visual publication receipts

**Files:**
- Create: `scripts/snowmass_qc_contract.py`
- Create: `scripts/test_snowmass_qc_contract.py`
- Modify: `scripts/audit_snowmass_translation_pdf.py`
- Modify: `scripts/package_snowmass_translation_pdf.py`
- Modify corresponding tests.

**Produces:** three independently hashed QC receipts and a combined publishability verdict.

- [ ] Semantic receipt checks numbers, units, protected literals, citations, locked terms, title and model-meta leakage.
- [ ] Structural receipt checks chunk order, placeholders, reference boundary, figure/table evidence, page count and hash lineage.
- [ ] Visual receipt records audited pages/regions, clipping, overflow, low-text anomalies and reviewer/tool contract.
- [ ] Packaging rejects absent, stale or failed QC receipts and records their hashes as parents.
- [ ] Bump packaging contract and prove old receipts fail resume.

### Task 5: Quarantine and idempotent recovery

**Files:**
- Modify: `scripts/run_snowmass_batch_production.py`
- Modify: `scripts/snowmass_batch_budget.py`
- Modify corresponding tests.

- [ ] Persist quarantine reason, failing artifact IDs and required operator/action transition.
- [ ] Prevent an unchanged quarantined input from re-entering paid work.
- [ ] Test reserve/start/settle/uncertain/reconcile/restart without duplicate charges.
- [ ] Require zero unresolved uncertain transactions for publishable and promotion states.

### Task 6: Gold fixtures and shadow release gate

**Files:**
- Create: `tests/fixtures/snowmass-production/README.md`
- Add minimal text/manifest fixtures under that directory.
- Create: `scripts/test_snowmass_shadow_gate.py`

- [ ] Cover title fragmentation, ordinary-text title, two running headers, references variants, figures/tables with and without regions, placeholder reordering, long documents, malformed PDFs, numbers, units, citations and model-meta responses.
- [ ] Run a zero-paid-call shadow from source fixture to packaged fixture, then require a fresh one-paper DeepSeek probe before `pilot5`.
- [ ] Assert that removing any receipt or changing any parent hash makes the paper non-publishable.

### Task 7: Integration and release decision

**Files:**
- Update production design documentation and `AGENTS.md` only where new hard contracts must persist.

- [ ] Run all focused and repository Snowmass tests, syntax compilation and `git diff --check`.
- [ ] Run the existing ten-paper artifacts through migration/read-only validation; label them legacy evidence, not fresh evidence.
- [ ] Obtain independent code review of P0 invariants.
- [ ] Commit verified changes with Lore trailers.
- [ ] Keep production frozen unless shadow passes with zero critical failures and no paid API requests.

## Self-review

- Coverage includes both confirmed defects: resume-based promotion and new-paper projection deadlock.
- Coverage includes runtime shebang failure, zero-region vacuous verification and unsafe publication chunk paths.
- QC is part of the state transition, not an optional after-the-fact command.
- No task claims that finite fixtures eliminate all future PDF variants; unknown variants are explicitly quarantined.
