#!/usr/bin/env python3
"""Validate Phase 4 release prerequisites without changing application state."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


CONTROLLER_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_EVIDENCE_RELATIVE = "docs/2026-07-14-refactor-protected-manifest.sha256"
PROTECTED_EVIDENCE_DIGEST = (
    "3dff6770adbf2cbfd519ba9d730e047daab89639473033d61ab11b78082ba330"
)
RELEASE_MANIFEST_ENV = "PHASE4_RELEASE_MANIFEST"
RELEASE_MANIFEST_DIGEST_ENV = "RELEASE_MANIFEST_SHA256"
CONTROLLER_SNAPSHOT_SCRIPT = CONTROLLER_ROOT / "scripts/snapshot_state.sh"
REQUIRED_JOBS = {
    "trust-controller",
    "collection-contract",
    "unit-fast",
    "characterization",
    "integration",
    "offline-eval",
    "security",
}
TOOL_LOOP_PROTECTED_PATHS = {
    "services/claude_api/cli_default_system_prompt.txt",
    "services/claude_api/client.py",
    "services/claude_api/loop_contracts.py",
    "services/claude_api/loop_observability.py",
    "services/claude_api/message_codec.py",
    "services/claude_api/request_shape.py",
    "services/claude_api/tool_display.py",
    "services/claude_api/tool_execution.py",
    "services/claude_api/tool_loop.py",
    "tests/test_claude_api.py",
    "tests/test_claude_api_tool_loop.py",
    "tests/test_tool_loop_refactor_compat.py",
}
REQUIRED_RELEASE_PATHS = {
    ".github/actions/setup-tests/action.yml",
    ".github/workflows/lint.yml",
    ".github/workflows/notify-ci-failure.yml",
    ".github/workflows/notify-push.yml",
    ".github/workflows/release.yml",
    ".github/workflows/security.yml",
    ".github/workflows/test.yml",
    "pyproject.toml",
    "requirements-ci.lock",
    "requirements.lock",
    "scripts/check_release_gate.py",
    "scripts/check_test_isolation.py",
    "scripts/run_test_lane.py",
    "tests/README.md",
    "tests/conftest.py",
    "tests/contract/test_phase4_ci_contracts.py",
    "tests/integration/test_phase4_release_gate.py",
    "tests/integration/test_free_code_bridge_subprocess.py",
    "tests/integration/test_mem0_subprocess.py",
    "tests/taxonomy.toml",
    "tests/test_oauth_manager.py",
    "tests/unit/test_phase4_scripts.py",
}
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
CONFLICT_PATTERN = r"^(<<<<<<<|=======|>>>>>>>)($|[[:space:]])"
MAX_HEALTH_BODY_BYTES = 64 * 1024
MAX_RELEASE_MANIFEST_BYTES = 4 * 1024 * 1024
HEALTHY_STATUSES = {"healthy", "ok", "ready"}


class _DuplicateJSONKeyError(ValueError):
    """Signal that a JSON object contains an ambiguous duplicate member."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json_fail_closed(raw: str | bytes, *, label: str) -> object:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKeyError) as exc:
        raise RuntimeError(
            f"{label} must be valid JSON without duplicate keys"
        ) from exc


@dataclass(frozen=True)
class ReleaseManifest:
    base_sha: str
    candidate_sha: str
    files: dict[str, str]
    deletions: frozenset[str]


def check_job_results(raw: str | None) -> None:
    if not raw:
        raise RuntimeError("CI job results are required")
    payload = _load_json_fail_closed(raw, label="CI job results")
    if not isinstance(payload, dict):
        raise RuntimeError("CI job results must be a JSON object")
    missing = REQUIRED_JOBS - set(payload)
    failed: dict[str, object] = {}
    for name in REQUIRED_JOBS & set(payload):
        details = payload[name]
        if not isinstance(details, dict) or details.get("result") != "success":
            failed[name] = details.get("result") if isinstance(details, dict) else None
    if missing or failed:
        raise RuntimeError(
            f"CI lane failure: missing={sorted(missing)}, failed={failed}"
        )


def _validate_sha(value: str, *, label: str) -> None:
    if not SHA_PATTERN.fullmatch(value):
        raise RuntimeError(f"{label} must be a full lowercase commit SHA")


def _candidate_repository(path: Path) -> Path:
    if not path.is_absolute():
        raise RuntimeError("candidate repository path must be absolute")
    if path.is_symlink():
        raise RuntimeError("candidate repository path must not be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeError("candidate repository path does not exist") from exc
    if not resolved.is_dir():
        raise RuntimeError("candidate repository path must be a directory")
    if resolved == CONTROLLER_ROOT.resolve():
        raise RuntimeError("candidate repository must be separate from controller")
    completed = _git("rev-parse", "--show-toplevel", repo=resolved, check=False)
    if completed.returncode != 0:
        raise RuntimeError("candidate repository is not a Git checkout")
    try:
        git_root = Path(completed.stdout.strip()).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError("candidate repository Git root is invalid") from exc
    if git_root != resolved:
        raise RuntimeError("candidate repository must name the Git checkout root")
    return resolved


def _git(
    *args: str,
    repo: Path,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=text,
    )


def check_commit_range(base_sha: str, candidate_sha: str, *, repo: Path) -> None:
    _validate_sha(base_sha, label="base SHA")
    _validate_sha(candidate_sha, label="candidate SHA")
    for label, value in (("base", base_sha), ("candidate", candidate_sha)):
        result = _git("cat-file", "-e", f"{value}^{{commit}}", repo=repo, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"{label} commit is unavailable: {value}")
    ancestor = _git(
        "merge-base", "--is-ancestor", base_sha, candidate_sha, repo=repo, check=False
    )
    if ancestor.returncode != 0:
        raise RuntimeError("base SHA is not an ancestor of candidate SHA")
    checked_out = _git("rev-parse", "HEAD", repo=repo).stdout.strip()
    if checked_out != candidate_sha:
        raise RuntimeError(
            f"checked-out SHA {checked_out} does not match candidate {candidate_sha}"
        )


def check_clean_checkout(*, repo: Path) -> None:
    completed = _git("status", "--porcelain", "--untracked-files=all", repo=repo)
    if completed.stdout:
        raise RuntimeError("dirty deploy rejected:\n" + completed.stdout.rstrip())


def check_diff(base_sha: str, candidate_sha: str, *, repo: Path) -> None:
    _git("diff", "--check", base_sha, candidate_sha, "--", repo=repo)


def check_conflict_markers(candidate_sha: str, *, repo: Path) -> None:
    completed = _git(
        "grep",
        "-I",
        "-n",
        "-E",
        CONFLICT_PATTERN,
        candidate_sha,
        "--",
        repo=repo,
        check=False,
    )
    if completed.returncode == 0:
        raise RuntimeError(
            "merge conflict markers found:\n" + completed.stdout.rstrip()
        )
    if completed.returncode != 1:
        raise RuntimeError(completed.stderr.rstrip() or "conflict-marker scan failed")


def _git_blob(repo: Path, commit: str, relative: str) -> bytes:
    completed = _git("show", f"{commit}:{relative}", repo=repo, check=False, text=False)
    if completed.returncode != 0:
        raise RuntimeError(f"required path missing at candidate: {relative}")
    return completed.stdout


def _validate_manifest_path(relative: str) -> str:
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in relative
        or str(path) in {"", "."}
    ):
        raise RuntimeError(f"unsafe release manifest path: {relative}")
    normalized = str(path)
    if normalized == PROTECTED_EVIDENCE_RELATIVE:
        raise RuntimeError("protected evidence cannot be used as the release manifest")
    return normalized


def _manifest_file(path_value: str, *, repo: Path) -> Path:
    requested = Path(path_value)
    if requested.is_absolute():
        source = requested
    else:
        relative = _validate_manifest_path(path_value)
        source = repo / relative
    if source.is_symlink():
        raise RuntimeError("release manifest must not be a symbolic link")
    resolved = source.resolve(strict=False)
    if not requested.is_absolute():
        try:
            resolved.relative_to(repo.resolve())
        except ValueError as exc:
            raise RuntimeError("release manifest path escapes the repository") from exc
    protected = (repo / PROTECTED_EVIDENCE_RELATIVE).resolve(strict=False)
    if resolved == protected:
        raise RuntimeError("protected evidence cannot be used as the release manifest")
    if resolved.is_symlink() or not resolved.is_file():
        raise RuntimeError(f"release manifest is not a regular file: {resolved}")
    if resolved.stat().st_size > MAX_RELEASE_MANIFEST_BYTES:
        raise RuntimeError("release manifest is too large")
    return resolved


def _normalize_manifest_entry_path(relative: object, *, label: str) -> str:
    if not isinstance(relative, str):
        raise RuntimeError(f"{label} path must be a string")
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in relative
        or str(path) in {"", "."}
    ):
        raise RuntimeError(f"unsafe {label} path: {relative}")
    return str(path)


def _parse_release_manifest(raw: bytes) -> ReleaseManifest:
    payload = _load_json_fail_closed(raw, label="release manifest")
    if not isinstance(payload, dict):
        raise RuntimeError("release manifest must be a JSON object")
    expected_keys = {
        "schema_version",
        "base_sha",
        "candidate_sha",
        "files",
        "deletions",
    }
    if set(payload) != expected_keys or payload.get("schema_version") != 1:
        raise RuntimeError("release manifest schema is invalid")
    base_sha = payload.get("base_sha")
    candidate_sha = payload.get("candidate_sha")
    if not isinstance(base_sha, str) or not isinstance(candidate_sha, str):
        raise RuntimeError("release manifest SHAs must be strings")
    _validate_sha(base_sha, label="release manifest base SHA")
    _validate_sha(candidate_sha, label="release manifest candidate SHA")

    raw_files = payload.get("files")
    raw_deletions = payload.get("deletions")
    if not isinstance(raw_files, dict) or not isinstance(raw_deletions, list):
        raise RuntimeError("release manifest files/deletions schema is invalid")
    files: dict[str, str] = {}
    for raw_path, digest in raw_files.items():
        relative = _normalize_manifest_entry_path(raw_path, label="release")
        if relative in files:
            raise RuntimeError(f"duplicate release path: {relative}")
        if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
            raise RuntimeError(f"invalid release digest: {relative}")
        files[relative] = digest
    deletions: set[str] = set()
    for raw_path in raw_deletions:
        relative = _normalize_manifest_entry_path(raw_path, label="release deletion")
        if relative in deletions:
            raise RuntimeError(f"duplicate release deletion: {relative}")
        deletions.add(relative)
    overlap = set(files) & deletions
    if overlap:
        raise RuntimeError(
            f"release paths are both hashed and deleted: {sorted(overlap)}"
        )
    if not files and not deletions:
        raise RuntimeError("release manifest has no entries")
    missing = REQUIRED_RELEASE_PATHS - set(files)
    if missing:
        raise RuntimeError(
            f"release manifest is missing required paths: {sorted(missing)}"
        )
    return ReleaseManifest(
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        files=files,
        deletions=frozenset(deletions),
    )


def _candidate_diff_paths(
    base_sha: str,
    candidate_sha: str,
    *,
    repo: Path,
) -> tuple[set[str], set[str]]:
    completed = _git(
        "diff",
        "--name-status",
        "-z",
        "--no-renames",
        base_sha,
        candidate_sha,
        "--",
        repo=repo,
        text=False,
    )
    fields = completed.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise RuntimeError("candidate diff returned an invalid name-status stream")
    changed: set[str] = set()
    deleted: set[str] = set()
    for offset in range(0, len(fields), 2):
        try:
            status = fields[offset].decode("ascii")
            relative = fields[offset + 1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("candidate diff contains a non-UTF-8 path") from exc
        relative = _normalize_manifest_entry_path(relative, label="candidate diff")
        if not status or status[0] not in {"A", "C", "D", "M", "R", "T", "U"}:
            raise RuntimeError(f"unsupported candidate diff status: {status}")
        changed.add(relative)
        if status[0] == "D":
            deleted.add(relative)
    names_result = _git(
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        base_sha,
        candidate_sha,
        "--",
        repo=repo,
        text=False,
    )
    try:
        name_paths = {
            _normalize_manifest_entry_path(raw.decode("utf-8"), label="candidate diff")
            for raw in names_result.stdout.split(b"\0")
            if raw
        }
    except UnicodeDecodeError as exc:
        raise RuntimeError("candidate diff contains a non-UTF-8 path") from exc
    if name_paths != changed:
        raise RuntimeError("candidate name-only and name-status diffs disagree")
    return changed, deleted


def _parse_manifest(
    raw: bytes,
    *,
    required_paths: set[str] | frozenset[str],
    label: str,
) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{label} manifest must be UTF-8 text") from exc
    entries: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            expected, relative = line.split(maxsplit=1)
        except ValueError as exc:
            raise RuntimeError(f"invalid {label} manifest line {line_number}") from exc
        relative = relative.strip()
        path = PurePosixPath(relative)
        if not DIGEST_PATTERN.fullmatch(expected):
            raise RuntimeError(f"invalid {label} digest: {relative}")
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in relative
            or str(path) in {"", "."}
        ):
            raise RuntimeError(f"unsafe {label} path: {relative}")
        normalized = str(path)
        if normalized in entries:
            raise RuntimeError(f"duplicate {label} path: {normalized}")
        entries[normalized] = expected
    if not entries:
        raise RuntimeError(f"{label} manifest has no entries")
    missing = required_paths - set(entries)
    if missing:
        raise RuntimeError(
            f"{label} manifest is missing required paths: {sorted(missing)}"
        )
    return entries


def _check_manifest_entries(
    candidate_sha: str,
    entries: dict[str, str],
    *,
    label: str,
    repo: Path,
) -> None:
    for relative, expected in entries.items():
        actual = hashlib.sha256(_git_blob(repo, candidate_sha, relative)).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise RuntimeError(f"{label} manifest mismatch: {relative}")


def _find_manifest_mismatches(
    candidate_sha: str,
    entries: dict[str, str],
    *,
    repo: Path,
) -> list[str]:
    mismatches = []
    for relative, expected in entries.items():
        try:
            blob = _git_blob(repo, candidate_sha, relative)
        except RuntimeError:
            mismatches.append(relative)
            continue
        actual = hashlib.sha256(blob).hexdigest()
        if not hmac.compare_digest(actual, expected):
            mismatches.append(relative)
    return sorted(mismatches)


def check_tool_loop_protected_evidence(
    candidate_sha: str,
    *,
    repo: Path,
) -> list[str]:
    manifest = _git_blob(repo, candidate_sha, PROTECTED_EVIDENCE_RELATIVE)
    actual_digest = hashlib.sha256(manifest).hexdigest()
    if not hmac.compare_digest(actual_digest, PROTECTED_EVIDENCE_DIGEST):
        raise RuntimeError("protected evidence differs from the Phase 0 snapshot")
    entries = _parse_manifest(
        manifest,
        required_paths=TOOL_LOOP_PROTECTED_PATHS,
        label="protected evidence",
    )
    protected_entries = {
        relative: entries[relative] for relative in TOOL_LOOP_PROTECTED_PATHS
    }
    _check_manifest_entries(
        candidate_sha,
        protected_entries,
        label="tool-loop protected evidence",
        repo=repo,
    )
    return _find_manifest_mismatches(
        candidate_sha,
        {
            relative: expected
            for relative, expected in entries.items()
            if relative not in TOOL_LOOP_PROTECTED_PATHS
        },
        repo=repo,
    )


def check_release_manifest(
    base_sha: str,
    candidate_sha: str,
    manifest_path: str,
    trusted_digest: str,
    *,
    repo: Path,
) -> None:
    _validate_sha(base_sha, label="base SHA")
    _validate_sha(candidate_sha, label="candidate SHA")
    if not DIGEST_PATTERN.fullmatch(trusted_digest):
        raise RuntimeError(
            "trusted release manifest digest must be a lowercase SHA-256 value"
        )
    manifest = _manifest_file(manifest_path, repo=repo).read_bytes()
    actual_manifest_digest = hashlib.sha256(manifest).hexdigest()
    if not hmac.compare_digest(actual_manifest_digest, trusted_digest):
        raise RuntimeError("release manifest does not match the external digest pin")
    parsed = _parse_release_manifest(manifest)
    if parsed.base_sha != base_sha:
        raise RuntimeError(
            "release manifest base SHA does not match the requested range"
        )
    if parsed.candidate_sha != candidate_sha:
        raise RuntimeError("release manifest candidate SHA is stale or mismatched")
    changed, deleted = _candidate_diff_paths(
        base_sha,
        candidate_sha,
        repo=repo,
    )
    changed_files = changed - deleted
    missing_changed = changed_files - set(parsed.files)
    if missing_changed:
        raise RuntimeError(
            f"release manifest omits changed candidate paths: {sorted(missing_changed)}"
        )
    if parsed.deletions != deleted:
        raise RuntimeError(
            "release manifest deletion records do not match candidate diff: "
            f"missing={sorted(deleted - parsed.deletions)}, "
            f"stale={sorted(parsed.deletions - deleted)}"
        )
    _check_manifest_entries(
        candidate_sha,
        parsed.files,
        label="release",
        repo=repo,
    )


def _snapshot_environment(home: Path) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "SNAPSHOT_R2_SYNC": "0",
    }
    return environment


def _write_rehearsal_inventory(inventory: Path, state_root: Path) -> None:
    if not state_root.is_absolute():
        raise RuntimeError("snapshot rehearsal live-state path must be absolute")
    payload = {
        "format": "discord-bot-state-inventory-v1",
        "services": [{"name": "release-gate", "path": str(state_root)}],
    }
    inventory.write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    inventory.chmod(0o600)
    stat_result = inventory.stat()
    if stat_result.st_uid != os.getuid() or stat_result.st_mode & 0o022:
        raise RuntimeError("snapshot rehearsal inventory owner or mode is unsafe")


def rehearse_backup_restore() -> None:
    script = CONTROLLER_SNAPSHOT_SCRIPT.resolve()
    if not script.is_file() or not os.access(script, os.X_OK):
        raise RuntimeError(f"snapshot API is missing or not executable: {script}")
    with tempfile.TemporaryDirectory(prefix="phase4-release-gate-") as tmp:
        root = Path(tmp)
        state = root / "live-state"
        snapshots = root / "snapshots"
        home = root / "home"
        inventory = root / "state-inventory.json"
        state.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        (state / "sessions.json").write_text(
            json.dumps({"channel": {"session_id": "gate"}}), encoding="utf-8"
        )
        database = sqlite3.connect(state / "state.db")
        try:
            database.execute("CREATE TABLE gate (id INTEGER PRIMARY KEY, value TEXT)")
            database.execute("INSERT INTO gate(value) VALUES ('restorable')")
            database.commit()
        finally:
            database.close()
        state.chmod(0o700)
        _write_rehearsal_inventory(inventory, state.resolve())
        snapshot_environment = _snapshot_environment(home) | {
            "SNAPSHOT_INVENTORY_OWNER_UID": str(os.getuid()),
            "SNAPSHOT_REQUIRE_READ_ONLY": "0",
            "SNAPSHOT_ROOT": str(snapshots.resolve()),
            "SNAPSHOT_STATE_INVENTORY": str(inventory.resolve()),
        }
        created = subprocess.run(
            [str(script)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=snapshot_environment,
        )
        destinations = [
            line.removeprefix("[snapshot] done: ").strip()
            for line in created.stdout.splitlines()
            if line.startswith("[snapshot] done: ")
        ]
        if len(destinations) != 1:
            raise RuntimeError("snapshot API did not return exactly one snapshot path")
        snapshot = Path(destinations[0]).resolve()
        try:
            snapshot.relative_to(snapshots.resolve())
        except ValueError as exc:
            raise RuntimeError(
                "snapshot API returned a path outside the rehearsal root"
            ) from exc
        if not snapshot.is_dir():
            raise RuntimeError("snapshot API returned a missing snapshot path")
        restored_state = snapshot / "services" / "release-gate"
        if json.loads((restored_state / "sessions.json").read_bytes()) != {
            "channel": {"session_id": "gate"}
        }:
            raise RuntimeError("snapshot API did not preserve rehearsal JSON state")
        restored_database = sqlite3.connect(
            f"file:{restored_state / 'state.db'}?mode=ro",
            uri=True,
        )
        try:
            row = restored_database.execute("SELECT value FROM gate").fetchone()
        finally:
            restored_database.close()
        if row != ("restorable",):
            raise RuntimeError("snapshot API did not preserve rehearsal database state")
        verified = subprocess.run(
            [str(script), "--verify", str(snapshot)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=snapshot_environment,
        )
        if "rehearsal OK" not in verified.stdout:
            raise RuntimeError("snapshot API did not confirm restore integrity")


def _normalize_allowed_hosts(raw_hosts: list[str] | tuple[str, ...]) -> frozenset[str]:
    allowed: set[str] = set()
    for raw in raw_hosts:
        for value in raw.split(","):
            host = value.strip().rstrip(".").lower()
            if not host:
                continue
            if "://" in host or any(character in host for character in "/?#@"):
                raise RuntimeError(f"invalid staging allowlist host: {value.strip()}")
            try:
                host = host.encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise RuntimeError(
                    f"invalid staging allowlist host: {value.strip()}"
                ) from exc
            allowed.add(host)
    return frozenset(allowed)


def _validate_staging_url(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise RuntimeError("staging health URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("staging health URL must not contain user information")
    if parsed.fragment:
        raise RuntimeError("staging health URL must not contain a fragment")
    try:
        host = (
            (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        )
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("staging health URL has an invalid host or port") from exc
    if not host or host not in allowed_hosts:
        raise RuntimeError(f"staging health host is not allowed: {host or '<missing>'}")
    if port is not None and not 1 <= port <= 65535:
        raise RuntimeError("staging health URL has an invalid port")


class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_staging_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _content_type(response: object) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    if hasattr(headers, "get_content_type"):
        return str(headers.get_content_type()).lower()
    value = headers.get("Content-Type", "")
    return str(value).split(";", 1)[0].strip().lower()


def _validate_health_payload(body: bytes, candidate_sha: str) -> None:
    payload = _load_json_fail_closed(body, label="staging health response")
    if not isinstance(payload, dict):
        raise RuntimeError("staging health response must be a JSON object")
    status = payload.get("status")
    if not isinstance(status, str) or status.lower() not in HEALTHY_STATUSES:
        raise RuntimeError("staging health response requires a healthy status string")
    if payload.get("release_sha") != candidate_sha:
        raise RuntimeError("staging release_sha does not match the candidate SHA")
    if payload.get("build_sha") != candidate_sha:
        raise RuntimeError("staging build_sha does not match the candidate SHA")


def check_staging(
    url: str | None,
    *,
    candidate_sha: str,
    required: bool,
    allowed_hosts: list[str] | tuple[str, ...],
    opener: object | None = None,
) -> None:
    _validate_sha(candidate_sha, label="candidate SHA")
    if not url:
        if required:
            raise RuntimeError(
                "staging health URL is required for a manual release gate"
            )
        return
    allowed = _normalize_allowed_hosts(allowed_hosts)
    if not allowed:
        raise RuntimeError("staging health host allowlist is required")
    _validate_staging_url(url, allowed)
    client = opener or urllib.request.build_opener(_ValidatedRedirectHandler(allowed))
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    with client.open(request, timeout=15) as response:
        final_url = response.geturl() if hasattr(response, "geturl") else url
        _validate_staging_url(final_url, allowed)
        status = getattr(response, "status", None)
        if not isinstance(status, int) or not 200 <= status < 300:
            raise RuntimeError(f"staging smoke returned HTTP {status}")
        if _content_type(response) != "application/json":
            raise RuntimeError("staging health response must use application/json")
        body = response.read(MAX_HEALTH_BODY_BYTES + 1)
    if not body or len(body) > MAX_HEALTH_BODY_BYTES:
        raise RuntimeError("staging health response body is empty or too large")
    _validate_health_payload(body, candidate_sha)


def check_rollback_threshold(observed: str, threshold: str) -> None:
    try:
        observed_value = Decimal(observed)
        threshold_value = Decimal(threshold)
    except InvalidOperation as exc:
        raise RuntimeError("rollback rates must be decimal numbers") from exc
    if not observed_value.is_finite() or observed_value < 0:
        raise RuntimeError("observed error rate must be finite and non-negative")
    if not threshold_value.is_finite() or threshold_value <= 0:
        raise RuntimeError("rollback threshold must be finite and greater than zero")
    if observed_value >= threshold_value:
        raise RuntimeError(
            f"rollback required: observed error rate {observed_value}% "
            f">= threshold {threshold_value}%"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-results", required=True)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument(
        "--release-manifest",
        default=os.environ.get(RELEASE_MANIFEST_ENV),
    )
    parser.add_argument(
        "--release-manifest-digest",
        default=os.environ.get(RELEASE_MANIFEST_DIGEST_ENV),
    )
    parser.add_argument("--staging-health-url")
    parser.add_argument("--staging-allowed-host", action="append", default=[])
    parser.add_argument("--require-staging", action="store_true")
    parser.add_argument("--observed-error-rate", default="0")
    parser.add_argument("--rollback-threshold", default="1")
    args = parser.parse_args(argv)
    if not args.release_manifest:
        parser.error(f"--release-manifest or {RELEASE_MANIFEST_ENV} is required")
    if not args.release_manifest_digest:
        parser.error(
            f"--release-manifest-digest or {RELEASE_MANIFEST_DIGEST_ENV} is required"
        )

    candidate_repo = _candidate_repository(args.repo)
    check_job_results(args.job_results)
    check_commit_range(args.base_sha, args.candidate_sha, repo=candidate_repo)
    check_diff(args.base_sha, args.candidate_sha, repo=candidate_repo)
    check_conflict_markers(args.candidate_sha, repo=candidate_repo)
    check_clean_checkout(repo=candidate_repo)
    non_tool_loop_mismatches = check_tool_loop_protected_evidence(
        args.candidate_sha,
        repo=candidate_repo,
    )
    if non_tool_loop_mismatches:
        print(
            "protected evidence non-tool-loop mismatches: "
            + ", ".join(non_tool_loop_mismatches)
        )
    check_release_manifest(
        args.base_sha,
        args.candidate_sha,
        args.release_manifest,
        args.release_manifest_digest,
        repo=candidate_repo,
    )
    rehearse_backup_restore()
    check_staging(
        args.staging_health_url,
        candidate_sha=args.candidate_sha,
        required=args.require_staging,
        allowed_hosts=args.staging_allowed_host,
    )
    check_rollback_threshold(args.observed_error_rate, args.rollback_threshold)
    print("release gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
