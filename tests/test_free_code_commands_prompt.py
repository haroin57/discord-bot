"""PoC: prompt-type commands — templates, allowedTools, shell/arg expansion.

Confirms each ``type: 'prompt'`` command reproduces its upstream template verbatim,
expands embedded ``!`cmd``` via the runtime, threads args/attribution correctly, and
carries the right metadata (``progress_message``, ``allowed_tools``, ``source``).
"""

from __future__ import annotations

from free_code_commands import build_registry
from free_code_commands.commands import prompt_commands as pc
from free_code_commands.runtime import Attribution, RecordingRuntime, ShellResult


def _by_name(name):
    return next(c for c in pc.COMMANDS if c.name == name)


async def _text(cmd, args, rt):
    text = f"/{cmd.name} {args}".rstrip()
    invocation = await build_registry().invoke(text, rt)
    assert invocation is not None
    blocks = invocation.result
    assert len(blocks) == 1 and blocks[0].type == "text"
    return blocks[0].text


# --------------------------------------------------------------------------
# /commit
# --------------------------------------------------------------------------


async def test_commit_metadata():
    commit = _by_name("commit")
    assert commit.progress_message == "creating commit"
    assert commit.source == "builtin"
    assert commit.allowed_tools == (
        "Bash(git add:*)",
        "Bash(git status:*)",
        "Bash(git commit:*)",
    )


async def test_commit_prompt_expands_shell_and_contains_template():
    rt = RecordingRuntime(
        shell_outputs={
            "git status": ShellResult(stdout="On branch main\nnothing to commit"),
            "git diff HEAD": ShellResult(stdout="diff --git a/x b/x"),
            "git branch --show-current": ShellResult(stdout="main"),
            "git log --oneline -10": ShellResult(stdout="abc123 initial"),
        }
    )
    text = await _text(_by_name("commit"), "", rt)
    assert "## Git Safety Protocol" in text
    assert "## Your task" in text
    assert "NEVER use git commit --amend" in text
    # shell markers expanded (no residual backtick-bang), outputs injected
    assert "!`git status`" not in text
    assert "On branch main" in text
    assert "abc123 initial" in text


async def test_commit_appends_attribution_when_present():
    rt = RecordingRuntime(
        attribution=Attribution(commit="Co-Authored-By: Claude <noreply@anthropic.com>")
    )
    text = await _text(_by_name("commit"), "", rt)
    assert "Co-Authored-By: Claude <noreply@anthropic.com>" in text


# --------------------------------------------------------------------------
# /commit-push-pr
# --------------------------------------------------------------------------


async def test_commit_push_pr_uses_default_branch_and_appends_args():
    rt = RecordingRuntime(default_branch="develop")
    text = await _text(_by_name("commit-push-pr"), "make it quick", rt)
    assert "git diff develop...HEAD" in text
    assert "gh pr create" in text
    assert "## Additional instructions from user" in text
    assert "make it quick" in text


async def test_commit_push_pr_allowed_tools_include_gh_and_toolsearch():
    cmd = _by_name("commit-push-pr")
    assert "Bash(gh pr create:*)" in cmd.allowed_tools
    assert "ToolSearch" in cmd.allowed_tools
    assert cmd.content_length > 0  # computed from the 'main' template length


# --------------------------------------------------------------------------
# /review, /pr-comments
# --------------------------------------------------------------------------


async def test_review_prompt_injects_pr_number():
    text = await _text(_by_name("review"), "1234", RecordingRuntime())
    assert "expert code reviewer" in text
    assert "PR number: 1234" in text


async def test_pr_comments_non_ant_returns_fetch_prompt():
    text = await _text(_by_name("pr-comments"), "", RecordingRuntime())
    assert "## Comments" in text
    assert "gh pr view --json number" in text


async def test_pr_comments_ant_returns_moved_to_plugin_notice():
    text = await _text(_by_name("pr-comments"), "", RecordingRuntime(user_type="ant"))
    assert "moved to a plugin" in text
    assert "claude plugin install pr-comments@claude-code-marketplace" in text


# --------------------------------------------------------------------------
# /init, /security-review, /statusline, /insights, /init-verifiers
# --------------------------------------------------------------------------


async def test_init_returns_template_and_records_onboarding():
    rt = RecordingRuntime()
    text = await _text(_by_name("init"), "", rt)
    assert text.strip()  # non-empty bundled template
    assert ("init:onboarding-complete", {}) in [(n, d) for n, d in rt.opened_dialogs]


async def test_security_review_expands_and_nonempty():
    rt = RecordingRuntime()
    text = await _text(_by_name("security-review"), "", rt)
    assert text.strip()
    assert "!`" not in text  # frontmatter shell markers expanded


async def test_statusline_default_and_custom_prompt():
    default = await _text(_by_name("statusline"), "", RecordingRuntime())
    assert 'subagent_type "statusline-setup"' in default
    assert "Configure my statusLine" in default
    custom = await _text(_by_name("statusline"), "make it blue", RecordingRuntime())
    assert "make it blue" in custom


async def test_statusline_is_non_interactive_disabled():
    assert _by_name("statusline").supports_non_interactive is False
    invocation = await build_registry().invoke(
        "/statusline",
        RecordingRuntime(non_interactive=True),
    )
    assert invocation is None


async def test_insights_headless_shape():
    text = await _text(_by_name("insights"), "", RecordingRuntime())
    assert "/insights" in text
    assert "shareable insights report" in text


async def test_init_verifiers_nonempty():
    text = await _text(_by_name("init-verifiers"), "", RecordingRuntime())
    assert text.strip()
