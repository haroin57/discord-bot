"""Filesystem layout and pure runtime path parsing.

The module-level names remain compatibility aliases.  RuntimeBootstrap calls
``build_runtime_paths`` again after dotenv loading and commits the resulting
values atomically before importing services that snapshot config constants.
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class PathConfigurationError(RuntimeError):
    """A configured runtime path is outside an approved storage boundary."""


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    bot_dir: str
    state_dir: str
    history_dir: str
    sessions_path: str
    runtime_state_path: str
    invest_kb_path: str
    settings_path: str
    credentials_path: str
    attachment_dir: str
    mermaid_output_dir: str
    table_renders_dir: str
    math_renders_dir: str
    session_ttl_s: int


# Compatibility aliases are installed at the bottom of the module.
BOT_DIR: str
STATE_DIR: str
HISTORY_DIR: str
SESSIONS_PATH: str
RUNTIME_STATE_PATH: str
INVEST_KB_PATH: str
SETTINGS_PATH: str
CREDENTIALS_PATH: str
ATTACHMENT_DIR: str
MERMAID_OUTPUT_DIR: str
TABLE_RENDERS_DIR: str
MATH_RENDERS_DIR: str
SESSION_TTL_S: int


def _parse_positive_int(
    environ: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise PathConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise PathConfigurationError(f"{name} must be a positive integer")
    return value


def _normalise_configured_path(name: str, raw: object) -> str:
    if not isinstance(raw, str):
        raise PathConfigurationError(f"{name} must be a string path")
    if not raw.strip():
        raise PathConfigurationError(f"{name} must not be empty")
    if "\x00" in raw:
        raise PathConfigurationError(f"{name} contains a NUL byte")
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise PathConfigurationError(f"{name} must be an absolute path")
    normalised = os.path.abspath(expanded)
    if Path(expanded).parts != Path(normalised).parts:
        raise PathConfigurationError(f"{name} must not contain traversal components")
    return normalised


def _require_descendant(name: str, path: str, roots: tuple[str, ...]) -> None:
    for root in roots:
        root = os.path.abspath(root)
        try:
            if os.path.commonpath((root, path)) == root and path != root:
                return
        except ValueError:
            continue
    allowed = ", ".join(roots)
    raise PathConfigurationError(
        f"{name} must be below an approved runtime root ({allowed})"
    )


def build_runtime_paths(
    environ: Mapping[str, str],
    *,
    bot_dir: str | None = None,
) -> RuntimePaths:
    """Parse runtime paths without filesystem I/O or global mutation."""
    source_bot_dir = bot_dir or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    parsed_bot_dir = _normalise_configured_path("BOT_DIR", source_bot_dir)
    temp_root = os.path.abspath(tempfile.gettempdir())
    persistent_roots = (parsed_bot_dir, temp_root, "/var/lib")
    transient_roots = (parsed_bot_dir, temp_root, "/var/cache")

    state_dir = _normalise_configured_path(
        "BOT_STATE_DIR",
        environ.get("BOT_STATE_DIR", os.path.join(parsed_bot_dir, ".state")),
    )
    _require_descendant("BOT_STATE_DIR", state_dir, persistent_roots)
    attachment_dir = _normalise_configured_path(
        "BOT_ATTACHMENT_DIR",
        environ.get(
            "BOT_ATTACHMENT_DIR",
            os.path.join(temp_root, "discord_bot_attachments"),
        ),
    )
    _require_descendant("BOT_ATTACHMENT_DIR", attachment_dir, transient_roots)

    render_paths: dict[str, str] = {}
    for name, default_name in (
        ("MERMAID_OUTPUT_DIR", "mermaid_output"),
        ("TABLE_RENDERS_DIR", "table_renders"),
        ("MATH_RENDERS_DIR", "math_renders"),
    ):
        value = _normalise_configured_path(
            name,
            environ.get(name, os.path.join(temp_root, default_name)),
        )
        _require_descendant(name, value, transient_roots)
        render_paths[name] = value

    return RuntimePaths(
        bot_dir=parsed_bot_dir,
        state_dir=state_dir,
        history_dir=os.path.join(state_dir, "history"),
        sessions_path=os.path.join(state_dir, "sessions.json"),
        runtime_state_path=os.path.join(state_dir, "runtime_state.json"),
        invest_kb_path=os.path.join(state_dir, "invest_kb"),
        settings_path=os.path.join(parsed_bot_dir, ".claude", "settings.json"),
        credentials_path=os.path.expanduser("~/.claude/.credentials.json"),
        attachment_dir=attachment_dir,
        mermaid_output_dir=render_paths["MERMAID_OUTPUT_DIR"],
        table_renders_dir=render_paths["TABLE_RENDERS_DIR"],
        math_renders_dir=render_paths["MATH_RENDERS_DIR"],
        session_ttl_s=_parse_positive_int(environ, "SESSION_TTL_S", 60 * 60),
    )


def install_runtime_paths(paths: RuntimePaths) -> None:
    """Update package compatibility aliases after successful parsing."""
    global BOT_DIR, STATE_DIR, HISTORY_DIR, SESSIONS_PATH, RUNTIME_STATE_PATH
    global INVEST_KB_PATH, SETTINGS_PATH, CREDENTIALS_PATH, ATTACHMENT_DIR
    global MERMAID_OUTPUT_DIR, TABLE_RENDERS_DIR, MATH_RENDERS_DIR, SESSION_TTL_S
    BOT_DIR = paths.bot_dir
    STATE_DIR = paths.state_dir
    HISTORY_DIR = paths.history_dir
    SESSIONS_PATH = paths.sessions_path
    RUNTIME_STATE_PATH = paths.runtime_state_path
    INVEST_KB_PATH = paths.invest_kb_path
    SETTINGS_PATH = paths.settings_path
    CREDENTIALS_PATH = paths.credentials_path
    ATTACHMENT_DIR = paths.attachment_dir
    MERMAID_OUTPUT_DIR = paths.mermaid_output_dir
    TABLE_RENDERS_DIR = paths.table_renders_dir
    MATH_RENDERS_DIR = paths.math_renders_dir
    SESSION_TTL_S = paths.session_ttl_s


def _ensure_mode_700(path: str) -> None:
    """Compatibility wrapper using the fail-closed no-follow implementation."""
    from services.runtime_bootstrap import ensure_private_directory

    ensure_private_directory(path)


_COMPAT_PATHS = build_runtime_paths(os.environ)
install_runtime_paths(_COMPAT_PATHS)


__all__ = [
    "ATTACHMENT_DIR",
    "BOT_DIR",
    "CREDENTIALS_PATH",
    "HISTORY_DIR",
    "INVEST_KB_PATH",
    "MATH_RENDERS_DIR",
    "MERMAID_OUTPUT_DIR",
    "PathConfigurationError",
    "RUNTIME_STATE_PATH",
    "RuntimePaths",
    "SESSIONS_PATH",
    "SESSION_TTL_S",
    "SETTINGS_PATH",
    "STATE_DIR",
    "TABLE_RENDERS_DIR",
    "_ensure_mode_700",
    "build_runtime_paths",
    "install_runtime_paths",
]
