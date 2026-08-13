# Task 6 Report

Date: 2026-08-13
Task: Full verification and four-paper zero-API projection
Base: `c76017fe`

## Status

Completed for the verified ready paper and blocked for the other three target papers at the revision-checkpoint layer.

This run did require code changes. The tracked fixes in:

- `scripts/run_snowmass_refined_translation.py`
- `scripts/snowmass_style_batching.py`
- `scripts/test_run_snowmass_refined_translation.py`
- `scripts/test_snowmass_style_batching.py`

corrected the final-review projection semantics and locked them with regression coverage. I updated this report after rerunning the requested verification and projection checks.

## Corrected Semantics

- Recovery worst-case projection now uses the same recovery planner as execution: `plan_style_batches(..., recovery=True)` over the model items. The ceiling is therefore computed from the same safe item-and-character batching semantics that actual recovery requests use.
- Academic projection is now explicit about exactness:
  - `semantics: "exact"` only when all `anti_ai` checkpoints already exist and academic planning can consume `anti_ai` outputs.
  - `semantics: "conservative"` when `anti_ai` is still pending. In that case the report preserves `estimated_*` exact-planner counts for visibility, but request gating must use the conservative `worst_case_requests`.
- The conservative academic ceiling is mechanically safe:
  - one model item per normal request
  - one model item per recovery request
  - local passthrough and fixed chunks remain zero-request

## Verification Summary

### 1. Full unittest suite

Command:

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

Result:

- `Ran 287 tests in 4.000s`
- `OK`

### 2. py_compile on the six Python verification targets

Command:

```bash
python3 -m py_compile \
  scripts/run_snowmass_batch_production.py \
  scripts/run_snowmass_refined_translation.py \
  scripts/snowmass_style_batching.py \
  scripts/test_run_snowmass_batch_production.py \
  scripts/test_run_snowmass_refined_translation.py \
  scripts/test_snowmass_style_batching.py
```

Result:

- Passed with no output.

### 3. git diff hygiene

Command:

```bash
git diff --check
```

Result:

- Clean after the tracked fixes and this report update.

### 4. Zero-client / zero-credential proof for projection-only mode

I reran the actual `--style-projection-only` entrypoint for all four target papers in-process with hard sentinels on:

- `runner.load_api_key`
- `runner.DeepSeekClient`

The rights input was a temporary JSON-list manifest derived from `output/snowmass2021_translation/production/rights_snapshot.json["records"]`, because `main()` requires a list-shaped manifest.

Observed call counts across all four projection runs:

- `load_api_key`: `0`
- `DeepSeekClient`: `0`

This proves the `--style-projection-only` path returned before credential loading or client construction for every checked paper.

## Four-Paper Projection Results

Target records:

- `arxiv:2111.02442`
- `arxiv:2206.03456`
- `arxiv:2204.00001`
- `arxiv:2203.06380`

### Per-paper results

| Record | Projection ready | Missing revision chunks | anti_ai semantics | academic semantics | anti_ai normal | academic normal | anti_ai worst-case | academic worst-case | Normal API calls | Worst-case API calls |
| --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `arxiv:2111.02442` | yes | 0 | `exact` | `exact` | 15 | 15 | 59 | 59 | 30 | 118 |
| `arxiv:2206.03456` | no | 1430 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| `arxiv:2204.00001` | no | 377 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| `arxiv:2203.06380` | no | 357 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

### Aggregate

Only one paper was projection-ready, so only a ready-paper aggregate is meaningful:

- `anti_ai normal`: `15`
- `academic normal`: `15`
- `anti_ai worst-case`: `59`
- `academic worst-case`: `59`
- `projected normal API calls`: `30`
- `projected worst-case API calls`: `118`

These totals are unchanged from the earlier report because the only ready paper already had complete `anti_ai` checkpoints, so academic planning for that paper remained exact before and after the fix. The corrected conservative academic semantics matter for papers whose revision stage is ready while `anti_ai` is still incomplete; none of the four Task 6 targets were in that intermediate state.

The four-paper aggregate is still blocked because three records have missing revision checkpoints.

## Focused Read-Only Review

I rechecked the four requested risk areas after the fixes:

- Cross-chunk contamination in `_critique_context_for_chunk()` and the sharded critique merge path.
- Accounting duplication in `collect_article_run_usage()` and the style-batch ledger merge path.
- Uncertain transport handling in `_run_paper_model_phase()` and `execute_style_stage()`.
- Restart identity in style-batch `request_key` / `attempt_id` generation and uncertainty gating.

Finding:

- No further actionable defect found in the current tracked code after the projection-semantics fixes.

Evidence reviewed:

- Chunk-scoped critique text is still filtered per chunk ID before style batching.
- Style-batch request usage is still de-duplicated by `attempt_id`, and chunk-stage usage with `request_key` is still skipped to avoid double counting.
- Uncertain requests are still persisted as uncertain and cannot be silently replayed without the recorded contract.
- Request identity is still derived from protocol, stage, model, ordered item keys, instructions, serialized payload, and output token ceiling.

## Blockers

The three incomplete target papers are blocked at the projection checkpoint layer:

- `arxiv:2206.03456` is missing revision checkpoints for all 1430 chunks.
- `arxiv:2204.00001` is missing revision checkpoints for all 377 chunks.
- `arxiv:2203.06380` is missing revision checkpoints for all 357 chunks.

No paid work was launched.

## Next Paid-Pilot Ceiling

For the only projection-ready paper in this four-paper set, the exact next paid-pilot ceiling remains:

- `projected_worst_case_api_calls = 118`

There is still no valid combined ceiling for the full four-paper set until the three blocked records are made projection-ready.
