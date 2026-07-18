"""Shared fixtures for tests."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest


_PRIMARY_MARKERS = {
    "unit",
    "contract",
    "characterization",
    "integration",
    "eval",
    "legacy",
}

_TEST_ROOT = Path(__file__).parent
_TAXONOMY_PATH = _TEST_ROOT / "taxonomy.toml"
_APPLICATION_ROOTS = (
    "bot",
    "bot_commands",
    "bot_pipeline",
    "config",
    "free_code_commands",
    "main",
    "main_async",
    "mcp_mem0_server",
    "services",
    "utils",
)

_SESSION_ENV: dict[str, str] | None = None
_SESSION_ROOT: str | None = None
_COLLECTION_ENV: dict[str, str] | None = None
_COLLECTION_CWD: str | None = None
_COLLECTION_WORKSPACE: dict[str, tuple[str, str]] | None = None
_FLAT_FILE_MARKERS: dict[str, str] | None = None
_WORKSPACE_ROOT = _TEST_ROOT.parent
_WORKSPACE_SNAPSHOT_EXCLUDES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".state",
    ".venv",
    "__pycache__",
    "node_modules",
}


def _workspace_snapshot(root: Path = _WORKSPACE_ROOT) -> dict[str, tuple[str, str]]:
    """Hash versionable workspace content while excluding framework caches."""
    snapshot: dict[str, tuple[str, str]] = {}
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _WORKSPACE_SNAPSHOT_EXCLUDES
        )
        base = Path(directory)
        for filename in sorted(filenames):
            path = base / filename
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                snapshot[relative] = ("symlink", os.readlink(path))
                continue
            if not path.is_file():
                continue
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            snapshot[relative] = ("file", digest.hexdigest())
    return snapshot


def _load_taxonomy(
    test_root: Path = _TEST_ROOT,
    taxonomy_path: Path = _TAXONOMY_PATH,
) -> dict[str, str]:
    payload = tomllib.loads(taxonomy_path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("files"), dict):
        raise pytest.UsageError(
            "tests/taxonomy.toml must contain version=1 and [files]"
        )
    markers = payload["files"]
    invalid = {
        name: marker
        for name, marker in markers.items()
        if not isinstance(name, str) or marker not in _PRIMARY_MARKERS
    }
    actual = {path.name for path in test_root.glob("test_*.py")}
    declared = set(markers)
    if invalid or actual != declared:
        raise pytest.UsageError(
            "invalid explicit flat-test taxonomy: "
            f"invalid={invalid}, missing={sorted(actual - declared)}, "
            f"stale={sorted(declared - actual)}"
        )
    return dict(markers)


def pytest_configure(config):
    """Route collection-time application I/O into a disposable process root."""
    global _COLLECTION_CWD, _COLLECTION_ENV, _COLLECTION_WORKSPACE
    global _FLAT_FILE_MARKERS
    global _SESSION_ENV, _SESSION_ROOT
    if _SESSION_ENV is not None:
        return

    _FLAT_FILE_MARKERS = _load_taxonomy()
    _SESSION_ENV = dict(os.environ)
    _SESSION_ROOT = tempfile.mkdtemp(prefix=f"discord-bot-pytest-{os.getpid()}-")
    root = Path(_SESSION_ROOT)
    isolated_env = {
        "HOME": str(root / "home"),
        "BOT_STATE_DIR": str(root / "state"),
        "BOT_ATTACHMENT_DIR": str(root / "attachments"),
        "BOT_LOG_PATH": str(root / "bot.log"),
        "MERMAID_OUTPUT_DIR": str(root / "mermaid"),
        "TABLE_RENDERS_DIR": str(root / "tables"),
        "MATH_RENDERS_DIR": str(root / "math"),
        # mcp_mem0_server is a child-process entrypoint and deliberately pins
        # this flag at import. Make that subprocess contract part of the test
        # baseline instead of allowing collection to create it implicitly.
        "OAUTH_NO_REFRESH": "1",
        # Collection imports must not hydrate the test process from the live
        # repository .env. Tests of RuntimeBootstrap patch its explicit loader.
        "PYTHON_DOTENV_DISABLED": "1",
        "R2_ARCHIVE_ENABLED": "false",
    }
    os.environ.update(isolated_env)
    _COLLECTION_ENV = dict(os.environ)
    _COLLECTION_CWD = os.getcwd()
    if "PYTEST_XDIST_WORKER" not in os.environ:
        _COLLECTION_WORKSPACE = _workspace_snapshot()


def pytest_collection_finish(session):
    """Fail when collection mutates process state outside pytest configuration."""
    if _COLLECTION_ENV is None or _COLLECTION_CWD is None:
        return
    changed_environment = sorted(
        key
        for key in set(_COLLECTION_ENV) | set(os.environ)
        if _COLLECTION_ENV.get(key) != os.environ.get(key)
    )
    changed_workspace: list[str] = []
    if _COLLECTION_WORKSPACE is not None:
        current_workspace = _workspace_snapshot()
        changed_workspace = sorted(
            relative
            for relative in set(_COLLECTION_WORKSPACE) | set(current_workspace)
            if _COLLECTION_WORKSPACE.get(relative) != current_workspace.get(relative)
        )
    if changed_environment or os.getcwd() != _COLLECTION_CWD or changed_workspace:
        raise pytest.UsageError(
            "collection mutated process state: "
            f"environment={changed_environment}, "
            f"cwd_changed={os.getcwd() != _COLLECTION_CWD}, "
            f"workspace={changed_workspace}"
        )


def pytest_unconfigure(config):
    """Restore the process environment and remove collection-time files."""
    global _COLLECTION_CWD, _COLLECTION_ENV, _COLLECTION_WORKSPACE
    global _FLAT_FILE_MARKERS
    global _SESSION_ENV, _SESSION_ROOT
    if _SESSION_ENV is None:
        return

    os.environ.clear()
    os.environ.update(_SESSION_ENV)
    _SESSION_ENV = None
    if _SESSION_ROOT is not None:
        shutil.rmtree(_SESSION_ROOT)
    _SESSION_ROOT = None
    _COLLECTION_ENV = None
    _COLLECTION_CWD = None
    _COLLECTION_WORKSPACE = None
    _FLAT_FILE_MARKERS = None


def _path_marker(item) -> str:
    path = Path(str(item.path))
    try:
        relative = path.relative_to(_TEST_ROOT)
    except ValueError as exc:
        raise pytest.UsageError(f"test outside configured taxonomy: {path}") from exc
    if len(relative.parts) > 1 and relative.parts[0] in _PRIMARY_MARKERS:
        return relative.parts[0]
    if len(relative.parts) != 1 or _FLAT_FILE_MARKERS is None:
        raise pytest.UsageError(f"test has no explicit taxonomy entry: {relative}")
    try:
        return _FLAT_FILE_MARKERS[relative.name]
    except KeyError as exc:
        raise pytest.UsageError(
            f"flat test has no explicit taxonomy entry: {relative.name}"
        ) from exc


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Assign one Phase 4 primary marker to every collected test."""
    errors = []
    for item in items:
        expected = _path_marker(item)
        assigned = {
            marker.name
            for marker in item.iter_markers()
            if marker.name in _PRIMARY_MARKERS
        }
        if len(assigned) > 1:
            errors.append(
                f"{item.nodeid}: multiple primary markers: {sorted(assigned)}"
            )
            continue
        if assigned and assigned != {expected}:
            errors.append(
                f"{item.nodeid}: primary marker {sorted(assigned)} does not match "
                f"explicit taxonomy {expected}"
            )
            continue
        if not assigned:
            item.add_marker(getattr(pytest.mark, expected))
    if errors:
        raise pytest.UsageError("\n".join(errors))


@pytest.fixture
def tmp_state_dir(tmp_path, monkeypatch):
    """Provide a temporary state directory and patch config paths."""
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    sessions_path = tmp_path / "sessions.json"

    monkeypatch.setattr("config.STATE_DIR", str(tmp_path))
    monkeypatch.setattr("config.HISTORY_DIR", str(history_dir))
    monkeypatch.setattr("config.SESSIONS_PATH", str(sessions_path))

    # Also patch the session module's imported references
    monkeypatch.setattr("services.session.HISTORY_DIR", str(history_dir))
    monkeypatch.setattr("services.session.SESSIONS_PATH", str(sessions_path))

    return tmp_path


@pytest.fixture
def tmp_attachment_dir(tmp_path, monkeypatch):
    """Provide a temporary attachment directory."""
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    monkeypatch.setattr("config.ATTACHMENT_DIR", str(att_dir))
    monkeypatch.setattr("utils.attachments.ATTACHMENT_DIR", str(att_dir))
    return att_dir


@pytest.fixture(autouse=True)
def _restore_process_state(request):
    """Detect process leaks and make application imports fresh for the next test."""
    original_env = dict(os.environ)
    original_cwd = os.getcwd()
    original_modules = dict(sys.modules)
    yield

    all_changed_env = {
        key
        for key in set(original_env) | set(os.environ)
        if original_env.get(key) != os.environ.get(key)
    }
    framework_env = {
        key: os.environ.get(key)
        for key in all_changed_env
        if _is_framework_environment_transition(
            key,
            original_env.get(key),
            os.environ.get(key),
            request.node.nodeid,
        )
    }
    changed_env = sorted(all_changed_env - set(framework_env))
    changed_cwd = os.getcwd() != original_cwd
    os.chdir(original_cwd)
    os.environ.clear()
    os.environ.update(original_env)
    for key, value in framework_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    for name in sorted(set(sys.modules) - set(original_modules), reverse=True):
        if _is_application_module(name):
            _remove_module(name, sys.modules.get(name))
    for name, module in original_modules.items():
        if sys.modules.get(name) is not module:
            sys.modules[name] = module
            _restore_parent_binding(name, module)

    if (changed_env or changed_cwd) and request.node.get_closest_marker(
        "process_state_mutation"
    ) is None:
        pytest.fail(
            "test leaked process state; use monkeypatch or mark an intentional "
            "lifecycle mutation with process_state_mutation: "
            f"environment={changed_env}, cwd_changed={changed_cwd}",
            pytrace=False,
        )


def _is_framework_environment_transition(
    key: str,
    before: str | None,
    after: str | None,
    nodeid: str,
) -> bool:
    """Recognize only pytest's exact per-phase current-test transition."""
    return (
        key == "PYTEST_CURRENT_TEST"
        and before == f"{nodeid} (setup)"
        and after == f"{nodeid} (teardown)"
    )


def _is_application_module(name: str) -> bool:
    return any(
        name == root or name.startswith(f"{root}.") for root in _APPLICATION_ROOTS
    )


def _remove_module(name: str, module: object) -> None:
    sys.modules.pop(name, None)
    parent_name, separator, child_name = name.rpartition(".")
    if not separator:
        return
    parent = sys.modules.get(parent_name)
    if parent is not None and getattr(parent, child_name, None) is module:
        delattr(parent, child_name)


def _restore_parent_binding(name: str, module: object) -> None:
    parent_name, separator, child_name = name.rpartition(".")
    if not separator:
        return
    parent = sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, child_name, module)


@pytest.fixture(autouse=True)
def _isolate_bot_state():
    """Reset already-imported bot state between tests without importing bot.

    `bot.cooldowns` and `bot._daily_quota` are OrderedDicts mutated by the
    on_message hot path. Without isolation, test order influences outcomes
    within a worker. Pure utility tests must not import the application merely
    because this fixture exists.
    """
    _bot = sys.modules.get("bot")
    if _bot is None:
        yield
        return

    _rl = sys.modules.get("services.rate_limit")
    if _rl is not None:
        _rl.reset_for_tests()
    # P4a: bot.sessions is now the process-wide shared dict from
    # services.session.shared_sessions(). Clear its CONTENTS (not rebind —
    # bot.py and web_api hold references to the same object) so entries
    # created by one test never leak into the next.
    shared = getattr(_bot, "sessions", None)
    if shared is not None:
        shared.clear()
    try:
        yield
    finally:
        if _rl is not None:
            _rl.reset_for_tests()
        if shared is not None:
            shared.clear()
