"""PoC: the Discord bridge reproduces free-code's prompt-slash-command algorithm.

Asserts each step of ``getMessagesForPromptSlashCommand`` has an identical Python
equivalent (tag format, allowedTools parsing, template expansion, ordering) and
that the Discord adapter registers the wired subset. See ADR-0048.
"""

from __future__ import annotations

import pytest

from free_code_commands.commands.prompt_commands import commit, review, security_review
from free_code_commands.runtime import RecordingRuntime, ShellResult
from services.free_code_bridge import (
    BotCommandRuntime,
    build_prompt_command_turn,
    format_slash_command_loading_metadata,
    parse_tool_list_from_cli,
)


# --------------------------------------------------------------------------
# Step 3 — parseToolListFromCLI parity
# --------------------------------------------------------------------------


def test_parse_tool_list_passthrough():
    assert parse_tool_list_from_cli(["Bash(git add:*)", "Bash(git status:*)"]) == [
        "Bash(git add:*)",
        "Bash(git status:*)",
    ]


def test_parse_tool_list_splits_top_level_commas_only():
    # top-level comma splits; comma inside parens is preserved
    assert parse_tool_list_from_cli(["A,B"]) == ["A", "B"]
    assert parse_tool_list_from_cli(["Bash(x,y:*)"]) == ["Bash(x,y:*)"]
    assert parse_tool_list_from_cli(["A, Bash(a,b), C"]) == ["A", "Bash(a,b)", "C"]


def test_parse_tool_list_empty_and_blank():
    assert parse_tool_list_from_cli([]) == []
    assert parse_tool_list_from_cli(["", "  "]) == []


# --------------------------------------------------------------------------
# Step 2 — formatSlashCommandLoadingMetadata parity
# --------------------------------------------------------------------------


def test_metadata_tags_without_args():
    assert format_slash_command_loading_metadata("commit", "") == (
        "<command-message>commit</command-message>\n"
        "<command-name>/commit</command-name>"
    )


def test_metadata_tags_with_args():
    assert format_slash_command_loading_metadata("review", "1234") == (
        "<command-message>review</command-message>\n"
        "<command-name>/review</command-name>\n"
        "<command-args>1234</command-args>"
    )


# --------------------------------------------------------------------------
# Steps 1/4/5 — build_prompt_command_turn parity
# --------------------------------------------------------------------------


def _git_runtime() -> RecordingRuntime:
    return RecordingRuntime(
        shell_outputs={
            "git status": ShellResult(stdout="On branch master\nnothing to commit"),
            "git diff HEAD": ShellResult(stdout="diff --git a/x b/x"),
            "git branch --show-current": ShellResult(stdout="master"),
            "git log --oneline -10": ShellResult(stdout="abc123 initial"),
        }
    )


async def test_commit_turn_expands_and_tags():
    p = await build_prompt_command_turn(commit(), "", _git_runtime())
    # Step 2: tags present, and Step 1: template + expanded shell output
    assert "<command-name>/commit</command-name>" in p.prompt
    assert "Git Safety Protocol" in p.prompt
    assert "On branch master" in p.prompt  # !`git status` was expanded
    assert "!`git status`" not in p.prompt
    # Step 3: allowed tools parsed from the command
    assert p.allowed_tools == ["Bash(git add:*)", "Bash(git status:*)", "Bash(git commit:*)"]
    assert p.progress_message == "creating commit"


async def test_turn_ordering_tags_before_expanded():
    p = await build_prompt_command_turn(commit(), "", _git_runtime())
    # free-code sends metadata message first, then the prompt: tags precede body.
    assert p.prompt.index("<command-name>") < p.prompt.index("Git Safety Protocol")


async def test_turn_wrapped_as_trusted_admin_input():
    p = await build_prompt_command_turn(commit(), "", _git_runtime())
    assert p.prompt.startswith("<trusted_user_input>")
    assert p.prompt.rstrip().endswith("</trusted_user_input>")


async def test_turn_can_skip_trust_wrap():
    p = await build_prompt_command_turn(commit(), "", _git_runtime(), wrap_trusted=False)
    assert not p.prompt.startswith("<trusted_user_input>")
    assert p.prompt.startswith("<command-message>commit</command-message>")


async def test_review_injects_pr_number():
    p = await build_prompt_command_turn(review(), "42", _git_runtime())
    assert "PR number: 42" in p.prompt
    assert "<command-args>42</command-args>" in p.prompt
    assert "expert code reviewer" in p.prompt


async def test_security_review_expands_frontmatter_shell():
    p = await build_prompt_command_turn(security_review(), "", _git_runtime())
    assert p.expanded_prompt.strip()
    assert "!`" not in p.expanded_prompt  # frontmatter shell markers expanded


async def test_non_prompt_command_rejected():
    from free_code_commands.commands.local_commands import cost

    with pytest.raises(ValueError):
        await build_prompt_command_turn(cost(), "", _git_runtime())


# --------------------------------------------------------------------------
# BotCommandRuntime — cwd-scoped shell + default branch
# --------------------------------------------------------------------------


async def test_runtime_runs_shell_in_working_dir(tmp_path):
    (tmp_path / "marker.txt").write_text("hello")
    rt = BotCommandRuntime(working_dir=str(tmp_path))
    res = await rt.run_shell("ls")
    assert "marker.txt" in res.stdout


async def test_runtime_attribution_is_bot_trailer():
    rt = BotCommandRuntime()
    attr = rt.attribution_texts()
    assert "Co-Authored-By" in attr.commit


# --------------------------------------------------------------------------
# Discord adapter registration
# --------------------------------------------------------------------------


class _FakeTree:
    def __init__(self):
        self.registered: list[tuple[str, str]] = []

    def command(self, *, name: str, description: str):
        def _decorator(fn):
            self.registered.append((name, description))
            return fn

        return _decorator


class _FakeDeps:
    is_admin = staticmethod(lambda user: True)
    model_overrides: dict = {}
    effort_overrides: dict = {}
    tier_overrides: dict = {}
    get_channel_sema = staticmethod(lambda cid: None)
    bot = None
    track_task = staticmethod(lambda *a, **k: None)
    send_segmented = staticmethod(lambda *a, **k: None)
    get_http_session = staticmethod(lambda: None)


def test_register_wires_three_commands():
    from bot_commands import free_code_slash

    tree = _FakeTree()
    free_code_slash.register(tree, _FakeDeps())
    names = [n for n, _ in tree.registered]
    assert names == ["commit", "review", "security-review"]
    # description carries the admin-only marker and stays within Discord's 100-char cap
    for _name, desc in tree.registered:
        assert len(desc) <= 100
        assert "admin only" in desc
