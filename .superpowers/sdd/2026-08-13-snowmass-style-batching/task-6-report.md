# Task 6 Report

Date: 2026-08-13
Task: Full verification and four-paper zero-API projection
Base: `c76017fe`

## Status

Completed for the verified ready paper and blocked for the other three target papers at the revision-checkpoint layer.

No code or production-document edits were required. I only wrote this report file.

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

- `Ran 284 tests in 3.025s`
- `OK`

### 2. py_compile on tracked Python files changed since `c76017fe`

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
git diff --check c76017fe..HEAD
```

Result:

- Clean. No whitespace or patch-format violations.

### 4. Zero-client / zero-credential proof for projection-only mode

I ran `scripts/run_snowmass_refined_translation.py` in-process with hard sentinels on:

- `runner.load_api_key`
- `runner.DeepSeekClient`

Observed call counts:

- `load_api_key`: `0`
- `DeepSeekClient`: `0`

This proves the `--style-projection-only` path returned before credential loading or client construction for the checked papers.

## Four-Paper Projection Results

Target records:

- `arxiv:2111.02442`
- `arxiv:2206.03456`
- `arxiv:2204.00001`
- `arxiv:2203.06380`

### Per-paper results

| Record | Projection ready | Missing revision chunks | anti_ai normal | academic normal | anti_ai worst-case | academic worst-case | Normal API calls | Worst-case API calls |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `arxiv:2111.02442` | yes | 0 | 15 | 15 | 59 | 59 | 30 | 118 |
| `arxiv:2206.03456` | no | 1430 | n/a | n/a | n/a | n/a | n/a | n/a |
| `arxiv:2204.00001` | no | 377 | n/a | n/a | n/a | n/a | n/a | n/a |
| `arxiv:2203.06380` | no | 357 | n/a | n/a | n/a | n/a | n/a | n/a |

### Aggregate

Only one paper was projection-ready, so only a ready-paper aggregate is meaningful:

- `anti_ai normal`: `15`
- `academic normal`: `15`
- `anti_ai worst-case`: `59`
- `academic worst-case`: `59`
- `projected normal API calls`: `30`
- `projected worst-case API calls`: `118`

The four-paper aggregate is blocked because three records have missing revision checkpoints.

## Focused Read-Only Review

I reviewed the current implementations for the four requested risk areas:

- Cross-chunk contamination in `_critique_context_for_chunk()` and the sharded critique merge path.
- Accounting duplication in `collect_article_run_usage()` and the style-batch ledger merge path.
- Uncertain transport handling in `_run_paper_model_phase()` and `execute_style_stage()`.
- Restart identity in style-batch `request_key` / `attempt_id` generation and uncertainty gating.

Finding:

- No actionable defect found in the current tracked code.

Evidence reviewed:

- Chunk-scoped critique text is filtered per chunk ID before style batching.
- Style-batch request usage is de-duplicated by `attempt_id`, and chunk-stage usage with `request_key` is skipped to avoid double counting.
- Uncertain requests are persisted as uncertain and cannot be silently replayed without the recorded contract.
- Request identity is derived from protocol, stage, model, ordered item keys, instructions, serialized payload, and output token ceiling.

## Blockers

The three incomplete target papers are blocked at the projection checkpoint layer:

- `arxiv:2206.03456` is missing revision checkpoints for all 1430 chunks.
- `arxiv:2204.00001` is missing revision checkpoints for all 377 chunks.
- `arxiv:2203.06380` is missing revision checkpoints for all 357 chunks.

No paid work was launched.

## Next Paid-Pilot Ceiling

For the only projection-ready paper in this four-paper set, the exact next paid-pilot ceiling is:

- `projected_worst_case_api_calls = 118`

There is no valid combined ceiling for the full four-paper set until the three blocked records are made projection-ready.
