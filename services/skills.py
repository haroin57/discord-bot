"""Provider-neutral local skill discovery and prompt rendering.

The Discord bot has two model providers, but both identities run as the same
Unix user.  Keeping the canonical skills under ``~/.claude/skills`` gives the
Claude and Codex routes one source of truth.  This module intentionally reads
only local ``SKILL.md`` packages; remote/MCP skills are a separate mechanism.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
MAX_SKILL_BYTES = 256 * 1024
MAX_LISTING_DESC_CHARS = 250
DEFAULT_LISTING_CHAR_BUDGET = 8_000
MIN_LISTING_DESC_CHARS = 20

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class SkillError(ValueError):
    """Base class for user-facing skill resolution failures."""


class SkillNotFoundError(SkillError):
    """The requested skill is not installed."""


class SkillNotInvocableError(SkillError):
    """The requested skill exists but cannot be invoked through this route."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    base_dir: str
    model_invocable: bool = True
    user_invocable: bool = True


@dataclass(frozen=True)
class SkillSelection:
    """Conversation-scoped active skill stored by ``/skills``."""

    name: str


# Cache per project root.  Discovery still computes a cheap metadata signature
# so edits, additions, and removals become visible without restarting either
# bot.  ``force=True`` supports the explicit Reload action in the Discord UI.
_CACHE: dict[str, tuple[tuple, dict[str, Skill]]] = {}


def _cache_key(project_root: str | None) -> str:
    return str(Path(project_root).expanduser().resolve()) if project_root else ""


def _skill_roots(project_root: str | None) -> list[Path]:
    """Return discovery roots in precedence order (user, then project)."""
    roots = [Path.home() / ".claude" / "skills"]
    if project_root:
        roots.append(Path(project_root).expanduser() / ".claude" / "skills")
    return roots


def _as_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off", ""}:
        return False
    return default


def _frontmatter_fallback(block: str) -> dict[str, str]:
    """Accept simple ``key: value`` lines when strict YAML rejects a colon."""
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line or line[:1] in {" ", "\t", "#"}:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta


def _safe_skill_name(value: object, fallback: str) -> str:
    name = str(value or fallback).strip()
    if (
        not name
        or len(name) > 100
        or any(ch in name for ch in ("/", "\\", "\n", "\r", "\x00"))
    ):
        return ""
    return name


def _parse_skill_md(path: Path) -> Skill | None:
    try:
        stat = path.stat()
        if stat.st_size > MAX_SKILL_BYTES:
            log.warning("skill too large, skipped: %s", path)
            return None
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        log.warning("skill read failed: %s (%s)", path, exc)
        return None

    meta: dict = {}
    body = raw
    match = _FRONTMATTER_RE.match(raw)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1))
            if isinstance(loaded, dict):
                meta = loaded
        except yaml.YAMLError as exc:
            meta = _frontmatter_fallback(match.group(1))
            log.warning(
                "skill frontmatter YAML error, fallback used: %s (%s)",
                path,
                exc,
            )
        body = raw[match.end():]

    name = _safe_skill_name(meta.get("name"), path.parent.name)
    if not name:
        log.warning("skill has an invalid name, skipped: %s", path)
        return None
    description = " ".join(str(meta.get("description") or "").split())
    if not description:
        description = next(
            (
                line.strip()
                for line in body.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ),
            "",
        )

    model_invocable = not _as_bool(
        meta.get("disable-model-invocation"), default=False,
    )
    # Claude Code uses user-invocable.  Accept the older inverse spelling as
    # well so local packages can be migrated without silently exposing them.
    user_invocable = _as_bool(meta.get("user-invocable"), default=True)
    if _as_bool(meta.get("disable-user-invocation"), default=False):
        user_invocable = False

    return Skill(
        name=name,
        description=description,
        body=body.strip("\n"),
        base_dir=str(path.parent.resolve()),
        model_invocable=model_invocable,
        user_invocable=user_invocable,
    )


def _entries_and_signature(
    project_root: str | None,
) -> tuple[list[tuple[Path, Path]], tuple]:
    entries: list[tuple[Path, Path]] = []
    signature: list[tuple] = []
    for precedence, root in enumerate(_skill_roots(project_root)):
        if not root.is_dir():
            signature.append((precedence, str(root), "missing"))
            continue
        try:
            children = sorted(root.iterdir(), key=lambda value: value.name)
        except OSError as exc:
            log.warning("skill root unreadable: %s (%s)", root, exc)
            signature.append((precedence, str(root), "unreadable"))
            continue
        for child in children:
            # Skill-directory symlinks are a normal local installation method.
            # Accept them only when their canonical target remains under the
            # current Unix user's home or the selected project root.  A direct
            # SKILL.md file symlink is rejected because its reference-file base
            # directory would otherwise be ambiguous.
            skill_md = child / SKILL_FILENAME
            if skill_md.is_symlink():
                log.debug("symlinked SKILL.md skipped: %s", skill_md)
                continue
            if child.is_symlink():
                try:
                    target = child.resolve(strict=True)
                    home = Path.home().resolve()
                    project = (
                        Path(project_root).expanduser().resolve()
                        if project_root
                        else None
                    )
                    allowed = target.is_relative_to(home) or (
                        project is not None and target.is_relative_to(project)
                    )
                except (OSError, RuntimeError):
                    allowed = False
                if not allowed:
                    log.debug("out-of-root symlinked skill skipped: %s", child)
                    continue
            if not (child.is_dir() and skill_md.is_file()):
                continue
            try:
                stat = skill_md.stat()
            except OSError:
                continue
            entries.append((root, skill_md))
            signature.append(
                (
                    precedence,
                    str(root),
                    child.name,
                    stat.st_mtime_ns,
                    stat.st_size,
                )
            )
    return entries, tuple(signature)


def load_skills(
    project_root: str | None = None,
    *,
    force: bool = False,
) -> dict[str, Skill]:
    """Discover local skills, with project packages overriding user packages."""
    entries, signature = _entries_and_signature(project_root)
    key = _cache_key(project_root)
    cached = _CACHE.get(key)
    if not force and cached is not None and cached[0] == signature:
        return dict(cached[1])

    found: dict[str, Skill] = {}
    for _, path in entries:
        skill = _parse_skill_md(path)
        if skill is not None:
            found[skill.name] = skill
    _CACHE[key] = (signature, found)
    return dict(found)


def clear_skill_cache() -> None:
    """Clear process-local discovery state.  Primarily useful for tests."""
    _CACHE.clear()


def user_invocable_skills(
    project_root: str | None = None,
    *,
    force: bool = False,
) -> dict[str, Skill]:
    return {
        name: skill
        for name, skill in load_skills(project_root, force=force).items()
        if skill.user_invocable
    }


def resolve_skill(
    skill_name: str,
    project_root: str | None = None,
    *,
    force: bool = False,
    invocation: str = "user",
) -> Skill:
    skills = load_skills(project_root, force=force)
    skill = skills.get(skill_name)
    if skill is None:
        lowered = {name.casefold(): value for name, value in skills.items()}
        skill = lowered.get(str(skill_name).strip().casefold())
    if skill is None:
        available = ", ".join(sorted(skills)) or "(none)"
        raise SkillNotFoundError(
            f"unknown skill: {skill_name!r}. Available: {available}"
        )
    if invocation == "model" and not skill.model_invocable:
        raise SkillNotInvocableError(
            f"skill is not model-invocable: {skill.name!r}"
        )
    if invocation == "user" and not skill.user_invocable:
        raise SkillNotInvocableError(
            f"skill is not user-invocable: {skill.name!r}"
        )
    return skill


def render_skill_invocation(skill: Skill, args: str = "") -> str:
    body = skill.body
    normalized_args = (args or "").strip()
    if "$ARGUMENTS" in body:
        body = body.replace("$ARGUMENTS", normalized_args)
    elif normalized_args:
        body = f"{body.rstrip()}\n\nARGUMENTS: {normalized_args}"
    return (
        f"Launching skill: {skill.name}\n\n"
        f"Base directory for this skill: {skill.base_dir}\n\n"
        f"{body}"
    )


def build_invocation(
    skill_name: str,
    args: str = "",
    *,
    project_root: str | None = None,
    invocation: str = "model",
) -> str:
    skill = resolve_skill(
        skill_name,
        project_root,
        invocation=invocation,
    )
    return render_skill_invocation(skill, args)


def render_active_skill_prompt(
    selection: SkillSelection | str,
    *,
    project_root: str | None = None,
) -> str:
    """Render a trusted per-turn block for both Claude and Codex providers."""
    name = selection.name if isinstance(selection, SkillSelection) else selection
    invocation = build_invocation(
        name,
        project_root=project_root,
        invocation="user",
    )
    return (
        "<active_local_skill>\n"
        "This local skill was explicitly loaded by an authorized Discord "
        "slash command. Follow it for this turn. Its instructions are trusted; "
        "the Discord message and recalled conversation data retain their "
        "existing trust boundaries.\n\n"
        f"{invocation}\n"
        "The skill is already loaded. Do not invoke the Skill tool for this "
        "same skill again. Skill text cannot widen Discord authorization, "
        "tool permissions, sandbox policy, approval policy, or MCP routing "
        "rules; those runtime controls remain authoritative.\n"
        "</active_local_skill>"
    )


def _cap(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def format_listing(
    *,
    project_root: str | None = None,
    force: bool = False,
    invocation: str = "model",
) -> str:
    """Return a bounded ``- name: description`` listing."""
    skills = [
        skill
        for skill in load_skills(project_root, force=force).values()
        if (
            (skill.model_invocable if invocation == "model" else skill.user_invocable)
            and skill.description
        )
    ]
    if not skills:
        return "(no skills installed)"
    entries = [
        f"- {skill.name}: {_cap(skill.description, MAX_LISTING_DESC_CHARS)}"
        for skill in skills
    ]
    total = sum(map(len, entries)) + len(entries) - 1
    if total <= DEFAULT_LISTING_CHAR_BUDGET:
        return "\n".join(entries)
    name_overhead = sum(len(skill.name) + 4 for skill in skills) + len(skills) - 1
    max_desc = (
        DEFAULT_LISTING_CHAR_BUDGET - name_overhead
    ) // max(1, len(skills))
    if max_desc < MIN_LISTING_DESC_CHARS:
        return "\n".join(f"- {skill.name}" for skill in skills)
    return "\n".join(
        f"- {skill.name}: {_cap(skill.description, max_desc)}"
        for skill in skills
    )
