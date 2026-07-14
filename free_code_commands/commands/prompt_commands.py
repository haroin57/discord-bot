"""``type: 'prompt'`` commands — ported from free-code's prompt slash commands.

A prompt command expands into a list of Anthropic content blocks that are sent to
the model as the user's turn. The port preserves each command's exact template
text, ``allowedTools``, ``progressMessage`` and argument handling; runtime-bound
inputs (git status via ``!`cmd```, default branch, attribution trailers, the
insights report) come through :class:`~free_code_commands.runtime.CommandRuntime`.

Sources:
    commit.ts, commit-push-pr.ts, review.ts, pr_comments/index.ts,
    createMovedToPluginCommand.ts, init.ts, init-verifiers.ts,
    security-review.ts, statusline.tsx, insights.ts
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..frontmatter import parse_frontmatter, parse_slash_command_tools
from ..runtime import CommandRuntime
from ..shell import execute_shell_commands_in_prompt
from ..types import ContentBlock, PromptCommand, text_block

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "_prompts"


def _load_prompt(name: str) -> str:
    """Read a verbatim prompt template bundled under ``_prompts/``."""

    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# /commit  (commit.ts)
# ---------------------------------------------------------------------------

_COMMIT_ALLOWED_TOOLS = [
    "Bash(git add:*)",
    "Bash(git status:*)",
    "Bash(git commit:*)",
]


def _commit_prompt_content(rt: CommandRuntime) -> str:
    commit_attribution = rt.attribution_texts().commit

    prefix = ""
    if rt.is_ant() and rt.is_undercover():
        prefix = rt.undercover_instructions() + "\n"

    attribution_line = f"\n\n{commit_attribution}" if commit_attribution else ""

    return f"""{prefix}## Context

- Current git status: !`git status`
- Current git diff (staged and unstaged changes): !`git diff HEAD`
- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -10`

## Git Safety Protocol

- NEVER update the git config
- NEVER skip hooks (--no-verify, --no-gpg-sign, etc) unless the user explicitly requests it
- CRITICAL: ALWAYS create NEW commits. NEVER use git commit --amend, unless the user explicitly requests it
- Do not commit files that likely contain secrets (.env, credentials.json, etc). Warn the user if they specifically request to commit those files
- If there are no changes to commit (i.e., no untracked files and no modifications), do not create an empty commit
- Never use git commands with the -i flag (like git rebase -i or git add -i) since they require interactive input which is not supported

## Your task

Based on the above changes, create a single git commit:

1. Analyze all staged changes and draft a commit message:
   - Look at the recent commits above to follow this repository's commit message style
   - Summarize the nature of the changes (new feature, enhancement, bug fix, refactoring, test, docs, etc.)
   - Ensure the message accurately reflects the changes and their purpose (i.e. "add" means a wholly new feature, "update" means an enhancement to an existing feature, "fix" means a bug fix, etc.)
   - Draft a concise (1-2 sentences) commit message that focuses on the "why" rather than the "what"

2. Stage relevant files and create the commit using HEREDOC syntax:
```
git commit -m "$(cat <<'EOF'
Commit message here.{attribution_line}
EOF
)"
```

You have the capability to call multiple tools in a single response. Stage and create the commit using a single message. Do not use any other tools or do anything else. Do not send any other text or messages besides these tool calls."""


async def _commit_get_prompt(args: str, rt: CommandRuntime) -> list[ContentBlock]:
    content = _commit_prompt_content(rt)
    final = await execute_shell_commands_in_prompt(content, rt, "/commit")
    return [text_block(final)]


def commit() -> PromptCommand:
    return PromptCommand(
        name="commit",
        description="Create a git commit",
        progress_message="creating commit",
        allowed_tools=list(_COMMIT_ALLOWED_TOOLS),
        content_length=0,
        source="builtin",
        get_prompt_fn=_commit_get_prompt,
    )


# ---------------------------------------------------------------------------
# /commit-push-pr  (commit-push-pr.ts)
# ---------------------------------------------------------------------------

_COMMIT_PUSH_PR_ALLOWED_TOOLS = [
    "Bash(git checkout --branch:*)",
    "Bash(git checkout -b:*)",
    "Bash(git add:*)",
    "Bash(git status:*)",
    "Bash(git push:*)",
    "Bash(git commit:*)",
    "Bash(gh pr create:*)",
    "Bash(gh pr edit:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr merge:*)",
    "ToolSearch",
    "mcp__slack__send_message",
    "mcp__claude_ai_Slack__slack_send_message",
]


def _commit_push_pr_content(
    rt: CommandRuntime, default_branch: str, pr_attribution: Optional[str]
) -> str:
    attribution = rt.attribution_texts()
    commit_attribution = attribution.commit
    effective_pr_attribution = pr_attribution if pr_attribution is not None else attribution.pr
    safe_user = rt.env("SAFEUSER") or ""
    username = rt.env("USER") or ""

    prefix = ""
    reviewer_arg = " and `--reviewer anthropics/claude-code`"
    add_reviewer_arg = " (and add `--add-reviewer anthropics/claude-code`)"
    changelog_section = (
        "\n\n## Changelog\n"
        "<!-- CHANGELOG:START -->\n"
        "[If this PR contains user-facing changes, add a changelog entry here. Otherwise, remove this section.]\n"
        "<!-- CHANGELOG:END -->"
    )
    slack_step = (
        "\n\n5. After creating/updating the PR, check if the user's CLAUDE.md mentions posting to Slack channels. "
        "If it does, use ToolSearch to search for \"slack send message\" tools. If ToolSearch finds a Slack tool, "
        "ask the user if they'd like you to post the PR URL to the relevant Slack channel. Only post if the user confirms. "
        "If ToolSearch returns no results or errors, skip this step silently—do not mention the failure, do not attempt "
        "workarounds, and do not try alternative approaches."
    )
    if rt.is_ant() and rt.is_undercover():
        prefix = rt.undercover_instructions() + "\n"
        reviewer_arg = ""
        add_reviewer_arg = ""
        changelog_section = ""
        slack_step = ""

    commit_attr_clause = (
        ", ending with the attribution text shown in the example below" if commit_attribution else ""
    )
    commit_attr_line = f"\n\n{commit_attribution}" if commit_attribution else ""
    pr_attr_line = f"\n\n{effective_pr_attribution}" if effective_pr_attribution else ""

    return f"""{prefix}## Context

- `SAFEUSER`: {safe_user}
- `whoami`: {username}
- `git status`: !`git status`
- `git diff HEAD`: !`git diff HEAD`
- `git branch --show-current`: !`git branch --show-current`
- `git diff {default_branch}...HEAD`: !`git diff {default_branch}...HEAD`
- `gh pr view --json number 2>/dev/null || true`: !`gh pr view --json number 2>/dev/null || true`

## Git Safety Protocol

- NEVER update the git config
- NEVER run destructive/irreversible git commands (like push --force, hard reset, etc) unless the user explicitly requests them
- NEVER skip hooks (--no-verify, --no-gpg-sign, etc) unless the user explicitly requests it
- NEVER run force push to main/master, warn the user if they request it
- Do not commit files that likely contain secrets (.env, credentials.json, etc)
- Never use git commands with the -i flag (like git rebase -i or git add -i) since they require interactive input which is not supported

## Your task

Analyze all changes that will be included in the pull request, making sure to look at all relevant commits (NOT just the latest commit, but ALL commits that will be included in the pull request from the git diff {default_branch}...HEAD output above).

Based on the above changes:
1. Create a new branch if on {default_branch} (use SAFEUSER from context above for the branch name prefix, falling back to whoami if SAFEUSER is empty, e.g., `username/feature-name`)
2. Create a single commit with an appropriate message using heredoc syntax{commit_attr_clause}:
```
git commit -m "$(cat <<'EOF'
Commit message here.{commit_attr_line}
EOF
)"
```
3. Push the branch to origin
4. If a PR already exists for this branch (check the gh pr view output above), update the PR title and body using `gh pr edit` to reflect the current diff{add_reviewer_arg}. Otherwise, create a pull request using `gh pr create` with heredoc syntax for the body{reviewer_arg}.
   - IMPORTANT: Keep PR titles short (under 70 characters). Use the body for details.
```
gh pr create --title "Short, descriptive title" --body "$(cat <<'EOF'
## Summary
<1-3 bullet points>

## Test plan
[Bulleted markdown checklist of TODOs for testing the pull request...]{changelog_section}{pr_attr_line}
EOF
)"
```

You have the capability to call multiple tools in a single response. You MUST do all of the above in a single message.{slack_step}

Return the PR URL when you're done, so the user can see it."""


async def _commit_push_pr_get_prompt(args: str, rt: CommandRuntime) -> list[ContentBlock]:
    default_branch = await rt.get_default_branch()
    pr_attribution = await rt.get_enhanced_pr_attribution()
    content = _commit_push_pr_content(rt, default_branch, pr_attribution)
    trimmed = (args or "").strip()
    if trimmed:
        content += f"\n\n## Additional instructions from user\n\n{trimmed}"
    final = await execute_shell_commands_in_prompt(content, rt, "/commit-push-pr")
    return [text_block(final)]


def commit_push_pr() -> PromptCommand:
    return PromptCommand(
        name="commit-push-pr",
        description="Commit, push, and open a PR",
        progress_message="creating commit and PR",
        allowed_tools=list(_COMMIT_PUSH_PR_ALLOWED_TOOLS),
        content_length=len(_commit_push_pr_content(CommandRuntime(), "main", None)),
        source="builtin",
        get_prompt_fn=_commit_push_pr_get_prompt,
    )


# ---------------------------------------------------------------------------
# /review  (review.ts)
# ---------------------------------------------------------------------------


def _local_review_prompt(args: str) -> str:
    return f"""
      You are an expert code reviewer. Follow these steps:

      1. If no PR number is provided in the args, run `gh pr list` to show open PRs
      2. If a PR number is provided, run `gh pr view <number>` to get PR details
      3. Run `gh pr diff <number>` to get the diff
      4. Analyze the changes and provide a thorough code review that includes:
         - Overview of what the PR does
         - Analysis of code quality and style
         - Specific suggestions for improvements
         - Any potential issues or risks

      Keep your review concise but thorough. Focus on:
      - Code correctness
      - Following project conventions
      - Performance implications
      - Test coverage
      - Security considerations

      Format your review with clear sections and bullet points.

      PR number: {args}
    """


async def _review_get_prompt(args: str, rt: CommandRuntime) -> list[ContentBlock]:
    return [text_block(_local_review_prompt(args))]


def review() -> PromptCommand:
    return PromptCommand(
        name="review",
        description="Review a pull request",
        progress_message="reviewing pull request",
        content_length=0,
        source="builtin",
        get_prompt_fn=_review_get_prompt,
    )


# ---------------------------------------------------------------------------
# createMovedToPluginCommand  (createMovedToPluginCommand.ts)
# ---------------------------------------------------------------------------


def create_moved_to_plugin_command(
    *,
    name: str,
    description: str,
    progress_message: str,
    plugin_name: str,
    plugin_command: str,
    get_prompt_while_marketplace_is_private: Callable[
        [str, CommandRuntime], Awaitable[list[ContentBlock]]
    ],
) -> PromptCommand:
    """Port of ``createMovedToPluginCommand``.

    For ant users the command explains the plugin has moved and how to install
    it; everyone else falls back to the provided (still-inlined) prompt builder.
    """

    async def _get_prompt(args: str, rt: CommandRuntime) -> list[ContentBlock]:
        if rt.is_ant():
            return [
                text_block(
                    f"""This command has been moved to a plugin. Tell the user:

1. To install the plugin, run:
   claude plugin install {plugin_name}@claude-code-marketplace

2. After installation, use /{plugin_name}:{plugin_command} to run this command

3. For more information, see: https://github.com/anthropics/claude-code-marketplace/blob/main/{plugin_name}/README.md

Do not attempt to run the command. Simply inform the user about the plugin installation."""
                )
            ]
        return await get_prompt_while_marketplace_is_private(args, rt)

    return PromptCommand(
        name=name,
        description=description,
        progress_message=progress_message,
        content_length=0,
        source="builtin",
        get_prompt_fn=_get_prompt,
    )


# ---------------------------------------------------------------------------
# /pr-comments  (pr_comments/index.ts, via createMovedToPluginCommand)
# ---------------------------------------------------------------------------


async def _pr_comments_private_prompt(args: str, rt: CommandRuntime) -> list[ContentBlock]:
    extra = ("Additional user input: " + args) if args else ""
    return [
        text_block(
            f"""You are an AI assistant integrated into a git-based version control system. Your task is to fetch and display comments from a GitHub pull request.

Follow these steps:

1. Use `gh pr view --json number,headRepository` to get the PR number and repository info
2. Use `gh api /repos/{{owner}}/{{repo}}/issues/{{number}}/comments` to get PR-level comments
3. Use `gh api /repos/{{owner}}/{{repo}}/pulls/{{number}}/comments` to get review comments. Pay particular attention to the following fields: `body`, `diff_hunk`, `path`, `line`, etc. If the comment references some code, consider fetching it using eg `gh api /repos/{{owner}}/{{repo}}/contents/{{path}}?ref={{branch}} | jq .content -r | base64 -d`
4. Parse and format all comments in a readable way
5. Return ONLY the formatted comments, with no additional text

Format the comments as:

## Comments

[For each comment thread:]
- @author file.ts#line:
  ```diff
  [diff_hunk from the API response]
  ```
  > quoted comment text

  [any replies indented]

If there are no comments, return "No comments found."

Remember:
1. Only show the actual comments, no explanatory text
2. Include both PR-level and code review comments
3. Preserve the threading/nesting of comment replies
4. Show the file and line number context for code review comments
5. Use jq to parse the JSON responses from the GitHub API

{extra}
"""
        )
    ]


def pr_comments() -> PromptCommand:
    return create_moved_to_plugin_command(
        name="pr-comments",
        description="Get comments from a GitHub pull request",
        progress_message="fetching PR comments",
        plugin_name="pr-comments",
        plugin_command="pr-comments",
        get_prompt_while_marketplace_is_private=_pr_comments_private_prompt,
    )


# ---------------------------------------------------------------------------
# /init  (init.ts)
# ---------------------------------------------------------------------------


def _new_init_enabled(rt: CommandRuntime) -> bool:
    return rt.feature("NEW_INIT") and (rt.is_ant() or rt.env_truthy("CLAUDE_CODE_NEW_INIT"))


async def _init_get_prompt(args: str, rt: CommandRuntime) -> list[ContentBlock]:
    # maybeMarkProjectOnboardingComplete() is a side effect with no headless
    # equivalent; the runtime hook is a no-op by default.
    rt.open_dialog("init:onboarding-complete")
    template = "init_new.md" if _new_init_enabled(rt) else "init_old.md"
    return [text_block(_load_prompt(template))]


def init() -> PromptCommand:
    return PromptCommand(
        name="init",
        description="Initialize a new CLAUDE.md file with codebase documentation",
        description_fn=lambda rt: (
            "Initialize new CLAUDE.md file(s) and optional skills/hooks with codebase documentation"
            if _new_init_enabled(rt)
            else "Initialize a new CLAUDE.md file with codebase documentation"
        ),
        progress_message="analyzing your codebase",
        content_length=0,
        source="builtin",
        get_prompt_fn=_init_get_prompt,
    )


# ---------------------------------------------------------------------------
# /init-verifiers  (init-verifiers.ts)
# ---------------------------------------------------------------------------


async def _init_verifiers_get_prompt(args: str, rt: CommandRuntime) -> list[ContentBlock]:
    return [text_block(_load_prompt("init_verifiers.md"))]


def init_verifiers() -> PromptCommand:
    return PromptCommand(
        name="init-verifiers",
        description="Create verifier skill(s) for automated verification of code changes",
        progress_message="analyzing your project and creating verifier skills",
        content_length=0,
        source="builtin",
        get_prompt_fn=_init_verifiers_get_prompt,
    )


# ---------------------------------------------------------------------------
# /security-review  (security-review.ts, via createMovedToPluginCommand)
# ---------------------------------------------------------------------------


async def _security_review_private_prompt(args: str, rt: CommandRuntime) -> list[ContentBlock]:
    markdown = _load_prompt("security_review.md")
    frontmatter, content = parse_frontmatter(markdown)
    # allowed tools parsed (used to widen shell permissions upstream); kept for
    # parity even though the default runtime permits all shell commands.
    _allowed_tools = parse_slash_command_tools(frontmatter.get("allowed-tools"))
    processed = await execute_shell_commands_in_prompt(content, rt, "security-review")
    return [text_block(processed)]


def security_review() -> PromptCommand:
    return create_moved_to_plugin_command(
        name="security-review",
        description="Complete a security review of the pending changes on the current branch",
        progress_message="analyzing code changes for security risks",
        plugin_name="security-review",
        plugin_command="security-review",
        get_prompt_while_marketplace_is_private=_security_review_private_prompt,
    )


# ---------------------------------------------------------------------------
# /statusline  (statusline.tsx)
# ---------------------------------------------------------------------------

_AGENT_TOOL_NAME = "Task"  # AGENT_TOOL_NAME constant


async def _statusline_get_prompt(args: str, rt: CommandRuntime) -> list[ContentBlock]:
    prompt = args.strip() or "Configure my statusLine from my shell PS1 configuration"
    return [
        text_block(
            f'Create an {_AGENT_TOOL_NAME} with subagent_type "statusline-setup" and the prompt "{prompt}"'
        )
    ]


def statusline() -> PromptCommand:
    return PromptCommand(
        name="statusline",
        description="Set up Claude Code's status line UI",
        progress_message="setting up statusLine",
        allowed_tools=[_AGENT_TOOL_NAME, "Read(~/**)", "Edit(~/.claude/settings.json)"],
        content_length=0,
        source="builtin",
        disable_non_interactive=True,
        get_prompt_fn=_statusline_get_prompt,
    )


# ---------------------------------------------------------------------------
# /insights  (insights.ts)
# ---------------------------------------------------------------------------
# The upstream getPromptForCommand runs a large local pipeline (generateUsageReport)
# that scans session logs and calls a model to extract facets. That pipeline is
# CLI-runtime-bound; the port keeps the command's contract — turn report data into
# the final model prompt — and sources the report from the runtime.


async def _insights_get_prompt(args: str, rt: CommandRuntime) -> list[ContentBlock]:
    report = getattr(rt, "generate_usage_report", None)
    if report is None:
        # No report backend wired up: emit the same shape with empty data so the
        # command is still exercisable headlessly.
        report_url = "file:///dev/null"
        insights_json = "{}"
        upload_hint = ""
    else:
        data = await report(collect_remote="--homespaces" in (args or ""))
        report_url = data.get("report_url", "file:///dev/null")
        insights_json = data.get("insights_json", "{}")
        upload_hint = data.get("upload_hint", "")

    return [
        text_block(
            f"""The user just ran /insights to generate a usage report analyzing their Claude Code sessions.

Here is the full insights data:
{insights_json}

Report URL: {report_url}

Now output the following message exactly:

<message>
Your shareable insights report is ready:
{report_url}{upload_hint}

Want to dig into any section or try one of the suggestions?
</message>"""
        )
    ]


def insights() -> PromptCommand:
    return PromptCommand(
        name="insights",
        description="Generate a report analyzing your Claude Code sessions",
        progress_message="analyzing your sessions",
        content_length=0,
        source="builtin",
        get_prompt_fn=_insights_get_prompt,
    )


COMMANDS = [
    commit(),
    commit_push_pr(),
    review(),
    pr_comments(),
    init(),
    init_verifiers(),
    security_review(),
    statusline(),
    insights(),
]
