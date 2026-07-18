"""``type: 'local-jsx'`` commands — ported from free-code's interactive slash commands.

Upstream these render an Ink (React-in-terminal) element and finish by calling
``onDone(result, options)``. There is no terminal here, so each port keeps the
command's exact metadata (name, aliases, description, argumentHint, availability,
isEnabled/isHidden, dynamic ``get description()``) and performs its non-UI intent
through :class:`~free_code_commands.runtime.CommandRuntime`, returning a
:class:`~free_code_commands.types.JsxOutcome` that records which dialog would have
opened. That is enough to dispatch, gate, and describe every command headlessly —
which is what the PoC suite verifies.

Each entry's ``# <name>`` comment names the upstream ``src/commands/<path>`` file.
The interactive twins ``context`` and ``extra-usage`` share a name with their
non-interactive ``local`` counterparts in :mod:`.local_commands`; the registry
keeps both and resolves by enablement (see ``CommandRegistry.resolve``).
"""

from __future__ import annotations

from ..registry import _local_jsx_command, _metadata_catalog
from ..runtime import CommandRuntime
from ..types import JsxOutcome

# Terminals that natively support CSI u / Kitty keyboard protocol — /terminal-setup
# hides itself on these (port of NATIVE_CSIU_TERMINALS in terminalSetup/index.ts).
_NATIVE_CSIU_TERMINALS = {
    "ghostty": "Ghostty",
    "kitty": "Kitty",
    "iTerm.app": "iTerm2",
    "WezTerm": "WezTerm",
}


def _dialog(name: str):
    """Build a headless ``call`` that records opening the ``name`` dialog.

    Mirrors the TS command's ``load()`` → render step: no UI here, so we route the
    intent through ``rt.open_dialog`` (recorded by ``RecordingRuntime``) and report
    it back via :class:`JsxOutcome`.
    """

    def _call(args: str, rt: CommandRuntime) -> JsxOutcome:
        rt.open_dialog(name, args=args)
        return JsxOutcome(dialog=name)

    return _call


def _jsx(name: str, description: str, **kw):
    """Construct a local-jsx command with the default dialog-opening ``call``."""

    kw.setdefault("call_fn", _dialog(name))
    return _local_jsx_command(name=name, description=description, **kw)


def _mcp_summary(rows: list[dict]) -> str:
    enabled = sum(bool(row.get("enabled")) for row in rows)
    selected = sum(bool(row.get("selected")) for row in rows)
    reachable = sum(row.get("probe_status") == "reachable" for row in rows)
    failed = sum(row.get("probe_status") == "failed" for row in rows)
    return (
        f"MCP servers: {len(rows)} configured, {enabled} enabled, "
        f"{selected} selected, {reachable} reachable, {failed} failed."
    )


async def _mcp_call(args: str, rt: CommandRuntime) -> JsxOutcome:
    """Headless port of free-code's MCP action dispatcher."""

    raw = (args or "").strip()
    parts = raw.split()
    action = parts[0].lower() if parts else "status"

    def finish(message: str, **data) -> JsxOutcome:
        payload = {"args": args, "action": action, **data}
        rt.open_dialog("mcp", **payload)
        return JsxOutcome(message=message, dialog="mcp", data=payload)

    if action in {"status", "list"}:
        if len(parts) > 1:
            return finish("Usage: /mcp [reconnect|tools|enable|disable] [server|all]")
        rows = rt.mcp_list()
        return finish(_mcp_summary(rows), servers=rows)

    if action not in {"reconnect", "tools", "enable", "disable"}:
        return finish(
            f"Unknown MCP action: {action}. "
            "Use status, reconnect, tools, enable, or disable."
        )
    if len(parts) != 2:
        return finish(
            f"Usage: /mcp {action} <server"
            + ("|all>" if action in {"enable", "disable"} else ">")
        )

    target = parts[1]
    try:
        if action == "reconnect":
            result = await rt.mcp_probe(target)
            status = result.get("status", "failed")
            count = len(result.get("tool_names", ()))
            message = f"{target}: {status}"
            if status == "reachable":
                message += f" ({count} tools)"
            elif result.get("error"):
                message += f" ({result['error']})"
            return finish(message, server=target, probe=result)

        if action == "tools":
            names = rt.mcp_tool_names(target)
            message = (
                f"{target}: {len(names)} tools"
                if names
                else f"{target}: no known tools; run reconnect to probe."
            )
            return finish(message, server=target, tools=names)

        enabled = action == "enable"
        result = rt.mcp_set_enabled(target, enabled)
        changed = tuple(result.get("changed_keys", ()))
        state = "enabled" if enabled else "disabled"
        if changed:
            message = f"{target}: {state} ({len(changed)} changed)"
        else:
            message = f"{target}: already {state}"
        return finish(message, server=target, mutation=result)
    except KeyError:
        return finish(f"Unknown MCP server: {target}", server=target)


_DEFINITIONS = (
    # add-dir/index.ts
    _jsx("add-dir", "Add a new working directory", argument_hint="<path>"),
    # agents/index.ts
    _jsx("agents", "Manage agent configurations"),
    # branch/index.ts — aliases ['fork'] only when the FORK_SUBAGENT feature is
    # off (which is the default); when /fork is registered the alias is dropped.
    _jsx(
        "branch",
        "Create a branch of the current conversation at this point",
        aliases=["fork"],
        argument_hint="[name]",
    ),
    # btw/index.ts
    _jsx(
        "btw",
        "Ask a quick side question without interrupting the main conversation",
        immediate=True,
        argument_hint="<question>",
    ),
    # chrome/index.ts
    _jsx(
        "chrome",
        "Claude in Chrome (Beta) settings",
        availability=["claude-ai"],
        is_enabled_fn=lambda rt: not rt.is_non_interactive(),
    ),
    # color/index.ts
    _jsx(
        "color",
        "Set the prompt bar color for this session",
        immediate=True,
        argument_hint="<color|default>",
    ),
    # config/index.ts
    _jsx("config", "Open config panel", aliases=["settings"]),
    # copy/index.ts
    _jsx(
        "copy",
        "Copy Claude's last response to clipboard (or /copy N for the Nth-latest)",
    ),
    # desktop/index.ts
    _jsx(
        "desktop",
        "Continue the current session in Claude Desktop",
        aliases=["app"],
        availability=["claude-ai"],
        is_enabled_fn=lambda rt: rt.is_supported_desktop_platform(),
        is_hidden_fn=lambda rt: not rt.is_supported_desktop_platform(),
    ),
    # context/index.ts (interactive twin of the local `context`)
    _jsx(
        "context",
        "Visualize current context usage as a colored grid",
        is_enabled_fn=lambda rt: not rt.is_non_interactive(),
    ),
    # diff/index.ts
    _jsx("diff", "View uncommitted changes and per-turn diffs"),
    # doctor/index.ts
    _jsx(
        "doctor",
        "Diagnose and verify your Claude Code installation and settings",
        is_enabled_fn=lambda rt: not rt.env_truthy("DISABLE_DOCTOR_COMMAND"),
    ),
    # effort/index.ts
    _jsx(
        "effort",
        "Set effort level for model usage",
        argument_hint="[low|medium|high|max|auto]",
    ),
    # exit/index.ts
    _jsx("exit", "Exit the REPL", aliases=["quit"], immediate=True),
    # fast/index.ts — dynamic description + gated on fast mode availability
    _jsx(
        "fast",
        "Toggle fast mode",
        availability=["claude-ai", "console"],
        argument_hint="[on|off]",
        is_enabled_fn=lambda rt: rt.fast_mode_enabled(),
        is_hidden_fn=lambda rt: not rt.fast_mode_enabled(),
        description_fn=lambda rt: (
            f"Toggle fast mode ({rt.fast_mode_model_display()} only)"
        ),
    ),
    # feedback/index.ts — aliases ['bug']; disabled on 3P back ends / opt-outs
    _jsx(
        "feedback",
        "Submit feedback about Claude Code",
        aliases=["bug"],
        argument_hint="[report]",
        is_enabled_fn=lambda rt: (
            not (
                rt.env_truthy("CLAUDE_CODE_USE_BEDROCK")
                or rt.env_truthy("CLAUDE_CODE_USE_VERTEX")
                or rt.env_truthy("CLAUDE_CODE_USE_FOUNDRY")
                or rt.env_truthy("DISABLE_FEEDBACK_COMMAND")
                or rt.env_truthy("DISABLE_BUG_COMMAND")
                or rt.is_ant()
                or not rt.policy_allowed("allow_product_feedback")
            )
        ),
    ),
    # help/index.ts
    _jsx("help", "Show help and available commands"),
    # hooks/index.ts
    _jsx("hooks", "View hook configurations for tool events", immediate=True),
    # ide/index.ts
    _jsx("ide", "Manage IDE integrations and show status", argument_hint="[open]"),
    # install-github-app/index.ts
    _jsx(
        "install-github-app",
        "Set up Claude GitHub Actions for a repository",
        availability=["claude-ai", "console"],
        is_enabled_fn=lambda rt: (
            not rt.env_truthy("DISABLE_INSTALL_GITHUB_APP_COMMAND")
        ),
    ),
    # mcp/index.ts
    _jsx(
        "mcp",
        "Manage MCP servers",
        immediate=True,
        argument_hint="[enable|disable [server-name]]",
        call_fn=_mcp_call,
    ),
    # memory/index.ts
    _jsx("memory", "Edit Claude memory files"),
    # mobile/index.ts
    _jsx(
        "mobile",
        "Show QR code to download the Claude mobile app",
        aliases=["ios", "android"],
    ),
    # model/index.ts — dynamic description shows the current model
    _jsx(
        "model",
        "Set the AI model for Claude Code",
        argument_hint="[model]",
        description_fn=lambda rt: (
            f"Set the AI model for Claude Code (currently {rt.current_model_display()})"
        ),
    ),
    # output-style/index.ts — deprecated, hidden
    _jsx(
        "output-style",
        "Deprecated: use /config to change output style",
        _is_hidden=True,
    ),
    # remote-env/index.ts
    _jsx(
        "remote-env",
        "Configure the default remote environment for teleport sessions",
        is_enabled_fn=lambda rt: (
            rt.is_claude_ai_subscriber() and rt.policy_allowed("allow_remote_sessions")
        ),
        is_hidden_fn=lambda rt: (
            not rt.is_claude_ai_subscriber()
            or not rt.policy_allowed("allow_remote_sessions")
        ),
    ),
    # plugin/index.tsx
    _jsx(
        "plugin",
        "Manage Claude Code plugins",
        aliases=["plugins", "marketplace"],
        immediate=True,
    ),
    # rename/index.ts
    _jsx(
        "rename",
        "Rename the current conversation",
        immediate=True,
        argument_hint="[name]",
    ),
    # resume/index.ts
    _jsx(
        "resume",
        "Resume a previous conversation",
        aliases=["continue"],
        argument_hint="[conversation id or search term]",
    ),
    # session/index.ts
    _jsx(
        "session",
        "Show remote session URL and QR code",
        aliases=["remote"],
        is_enabled_fn=lambda rt: rt.is_remote_mode(),
        is_hidden_fn=lambda rt: not rt.is_remote_mode(),
    ),
    # skills/index.ts
    _jsx("skills", "List available skills"),
    # stats/index.ts
    _jsx("stats", "Show your Claude Code usage statistics and activity"),
    # status/index.ts
    _jsx(
        "status",
        "Show Claude Code status including version, model, account, API connectivity, and tool statuses",
        immediate=True,
    ),
    # tag/index.ts — ant-only
    _jsx(
        "tag",
        "Toggle a searchable tag on the current session",
        argument_hint="<tag-name>",
        is_enabled_fn=lambda rt: rt.is_ant(),
    ),
    # theme/index.ts
    _jsx("theme", "Change the theme"),
    # feedback registered above; review/ultrareview:
    # review/ultrareviewCommand.tsx — gated on the bughunter GB config
    _jsx(
        "ultrareview",
        "~10–20 min · Finds and verifies bugs in your branch. Runs in Claude Code on "
        "the web. See https://code.claude.com/docs/en/claude-code-on-the-web",
        is_enabled_fn=lambda rt: rt.ultrareview_enabled(),
    ),
    # terminalSetup/index.ts — dynamic description + hides on native CSI-u terminals
    _jsx(
        "terminal-setup",
        "Install Shift+Enter key binding for newlines",
        is_hidden_fn=lambda rt: (
            rt.terminal() is not None and rt.terminal() in _NATIVE_CSIU_TERMINALS
        ),
        description_fn=lambda rt: (
            "Enable Option+Enter key binding for newlines and visual bell"
            if rt.terminal() == "Apple_Terminal"
            else "Install Shift+Enter key binding for newlines"
        ),
    ),
    # upgrade/index.ts
    _jsx(
        "upgrade",
        "Upgrade to Max for higher rate limits and more Opus",
        availability=["claude-ai"],
        is_enabled_fn=lambda rt: (
            not rt.env_truthy("DISABLE_UPGRADE_COMMAND")
            and rt.subscription_type() != "enterprise"
        ),
    ),
    # extra-usage/index.ts (interactive twin of the local `extra-usage`)
    _jsx(
        "extra-usage",
        "Configure extra usage to keep working when limits are hit",
        is_enabled_fn=lambda rt: (
            rt.is_overage_provisioning_allowed()
            and not rt.env_truthy("DISABLE_EXTRA_USAGE_COMMAND")
            and not rt.is_non_interactive()
        ),
    ),
    # rate-limit-options/index.ts — internal, hidden
    _jsx(
        "rate-limit-options",
        "Show options when rate limit is reached",
        _is_hidden=True,
        is_enabled_fn=lambda rt: rt.is_claude_ai_subscriber(),
    ),
    # usage/index.ts
    _jsx("usage", "Show plan usage limits", availability=["claude-ai"]),
    # thinkback/index.ts — note the user-facing name is 'think-back'
    _jsx(
        "think-back",
        "Your 2025 Claude Code Year in Review",
        is_enabled_fn=lambda rt: rt.feature("tengu_thinkback"),
    ),
    # permissions/index.ts
    _jsx(
        "permissions",
        "Manage allow & deny tool permission rules",
        aliases=["allowed-tools"],
    ),
    # plan/index.ts
    _jsx(
        "plan",
        "Enable plan mode or view the current session plan",
        argument_hint="[open|<description>]",
    ),
    # privacy-settings/index.ts
    _jsx(
        "privacy-settings",
        "View and update your privacy settings",
        is_enabled_fn=lambda rt: rt.is_consumer_subscriber(),
    ),
    # export/index.ts
    _jsx(
        "export",
        "Export the current conversation to a file or clipboard",
        argument_hint="[filename]",
    ),
    # sandbox-toggle/index.ts — dynamic status description, hidden off-platform
    _jsx(
        "sandbox",
        'exclude "command pattern"',
        argument_hint='exclude "command pattern"',
        immediate=True,
        is_hidden_fn=lambda rt: not rt.sandbox_supported(),
        description_fn=lambda rt: rt.sandbox_status(),
    ),
    # logout/index.ts — only registered when not on 3P services (see _all.py)
    _jsx(
        "logout",
        "Sign out from your Anthropic account",
        is_enabled_fn=lambda rt: (
            not rt.is_using_3p_services()
            and not rt.env_truthy("DISABLE_LOGOUT_COMMAND")
        ),
    ),
    # login/index.ts — dynamic description; only registered when not on 3P services
    _jsx(
        "login",
        "Sign in with your Anthropic account",
        is_enabled_fn=lambda rt: (
            not rt.is_using_3p_services() and not rt.env_truthy("DISABLE_LOGIN_COMMAND")
        ),
        description_fn=lambda rt: (
            "Switch Anthropic accounts"
            if rt.has_anthropic_api_key_auth()
            else "Sign in with your Anthropic account"
        ),
    ),
    # passes/index.ts — dynamic description + eligibility-gated visibility
    _jsx(
        "passes",
        "Share a free week of Claude Code with friends",
        is_hidden_fn=lambda rt: not rt.passes_eligible(),
        description_fn=lambda rt: (
            "Share a free week of Claude Code with friends and earn extra usage"
            if rt.passes_has_reward()
            else "Share a free week of Claude Code with friends"
        ),
    ),
    # tasks/index.ts
    _jsx("tasks", "List and manage background tasks", aliases=["bashes"]),
)

COMMANDS = _metadata_catalog(_DEFINITIONS)
