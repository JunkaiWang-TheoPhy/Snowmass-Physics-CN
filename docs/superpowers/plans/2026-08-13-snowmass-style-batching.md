# Snowmass Style-Pass Batching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-chunk anti-AI and academic model calls with resumable, exact-ID batches while retaining separate ordered stages and per-chunk deterministic QC.

**Architecture:** A new focused module owns batch planning, protocol parsing, request identity, execution, and projection. `run_snowmass_refined_translation.py` keeps paper-phase ordering and delegates only the final two style stages to that module. Existing `run_snowmass_translation.process_chunk()` remains the fallback for chunks that cannot enter a normal batch and remains the source of shared protection, restoration, QC, checkpoint, and cost helpers.

**Tech Stack:** Python 3 standard library, `unittest`, existing DeepSeek Responses client, existing persistent RMB/API-call ledger, atomic JSON/text checkpoints.

## Global Constraints

- Keep translation, terminology, anti-AI, and academic polishing as four causally ordered stages.
- Normal batches contain at most 24 chunks and 18,000 protected-input characters.
- Recovery batches contain at most 8 chunks and run at most once.
- Keep maximum response allowance between 4,096 and 20,000 tokens.
- Missing, duplicate, unknown, blank, or malformed response IDs fail closed.
- Commit valid sibling chunks independently before retrying failed siblings.
- Preserve numbers, units, formulas, citations, URLs, names, glossary decisions, and protected structures per chunk.
- References, figure text, table text, fixed translations, and other passthrough policies consume no paid style request.
- Enforce the live 273-paper rights gate, finite positive RMB budget up to ¥1000, and finite positive API-call cap; zero never means unlimited.
- Do not start a paid pilot until a zero-API projection fits the declared request cap.

---

### Task 1: Pure batch planner and exact-ID response protocol

**Files:**
- Create: `scripts/snowmass_style_batching.py`
- Create: `scripts/test_snowmass_style_batching.py`

**Interfaces:**
- Produces: `StyleBatchItem`, `StyleBatch`, `plan_style_batches()`, `build_style_batch_payload()`, `parse_style_batch_response()`, and `style_batch_request_key()`.
- Consumes: `run_snowmass_translation.text_hash()` and standard JSON only.

- [ ] **Step 1: Write failing planner tests**

```python
def test_twenty_five_items_plan_as_two_normal_batches():
    items = tuple(item(f"chunk{i:04d}", "甲" * 100) for i in range(25))
    batches = batching.plan_style_batches(items)
    self.assertEqual([len(batch.items) for batch in batches], [24, 1])

def test_character_limit_closes_batch_before_overflow():
    items = (item("chunk0001", "a" * 10_000), item("chunk0002", "b" * 9_000))
    self.assertEqual([len(x.items) for x in batching.plan_style_batches(items)], [1, 1])
```

- [ ] **Step 2: Run planner tests and verify RED**

Run: `python3 -m unittest scripts.test_snowmass_style_batching.StyleBatchPlanningTests`

Expected: import or attribute failure because the new planner does not exist.

- [ ] **Step 3: Implement immutable planner types and limits**

```python
STYLE_BATCH_PROTOCOL = "snowmass-style-batch-v1"
NORMAL_BATCH_CHUNKS = 24
NORMAL_BATCH_CHARACTERS = 18_000
RECOVERY_BATCH_CHUNKS = 8

@dataclass(frozen=True)
class StyleBatchItem:
    chunk_id: str
    protected_text: str
    source_hash: str
    prior_hash: str
    glossary_text: str
    context: str
    item_key: str

@dataclass(frozen=True)
class StyleBatch:
    items: tuple[StyleBatchItem, ...]
    recovery: bool = False
```

`plan_style_batches()` must preserve input order, reject duplicate IDs, close on either limit, and place an oversized item alone.

- [ ] **Step 4: Write failing protocol tests**

```python
def test_response_requires_exact_nonblank_id_mapping():
    expected = ("chunk0001", "chunk0002")
    good = '{"translations":{"chunk0001":"甲","chunk0002":"乙"}}'
    self.assertEqual(set(batching.parse_style_batch_response(good, expected)), set(expected))
    for bad in (
        '{"translations":{"chunk0001":"甲"}}',
        '{"translations":{"chunk0001":"甲","chunk0002":"乙","chunk9999":"丙"}}',
        '{"translations":{"chunk0001":"甲","chunk0002":""}}',
    ):
        with self.assertRaises(batching.StyleBatchProtocolError):
            batching.parse_style_batch_response(bad, expected)
```

- [ ] **Step 5: Implement payload, parser, and deterministic request identity**

The payload must have this exact shape:

```json
{
  "protocol": "snowmass-style-batch-v1",
  "stage": "anti_ai",
  "chunks": [
    {
      "id": "chunk0001",
      "text": "protected text",
      "locked_terminology": "source => target",
      "read_only_context": "chunk-local critique"
    }
  ]
}
```

The request key must hash protocol version, stage, model, ordered item keys, instructions, serialized payload, and maximum output tokens.

- [ ] **Step 6: Run Task 1 tests and commit**

Run: `python3 -m unittest scripts.test_snowmass_style_batching`

Expected: all Task 1 tests pass.

Commit with a Lore message explaining why exact-ID batching is required.

---

### Task 2: Resumable per-chunk preparation and local policy handling

**Files:**
- Modify: `scripts/snowmass_style_batching.py`
- Modify: `scripts/test_snowmass_style_batching.py`

**Interfaces:**
- Consumes: Task 1 types; `runner.protect_stage_text()`, `runner.restore_stage_text()`, `runner.validate_chunk()`, `runner.stage_output_path()`, `runner.atomic_json()`, and `runner.atomic_text()`.
- Produces: `prepare_style_items()` and `StyleStagePlan` with `reused`, `local`, `model_items`, `normal_batches`, and `worst_case_requests`.

- [ ] **Step 1: Write failing checkpoint and local-policy tests**

```python
def test_passthrough_and_valid_checkpoint_do_not_enter_paid_batches():
    plan = batching.prepare_style_items(
        article_dir=article,
        chunks=chunks,
        task_factory=task_factory,
        terms=[],
        stage="anti_ai",
        input_stage="revision",
        context_factory=lambda _chunk: "",
    )
    self.assertEqual(plan.model_items, ())
    self.assertEqual(plan.worst_case_requests, 0)
```

Add separate tests for a stale prior-stage hash, a valid batch item checkpoint, a hard exact translation, and a reference passthrough. Each test must assert output hash and stage policy fields, not only file existence.

- [ ] **Step 2: Run preparation tests and verify RED**

Run: `python3 -m unittest scripts.test_snowmass_style_batching.StylePreparationTests`

Expected: failure because `prepare_style_items()` does not exist.

- [ ] **Step 3: Implement per-chunk item identity and checkpoint reuse**

```python
item_key = runner.text_hash(json.dumps({
    "protocol": STYLE_BATCH_PROTOCOL,
    "stage": stage,
    "chunk_id": chunk_id,
    "source_hash": runner.text_hash(source),
    "prior_hash": runner.text_hash(prior),
    "glossary": selected_terms,
    "context_identity": context_identity,
    "policy": policy,
}, ensure_ascii=False, sort_keys=True))
```

A reusable checkpoint requires `status == "complete"`, matching item key and policy, `qc.ok is True`, and a matching output hash. Passthrough output is the original source text; fixed and deterministic local policies use their existing exact content. Run live QC before marking any local result complete.

- [ ] **Step 4: Implement protected model-item preparation and projection**

For each unresolved model-required chunk, compile source-specific glossary terms, protect the prior-stage text, store restoration data in memory, and create a `StyleBatchItem`. `worst_case_requests` equals normal batch count plus recovery batch count computed from all model items at a maximum of 8 per recovery batch.

- [ ] **Step 5: Run preparation tests and commit**

Run: `python3 -m unittest scripts.test_snowmass_style_batching`

Expected: all Task 1 and Task 2 tests pass.

Commit with a Lore message recording checkpoint identity and passthrough behavior.

---

### Task 3: Paid batch execution, sibling durability, and one recovery pass

**Files:**
- Modify: `scripts/snowmass_style_batching.py`
- Modify: `scripts/test_snowmass_style_batching.py`

**Interfaces:**
- Consumes: `StyleStagePlan`; a client exposing `complete(instructions, input_text, max_output_tokens)`; a budget guard exposing `reserve()`, `settle()`, `commit_estimate()`, and optionally `snapshot()`.
- Produces: `execute_style_stage()` returning `StyleStageResult` with planned, completed, reused, local, failed, normal request, recovery request, token, and RMB counts.

- [ ] **Step 1: Write failing mixed-success and protocol tests**

Use a fake client that returns one valid chunk and one number-changing chunk in the same response. Assert that the valid sibling is atomically committed, only the failed ID enters recovery, and malformed exact-ID responses commit nothing from that request.

```python
self.assertEqual(client.requested_ids, [
    ("chunk0001", "chunk0002"),
    ("chunk0002",),
])
self.assertEqual(status("chunk0001", "anti_ai")["status"], "complete")
```

- [ ] **Step 2: Run execution tests and verify RED**

Run: `python3 -m unittest scripts.test_snowmass_style_batching.StyleExecutionTests`

Expected: failure because `execute_style_stage()` does not exist.

- [ ] **Step 3: Implement one reserved request per batch**

Before `client.complete()`, reserve one request and conservative cost. On a settled response, call `settle()` exactly once and append one article cost-ledger event keyed by batch request ID. On ambiguous transport, call `commit_estimate()` exactly once, persist uncertain batch state, and do not replay automatically.

- [ ] **Step 4: Implement per-item restoration, QC, and durable sibling commits**

For each returned ID, restore only that item's protected structures and validate against only that item's source and selected glossary. Write successful output and chunk status immediately. Persist rejected candidates for failed items using `runner.persist_rejected_candidate()`. Do not attach the full batch usage to every chunk status; record it once in `style_batch_status.json` and the article cost ledger.

- [ ] **Step 5: Implement one recovery pass**

Protocol failure retries the affected IDs in groups of at most 8. Per-chunk QC failure retries only failed IDs. Recovery responses go through the same exact-ID, restoration, and QC path. Remaining failures raise a barrier error after all successful siblings are durable.

- [ ] **Step 6: Add request-cap projection rejection test**

When `budget_guard.snapshot()` reports fewer remaining API calls than `plan.worst_case_requests`, `execute_style_stage()` must raise `RequestLimitExceededError` before the first client call.

- [ ] **Step 7: Run execution tests and commit**

Run: `python3 -m unittest scripts.test_snowmass_style_batching scripts.test_snowmass_batch_budget`

Expected: all tests pass, including concurrent call-cap tests.

Commit with a Lore message explaining sibling durability and bounded recovery.

---

### Task 4: Integrate separate anti-AI and academic batch barriers

**Files:**
- Modify: `scripts/run_snowmass_refined_translation.py`
- Modify: `scripts/test_run_snowmass_refined_translation.py`
- Modify: `scripts/snowmass_style_batching.py`
- Modify: `scripts/test_snowmass_style_batching.py`

**Interfaces:**
- Consumes: `prepare_style_items()` and `execute_style_stage()` from Tasks 2–3.
- Produces: `run_batched_final_style_passes()` and a completed academic checkpoint compatible with `_verified_merge(..., "academic")` and publication QC.

- [ ] **Step 1: Write failing stage-order integration test**

Create three body chunks and one reference chunk. The fake client records stages and IDs. Assert all anti-AI requests finish before the first academic request, the reference ID never appears in a request, and final academic files exist for every chunk.

- [ ] **Step 2: Run integration test and verify RED**

Run: `python3 -m unittest scripts.test_run_snowmass_refined_translation.RefinedOrchestratorTests.test_final_style_uses_ordered_exact_id_batches`

Expected: the current per-chunk barrier produces more requests or lacks batch records.

- [ ] **Step 3: Replace only the final per-chunk barrier**

Keep draft, critique, and revision logic unchanged. Replace the `stages=("anti_ai", "academic")` `_run_chunk_barrier()` call with:

```python
anti_ai = style_batching.run_style_stage(
    article_dir=article_dir,
    chunks=chunks,
    task_factory=chunk_task,
    terms=terms,
    stage="anti_ai",
    input_stage="revision",
    context_factory=lambda chunk: _critique_context_for_chunk(critique, str(chunk["id"])),
    client=client,
    budget_guard=budget_guard,
    run_id=run_id,
)
academic = style_batching.run_style_stage(
    article_dir=article_dir,
    chunks=chunks,
    task_factory=chunk_task,
    terms=terms,
    stage="academic",
    input_stage="anti_ai",
    context_factory=lambda chunk: _critique_context_for_chunk(critique, str(chunk["id"])),
    client=client,
    budget_guard=budget_guard,
    run_id=run_id,
)
```

- [ ] **Step 4: Persist stage projections and execution receipts**

Write `style_batch_projection.json` before paid calls with exact normal and worst-case requests for both stages. Replace `execution_mode: observational_projection_only` with `execution_mode: exact_id_batching`. Store actual request counts after each stage without erasing the pre-launch projection.

- [ ] **Step 5: Run refined-orchestrator regressions and commit**

Run: `python3 -m unittest scripts.test_snowmass_style_batching scripts.test_run_snowmass_refined_translation scripts.test_run_snowmass_translation scripts.test_snowmass_translation_qc scripts.test_snowmass_document_units`

Expected: all tests pass and existing draft/revision behavior is unchanged.

Commit with a Lore message explaining why only final style barriers changed.

---

### Task 5: Zero-API projection command and production hard gate

**Files:**
- Modify: `scripts/run_snowmass_refined_translation.py`
- Modify: `scripts/run_snowmass_batch_production.py`
- Modify: `scripts/test_run_snowmass_refined_translation.py`
- Modify: `scripts/test_run_snowmass_batch_production.py`
- Modify: `docs/superpowers/specs/2026-08-12-snowmass-batch-production-control-design.md`

**Interfaces:**
- Produces: refined CLI `--style-projection-only`; batch summary fields `style_projection`, `projected_normal_api_calls`, and `projected_worst_case_api_calls`.
- Consumes: Task 4 projection data and `PersistentBudgetGuard.stage_remaining_api_calls`.

- [ ] **Step 1: Write failing zero-credential projection test**

Invoke the refined CLI with `--style-projection-only` and patch `runner.load_api_key` to raise if called. Assert exit code zero and a JSON report with separate anti-AI and academic counts.

- [ ] **Step 2: Implement projection-only mode**

Projection mode reads local manifests, prior-stage artifacts, passthrough policies, glossary, and checkpoints; it never loads credentials or creates a client. If revision artifacts are incomplete, report `projection_ready: false` with missing chunk IDs rather than guessing.

- [ ] **Step 3: Write failing production cap test**

Construct a preflight whose projected worst case is 17 calls and configured stage cap is 16. Assert production fails before `client.complete()` and before any budget reservation.

- [ ] **Step 4: Implement aggregate projection and launch gate**

Aggregate selected papers' ready projections. Refuse paid launch when any projection is not ready or when the total projected worst case exceeds the stage call cap. Include the projection in durable run snapshots and terminal summaries.

- [ ] **Step 5: Run production-control regressions and commit**

Run: `python3 -m unittest scripts.test_snowmass_style_batching scripts.test_run_snowmass_refined_translation scripts.test_run_snowmass_batch_production scripts.test_snowmass_batch_budget`

Expected: all tests pass, including rights, RMB, request-cap, resume, and projection-only tests.

Commit with a Lore message recording the no-credential preflight requirement.

---

### Task 6: Full verification and four-paper zero-API projection

**Files:**
- Modify only if verification exposes a defect in files owned by Tasks 1–5.
- Record generated projection under the existing untracked production-control output tree; do not commit local paper artifacts.

**Interfaces:**
- Consumes all previous tasks.
- Produces completion evidence and the exact call ceiling for the next paid pilot.

- [ ] **Step 1: Run the complete relevant suite**

Run:

```bash
python3 -m unittest \
  scripts.test_snowmass_style_batching \
  scripts.test_snowmass_document_units \
  scripts.test_snowmass_translation_qc \
  scripts.test_run_snowmass_translation \
  scripts.test_run_snowmass_refined_translation \
  scripts.test_snowmass_batch_budget \
  scripts.test_run_snowmass_batch_production
```

Expected: zero failures.

- [ ] **Step 2: Compile and inspect**

Run `python3 -m py_compile` on every modified Python file, `git diff --check`, and an independent read-only code review focused on cross-chunk contamination, accounting duplication, uncertain transport, and restart identity.

- [ ] **Step 3: Run projection-only mode for the four incomplete pilot papers**

Target records:

- `arxiv:2111.02442`
- `arxiv:2206.03456`
- `arxiv:2204.00001`
- `arxiv:2203.06380`

Verify the command performs zero client calls and reports exact anti-AI normal, academic normal, and worst-case recovery requests. Do not launch paid work in this task.

- [ ] **Step 4: Commit any verification-driven fixes**

Use a Lore commit that lists the exact test count, projection totals, independent review result, and any remaining untested paid behavior.
