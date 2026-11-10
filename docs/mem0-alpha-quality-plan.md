# Mem0 alpha quality improvement plan

Date: 2026-09-09 (JST). Baseline: `bd2808baa3d1e7700cb2591cc289abb4f6b240d6`,
vendored `mem0-alpha==0.3.0+botperf.1`.

## Scope and process

The user requested a whole-system audit, an implementation plan before edits,
and implementation of practical search-accuracy and memory-capability improvements.
This plan is recorded before implementation. Herdr's existing YOLO review pane
and independent read-only reviewers audited the ingestion, retrieval and lifecycle
paths. Synthetic reproductions do not access production memory or providers.

Change the controlled `vendor/mem0-alpha` snapshot and its bot integration. The
dirty upstream checkout `/home/ubuntu/mem0-work/Mem0-alpha` is read-only. Preserve
existing user data, collection names, authentication, models and shared-server
context policy. Memory ownership must describe the actual speaker; this does not
change which authorized conversations the bot can use as shared context.
No production restart, destructive reindex, automatic forgetting, public push or
embedding-model migration belongs to this implementation phase.

## Audit coverage

| Area | Modules / integration | Main risks and planned improvements |
| --- | --- | --- |
| Recording | `batch`, `bot_record` | Missing request-ID compatibility, destructive pop before success, mixed-speaker attribution, durable retry and idempotence. |
| Extraction | `extract`, `ingest`, `llm`, auth boundaries | Long-input truncation, ignored subsequent messages, malformed response fields, failure mistaken for successful empty extraction, explicit source provenance. |
| Knowledge updates | `dedup`, `conflict`, `attribute_registry`, `reinforce` | Similar-vector corrections dropped, archive before replacement commit, scope mismatch, duplicate/retry claims, lost reinforcement increments. |
| Retrieval | `search`, `bm25_index`, `morpho` | Metadata-free lexical hits resurrect archived facts, distance/RRF mismatch, discarded best duplicate distance, limit before filters, short terms / versions / dates, reranker empty result and duplicate indices. |
| Associations | `graph`, `priming`, core cache | User isolation, stale cache after mutations, irrelevant association promotion and configuration-sensitive cache keys. |
| Historical recall | `history_index`, `history_embed`, `replay` | Same-timestamp rows lost, replay cursor skips unprocessed/failed turns, stable pagination and ownership. |
| Consolidation | `decay`, `replay`, `fact_store` | Repeated reinforcement makes facts appear old, unsupported abstraction, summary insertion failure followed by archive, provenance/condition/scope preservation. |
| Durability and API | `sqlite_store`, `client`, `mcp_server`, settings, format | Missing state directories, stale configuration paths, get-all SDK implicit cap, deletion side-store cleanup, bounded tool parameters and useful diagnostic output. |
| Evaluation | Existing bot and upstream alpha tests | Current small scenario suite mostly mocks decisions; add real SQLite lexical retrieval and deterministic provider fixtures, plus alpha bot integration. |

## Implementation phases and acceptance criteria

### 1. Lossless and attributable ingestion (highest priority)

- Accept existing `request_id` calls; deduplicate redelivery and retain pending
  batches until explicit success. Concurrent append, retry, process restart and
  failed providers must not silently discard turns.
- Attach stable subject and source identifiers to new turns. A batch shared by a
  channel must not attribute one person's claims to the final caller. Legacy
  entries with unknown authors must not be guessed into another user's facts.
- Archive predecessors only after the corresponding replacement is confirmed
  stored. Partial failure must be reported and safely retryable. Release failed
  insert claims and scope deduplication appropriately.
- Exact duplicate suppression remains deterministic. Similarity alone must not
  suppress changes in quantities, negation, conditions or current values.
- Extract all supplied conversation turns and bounded chunks of long input;
  disclose limits/errors instead of acknowledging truncated content as complete.
  Isolate malformed facts/relations and retain valid independent items.
- Preserve source IDs/times and avoid treating assistant-generated suggestions
  as independently established user facts.

### 2. Accurate and inspectable retrieval

- Hydrate auxiliary-index candidates from the authoritative memory store and
  consistently enforce current validity and configured scope. Deleted/stale hits
  must not return even if an auxiliary index is stale.
- Preserve rank-fusion semantics separately from vector distance; retain the
  best evidence when queries produce duplicate IDs. Apply filters before final
  limits and bound oversampling/provider concurrency.
- Improve lexical treatment of short identifiers, versions, dates and Japanese
  terms without destroying the original stored text.
- Respect an explicit reranker rejection, reject malformed/duplicate selections,
  and avoid treating synthetic graph/core scores as strong retrieval evidence.
- Return provenance/retrieval evidence where useful without making untrusted
  memory text into instructions. Invalidate relevant cached core facts after
  updates and deletion.

### 3. Reliable replay, consolidation and public APIs

- Page history by a stable `(timestamp, row ID)` cursor; do not drop co-timestamp
  messages. Advance replay only through successfully handled input; isolate user
  cursors and reject or report ambiguous speaker ownership.
- Keep consolidation dry-run by default. Group only compatible active facts;
  retain originals if summary storage fails and preserve derivation metadata.
- Centralize deletion cleanup and cache invalidation. Make full enumeration
  explicit instead of relying on an SDK's default limit. Keep SDK compatibility
  handling narrow and tested.
- Improve filesystem initialization/configuration behavior without reconfiguring
  a running production client or silently changing a collection.
- Add narrowly scoped API/MCP parameters or diagnostics supported by regression
  tests; do not build unrelated infrastructure or change authentication flows.

### 4. Evaluation, packaging and handoff

- Add deterministic regression tests for every confirmed issue addressed.
- Add labeled synthetic retrieval cases and report Recall@k, MRR and nDCG,
  stale/deleted and wrong-owner leakage, plus update/retry behavior. Use actual
  temporary SQLite FTS and controlled fake vectors/LLM responses. Clearly state
  that these scores are not real-provider or public-benchmark accuracy.
- Evaluation dimensions include extraction, cross-turn recall, temporal order,
  knowledge updates and abstention, informed by the primary LongMemEval paper:
  <https://arxiv.org/abs/2410.10813>. This is not a LongMemEval benchmark run.
- Run standalone alpha regressions, alpha bridge tests, relevant existing tests,
  then the full bot suite. Build a regular wheel, verify its source checksums and
  import origin, and rerun the isolated quality suite against the wheel.
- Update package version, provenance and checksum manifest only after integration.
  Record exact results, remaining limitations and rollout/rollback requirements.

## Deliberately excluded unvalidated changes

Changing embedding models or global relevance thresholds needs real labeled
queries and a separate corpus migration experiment. Automatic destructive
forgetting, broad user-identity inference from names, and indiscriminate retrieval
across unrelated users are not enabled. Real-provider extraction accuracy and
production latency remain unmeasured until a separately authorized evaluation.
