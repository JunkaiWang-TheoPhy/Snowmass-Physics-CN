# Snowmass Production Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify a source-first, rights-gated, resumable DeepSeek V4 Flash pipeline for the 273-record Snowmass first wave, then run staged paid pilots before full production.

**Architecture:** A pure-Python preparation layer creates an immutable rights snapshot, safely extracts arXiv sources, chooses and expands the main TeX document, protects non-translatable structures, and emits semantic chunks. A separate API runner uses request keys and validated checkpoints, while deterministic validators gate every stage and paper assembly.

**Tech Stack:** Python 3 standard library, existing Poppler/Pandoc/Bun utilities, `unittest`, DeepSeek OpenAI-compatible Responses API, `codex-bg`.

## Global Constraints

- Only live manifest records whose `publication_allowed` value is exactly `true` may enter the first wave.
- The eligible count must be derived from `site/data/papers.json`; it must never be hard-coded.
- Never connect to or probe the two restricted SCNet nodes.
- Preserve source, draft, terminology, anti-AI, academic, response, usage and QC artifacts separately.
- Never expose the DeepSeek API key in repository files, logs or command output.
- New code uses only the Python standard library and existing project tools.

---

### Task 1: Rights snapshot and safe source package reader

**Files:**
- Create: `scripts/snowmass_pipeline.py`
- Create: `scripts/test_snowmass_pipeline.py`

**Interfaces:**
- Produces: `build_rights_snapshot(manifest_path: Path, output_path: Path) -> dict`
- Produces: `detect_source_package(path: Path) -> Literal["tar", "single_tex"]`
- Produces: `safe_extract_source(path: Path, destination: Path) -> list[Path]`

- [ ] **Step 1: Write failing tests** for exact-true filtering, manifest hash stability, duplicate IDs, tar traversal, escaping symlinks, normal tar extraction and single-TeX gzip extraction.

```python
def test_rights_snapshot_only_accepts_literal_true(self):
    snapshot = PIPELINE.build_rights_snapshot(self.manifest, self.output)
    self.assertEqual([row["record_id"] for row in snapshot["records"]], ["arxiv:allowed"])

def test_safe_extract_rejects_parent_traversal(self):
    with self.assertRaises(PIPELINE.UnsafeArchiveError):
        PIPELINE.safe_extract_source(self.archive_with("../escape.tex"), self.destination)
```

- [ ] **Step 2: Run tests and confirm RED.**

Run: `python3 -m unittest scripts.test_snowmass_pipeline -v`

- [ ] **Step 3: Implement the minimum snapshot and extractor.** Snapshot JSON contains schema version, source manifest path/hash, creation time, eligible count and selected records. Extraction inspects gzip contents before choosing tar or single-TeX behavior and validates every member before writing.

- [ ] **Step 4: Run the focused tests and full existing suite.**

Run: `python3 -m unittest scripts.test_snowmass_pipeline scripts.test_run_snowmass_translation scripts.test_public_manifest -v`

### Task 2: Main TeX selection and recursive include expansion

**Files:**
- Modify: `scripts/snowmass_pipeline.py`
- Modify: `scripts/test_snowmass_pipeline.py`

**Interfaces:**
- Produces: `rank_main_tex(root: Path) -> list[MainCandidate]`
- Produces: `expand_tex(main_path: Path, root: Path) -> ExpandedTex`

- [ ] **Step 1: Write failing tests** proving unique main selection, multiple-candidate reporting, exclusion of backups, nested include order, cycle detection, missing include reporting and include traversal rejection.

```python
def test_expand_tex_preserves_nested_include_order(self):
    result = PIPELINE.expand_tex(self.root / "main.tex", self.root)
    self.assertLess(result.text.index("first section"), result.text.index("second section"))

def test_expand_tex_rejects_include_outside_root(self):
    with self.assertRaises(PIPELINE.UnsafeIncludeError):
        PIPELINE.expand_tex(self.root / "main.tex", self.root)
```

- [ ] **Step 2: Confirm the new tests fail for missing behavior.**
- [ ] **Step 3: Implement immutable candidate records and bounded recursive expansion.** Main scoring uses document markers, title/abstract presence, include fan-in, filename penalties and length. Ambiguity is data, not an exception hidden from the report.
- [ ] **Step 4: Re-run all pipeline tests.**

### Task 3: Structural protection and semantic chunks

**Files:**
- Modify: `scripts/snowmass_pipeline.py`
- Modify: `scripts/test_snowmass_pipeline.py`
- Replace behavior in: `scripts/prepare_snowmass_chunks.py`

**Interfaces:**
- Produces: `protect_structures(text: str) -> ProtectedText`
- Produces: `validate_and_restore(text: str, mapping: dict[str, str]) -> str`
- Produces: `semantic_chunks(text: str, target_words: int = 1500, min_words: int = 1200, max_words: int = 1800) -> list[str]`

- [ ] **Step 1: Write failing tests** for inline/display math, citations, refs, labels, URLs, emails, duplicate sentinels, missing sentinels and non-splitting inside protected spans/lists/paragraphs.

```python
def test_restore_rejects_missing_sentinel(self):
    protected = PIPELINE.protect_structures("See \cite{atlas} and $p_T$.")
    damaged = protected.text.replace(next(iter(protected.mapping)), "")
    with self.assertRaises(PIPELINE.StructureMismatchError):
        PIPELINE.validate_and_restore(damaged, protected.mapping)
```

- [ ] **Step 2: Confirm RED.**
- [ ] **Step 3: Implement deterministic sentinels and boundary-aware chunking.** The chunk builder consumes the rights snapshot, chooses source-first inputs, records fallback reasons and refuses to overwrite translated outputs when source hashes differ.
- [ ] **Step 4: Verify focused and regression suites.**

### Task 4: Completed-response validation and idempotent checkpoints

**Files:**
- Modify: `scripts/run_snowmass_translation.py`
- Modify: `scripts/test_run_snowmass_translation.py`

**Interfaces:**
- Produces: `validate_response(response: dict, expected_model: str) -> ParsedResponse`
- Produces: `request_key(...) -> str`
- Produces: `checkpoint_is_valid(status: dict, output_path: Path, expected_key: str) -> bool`

- [ ] **Step 1: Write failing tests** for completed/incomplete/failed responses, wrong model, max-output truncation, missing output, output hash mismatch, stale request key and a valid checkpoint.

```python
def test_incomplete_response_is_not_accepted(self):
    with self.assertRaises(RUNNER.IncompleteResponseError):
        RUNNER.validate_response({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, RUNNER.MODEL)

def test_nonempty_stale_output_is_not_a_checkpoint(self):
    self.output.write_text("旧结果", encoding="utf-8")
    self.assertFalse(RUNNER.checkpoint_is_valid(self.status, self.output, "new-key"))
```

- [ ] **Step 2: Run focused tests and confirm RED.**
- [ ] **Step 3: Implement response parsing, request keys and two-phase checkpoint writes.** Store raw response metadata before validation. Do not retry ambiguous transport failures automatically; mark them `uncertain`.
- [ ] **Step 4: Run all runner and rights tests.**

### Task 5: Deterministic translation QC and conditional stages

**Files:**
- Create: `scripts/snowmass_translation_qc.py`
- Create: `scripts/test_snowmass_translation_qc.py`
- Modify: `scripts/run_snowmass_translation.py`

**Interfaces:**
- Produces: `validate_chunk(source: str, translated: str, mapping: dict, glossary: list[dict]) -> QCReport`
- Produces: `stage_decision(stage: str, text: str, glossary: list[dict]) -> StageDecision`

- [ ] **Step 1: Write failing tests** for changed numbers, units, URLs, citations, missing sentinels, locked-term violations, permitted acronyms and `no-op` terminology/anti-AI stages.
- [ ] **Step 2: Confirm RED.**
- [ ] **Step 3: Implement pure validators and integrate the hard gate.** A stage output is promoted only after QC. Conditional stages atomically copy the prior text and record the deterministic reason when no model call is needed.
- [ ] **Step 4: Run all new and existing tests.**

### Task 6: Production preparation report and interruption recovery

**Files:**
- Create: `scripts/prepare_snowmass_production.py`
- Create: `scripts/test_prepare_snowmass_production.py`
- Create at runtime: `output/snowmass2021_translation/production/rights_snapshot.json`
- Create at runtime: `output/snowmass2021_translation/production/preparation_report.json`

**Interfaces:**
- CLI: `python3 scripts/prepare_snowmass_production.py --rights-manifest site/data/papers.json --source-root output/snowmass2021_sources --output-root output/snowmass2021_translation/production`

- [ ] **Step 1: Write a failing integration test** using one tar source, one single-TeX gzip, one PDF fallback and one blocked record. Assert that only allowed records appear and rerunning reuses hash-valid artifacts.
- [ ] **Step 2: Confirm RED.**
- [ ] **Step 3: Implement the orchestration CLI and durable per-record reports.** It must continue after per-paper failures and exit nonzero when ambiguity or preparation failures remain.
- [ ] **Step 4: Run the integration test, force termination between two records, rerun, and verify no validated artifact changed.**
- [ ] **Step 5: Run preparation for the live 273-paper snapshot in a detached `codex-bg` job and inspect final counts.**

### Task 7: Paid staged pilot and expansion decision

**Files:**
- Create at runtime: `output/snowmass2021_translation/production/runs/<timestamp>/run.json`
- Create at runtime: per-paper stage, usage and QC artifacts

**Interfaces:**
- CLI: `python3 scripts/run_snowmass_translation.py --root output/snowmass2021_translation/production --concurrency N --article SLUG`

- [ ] **Step 1: Select one allowed source-rich median-length paper from the preparation report and run at concurrency 1.**
- [ ] **Step 2: Verify every chunk has a completed response, valid request key, output hash, hard-QC pass and usage record; manually compare technical passages, equations and citations.**
- [ ] **Step 3: Select a stratified ten-paper set covering short/median/long, formula-heavy, source ambiguity and PDF fallback; run at concurrency 4 with a ¥6 pilot stop.**
- [ ] **Step 4: Compute faithfulness, terminology, format, retry, hard-failure, cost and latency metrics. Stop if any design threshold fails.**
- [ ] **Step 5: If all gates pass, run 50 papers at concurrency 8, then the remaining eligible queue at concurrency 16 under the cumulative ¥100 hard stop. Use `codex-bg` and verify each completed batch before expansion.**

## Plan self-review

- Every production design requirement maps to Tasks 1–7.
- The pipeline derives the live eligible set and has no all-record bypass.
- Source selection, protection, chunking, response status, idempotency, QC, recovery and staged costs have explicit tests.
- Runtime artifacts are outside Git commits; source and tests remain reviewable.
- No implementation step requires a new dependency or either restricted SCNet node.
