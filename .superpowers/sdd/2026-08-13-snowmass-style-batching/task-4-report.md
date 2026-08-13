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
