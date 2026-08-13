## Status

Complete on August 13, 2026.

## Changes Made

- Added `revision_ready_projection(article_dir)` to compute a conservative pre-style request ceiling for `analysis`, `translate`, `terminology`, `critique`, and `revision`.
- Reused valid local checkpoints only when record identity and checkpoint hashes still validate, and fail-closed on `running`/`uncertain` pre-style phases.
- Counted sharded critique conservatively, including repair allowance when critique input is not yet fully materialized.
- Added focused tests for fresh, partial, complete, identity-mismatch, and uncertain-phase projection states.

## Verification

- `python3 -m unittest scripts.test_run_snowmass_refined_translation.RefinedOrchestratorTests.test_revision_ready_projection_counts_fresh_manifest_conservatively scripts.test_run_snowmass_refined_translation.RefinedOrchestratorTests.test_revision_ready_projection_reuses_valid_translate_checkpoint_only scripts.test_run_snowmass_refined_translation.RefinedOrchestratorTests.test_revision_ready_projection_returns_zero_after_complete_revision_ready_state scripts.test_run_snowmass_refined_translation.RefinedOrchestratorTests.test_revision_ready_projection_fails_closed_on_record_identity_mismatch scripts.test_run_snowmass_refined_translation.RefinedOrchestratorTests.test_revision_ready_projection_fails_closed_on_uncertain_chunk_phase`
  - Passed: 5 tests
- `python3 -m unittest scripts.test_run_snowmass_refined_translation`
  - Passed: 50 tests
- `python3 -m py_compile scripts/run_snowmass_refined_translation.py scripts/test_run_snowmass_refined_translation.py`
  - Passed
- `git diff --check`
  - Passed

## Concerns

- `revision_ready_projection()` currently returns a structured fail-closed report for identity mismatches and uncertain in-flight phases, but malformed JSON in local checkpoint files still raises instead of being downgraded into the report payload. The current production caller can catch that, but it remains a sharper edge than the other fail-closed paths.
- The critique ceiling is intentionally conservative when terminology outputs are not fully materialized; it may overestimate request count for some small papers, but it should not undercount the paid pre-style work.
