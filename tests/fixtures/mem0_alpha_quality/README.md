# Synthetic alpha quality evaluation

Run `python scripts/eval_mem0_alpha_quality.py --baseline-ref <commit> --output
report.json` with the test Python environment. Add `--check` to require every
candidate scenario to pass. `--source-dir /tmp/installed-wheel` accepts a directory
containing `llm_mem0`, so the same suite can verify a built wheel. Baseline and
candidate source-only snapshots run in different isolated children; no checkout,
live collection, credentials, `.env`, or network connection is used.

The corpus contains fictional facts only. `ratings` are independently declared
gold relevance grades (2 = directly answers, 1 = supporting). `vectors` describe
adversarial provider inputs, not actual embedding quality. SQLite FTS ranking and
the complete alpha fusion/filtering path are real. `index_owner` deliberately
models a corrupted/stale auxiliary index; `leak_vector_owner` models a provider
returning another owner's result. Neither changes expected ownership.

For ingestion, the deterministic backend recognizes `【記憶:id|text】` only inside
the extraction prompt's actual user block. This measures input transmission,
chunking, multi-turn retention, parsing, mutation and recall through public APIs.
It does not test whether an actual language model understands the conversation.
Conflict/reranking responses are scripted separately from expected results. An
unscripted model call is an evaluation error, not a successful abstention.

Each case gets a fresh temporary FTS/graph store, a fixed clock, and an explicitly
scoped fake memory provider. No real embedding/LLM/Chroma objects are constructed.
Audit guards reject network access, subprocesses, credential/dotenv reads, SQLite
outside the temporary workspace, and Python file writes outside that workspace.
These guards are defense-in-depth for trusted source, not an OS security sandbox.

Metrics are macro-averaged over answerable cases: Recall@k, Precision@k, MRR and
graded nDCG@k. Empty gold is excluded from those metrics, not awarded a perfect
score; absent answers are assessed by abstention precision/recall. Errors cannot
count as correct abstention. Wrong-owner, wrong-channel, stale/deleted and unknown
IDs are counted independently of text and ranking. Duplicate IDs do not improve
metrics and fail the case. Update cases also check provider state after injected
insert failure and retry. JSON includes per-category/per-case results, exact
fixture/source hashes, source-origin checks, provider call counts and synthetic
latency. Latency is diagnostic, not a production performance measurement.

Current scope does not establish real-provider accuracy, historical as-of query
support, cross-guild application authorization, cache isolation under concurrency,
long-term consolidation faithfulness, or production latency. Those require
separate integration or explicitly authorized real-provider evaluations. This is
not a LongMemEval benchmark run. Do not tune global relevance thresholds solely
to this small regression corpus; new defect reproductions should add fixtures.
The fake provider's state is in-memory: process-restart durability and real-store
transactions are not established by its failure/retry cases.
