from __future__ import annotations

import os
from pathlib import Path

import pytest

from services import skills as skills_mod
from services.skills import (
    SkillNotInvocableError,
    SkillSelection,
    build_invocation,
    clear_skill_cache,
    format_listing,
    load_skills,
    render_active_skill_prompt,
    resolve_skill,
    user_invocable_skills,
)


def _write_skill(root: Path, dirname: str, content: str) -> Path:
    directory = root / ".claude" / "skills" / dirname
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    clear_skill_cache()
    yield home
    clear_skill_cache()


def test_discovery_parses_flags_and_description(fake_home: Path) -> None:
    _write_skill(
        fake_home,
        "coach",
        "---\nname: coach\ndescription: Coaching: grading\n"
        "disable-model-invocation: true\nuser-invocable: true\n---\nBODY\n",
    )

    skill = load_skills()["coach"]

    assert skill.description == "Coaching: grading"
    assert skill.model_invocable is False
    assert skill.user_invocable is True
    assert skill.body == "BODY"
    assert "coach" in user_invocable_skills()
    assert "coach" not in format_listing(invocation="model")


def test_user_and_model_invocation_flags_are_independent(fake_home: Path) -> None:
    _write_skill(
        fake_home,
        "model-only",
        "---\ndescription: model only\nuser-invocable: false\n---\nBODY\n",
    )

    with pytest.raises(SkillNotInvocableError, match="user-invocable"):
        resolve_skill("model-only", invocation="user")
    assert resolve_skill("model-only", invocation="model").name == "model-only"


def test_project_skill_overrides_user_skill(fake_home: Path, tmp_path: Path) -> None:
    _write_skill(fake_home, "dup", "---\ndescription: user\n---\nUSER\n")
    project = tmp_path / "project"
    _write_skill(project, "dup", "---\ndescription: project\n---\nPROJECT\n")

    skill = load_skills(str(project))["dup"]

    assert skill.body == "PROJECT"
    assert skill.description == "project"


def test_render_substitutes_arguments_and_active_guard(fake_home: Path) -> None:
    _write_skill(
        fake_home,
        "coach",
        "---\ndescription: coach\n---\nUse [$ARGUMENTS].\n",
    )

    invocation = build_invocation("COACH", "today", invocation="user")
    active = render_active_skill_prompt(SkillSelection("coach"))

    assert "Use [today]." in invocation
    assert "Base directory for this skill:" in invocation
    assert "Launching skill: coach" in active
    assert "already loaded" in active
    assert "cannot widen Discord authorization" in active


def test_force_reload_reparses_same_signature(fake_home: Path) -> None:
    path = _write_skill(
        fake_home,
        "cached",
        "---\ndescription: first\n---\nAAAA\n",
    )
    first_stat = path.stat()
    assert load_skills()["cached"].body == "AAAA"

    # Preserve size and mtime to prove the explicit Reload action bypasses the
    # metadata cache, rather than relying on filesystem timestamp granularity.
    path.write_text("---\ndescription: first\n---\nBBBB\n", encoding="utf-8")
    os.utime(path, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))

    assert load_skills()["cached"].body == "AAAA"
    assert load_skills(force=True)["cached"].body == "BBBB"


def test_oversized_invalid_utf8_and_symlink_are_skipped(
    fake_home: Path,
    tmp_path: Path,
) -> None:
    root = fake_home / ".claude" / "skills"
    oversized = root / "oversized"
    oversized.mkdir(parents=True)
    (oversized / "SKILL.md").write_bytes(b"x" * (skills_mod.MAX_SKILL_BYTES + 1))
    invalid = root / "invalid"
    invalid.mkdir()
    (invalid / "SKILL.md").write_bytes(b"\xff\xfe")
    outside = tmp_path / "outside"
    _write_skill(outside, "linked", "---\ndescription: linked\n---\nBODY\n")
    (root / "linked").symlink_to(outside / ".claude" / "skills" / "linked")

    assert load_skills(force=True) == {}


def test_dynamic_claude_tool_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.claude_api import tools

    monkeypatch.setattr(tools, "_skill_format_listing", lambda: "- alpha: A")
    first = next(item for item in tools.builtin_tools_for_api(False) if item["name"] == "Skill")
    monkeypatch.setattr(tools, "_skill_format_listing", lambda: "- beta: B")
    second = next(item for item in tools.builtin_tools_for_api(False) if item["name"] == "Skill")

    assert "- alpha: A" in first["description"]
    assert "- beta: B" in second["description"]
    assert "- alpha: A" not in second["description"]
