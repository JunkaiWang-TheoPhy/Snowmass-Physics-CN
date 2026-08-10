# Task 6 Report

Date: 2026-08-10
Commit: `98d3f2f4fefdbbfef5d976867dc1451184f44b93`

## Scope

- Created `scripts/prepare_snowmass_production.py`
- Created `scripts/test_prepare_snowmass_production.py`
- Modified `scripts/snowmass_pipeline.py`
- Modified `scripts/test_snowmass_pipeline.py`
- Preserved unrelated untracked Snowmass scripts already present in the worktree
- Did not launch the live 273-paper preparation run

## RED Evidence

Initial Task 6 RED command:

```bash
python3 -m unittest scripts.test_prepare_snowmass_production -v
```

Initial RED result:

- `FAILED (failures=2)`
- Both failures were intentional and showed the missing implementation surface:
  - `AssertionError: Missing production preparer module: .../scripts/prepare_snowmass_production.py`

Follow-up integration RED for the discovered single-file TeX bug:

```bash
python3 -m unittest scripts.test_snowmass_pipeline.SourcePackageTests.test_safe_extract_renames_single_tex_payload_from_source_tar_gz_to_tex -v
```

Integration RED result:

- `FAILED (failures=1)`
- Exact mismatch:
  - extracted filename was `source.tar`
  - expected filename was `source.tex`

This proved the canonical `source.tar.gz` single-TeX payload could not flow through main-TeX ranking, and justified the narrow pipeline fix.

## GREEN Evidence

Focused production-prep suite:

```bash
python3 -m unittest scripts.test_prepare_snowmass_production -v
```

Result:

- `Ran 2 tests`
- `OK`

Focused integration recheck:

```bash
python3 -m unittest \
  scripts.test_snowmass_pipeline.SourcePackageTests.test_safe_extract_renames_single_tex_payload_from_source_tar_gz_to_tex \
  scripts.test_prepare_snowmass_production -v
```

Result:

- `Ran 3 tests`
- `OK`

Broader relevant regression suite:

```bash
python3 -m unittest scripts.test_snowmass_pipeline scripts.test_prepare_snowmass_production -v
```

Result:

- `Ran 33 tests`
- `OK`

Compile and diff hygiene:

```bash
python3 -m py_compile \
  scripts/snowmass_pipeline.py \
  scripts/prepare_snowmass_production.py \
  scripts/test_snowmass_pipeline.py \
  scripts/test_prepare_snowmass_production.py

git diff --check -- \
  scripts/snowmass_pipeline.py \
  scripts/prepare_snowmass_production.py \
  scripts/test_snowmass_pipeline.py \
  scripts/test_prepare_snowmass_production.py
```

Result:

- both commands exited successfully with no output

## Files Changed

- `scripts/prepare_snowmass_production.py`
- `scripts/test_prepare_snowmass_production.py`
- `scripts/snowmass_pipeline.py`
- `scripts/test_snowmass_pipeline.py`

## What Changed

- Added a dedicated production-preparation CLI that:
  - builds `rights_snapshot.json` from the rights manifest
  - loads only rights-eligible records from the source manifest
  - prepares records sequentially for durable interruption recovery
  - writes `preparation_record.json` under each prepared article directory
  - rewrites `preparation_report.json` after each completed record and on interruption
  - exits `2` when ambiguous or failed records remain and `130` on interruption
- Added a realistic end-to-end fixture covering:
  - tar source expansion
  - single-TeX gzip expansion
  - PDF-text fallback
  - blocked-record exclusion
  - per-paper failure continuation
  - rerun hash reuse
  - interruption-resume without rewriting validated artifacts
- Fixed single-file TeX extraction so TeX payloads stored as `source.tar.gz` are materialized as `.tex`, which keeps them discoverable by the existing main-TeX selection logic.

## Self-Review

- The new production script stays narrow and reuses the existing rights snapshot and chunk-prep helpers rather than adding a parallel implementation of source selection or chunking.
- Sequential orchestration is intentional here. It makes per-record durability and interruption semantics straightforward and testable, which matters more for this controller than throughput.
- The ambiguity signal is explicit data in the record reports: records with multiple document-bearing main-TeX candidates still prepare successfully, but the run remains non-green until they are reviewed.
- Resume behavior is grounded in artifact hashes and the existing `manifest_is_current()` logic from chunk preparation, and the automated interruption test exercises the reuse path directly.

## Concerns

- Ambiguity detection currently flags any archive with more than one document-marker candidate. That is conservative and appropriate for operator review, but later work may want a richer severity model if production inputs show benign duplicates.
- The production script is intentionally sequential. The controller can still launch it with `codex-bg`, but this task does not attempt concurrent preparation or performance tuning.
- No live 273-paper run was executed in this task, so final corpus-scale counts remain for the controller review step.

## Fix Round 1/5

Finding: a rerun wrote an empty `preparation_report.json` before processing began, so an interruption before the first record erased aggregate visibility even though durable per-record reports remained.

### RED Evidence

Command:

```bash
python3 -m unittest \
  scripts.test_prepare_snowmass_production.PrepareSnowmassProductionTests.test_rerun_interrupted_before_first_record_preserves_prior_aggregate_progress \
  -v
```

Result before the fix:

- `FAILED (failures=1)`
- Expected counts: `{'complete': 3, 'ambiguous': 1, 'failed': 1, 'reused': 0}`
- Actual counts after immediate rerun interruption: `{'complete': 0, 'ambiguous': 0, 'failed': 0, 'reused': 0}`

### GREEN Evidence

Focused regression after the fix:

```bash
python3 -m unittest \
  scripts.test_prepare_snowmass_production.PrepareSnowmassProductionTests.test_rerun_interrupted_before_first_record_preserves_prior_aggregate_progress \
  -v
```

Result: `Ran 1 test`, `OK`.

Full production-prep suite:

```bash
python3 -m unittest scripts.test_prepare_snowmass_production -v
```

Result: `Ran 3 tests`, `OK`.

Relevant pipeline regression suite:

```bash
python3 -m unittest scripts.test_snowmass_pipeline scripts.test_prepare_snowmass_production -v
```

Result: `Ran 34 tests`, `OK`.

Compile and diff hygiene:

```bash
python3 -m py_compile \
  scripts/snowmass_pipeline.py \
  scripts/prepare_snowmass_production.py \
  scripts/test_snowmass_pipeline.py \
  scripts/test_prepare_snowmass_production.py

git diff --check -- \
  scripts/prepare_snowmass_production.py \
  scripts/test_prepare_snowmass_production.py
```

Result: both commands exited successfully with no output.

### Implementation and Self-Review

- Aggregate state is seeded for the current rights-eligible source records from each durable `preparation_record.json`, with the prior aggregate entry as a fallback when the per-record file is missing or unreadable.
- Results are keyed by `record_id` and rendered in source-manifest order. Each processing step replaces its record instead of appending a duplicate.
- The regression exercises complete, ambiguous, and failed records, then interrupts the rerun before its first record and verifies that entries, counts, and processed count survive unchanged.
- Records no longer present in the current eligible source set are not carried into the new aggregate.

Concern: the fallback prior aggregate is trusted when its record ID matches. Normal processing replaces that entry; a permanently unreadable per-record report can remain visible during an interruption until its record is reached.

Commit: `ae711ee684848f4fe6ea54a20a6b715bc69fab06`

## Fix Round 2/5

Live finding: the production run reported `ValueError: substring not found` for `arxiv:2203.08033` and 13 other papers. In the reproduced input shape, math protection replaced `$^{#3}$` first, then the URL pattern matched across the adjacent sentinel. The URL replacement removed the math sentinel, and final source-order mapping failed when it could no longer find that sentinel.

The durable live report at `output/snowmass2021_translation/production/papers/arxiv_2203.08033/preparation_record.json` records the same `ValueError` failure.

### RED Evidence

Command:

```bash
python3 -m unittest \
  scripts.test_snowmass_pipeline.StructureProtectionTests.test_protect_structures_keeps_url_separate_from_immediately_following_math \
  scripts.test_snowmass_pipeline.StructureProtectionTests.test_protect_structures_keeps_math_separate_from_immediately_following_url \
  -v
```

Result before the fix:

- URL followed immediately by `$^{#3}$`: `ERROR`
- Exact failure: `ValueError: substring not found` from `protected.index(item[0])`
- Math followed immediately by URL: `ok`, retained as a symmetry regression
- Overall: `Ran 2 tests`, `FAILED (errors=1)`

### GREEN Evidence

The same focused command after the fix produced `Ran 2 tests`, `OK`.

Full relevant Snowmass suites:

```bash
python3 -m unittest \
  scripts.test_snowmass_pipeline \
  scripts.test_prepare_snowmass_production \
  scripts.test_run_snowmass_translation \
  scripts.test_snowmass_translation_qc \
  -v
```

Result: `Ran 74 tests`, `OK`. These unit and integration tests made no API calls.

Compile and diff hygiene:

```bash
python3 -m py_compile scripts/snowmass_pipeline.py scripts/test_snowmass_pipeline.py
git diff --check -- scripts/snowmass_pipeline.py scripts/test_snowmass_pipeline.py
```

Result: both commands exited successfully with no output.

### Implementation and Self-Review

- Every structure pattern now scans only unclaimed slices of the original source text, in the existing pattern order.
- Earlier patterns therefore retain precedence, while a later URL pattern can still protect the URL portion up to adjacent protected math instead of consuming a generated sentinel or dropping the whole URL match.
- Accepted source spans are replaced right-to-left, so replacements cannot invalidate the offsets of remaining spans.
- Mapping is still reordered by final sentinel position, and `validate_and_restore` retains exact count, order, unexpected-sentinel, and round-trip checks.
- The regression checks both mapping values in source order and exact restoration, so either sentinel loss or an overlap implementation that simply drops the URL remains detectable.

Concern: no live production rerun was launched in this fix round. The controller still needs to rerun preparation to verify all 14 affected papers and surface any unrelated corpus-specific failures.

Commit: `e28bcf6db7da61da0fc050384810a71b27bd3d62`

## Fix Round 3/5

Live finding: several real archives produced incomplete expanded sources or false ambiguity blocks because include parsing accepted only braced `input`/`include`, main ranking ignored outgoing source structure and path depth, and production inspection treated every secondary document candidate as ambiguous.

### RED Evidence

The initial focused command covered include-rich selection, root duplicate selection, unbraced commands, `subfile`, traversal, comments, and production ambiguity/reporting:

```bash
python3 -m unittest \
  scripts.test_snowmass_pipeline.MainTexSelectionTests.test_rank_main_tex_prefers_include_rich_whole_paper_over_short_standalone \
  scripts.test_snowmass_pipeline.MainTexSelectionTests.test_rank_main_tex_prefers_root_main_over_nested_duplicate \
  scripts.test_snowmass_pipeline.ExpandTexTests.test_expand_tex_supports_unbraced_input_and_include \
  scripts.test_snowmass_pipeline.ExpandTexTests.test_expand_tex_resolves_main_prefixed_subfiles_from_root \
  scripts.test_snowmass_pipeline.ExpandTexTests.test_expand_tex_rejects_main_prefixed_subfile_traversal \
  scripts.test_snowmass_pipeline.ExpandTexTests.test_expand_tex_ignores_commented_unbraced_inputs \
  scripts.test_prepare_snowmass_production.PrepareSnowmassProductionTests.test_main_prepares_allowed_records_writes_reports_and_reuses_valid_artifacts \
  -v
```

Result before implementation: `Ran 7 tests`, `FAILED (failures=7)`.

Observed failures matched the production defects:

- the short standalone candidate ranked above the include-rich whole paper
- the nested `archive/copy/main.tex` sorted above the root duplicate
- unbraced `input`/`include` and `subfile` bodies were absent
- `\main/../escape` was not parsed and therefore did not raise
- the production fixture reported 2 ambiguities instead of 1 because a lower-scored alternative was treated as blocking

A subsequent corpus probe showed nested master-relative unbraced paths resolving to duplicated directories. A dedicated regression was added:

```bash
python3 -m unittest \
  scripts.test_snowmass_pipeline.ExpandTexTests.test_expand_tex_falls_back_to_root_for_nested_master_relative_input \
  -v
```

Result before the fallback fix: `FAILED (failures=1)` because `detail body` was absent.

### GREEN Evidence

Focused parser/ranker tests passed after implementation, including root-relative fallback and production reporting.

Full relevant Snowmass suites:

```bash
python3 -m unittest \
  scripts.test_snowmass_pipeline \
  scripts.test_prepare_snowmass_production \
  scripts.test_run_snowmass_translation \
  scripts.test_snowmass_translation_qc \
  -v
```

Result: `Ran 81 tests`, `OK`. No API calls were made.

Compile and diff hygiene:

```bash
python3 -m py_compile \
  scripts/snowmass_pipeline.py \
  scripts/test_snowmass_pipeline.py \
  scripts/prepare_snowmass_production.py \
  scripts/test_prepare_snowmass_production.py

git diff --check -- \
  scripts/snowmass_pipeline.py \
  scripts/test_snowmass_pipeline.py \
  scripts/prepare_snowmass_production.py \
  scripts/test_prepare_snowmass_production.py
```

Result: both commands exited successfully with no output.

Temporary extraction validation against the five cited local archives, without writing production outputs:

- `2207.07641`: selected `main.tex`; 27 outgoing targets; 17,522 expanded words; 28 includes; 0 unresolved
- `2203.07545`: selected `nuSTORM-SnwMss22-WP.tex`; 12,525 expanded words; 31 includes; 0 unresolved
- `2201.07805`: selected `report.tex`; 47,894 expanded words; 9 includes; 0 unresolved
- `2203.07261`: selected `report.tex`; 35,711 expanded words; 6 includes; 0 unresolved
- `2205.12847`: selected `WhitePaper-SKBePol.tex`; 19 outgoing targets; 27,974 expanded words; 20 includes; 5 unresolved source files

All five inspections produced a unique top selection and no ambiguity reason.

### Implementation and Self-Review

- One command-aware tokenizer now handles braced and unbraced `input`/`include` plus `subfile`, while retaining line-comment offsets.
- Literal leading `\main/` subfile paths resolve from the extraction root. Nested master-relative source paths try the current file directory first and the extraction root second; explicit dot-prefixed relative paths retain current-directory semantics.
- Existing traversal checks, cycle detection, incoming penalties, and backup filename exclusion remain active.
- Main ranking now records and rewards distinct outgoing source targets and penalizes deeper paths, allowing an include-rich whole-paper entry and a root main to beat shorter or nested alternatives deterministically.
- Candidate reports use extraction-relative paths and include score, incoming/outgoing counts, depth, and content hash.
- Preparation records now include selected main path/score plus unresolved include and cycle counts.
- Ambiguity requires equal top scores with distinct content hashes. The fixture preserves an equal-score, equal-depth, distinct-content ambiguity while proving a lower-scored second document is non-blocking.

Concern: outgoing and depth score weights are deterministic heuristics. The five known failures now select correctly, but the controller should review selected-main audit fields and unresolved counts after the next live run. `2205.12847` references five files absent from its archive; those remain correctly visible as unresolved rather than silently fabricated.

No live production rerun was launched in this round.

Commit: `012eaff80a00adb6e801f2f98641a9e7abd9dc43`

## Fix Round 4/5

Remaining high finding: `inspect_source_package()` returned immediately for `single_tex`, so supported single-file TeX gzip inputs never ran the same extraction, main-TeX ranking, or include-expansion audit used for tar archives. Their preparation records therefore kept `selected_main_path`, `selected_main_score`, `unresolved_include_count`, and `include_cycle_count` as `null`.

### RED Evidence

Command:

```bash
python3 -m unittest \
  scripts.test_prepare_snowmass_production.PrepareSnowmassProductionTests.test_main_records_single_tex_selection_and_include_audit \
  -v
```

Result before the fix:

- `FAILED (failures=1)`
- Exact mismatch:
  - `selected_main_path` was `None`
  - expected `source.tex`

The fixture used a supported single-file TeX gzip with `\input{source}` and `\input{missing}`, so the failure isolated the report-inspection path rather than source extraction or chunk preparation.

### GREEN Evidence

Focused regression after the fix:

```bash
python3 -m unittest \
  scripts.test_prepare_snowmass_production.PrepareSnowmassProductionTests.test_main_records_single_tex_selection_and_include_audit \
  -v
```

Result: `Ran 1 test`, `OK`.

Owned-file suite:

```bash
python3 -m unittest scripts.test_prepare_snowmass_production -v
```

Result: `Ran 4 tests`, `OK`.

Relevant regression suite:

```bash
python3 -m unittest scripts.test_snowmass_pipeline scripts.test_prepare_snowmass_production -v
```

Result: `Ran 44 tests`, `OK`. No live run or API call was made.

Compile and diff hygiene:

```bash
python3 -m py_compile \
  scripts/prepare_snowmass_production.py \
  scripts/test_prepare_snowmass_production.py

git diff --check -- \
  scripts/prepare_snowmass_production.py \
  scripts/test_prepare_snowmass_production.py \
  .superpowers/sdd/2026-08-10-snowmass-production-translation/task-6-report.md
```

Result: both commands exited successfully with no output.

### Implementation and Self-Review

- Added a focused production-prep regression that exercises a supported single-file TeX gzip end to end and asserts the resulting preparation record carries:
  - `selected_main_path`
  - `selected_main_score`
  - integer unresolved include and cycle counts
- The fixture includes one self-include and one missing include, proving the `expand_tex()` audit actually ran for `single_tex` instead of merely filling defaults.
- The implementation is a one-line behavior change: after package-type detection, `inspect_source_package()` now lets `single_tex` flow through the existing safe extraction, ranking, ambiguity, and expansion logic already used for tar archives.
- Tar behavior remains unchanged because the extraction and inspection path was already shared; only the premature early return was removed.

Concern: this round validates the preparation-record inspection path only. It does not rerun the live production corpus, by request.
