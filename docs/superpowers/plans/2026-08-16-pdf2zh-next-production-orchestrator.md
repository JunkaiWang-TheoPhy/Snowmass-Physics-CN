# pdf2zh-next Production Orchestrator Plan

## Goal

Replace the paid custom parse/refill/render route with the pinned pdf2zh-next
runtime and promote it only through deterministic, resumable, rights-gated,
budget-bounded stages. The current custom route remains available only for
zero-paid diagnostics.

## Non-negotiable contracts

- Derive the eligible population from literal `publication_allowed: true` in
  `site/data/papers.json`; missing, false, null, and duplicate records fail
  closed.
- Use DeepSeek V4 Flash only. Do not install or invoke a local model.
- Require positive finite request, stage, and project caps. A zero cap is
  invalid, every stage is at most RMB 100, and the project is at most RMB 1000.
- Promote only through `deepseek_probe -> pilot5 -> pilot10 -> pilot25 ->
  batch50 -> remainder`, with a fresh complete seal for the previous stage.
- Preserve figure/plot/table interior text, bibliography entries, author names,
  collaboration names, numbers, formulas, citations, URLs, and locked terms.
- Keep raw render, protected render, audits, receipts, and sealed artifacts
  separate and hash-bound. Unchanged failures remain quarantined.
- Do not use either restricted SCNet node.

## Task 1: Make front matter and running-header protection generic

**Owned files:**

- `scripts/protect_snowmass_pdf2zh_output.py`
- `scripts/test_protect_snowmass_pdf2zh_output.py`

Add failing tests first, then implement:

1. Discover the source running header from recurring top-region blocks on
   source pages after page 1, with a minimum recurrence threshold and a
   deterministic tie-break.
2. Read translated candidates from the corresponding rectangles and choose one
   canonical normalized target by majority vote. Rewrite every detected header
   rectangle to that canonical value.
3. Discover page-1 author blocks between the title and affiliation/date area,
   including an immediately adjacent collaboration/topical-group line. Restore
   those blocks verbatim from the source while allowing affiliations to remain
   translated.
4. Add explicit auto-mode CLI flags without breaking the existing explicit
   header/repair interface. Fail closed on ambiguous discovery.
5. Bind discoveries, candidate counts, rectangles, source hashes, and output
   hash into the protection receipt.

Verification: focused unit tests plus a no-paid replay against
`arxiv:2203.07506`; the protected PDF must contain one canonical header and the
verbatim two-block author/group text.

## Task 2: Build a reusable per-paper seal pipeline

**Owned files:**

- new `scripts/seal_snowmass_pdf2zh_next_paper.py`
- new focused test file
- only minimal changes to existing audit helpers when required

Create one idempotent command that consumes a completed raw pdf2zh-next run and
performs generic protection, fresh official IR extraction, semantic audit,
structural audit, and page rendering/contact-sheet generation. Reuse
`snowmass_qc_contract.py` and `snowmass_production_contract.py` rather than
extending the weaker probe-only receipt joiner. Re-hash every live source, raw
translation, protected PDF, IR artifact, glossary, report, and environment lock
at seal time. Add a separate visual-review attestation bound to the protected
PDF and contact-sheet hashes; deterministic machine checks may prepare this
review but must not impersonate page-wide visual approval. Formal
`deepseek_probe` and `pilot5` require all-page coverage. Any failed, missing, or
stale gate must quarantine the paper and must not produce a passing seal.

Verification: replay the known full probe to the same passing protected-PDF
hash where deterministic inputs are identical; mutation tests must prove that
replacing any on-disk PDF, IR artifact, contact sheet, receipt, or environment
lock fails closed.

## Task 3: Implement the staged multi-paper orchestrator

**Owned files:**

- new `scripts/run_snowmass_pdf2zh_next_production.py`
- new focused test file
- reuse existing rights-selection and budget utilities without changing the
  legacy paid route

Implement `plan`, `launch`, `status`, `resume`, and `promote` operations. `plan`
must be zero-paid and write an immutable cohort plan, source/glossary hashes,
project-control path, launch projection, and explicit positive caps before any
DeepSeek client is constructed. `launch` must dispatch only planned papers,
avoid duplicates, use low bounded paper concurrency, and invoke the pinned A/B
runner plus Task 2 sealing. `resume` may reuse only hash-matching completed
artifacts. `promote` requires a fresh complete passing stage and the previous
stage seal. Failures enter fingerprinted quarantine.

Verification: unit tests for rights gating, disjoint deterministic cohorts,
zero-cap rejection, stage/project cap rejection, duplicate suppression,
resume/hash mismatch, quarantine, and promotion prerequisites.

## Task 4: Run the formal deepseek_probe zero-paid release rehearsal

Run the new `plan` and all local preflight checks for the deterministic formal
`deepseek_probe` cohort without creating a DeepSeek client. The previously
sealed `arxiv:2203.07506` full-paper run is engineering evidence only because it
is not the current deterministic cohort member; it must never be represented as
promotion evidence. Review the formal source PDF, cohort hash, projected maximum
requests/cost, runtime lock, glossary merge, and output paths. No paid request
is allowed in this task.

Verification: a signed/hashed local rehearsal receipt reports exactly one
eligible formal probe paper and a finite stage cap no greater than RMB 100.

## Task 5: Independent review, formal probe, then pilot5

Run fresh specification and code-quality reviews, fix all blockers, and execute
the complete focused and regression test suites. Only after those checks pass,
launch the formal `deepseek_probe` as a detached `codex-bg` job, immediately
verify its status and logs, and report the job name and budget ceiling. After it
finishes, require one fresh passing stage seal before generating the `pilot5`
zero-paid plan. Launch `pilot5` only after that plan and its independent review
pass. Paid launch is forbidden if any gate is stale, incomplete, ambiguous, or
failing.
