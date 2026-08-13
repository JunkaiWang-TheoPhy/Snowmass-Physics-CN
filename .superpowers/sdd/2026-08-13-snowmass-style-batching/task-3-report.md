# Task 3 Report

## RED

- Added `StyleExecutionTests` for:
  - mixed per-chunk QC in one batch: durable good sibling commit, failed-ID-only recovery, one batch ledger entry per request
  - protocol-wide exact-ID failure: no outputs committed from that request before whole-request recovery
  - preflight request-cap rejection before the first client call
  - ambiguous transport: one `commit_estimate()` and persisted uncertain batch state without auto-replay
- Verified initial RED with:
  - `python3 -m unittest scripts.test_snowmass_style_batching.StyleExecutionTests`
  - failure: `AttributeError: module 'snowmass_style_batching' has no attribute 'execute_style_stage'`

## GREEN

- Implemented `execute_style_stage()` and `StyleStageResult` in `scripts/snowmass_style_batching.py`.
- Added batch-level durable status file `style_batch_status.json` to store per-request usage/cost once.
- Reserved and settled budget exactly once per batch request; ambiguous transport commits the estimate exactly once and persists uncertain state.
- Protocol failures mark the whole request failed and recover only through one bounded recovery pass.
- Mixed QC responses durably commit successful siblings before retrying only failed chunk IDs.
- Rejected candidates are persisted for failed items via `runner.persist_rejected_candidate()`.
- Preflight request-cap rejection uses `budget_guard.snapshot()` and fails before the first client call when remaining calls are below `plan.worst_case_requests`.

## Verification

- `python3 -m unittest scripts.test_snowmass_style_batching.StyleExecutionTests`
- `python3 -m unittest scripts.test_snowmass_style_batching scripts.test_snowmass_batch_budget`
- `python3 -m py_compile scripts/snowmass_style_batching.py scripts/test_snowmass_style_batching.py`

## Commit

- `Keep style-batch billing atomic across sibling recovery`

## Self-Review

- Kept Task 1/2 planning and preparation interfaces intact.
- Consumed `restoration_data` only from the passed `StyleStagePlan`.
- Avoided copying batch usage/cost into per-chunk stage status; usage is recorded once per request in batch status and cost ledger.
- Ensured malformed exact-ID responses do not commit any sibling outputs from that request.
- Preserved successful sibling outputs before the single recovery pass.

## Concern

- `execute_style_stage()` currently raises after the single recovery pass on remaining failed IDs; callers need to treat that as the stage barrier and not attempt implicit extra retries.

---

## Round 1/5

### RED

- Added failing tests:
  - `test_protocol_failure_commits_nothing_before_retrying_the_whole_failed_request`
  - `test_failed_transport_commits_estimate_once_marks_batch_failed_and_clears_running_chunks`
  - `test_reserve_failure_does_not_commit_missing_reservation`
  - `test_unresolved_ambiguous_request_blocks_replay_before_second_client_call`
- RED command:
  - `python3 -m unittest scripts.test_snowmass_style_batching.StyleExecutionTests`
- RED output:
  - `FAILED (failures=3)`
  - protocol-failure attempt history collapsed to one record
  - failed transport left batch status at `running`
  - unresolved ambiguous replay still reached `client.complete()`

### GREEN

- Batch request persistence now keys updates by `attempt_id` instead of logical `request_key`.
- Added immutable-attempt metadata: `attempt_id` and `attempt_ordinal`; logical `request_key` remains stable across retries for audit/idempotency.
- Added unresolved-uncertain replay barrier before the first reserve/client call for the same logical request.
- Added failed-transport handling that:
  - commits the conservative estimate once only when a reservation exists
  - appends one `style_batch_failed_transport_reservation` ledger event
  - marks the batch attempt `failed`
  - marks all request-owned chunk stages `failed`
  - re-raises the original exception

### Verification

- Test names:
  - `StyleExecutionTests`
  - `test_protocol_failure_commits_nothing_before_retrying_the_whole_failed_request`
  - `test_failed_transport_commits_estimate_once_marks_batch_failed_and_clears_running_chunks`
  - `test_reserve_failure_does_not_commit_missing_reservation`
  - `test_unresolved_ambiguous_request_blocks_replay_before_second_client_call`
- Commands:
  - `python3 -m unittest scripts.test_snowmass_style_batching.StyleExecutionTests`
  - `python3 -m unittest scripts.test_snowmass_style_batching scripts.test_snowmass_batch_budget`
  - `python3 -m py_compile scripts/snowmass_style_batching.py scripts/test_snowmass_style_batching.py`
- Outputs:
  - `Ran 7 tests in 0.097s`
  - `OK`
  - `Ran 41 tests in 0.381s`
  - `OK`
  - `py_compile` exited cleanly with no output

### Commit

- `Preserve batch attempt history and fail closed on uncertain replays`

### Self-Review

- The logical request key still drives uncertainty identity and audit grouping.
- Recovery attempts no longer overwrite the original batch record.
- Reserve failures do not fabricate a conservative charge.

### Concern

- Replay blocking is implemented only as fail-closed; explicit authorized uncertain replay is still deferred to the later integration that wires in existing `retry_uncertain` semantics.
