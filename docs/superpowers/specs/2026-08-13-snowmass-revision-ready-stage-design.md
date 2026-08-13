# Snowmass `revision_ready` Stage Design

## Goal

Allow an eligible, prepared paper to advance safely and resumably through analysis, draft translation, terminology, critique, and revision, then stop before the batched anti-AI and academic stages. This closes the current bootstrap deadlock: exact style-request projection requires revision checkpoints, while the full production runner currently refuses paid work when those checkpoints do not yet exist.

## Selected design

Add `revision_ready` as an explicit refined-runner stop point and as a production terminal stage. The refined runner reuses all valid checkpoints and executes the existing causal sequence through `revision_merge`; it records `paper_status.status = "revision_ready"` and returns without planning or invoking either style stage.

The production runner treats `revision_ready` as a paid stage with the same rights gate, persistent RMB budget, finite positive API-request cap, run locks, uncertain-request protection, and deterministic snapshot identity used by other production stages. It must not run the style projection gate before this stage because style projection is its output prerequisite, not its launch prerequisite.

## Request and cost control

The stage uses the existing `PersistentBudgetGuard`. Every model call—paper analysis, missing translate/terminology calls, critique shards or repair, and revision calls—must reserve one request before transport and persist actual usage afterward. Existing completed checkpoints consume zero new requests. A finite positive `stage_max_api_calls` remains mandatory; `0` never means unlimited.

Preflight for `revision_ready` reports checkpoint-derived counts:

- completed and missing chunks for translate, terminology, and revision;
- completed/missing paper analysis and critique phases;
- a conservative request ceiling equal to all currently missing per-chunk model stages plus conservative paper-level/sharded critique allowance;
- `projection_ready` only when the ceiling can be computed from a valid manifest and checkpoint identity.

The launch gate compares this conservative ceiling with the remaining request cap before credentials or a client are created. Cost remains additionally bounded by the explicit stage RMB cap.

## Causal boundaries

The sequence remains strict:

1. analysis and deterministic prompt;
2. complete translate/terminology barrier;
3. deterministic draft merge;
4. complete critique/sharded-critique barrier;
5. complete revision barrier;
6. deterministic revision merge;
7. persist `revision_ready` and stop.

No anti-AI, academic, refill, BabelDOC render, QC, packaging, or publication action is permitted in this stage.

## Resume and failure behavior

Checkpoint hashes and record identity remain authoritative. Completed work is reused only when its output hash and input hash validate. `running` or `uncertain` paid requests fail closed unless the existing explicit uncertain-replay contract is satisfied. A budget or request-cap exhaustion leaves durable checkpoints and a structured nonzero exit status; rerunning the identical snapshot resumes rather than restarts.

## Production rollout

Use `revision_ready` first on the three incomplete pilot papers. After all three are ready, run a four-paper zero-API style projection. Only then launch the existing translated/rendered/QC/packaged pipeline, beginning with one paper, followed by four papers, ten papers, and finally the 273-record rights-gated campaign.

## Verification

Tests must prove:

- the refined runner stops after `revision_merge` and never invokes style stages;
- `revision_ready` preflight does not require revision checkpoints to exist;
- launch is refused before credential/client creation when its conservative request ceiling exceeds the cap;
- valid completed checkpoints reduce the ceiling;
- direct-script CLI invocation works;
- restart reuses checkpoints and preserves uncertainty safeguards;
- the existing full workflow remains unchanged for later terminal stages.
