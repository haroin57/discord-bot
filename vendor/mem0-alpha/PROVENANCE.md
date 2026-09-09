# Mem0 alpha runtime snapshot

This source-only snapshot provides the optional `MEM0_ALPHA_ENABLED` parity
lane without relying on an operator's editable development installation.

- Upstream repository: https://github.com/haroin57/Mem0-alpha
- Upstream checkout base: `887297d44ed239842cbb4a0f971de9a17f2f8efe`.
- Captured version: `0.3.0` on 2026-09-09 (JST).
- The base commit identifies version 0.1.0. This snapshot deliberately includes
  the already-running local 0.3.0 source changes and new runtime modules; it
  must not be represented as pristine upstream commit contents.
- Included files: `pyproject.toml`, `README.md`, MIT `LICENSE`, and all 31
  regular Python/package-data files under `llm_mem0/`.
- Excluded files: Git metadata, tests, caches, bytecode, environment files,
  authentication credentials, and application databases/history.
- `SNAPSHOT.sha256` pins every included upstream file by relative path. Verify
  with `sha256sum --check SNAPSHOT.sha256` from this directory.

The initial snapshot was byte-for-byte equal to the captured source. Local
performance patches on 2026-09-09 change `llm_mem0/search.py`, `llm_mem0/morpho.py`, and the package
version to `0.3.0+botperf.1`: off-loop morphological analysis, single-flight
bounded core-memory caching, cancellation ownership, and bounded concurrent
graph sibling reads. Shared Janome tokenization is serialized to protect its
mutable matcher caches. Candidate selection, scoring, scope and archive checks
are preserved. `SNAPSHOT.sha256` describes the current patched files.
The additional files are this provenance, the checksum manifest, and a local
build-artifact `.gitignore`.
The original upstream working directory is not modified by packaging.

The quality patch set dated 2026-09-09 is version `0.3.1+botquality.1`,
based on the bot repository's `bd2808baa3d1e7700cb2591cc289abb4f6b240d6`
snapshot (`0.3.0+botperf.1`). It adds durable subject-partitioned ingestion,
retry receipts and cooperative POSIX writer locks, grounded/chunked extraction,
conservative replacement and lineage handling, hydrated hybrid retrieval,
exact-term tokenization, explicit historical queries, source-aware replay,
transactional index rebuilding and cache revision propagation. The patch also
changes bot recording/session/MCP integration outside this package.
`../../docs/mem0-alpha-quality-plan.md` and
`../../docs/mem0-alpha-quality-results.md` record the audit, tests and limitations.
Existing databases are not reindexed or moved by packaging. Linux/POSIX flock
is now required for the shared-state durability path. The checksum manifest
continues to describe the complete current package sources, not the upstream
base or a production deployment.

Both requirements files use `./vendor/mem0-alpha`. Run pip from the repository
root, and build immutable releases from their archived `src` directory so the
dependency cannot resolve to mutable operator files. Pip builds and installs
a regular wheel; runtime import must resolve inside the release virtual
environment, never through an editable `.pth` link.

The second quality patch set is `0.3.2+botquality.2`, based on bot commit
`b42b908ce681b21ee02e37a56620c5780e042fc0` (`0.3.1+botquality.1`). It adds bounded
idle batch recovery, durable extraction continuations and confirmation events,
source revision replay, generation-fenced checkpoints, derived-support repair,
bounded retrieval refill, time-bound core caching, and common MCP/helper
contracts. The new package modules are `api_contracts.py`, `ingest_journal.py`,
and `lineage.py`; the current package contains 34 regular Python/package-data
files. Source changes and synthetic tests are detailed in
`../../docs/mem0-alpha-next-plan.md` and `../../docs/mem0-alpha-next-results.md`.
No production rollout or state migration is implied by this package version.
