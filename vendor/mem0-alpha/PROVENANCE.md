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

Every included upstream file is byte-for-byte equal to the captured source.
The only additional files are this provenance, the checksum manifest, and a
local build-artifact `.gitignore`.
The original upstream working directory is not modified by packaging.

Both requirements files use `./vendor/mem0-alpha`. Run pip from the repository
root, and build immutable releases from their archived `src` directory so the
dependency cannot resolve to mutable operator files. Pip builds and installs
a regular wheel; runtime import must resolve inside the release virtual
environment, never through an editable `.pth` link.
