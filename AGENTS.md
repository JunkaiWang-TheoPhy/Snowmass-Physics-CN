# Project Agent Notes

## User Workflow Defaults

- This is a user-owned repository. Work directly on `main` by default.
- If completed, verified work is already on a topic branch or worktree, automatically merge it locally into `main`. Do not ask the user to choose among local merge, push/PR, or keeping the branch; do not request a numeric reply; and do not stop at `READY TO MERGE`.
- A clean local merge into `main` is pre-authorized. Push, PR creation, history rewriting, destructive cleanup, and any resolution that would discard unrelated work require their own justification or explicit authority.
- These rules override workflow-skill steps that would otherwise ask how to finish a development branch.
- When a visual proposal, layout comparison, chart mockup, browser preview, or rendered-page review would materially improve a design decision, run it automatically. Do not ask for permission to open or generate the visualization first; proceed directly and present the result for review.

## DeepSeek provider

This machine has a user-level Codex provider named `deepseek`.

- Model: `deepseek-v4-flash`
- Base URL: `https://api.deepseek.com/`
- Protocol: OpenAI-compatible Responses API
- Authentication: read by Codex from the macOS Keychain entry with account `codex_0805` and service `codex.deepseek.api`
- Native worker: `deepseek_flash_worker`

To start an interactive or non-interactive Codex run on this provider, select it explicitly with `-m deepseek-v4-flash -c 'model_provider="deepseek"'`. The normal OpenAI Codex default is intentionally unchanged.

Codex may warn that the provider model catalog is unavailable because DeepSeek's `/models` response uses `data`; this does not prevent explicit `deepseek-v4-flash` runs or tool calls.

Do not put the DeepSeek API key in this repository, `AGENTS.md`, prompts, logs, or generated artifacts. Do not print or expose the key in command output. Codex's user-level config owns provider and authentication settings; this file only records the machine-local contract for agents working in this repository.

When delegating a bounded, read-only batch task, use the `deepseek_flash_worker` role only when the requested input is text-only and the result can be independently checked. If the provider or model is unavailable, report that explicitly instead of silently switching providers.

## User-mandated SCNet node restriction

The following is a hard operational requirement and applies to all future tasks in this repository:

- Do **not** use, SSH into, probe, submit to, or run any workload on `cancon.hpccube.com:65023` (Kunshan, account `scnfhax1m3`) or `dzeshell.hpccube.com:65032` (Sichuan, account `ack7pxlulo`). This includes read-only health checks, `sbatch --test-only`, API runners, translation jobs, and GPU jobs.
- The only possible exception is when the authorized `giggleliu` queue is demonstrably full or unavailable for the required work. The evidence must come from an already authorized/allowed source; do not contact either restricted node merely to determine whether the queue is full.
- Before using either restricted node, ask the user explicitly and state the proposed resource type/count, maximum wall time, expected maximum API cost if applicable, and a conservative upper budget in RMB. Do not connect or submit until the user has expressly approved that budget cap.
- Do not bypass this rule through SSH aliases, alternate account names, copied keys, or another scheduler endpoint that resolves to either restricted host.

## Default English-to-Chinese translation workflow

For English-to-Chinese work in this project, use this ordered default pipeline:

1. Translate the source faithfully.
2. Unify terminology against the locked glossary.
3. Remove AI mannerisms and formulaic phrasing.
4. Naturalize and academicize the Chinese.

The last two passes must not add facts or alter meaning, numbers, units, formulas, citations, links, names, or glossary decisions. Preserve source, draft, terminology, and final-stage artifacts separately so each pass remains auditable and resumable.

## Snowmass first-wave rights gate

The first production translation wave is restricted to records whose generated public rights manifest has `publication_allowed: true`. Treat missing records, missing fields, `false`, and `null` as blocked. Do not provide an all-records bypass in the translation runner. The current manifest yields 273 eligible records, but code must derive the live count from `site/data/papers.json` rather than hard-code it. Downloaded or preprocessed files for blocked records may remain stored locally, but they must not enter the translation work queue or published outputs.

## Snowmass Chinese-edition cover contract

Every published Chinese PDF must prepend one independent Chinese-edition information page without modifying or replacing the original paper's first page. Use `scripts/package_snowmass_translation_pdf.py` after BabelDOC rendering and preserve the raw rendered PDF separately.

The information page must use the project mountain artwork and include the collaboration name `Snowmass White Paper Chinese Translation Collaboration`, Chinese and English titles, original authors, separate arXiv/DOI/original-source links, `中文翻译贡献者：WangTheoPhys*`, `Website (Temporary): snowmass-physics-cn.netlify.app`, a QR code targeting `https://snowmass-physics-cn.netlify.app/`, translation version/date, the non-author-approved translation disclaimer, source license/attribution conditions, and `*Contact: WangTheoPhys@outlook.com`. Missing DOI values must be shown as unavailable rather than inferred. Packaging must fail closed outside the publication rights gate, avoid absolute local paths in receipts, and remain byte-reproducible for identical inputs.
