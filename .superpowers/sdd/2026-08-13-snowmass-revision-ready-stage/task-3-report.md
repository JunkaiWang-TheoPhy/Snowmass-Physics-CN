# Task 3 Report

## Scope

- Updated `scripts/run_snowmass_batch_production.py` to recognize `revision_ready` as a terminal production stage.
- Updated `scripts/test_run_snowmass_batch_production.py` with TDD coverage for revision-ready preflight aggregation, cap gating order, and `_run_article` early return behavior.

## Changes

- Routed production preflight and launch projection by `through_stage`.
- Kept the existing style projection and style gate unchanged for `rendered`, `qc_passed`, and `packaged`.
- Added a revision-ready aggregation path that consumes `refined.revision_ready_projection(article_dir)`, sums conservative missing-stage call ceilings, and fails closed on diagnostics or projection errors.
- Delayed credential loading and client construction until after the revision-ready projection gate and request-cap gate.
- Passed `stop_after_revision=True` into `refined.run_refined_article(...)` when `through_stage == "revision_ready"` and returned immediately without style projection, refill, QC, render, or packaging.
- Prevented the packaged-resume path from being reused for `revision_ready`, so the stage no longer misclassifies packaged artifacts as a revision-ready terminal result.

## Simplifications

- Added a small `_projection_summary(...)` dispatcher instead of rewriting the existing style projection code.
- Reused the existing top-level `projected_worst_case_api_calls` gate so the request-cap enforcement remains structurally identical across projection modes.

## Verification

- `python3 -m unittest scripts.test_run_snowmass_batch_production` → 37 tests passed.
- `python3 -m unittest scripts.test_run_snowmass_refined_translation` → 52 tests passed.
- `python3 -m py_compile scripts/run_snowmass_batch_production.py scripts/test_run_snowmass_batch_production.py` → passed.
- `python3 scripts/run_snowmass_batch_production.py --help` → passed and listed `revision_ready` in `--through-stage`.

## Remaining Risks

- The direct non-`--help` CLI verification path was already covered by unit tests, but the interactive shell harness in this session did not return usable output for an ad hoc temporary-manifest invocation.
- `revision_ready` runs intentionally do not resume from previously packaged artifacts; they rerun the refined orchestrator and rely on its checkpoint reuse instead.
