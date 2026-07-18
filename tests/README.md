# Phase 4 test taxonomy

Every collected test has exactly one primary pytest marker:

- `unit`: deterministic and isolated from external services.
- `contract`: stable public API, facade, import, and serialization contracts.
- `characterization`: existing observable behavior frozen during refactoring.
- `integration`: multi-component, real subprocess, or provisioned-service tests.
- `eval`: deterministic offline quality gates. Network access is prohibited.
- `legacy`: temporary superseded coverage with a named replacement and owner.

Existing flat files remain in place and are explicitly classified by
`tests/taxonomy.toml`. An unlisted flat test is a collection error. New tests
must be placed under `tests/<marker>/`; an explicit marker that disagrees with
the directory or manifest is also a collection error.

## Tool-loop ownership

`tests/test_claude_api_tool_loop.py` is the state-machine characterization
suite. It owns turn sequencing, callback order, fallback continuation, timeout
and checkpoint behavior, and historical monkeypatch seams. It must not become a
facade shape test.

`tests/test_tool_loop_refactor_compat.py` is the formal facade contract suite.
It owns import/re-export identity, signatures, global lookup and monkeypatch
forwarding across the split modules. It must not duplicate the state-machine
scenario matrix. The collection hook marks these protected files without
editing their current refactor snapshots.

## CI lanes

Run a lane with `python scripts/run_test_lane.py <lane> --workers auto`. The
available lanes are `collection-contract`, `unit-fast`, `characterization`,
`integration`, `legacy`, and `offline-eval`. Every invoked lane must collect at
least one test. The empty `legacy` migration target is not a required CI job;
invoking it before a test is classified there fails rather than returning a
false success.

Module-scope mutation of `sys.modules` or `os.environ`, and module-scope file
I/O, are prohibited. Use `monkeypatch`, `tmp_path`, and scoped fixtures. Run
`python scripts/check_test_isolation.py` before changing classification or test
bootstrap behavior.

The release gate requires two repository variables. `RELEASE_MANIFEST_SHA256`
is the externally reviewed SHA-256 digest of the separately generated Phase 4
release manifest, and `STAGING_HEALTH_ALLOWED_HOSTS` is a comma-separated exact
hostname allowlist. Pull requests cannot change either trust anchor. Generate
`docs/2026-07-14-refactor-release-manifest.sha256` only after final integration;
the Phase 0 protected manifest is immutable evidence and is never a release
manifest input or self-update target.
