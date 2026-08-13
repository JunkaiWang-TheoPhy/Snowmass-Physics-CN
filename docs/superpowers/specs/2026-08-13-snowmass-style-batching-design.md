# Snowmass Style-Pass Batching Design

## Objective

Reduce DeepSeek request count for the mandatory English-to-Chinese workflow without merging or removing its ordered stages:

1. faithful translation;
2. terminology unification;
3. removal of AI mannerisms;
4. Chinese naturalization and academic polishing.

The current bottleneck is stages 3 and 4. They call the model once per body chunk even when many chunks are short and already structurally valid. For large papers this multiplies request count by roughly twice the number of body chunks.

## Chosen approach

Batch multiple independent chunks into one model request for each style stage. Keep the anti-AI and academic stages separate and causally ordered. A batch uses a JSON slot protocol keyed by immutable chunk IDs. The response must contain exactly the requested IDs; each returned value is validated against its own source, glossary, protected structures, numbers, units, citations, URLs, and stage input.

Normal batches contain at most 24 chunks and at most 18,000 protected-input characters. A batch closes when either limit would be exceeded. Oversized chunks become single-chunk batches. The response allowance follows the existing bounded policy: at least 4,096 tokens and at most 20,000 tokens, scaled from protected-input length. Reference text, figure text, table text, fragile nonlinguistic fragments, hard exact translations, and other passthrough chunks never enter a paid style batch.

## Data flow

For each paper and style stage:

1. Read the verified prior-stage artifact for every chunk.
2. Reuse any checkpoint whose request identity, source hash, prior-stage hash, policy, and output hash remain valid.
3. Resolve deterministic no-op and passthrough chunks locally.
4. Pack only unresolved model-required chunks into ordered batches.
5. Submit one structured request per batch under the persistent RMB and API-call guards.
6. Parse the response fail-closed: exact batch ID set, one nonblank value per ID, no unknown IDs, and no missing IDs.
7. Restore protected structures and run per-chunk QC.
8. Commit successful chunk outputs independently and atomically.
9. Quarantine failed chunks and retry only those chunks once, in smaller recovery batches of at most 8 chunks.
10. If a recovery batch still fails, quarantine the affected chunks and stop the paper at the barrier without invalidating successful siblings.

The next style stage starts only after every chunk in the preceding stage has a valid checkpoint.

## Request identity and resumability

Each batch request identity includes:

- model and provider;
- stage and QC contract version;
- ordered chunk IDs;
- source hashes and prior-stage output hashes;
- selected glossary rules;
- protected batch payload;
- paper-context identity;
- maximum output tokens;
- batch protocol version.

Each chunk checkpoint records the enclosing batch request identity and its own output hash. On restart, valid chunk checkpoints are reused even if another chunk from the same historical batch failed. A changed batch composition does not invalidate a chunk whose complete per-chunk identity is unchanged.

No in-memory-only queue state is authoritative. All successful outputs and status transitions are written atomically before the next batch is submitted.

## Cost and request controls

The batch runner retains both existing hard gates:

- finite positive RMB budget, with the cross-paper project ceiling no greater than ¥1000;
- finite positive stage API-call cap, where zero is invalid rather than unlimited.

Before every request, one reservation is made for both estimated cost and one API call. Concurrent workers share the same locked ledger. Batch retries consume new reservations and cannot exceed either cap.

The production snapshot must report:

- unresolved chunks per style stage;
- planned normal batches;
- worst-case recovery batches;
- maximum possible requests before launch;
- actual requests, tokens, and RMB after launch.

Production must refuse to start when the projected worst-case request count exceeds the declared stage API-call cap.

## Quality and safety invariants

- Anti-AI and academic polishing remain separate passes.
- Batch context is read-only; text from one chunk must never appear in another chunk.
- Per-chunk numbers, units, equations, citations, URLs, names, glossary decisions, and protected structures remain exact.
- References and text inside figures, plots, vector graphics, and tables remain source-language passthrough where required by existing contracts.
- A malformed or incomplete batch response cannot partially overwrite unverified chunks.
- Successful siblings are durable before failed siblings are retried.
- Automatic recovery is limited to one retry pass.
- The 273-record publication-rights gate remains fail-closed and is evaluated from the live manifest.

## Failure handling

Transport ambiguity is recorded against the batch request identity and conservatively charged. It is not replayed automatically unless the existing uncertain-request policy authorizes replay.

Protocol failures split the affected normal batch into recovery batches of at most 8 chunks. Per-chunk QC failures retry only the failed chunk IDs. A second failure is terminal for those chunks and leaves the paper quarantined with a machine-readable reason.

Systemic failures continue to use the cross-paper circuit breaker. One isolated paper failure does not cancel unrelated papers, but repeated identical content failures must stop further paid submissions.

## Verification

Tests must prove:

- 25 eligible chunks plan as two normal batches, not 25 requests;
- anti-AI completes before academic batching begins;
- passthrough and already-valid chunks consume no batch slots or API calls;
- mixed batch success commits good chunks and retries only failed IDs;
- missing, duplicate, or unknown response IDs fail closed;
- recovery batches never exceed 8 chunks and occur at most once;
- restart reuses successful siblings from a partially failed historical batch;
- concurrent request reservations cannot exceed the finite cap;
- projected worst-case requests above the cap prevent launch;
- existing numeric, unit, glossary, reference, figure, table, and rights-gate regressions remain green.

Before a paid pilot resumes, a zero-API projection must demonstrate the exact number of pending anti-AI and academic batches for the four incomplete pilot papers.

## Non-goals

- Combining anti-AI and academic polishing into one model pass.
- Replacing deterministic per-chunk QC with model self-evaluation.
- Reprocessing completed and verified translations solely to adopt batching.
- Increasing article concurrency before the batching and recovery gates pass the pilot.
