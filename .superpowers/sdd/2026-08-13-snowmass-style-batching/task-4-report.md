# Task 4 Report

## Scope

- Integrated only the final `anti_ai` and `academic` style phases into exact-id batching.
- Left `draft`, `critique`, and `revision` orchestration unchanged.
- Limited code edits to:
  - `scripts/run_snowmass_refined_translation.py`
  - `scripts/test_run_snowmass_refined_translation.py`
  - `scripts/snowmass_style_batching.py`
  - `scripts/test_snowmass_style_batching.py`

## RED

- Added unit RED for exact projection serialization:
  - `python3 -m unittest scripts.test_snowmass_style_batching.StyleBatchPlanningTests.test_stage_plan_projection_reports_exact_batches_and_worst_case_requests scripts.test_snowmass_style_batching.StyleBatchPlanningTests.test_stage_result_projection_reports_actual_request_counts`
  - Initial failure: missing `stage_plan_projection()` and `stage_result_projection()`.
- Added integration RED for final-stage ordering:
  - `python3 -m unittest scripts.test_run_snowmass_refined_translation.RefinedOrchestratorTests.test_final_style_uses_ordered_exact_id_batches`
  - Initial failure: final style requests were still chunk-local, so observed calls were `anti_ai` / `academic` interleavings instead of stage-wide exact-id batches.

## GREEN

- Replaced the final per-chunk `stages=("anti_ai", "academic")` barrier with two explicit exact-id batch stages.
- `anti_ai` is now fully planned and executed before `academic` planning begins.
- Reference / passthrough chunks never enter paid style-batch requests; they complete locally and still produce academic outputs compatible with `_verified_merge(..., "academic")`.
- `style_batch_projection.json` now uses `execution_mode: exact_id_batching`, stores exact planned normal / worst-case request counts, and appends actual per-stage request totals without erasing the pre-launch plan.
- Kept the deferred finding intact: no usage-rollup / runner-aggregator work was pulled into Task 4.

## Verification

- Targeted RED/GREEN:
  - `python3 -m unittest scripts.test_snowmass_style_batching.StyleBatchPlanningTests.test_stage_plan_projection_reports_exact_batches_and_worst_case_requests scripts.test_snowmass_style_batching.StyleBatchPlanningTests.test_stage_result_projection_reports_actual_request_counts scripts.test_run_snowmass_refined_translation.RefinedOrchestratorTests.test_final_style_uses_ordered_exact_id_batches`
- Full regression:
  - `python3 -m unittest scripts.test_snowmass_style_batching scripts.test_run_snowmass_refined_translation scripts.test_run_snowmass_translation scripts.test_snowmass_translation_qc scripts.test_snowmass_document_units`
- Syntax:
  - `python3 -m py_compile scripts/run_snowmass_refined_translation.py scripts/test_run_snowmass_refined_translation.py scripts/snowmass_style_batching.py scripts/test_snowmass_style_batching.py`

## Commit

- Lore intent line:
  - `Preserve final style-stage causality under exact-id batching`

## Self-Review

- The final-stage scope stayed narrow; no draft/critique/revision logic changed.
- Academic planning is intentionally delayed until anti-AI outputs exist, so the academic batch plan is exact for the real anti-AI outputs instead of speculative.
- I removed the stale observational-projection helper/test pair to avoid leaving contradictory dead code behind.
- I did not alter unresolved-uncertain handling or add any implicit replay path beyond the pre-existing stage-batching recovery behavior.

## Fix Round 1

### RED

- Added projection compatibility assertions to:
  - `scripts.test_run_snowmass_refined_translation.RefinedOrchestratorTests.test_final_style_uses_ordered_exact_id_batches`
  - Initial failure: `style_batch_projection.json` no longer exposed legacy top-level aggregate keys such as `eligible_chunks`.
- Added status-layout and exception-message regressions to:
  - `scripts.test_snowmass_style_batching.StyleExecutionTests.test_batch_status_groups_requests_by_stage_and_migrates_legacy_shape`
  - `scripts.test_snowmass_style_batching.StyleExecutionTests.test_unresolved_ambiguous_request_blocks_replay_before_second_client_call`
  - `scripts.test_snowmass_style_batching.StyleExecutionTests.test_recovery_exhaustion_error_mentions_stage_and_record_id`
  - Initial failures: `style_batch_status.json` still wrote schema v1 top-level `stage`/`requests`, and both runtime errors lacked `stage` + `record_id`.

### GREEN

- Restored legacy-compatible top-level projection aggregates while preserving nested `planned` / `actual`.
- Switched `style_batch_status.json` to a stage-keyed layout:
  - `{"schema_version": 2, "stages": {"anti_ai": {"requests": [...]}, "academic": {"requests": [...]}}}`
- Added legacy-file migration so old schema v1 `stage`/`requests` files are read into the new structure without touching Task 5 consumer code.
- Enriched the two required failure paths with `stage` and `record_id`.

### Verification

- Targeted:
  - `python3 -m unittest scripts.test_run_snowmass_refined_translation.RefinedOrchestratorTests.test_final_style_uses_ordered_exact_id_batches scripts.test_snowmass_style_batching.StyleBatchPlanningTests.test_stage_plan_projection_reports_exact_batches_and_worst_case_requests scripts.test_snowmass_style_batching.StyleExecutionTests.test_batch_status_groups_requests_by_stage_and_migrates_legacy_shape scripts.test_snowmass_style_batching.StyleExecutionTests.test_unresolved_ambiguous_request_blocks_replay_before_second_client_call scripts.test_snowmass_style_batching.StyleExecutionTests.test_recovery_exhaustion_error_mentions_stage_and_record_id`
- Full regression:
  - `python3 -m unittest scripts.test_snowmass_style_batching scripts.test_run_snowmass_refined_translation scripts.test_run_snowmass_translation scripts.test_snowmass_translation_qc scripts.test_snowmass_document_units`
- Syntax:
  - `python3 -m py_compile scripts/run_snowmass_refined_translation.py scripts/test_run_snowmass_refined_translation.py scripts/snowmass_style_batching.py scripts/test_snowmass_style_batching.py`

### Commit

- Lore intent line:
  - `Restore Task 4 compatibility surfaces without reopening the runner`

### Self-Review

- Kept Task 5 scope closed: the production consumer stayed read-only, and compatibility was restored entirely on the Task 4 producer side.
- Preserved the nested exact-id batching telemetry so later aggregator work still has the richer shape available.
- The new status schema writes v2, but reads old v1 files without destructive migration.
