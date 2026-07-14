"""``type: 'local'`` commands — ported from free-code's local slash commands.

A local command computes headlessly and returns a
:class:`~free_code_commands.types.LocalCommandResult`. Self-contained logic
(cost, version, vim, files, advisor's control flow, bridge-kick's dispatch) is
translated directly; genuinely runtime-bound behaviour (compaction, context
analysis, plugin refresh, voice, browser opening) is delegated through the
:class:`~free_code_commands.runtime.CommandRuntime`.

Sources:
    cost/cost.ts, version.ts, clear/clear.ts, compact/compact.ts,
    context/context-noninteractive.ts, files/files.ts, heapdump/heapdump.ts,
    release-notes/release-notes.ts, reload-plugins/reload-plugins.ts,
    rewind/rewind.ts, stickers/stickers.ts, thinkback-play/thinkback-play.ts,
    vim/vim.ts, voice/voice.ts, keybindings/keybindings.ts,
    install-slack-app/install-slack-app.ts, advisor.ts, bridge-kick.ts,
    extra-usage/extra-usage-noninteractive.ts
"""

from __future__ import annotations

import re

from ..runtime import CommandRuntime
from ..types import LocalCommand, LocalCommandResult


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else noun + "s"


# ---------------------------------------------------------------------------
# /cost  (cost/cost.ts)
# ---------------------------------------------------------------------------


async def _cost_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    if rt.is_claude_ai_subscriber():
        if rt.is_using_overage():
            value = (
                "You are currently using your overages to power your Claude Code usage. "
                "We will automatically switch you back to your subscription rate limits when they reset"
            )
        else:
            value = "You are currently using your subscription to power your Claude Code usage"
        if rt.is_ant():
            value += f"\n\n[ANT-ONLY] Showing cost anyway:\n {rt.format_total_cost()}"
        return LocalCommandResult.text(value)
    return LocalCommandResult.text(rt.format_total_cost())


def cost() -> LocalCommand:
    return LocalCommand(
        name="cost",
        description="Show the total cost and duration of the current session",
        supports_non_interactive=True,
        # isHidden: ants always see it; other subscribers don't.
        is_hidden_fn=lambda rt: False if rt.is_ant() else rt.is_claude_ai_subscriber(),
        call_fn=_cost_call,
    )


# ---------------------------------------------------------------------------
# /version  (version.ts)
# ---------------------------------------------------------------------------


async def _version_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    bt = rt.build_time()
    value = f"{rt.version()} (built {bt})" if bt else rt.version()
    return LocalCommandResult.text(value)


def version() -> LocalCommand:
    return LocalCommand(
        name="version",
        description="Print the version this session is running (not what autoupdate downloaded)",
        is_enabled_fn=lambda rt: rt.is_ant(),
        supports_non_interactive=True,
        call_fn=_version_call,
    )


# ---------------------------------------------------------------------------
# /clear  (clear/clear.ts)
# ---------------------------------------------------------------------------


async def _clear_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    rt.clear_conversation()
    return LocalCommandResult.text("")


def clear() -> LocalCommand:
    return LocalCommand(
        name="clear",
        description="Clear conversation history and free up context",
        aliases=["reset", "new"],
        supports_non_interactive=False,
        call_fn=_clear_call,
    )


# ---------------------------------------------------------------------------
# /compact  (compact/compact.ts)
# ---------------------------------------------------------------------------


async def _compact_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    messages = rt.messages()
    if not messages:
        raise ValueError("No messages to compact")
    custom_instructions = args.strip()
    result = rt.compact_conversation(custom_instructions)
    display = None
    if isinstance(result, dict):
        display = result.get("userDisplayMessage")
    return LocalCommandResult.compact(result, display_text=display)


def compact() -> LocalCommand:
    return LocalCommand(
        name="compact",
        description=(
            "Clear conversation history but keep a summary in context. "
            "Optional: /compact [instructions for summarization]"
        ),
        argument_hint="<optional custom summarization instructions>",
        is_enabled_fn=lambda rt: not rt.env_truthy("DISABLE_COMPACT"),
        supports_non_interactive=True,
        call_fn=_compact_call,
    )


# ---------------------------------------------------------------------------
# /context (non-interactive)  (context/context-noninteractive.ts)
# ---------------------------------------------------------------------------


async def _context_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    return LocalCommandResult.text(rt.context_report())


def context_noninteractive() -> LocalCommand:
    return LocalCommand(
        name="context",
        description="Show current context usage",
        supports_non_interactive=True,
        is_enabled_fn=lambda rt: rt.is_non_interactive(),
        is_hidden_fn=lambda rt: not rt.is_non_interactive(),
        call_fn=_context_call,
    )


# ---------------------------------------------------------------------------
# /files  (files/files.ts)
# ---------------------------------------------------------------------------


async def _files_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    files = rt.context_files()
    if not files:
        return LocalCommandResult.text("No files in context")
    return LocalCommandResult.text("Files in context:\n" + "\n".join(files))


def files() -> LocalCommand:
    return LocalCommand(
        name="files",
        description="List all files currently in context",
        is_enabled_fn=lambda rt: rt.is_ant(),
        supports_non_interactive=True,
        call_fn=_files_call,
    )


# ---------------------------------------------------------------------------
# /heapdump  (heapdump/heapdump.ts)
# ---------------------------------------------------------------------------


async def _heapdump_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    result = rt.perform_heap_dump()
    if not result.get("success"):
        return LocalCommandResult.text(f"Failed to create heap dump: {result.get('error')}")
    return LocalCommandResult.text(f"{result.get('heap_path')}\n{result.get('diag_path')}")


def heapdump() -> LocalCommand:
    return LocalCommand(
        name="heapdump",
        description="Dump the JS heap to ~/Desktop",
        _is_hidden=True,
        supports_non_interactive=True,
        call_fn=_heapdump_call,
    )


# ---------------------------------------------------------------------------
# /release-notes  (release-notes/release-notes.ts)
# ---------------------------------------------------------------------------


def _format_release_notes(notes: list[tuple[str, list[str]]]) -> str:
    blocks = []
    for version_, items in notes:
        header = f"Version {version_}:"
        bullets = "\n".join(f"· {n}" for n in items)
        blocks.append(f"{header}\n{bullets}")
    return "\n\n".join(blocks)


async def _release_notes_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    groups = rt.release_note_groups()
    if groups:
        return LocalCommandResult.text(_format_release_notes(groups))
    return LocalCommandResult.text(f"See the full changelog at: {rt.changelog_url}")


def release_notes() -> LocalCommand:
    return LocalCommand(
        name="release-notes",
        description="View release notes",
        supports_non_interactive=True,
        call_fn=_release_notes_call,
    )


# ---------------------------------------------------------------------------
# /reload-plugins  (reload-plugins/reload-plugins.ts)
# ---------------------------------------------------------------------------


async def _reload_plugins_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    r = rt.refresh_active_plugins()

    def n(count: int, noun: str) -> str:
        return f"{count} {_plural(count, noun)}"

    parts = [
        n(r.get("enabled_count", 0), "plugin"),
        n(r.get("command_count", 0), "skill"),
        n(r.get("agent_count", 0), "agent"),
        n(r.get("hook_count", 0), "hook"),
        n(r.get("mcp_count", 0), "plugin MCP server"),
        n(r.get("lsp_count", 0), "plugin LSP server"),
    ]
    msg = f"Reloaded: {' · '.join(parts)}"
    errors = r.get("error_count", 0)
    if errors > 0:
        msg += f"\n{n(errors, 'error')} during load. Run /doctor for details."
    return LocalCommandResult.text(msg)


def reload_plugins() -> LocalCommand:
    return LocalCommand(
        name="reload-plugins",
        description="Activate pending plugin changes in the current session",
        supports_non_interactive=False,
        call_fn=_reload_plugins_call,
    )


# ---------------------------------------------------------------------------
# /rewind  (rewind/rewind.ts)
# ---------------------------------------------------------------------------


async def _rewind_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    rt.open_message_selector()
    return LocalCommandResult.skip()


def rewind() -> LocalCommand:
    return LocalCommand(
        name="rewind",
        description="Restore the code and/or conversation to a previous point",
        aliases=["checkpoint"],
        argument_hint="",
        supports_non_interactive=False,
        call_fn=_rewind_call,
    )


# ---------------------------------------------------------------------------
# /stickers  (stickers/stickers.ts)
# ---------------------------------------------------------------------------


async def _stickers_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    url = "https://www.stickermule.com/claudecode"
    if rt.open_browser(url):
        return LocalCommandResult.text("Opening sticker page in browser…")
    return LocalCommandResult.text(f"Failed to open browser. Visit: {url}")


def stickers() -> LocalCommand:
    return LocalCommand(
        name="stickers",
        description="Order Claude Code stickers",
        supports_non_interactive=False,
        call_fn=_stickers_call,
    )


# ---------------------------------------------------------------------------
# /install-slack-app  (install-slack-app/install-slack-app.ts)
# ---------------------------------------------------------------------------


async def _install_slack_app_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    url = "https://slack.com/marketplace/A08SF47R6P4-claude"
    if rt.open_browser(url):
        return LocalCommandResult.text("Opening Slack app installation page in browser…")
    return LocalCommandResult.text(f"Couldn't open browser. Visit: {url}")


def install_slack_app() -> LocalCommand:
    return LocalCommand(
        name="install-slack-app",
        description="Install the Claude Slack app",
        availability=["claude-ai"],
        supports_non_interactive=False,
        call_fn=_install_slack_app_call,
    )


# ---------------------------------------------------------------------------
# /thinkback-play  (thinkback-play/thinkback-play.ts)
# ---------------------------------------------------------------------------


async def _thinkback_play_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    return LocalCommandResult.text(rt.play_thinkback())


def thinkback_play() -> LocalCommand:
    return LocalCommand(
        name="thinkback-play",
        description="Play the thinkback animation",
        is_enabled_fn=lambda rt: rt.feature("tengu_thinkback"),
        _is_hidden=True,
        supports_non_interactive=False,
        call_fn=_thinkback_play_call,
    )


# ---------------------------------------------------------------------------
# /vim  (vim/vim.ts)
# ---------------------------------------------------------------------------


async def _vim_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    current = rt.editor_mode()
    if current == "emacs":  # backward-compat: treat emacs as normal
        current = "normal"
    new_mode = "vim" if current == "normal" else "normal"
    rt.set_editor_mode(new_mode)
    tail = (
        "Use Escape key to toggle between INSERT and NORMAL modes."
        if new_mode == "vim"
        else "Using standard (readline) keyboard bindings."
    )
    return LocalCommandResult.text(f"Editor mode set to {new_mode}. {tail}")


def vim() -> LocalCommand:
    return LocalCommand(
        name="vim",
        description="Toggle between Vim and Normal editing modes",
        supports_non_interactive=False,
        call_fn=_vim_call,
    )


# ---------------------------------------------------------------------------
# /voice  (voice/voice.ts)
# ---------------------------------------------------------------------------


async def _voice_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    if not rt.is_voice_mode_enabled():
        if not rt.is_anthropic_auth_enabled():
            return LocalCommandResult.text(
                "Voice mode requires a Claude.ai account. Please run /login to sign in."
            )
        return LocalCommandResult.text("Voice mode is not available.")

    if rt.voice_enabled():
        ok, error = rt.set_voice_enabled(False)
        if not ok:
            return LocalCommandResult.text(
                "Failed to update settings. Check your settings file for syntax errors."
            )
        return LocalCommandResult.text("Voice mode disabled.")

    available, reason = rt.recording_available()
    if not available:
        return LocalCommandResult.text(reason or "Voice mode is not available in this environment.")

    if not rt.voice_stream_available():
        return LocalCommandResult.text(
            "Voice mode requires a Claude.ai account. Please run /login to sign in."
        )

    deps_ok, install_command = rt.voice_dependencies()
    if not deps_ok:
        hint = (
            f"\nInstall audio recording tools? Run: {install_command}"
            if install_command
            else "\nInstall SoX manually for audio recording."
        )
        return LocalCommandResult.text(f"Voice mode requires audio recording tools.{hint}")

    ok, error = rt.set_voice_enabled(True)
    if not ok:
        return LocalCommandResult.text(
            "Failed to update settings. Check your settings file for syntax errors."
        )
    return LocalCommandResult.text("Voice mode enabled.")


def voice() -> LocalCommand:
    return LocalCommand(
        name="voice",
        description="Toggle voice mode",
        availability=["claude-ai"],
        is_enabled_fn=lambda rt: rt.feature("voice_growthbook"),
        is_hidden_fn=lambda rt: not rt.is_voice_mode_enabled(),
        supports_non_interactive=False,
        call_fn=_voice_call,
    )


# ---------------------------------------------------------------------------
# /keybindings  (keybindings/keybindings.ts)
# ---------------------------------------------------------------------------


async def _keybindings_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    if not rt.keybinding_customization_enabled():
        return LocalCommandResult.text(
            "Keybinding customization is not enabled. This feature is currently in preview."
        )
    result = rt.open_keybindings()
    path = result.get("path")
    created = result.get("created")
    editor_error = result.get("editor_error")
    if editor_error:
        verb = "Created" if created else "Opened"
        return LocalCommandResult.text(
            f"{verb} {path}. Could not open in editor: {editor_error}"
        )
    if created:
        return LocalCommandResult.text(
            f"Created {path} with template. Opened in your editor."
        )
    return LocalCommandResult.text(f"Opened {path} in your editor.")


def keybindings() -> LocalCommand:
    return LocalCommand(
        name="keybindings",
        description="Open or create your keybindings configuration file",
        is_enabled_fn=lambda rt: rt.keybinding_customization_enabled(),
        supports_non_interactive=False,
        call_fn=_keybindings_call,
    )


# ---------------------------------------------------------------------------
# /advisor  (advisor.ts)
# ---------------------------------------------------------------------------


async def _advisor_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    arg = args.strip().lower()
    base_model = rt.parse_model(rt.main_loop_model())

    if not arg:
        current = rt.advisor_model()
        if not current:
            return LocalCommandResult.text(
                'Advisor: not set\nUse "/advisor <model>" to enable (e.g. "/advisor opus").'
            )
        if not rt.model_supports_advisor(base_model):
            return LocalCommandResult.text(
                f"Advisor: {current} (inactive)\n"
                f"The current model ({base_model}) does not support advisors."
            )
        return LocalCommandResult.text(
            f'Advisor: {current}\nUse "/advisor unset" to disable or "/advisor <model>" to change.'
        )

    if arg in ("unset", "off"):
        prev = rt.advisor_model()
        rt.set_advisor_model(None)
        return LocalCommandResult.text(
            f"Advisor disabled (was {prev})." if prev else "Advisor already unset."
        )

    normalized_model = rt.normalize_model(arg)
    resolved_model = rt.parse_model(arg)
    valid, error = rt.validate_model(resolved_model)
    if not valid:
        return LocalCommandResult.text(
            f"Invalid advisor model: {error}"
            if error
            else f"Unknown model: {arg} ({resolved_model})"
        )

    if not rt.is_valid_advisor_model(resolved_model):
        return LocalCommandResult.text(
            f"The model {arg} ({resolved_model}) cannot be used as an advisor"
        )

    rt.set_advisor_model(normalized_model)

    if not rt.model_supports_advisor(base_model):
        return LocalCommandResult.text(
            f"Advisor set to {normalized_model}.\n"
            f"Note: Your current model ({base_model}) does not support advisors. "
            f"Switch to a supported model to use the advisor."
        )
    return LocalCommandResult.text(f"Advisor set to {normalized_model}.")


def advisor() -> LocalCommand:
    return LocalCommand(
        name="advisor",
        description="Configure the advisor model",
        argument_hint="[<model>|off]",
        is_enabled_fn=lambda rt: rt.can_configure_advisor(),
        is_hidden_fn=lambda rt: not rt.can_configure_advisor(),
        supports_non_interactive=True,
        call_fn=_advisor_call,
    )


# ---------------------------------------------------------------------------
# /bridge-kick  (bridge-kick.ts) — ant-only fault injection
# ---------------------------------------------------------------------------

_BRIDGE_USAGE = """/bridge-kick <subcommand>
  close <code>              fire ws_closed with the given code (e.g. 1002)
  poll <status> [type]      next poll throws BridgeFatalError(status, type)
  poll transient            next poll throws axios-style rejection (5xx/net)
  register fail [N]         next N registers transient-fail (default 1)
  register fatal            next register 403s (terminal)
  reconnect-session fail    next POST /bridge/reconnect fails
  heartbeat <status>        next heartbeat throws BridgeFatalError(status)
  reconnect                 call reconnectEnvironmentWithSession directly
  status                    print bridge state"""


def _is_finite_number(s: str) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


async def _bridge_kick_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    if not rt.bridge_debug_available():
        return LocalCommandResult.text(
            "No bridge debug handle registered. Remote Control must be connected (USER_TYPE=ant)."
        )

    parts = re.split(r"\s+", args.strip()) if args.strip() else [""]
    sub = parts[0]
    a = parts[1] if len(parts) > 1 else None
    b = parts[2] if len(parts) > 2 else None

    if sub == "close":
        if a is None or not _is_finite_number(a):
            return LocalCommandResult.text(f"close: need a numeric code\n{_BRIDGE_USAGE}")
        rt.bridge_kick("close", a, b)
        code = int(float(a))
        return LocalCommandResult.text(
            f"Fired transport close({code}). Watch debug.log for [bridge:repl] recovery."
        )

    if sub == "poll":
        if a == "transient":
            rt.bridge_kick("poll", a, b)
            return LocalCommandResult.text(
                "Next poll will throw a transient (axios rejection). Poll loop woken."
            )
        if a is None or not _is_finite_number(a):
            return LocalCommandResult.text(
                f"poll: need 'transient' or a status code\n{_BRIDGE_USAGE}"
            )
        status = int(float(a))
        error_type = b if b is not None else (
            "not_found_error" if status == 404 else "authentication_error"
        )
        rt.bridge_kick("poll", a, error_type)
        return LocalCommandResult.text(
            f"Next poll will throw BridgeFatalError({status}, {error_type}). Poll loop woken."
        )

    if sub == "register":
        if a == "fatal":
            rt.bridge_kick("register", a, b)
            return LocalCommandResult.text(
                "Next registerBridgeEnvironment will 403. Trigger with close/reconnect."
            )
        n = int(float(b)) if (b and _is_finite_number(b)) else 1
        rt.bridge_kick("register", a, str(n))
        return LocalCommandResult.text(
            f"Next {n} registerBridgeEnvironment call(s) will transient-fail. "
            f"Trigger with close/reconnect."
        )

    if sub == "reconnect-session":
        rt.bridge_kick("reconnect-session", a, b)
        return LocalCommandResult.text(
            "Next 2 POST /bridge/reconnect calls will 404. "
            "doReconnect Strategy 1 falls through to Strategy 2."
        )

    if sub == "heartbeat":
        status = int(float(a)) if (a and _is_finite_number(a)) else 401
        rt.bridge_kick("heartbeat", str(status), b)
        return LocalCommandResult.text(
            f"Next heartbeat will {status}. Watch for onHeartbeatFatal → work-state teardown."
        )

    if sub == "reconnect":
        rt.bridge_kick("reconnect", a, b)
        return LocalCommandResult.text(
            "Called reconnectEnvironmentWithSession(). Watch debug.log."
        )

    if sub == "status":
        return LocalCommandResult.text(rt.bridge_kick("status", a, b))

    return LocalCommandResult.text(_BRIDGE_USAGE)


def bridge_kick() -> LocalCommand:
    return LocalCommand(
        name="bridge-kick",
        description="Inject bridge failure states for manual recovery testing",
        is_enabled_fn=lambda rt: rt.is_ant(),
        supports_non_interactive=False,
        call_fn=_bridge_kick_call,
    )


# ---------------------------------------------------------------------------
# /extra-usage (non-interactive)  (extra-usage/extra-usage-noninteractive.ts)
# ---------------------------------------------------------------------------


async def _extra_usage_call(args: str, rt: CommandRuntime) -> LocalCommandResult:
    result = rt.run_extra_usage()
    if result.get("type") == "message":
        return LocalCommandResult.text(result.get("value", ""))
    if result.get("opened"):
        return LocalCommandResult.text(
            f"Browser opened to manage extra usage. If it didn't open, visit: {result.get('url')}"
        )
    return LocalCommandResult.text(
        f"Please visit {result.get('url')} to manage extra usage."
    )


def extra_usage_noninteractive() -> LocalCommand:
    return LocalCommand(
        name="extra-usage",
        description="Configure extra usage to keep working when limits are hit",
        supports_non_interactive=True,
        is_enabled_fn=lambda rt: rt.is_overage_provisioning_allowed()
        and not rt.env_truthy("DISABLE_EXTRA_USAGE_COMMAND")
        and rt.is_non_interactive(),
        is_hidden_fn=lambda rt: not rt.is_non_interactive(),
        call_fn=_extra_usage_call,
    )


COMMANDS = [
    cost(),
    version(),
    clear(),
    compact(),
    context_noninteractive(),
    files(),
    heapdump(),
    release_notes(),
    reload_plugins(),
    rewind(),
    stickers(),
    install_slack_app(),
    thinkback_play(),
    vim(),
    voice(),
    keybindings(),
    advisor(),
    bridge_kick(),
    extra_usage_noninteractive(),
]
