"""CommandRuntime — the injected context every ported command runs against.

Upstream, each command receives a ``ToolUseContext`` / ``LocalJSXCommandContext``
and freely reads module-level globals: ``process.env``, auth helpers
(``isClaudeAISubscriber``), git helpers (``getDefaultBranch``), the cost tracker
(``formatTotalCost``), ``bun:bundle`` ``feature()`` flags, conversation
``messages``, and UI callbacks (``setMessages``, ``onChangeAPIKey`` ...).

Porting every one of those globals verbatim would drag in the whole CLI runtime.
Instead we funnel them through a single seam — this class — so commands depend on
an interface, not on ambient state. That is exactly the dependency-injection the
TS ``context`` parameter already is; we just make it explicit and overridable.

:class:`CommandRuntime` ships working defaults (real env + real subprocess shell)
so it is usable as-is. :class:`RecordingRuntime` overrides the side-effecting
methods to record intent instead of performing it, which is what the PoC tests
drive.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ShellResult:
    """Result of a shell interpolation (``!`cmd```). Mirrors BashTool's data shape."""

    stdout: str = ""
    stderr: str = ""
    interrupted: bool = False


@dataclass(frozen=True)
class Attribution:
    """Port of ``getAttributionTexts()`` — commit/PR trailer strings."""

    commit: str = ""
    pr: str = ""


class CommandRuntime:
    """Default runtime. Subclass and override to change environment/behaviour.

    Every method corresponds to a global or context field the TS commands touch.
    Defaults are chosen to be safe and non-interactive.
    """

    # ---- environment (process.env) --------------------------------------
    def env(self, key: str) -> Optional[str]:
        return os.environ.get(key)

    def env_truthy(self, key: str) -> bool:
        """Port of ``isEnvTruthy`` — set and not one of the falsy strings."""

        v = self.env(key)
        if v is None:
            return False
        return v.strip().lower() not in ("", "0", "false", "no", "off")

    def user_type(self) -> Optional[str]:
        return self.env("USER_TYPE")

    def is_ant(self) -> bool:
        return self.user_type() == "ant"

    def platform(self) -> str:
        import sys

        return sys.platform  # 'linux' | 'darwin' | 'win32'-ish

    def arch(self) -> str:
        import platform

        return platform.machine()

    def terminal(self) -> Optional[str]:
        return self.env("TERM_PROGRAM")

    # ---- auth / subscription --------------------------------------------
    def is_claude_ai_subscriber(self) -> bool:
        return False

    def is_consumer_subscriber(self) -> bool:
        return self.is_claude_ai_subscriber()

    def has_anthropic_api_key_auth(self) -> bool:
        return bool(self.env("ANTHROPIC_API_KEY"))

    def subscription_type(self) -> Optional[str]:
        return None  # 'pro' | 'max' | 'team' | 'enterprise'

    def is_overage_provisioning_allowed(self) -> bool:
        return False

    def is_using_overage(self) -> bool:
        return False

    def is_undercover(self) -> bool:
        """Port of ``isUndercover()`` — ant-only "undercover" mode. Default off."""

        return False

    def undercover_instructions(self) -> str:
        """Port of ``getUndercoverInstructions()`` — prepended prefix when undercover."""

        return ""

    def is_using_3p_services(self) -> bool:
        """Port of ``isUsing3PServices()`` — Bedrock/Vertex/Foundry back ends.

        Gates registration of ``/login`` and ``/logout`` in ``commands.ts``.
        """

        return (
            self.env_truthy("CLAUDE_CODE_USE_BEDROCK")
            or self.env_truthy("CLAUDE_CODE_USE_VERTEX")
            or self.env_truthy("CLAUDE_CODE_USE_FOUNDRY")
        )

    # ---- feature flags (bun:bundle feature() + growthbook gates) ---------
    def feature(self, name: str) -> bool:
        """Port of ``feature('FLAG')`` / growthbook gate lookups. Default off."""

        return False

    def policy_allowed(self, policy: str) -> bool:
        return True

    # ---- cost tracker ----------------------------------------------------
    def format_total_cost(self) -> str:
        return "Total cost:            $0.00\nTotal duration (API):  0s\nTotal duration (wall): 0s"

    # ---- version macros --------------------------------------------------
    def version(self) -> str:
        return "0.0.0"

    def build_time(self) -> Optional[str]:
        return None

    # ---- git -------------------------------------------------------------
    async def get_default_branch(self) -> str:
        return "main"

    def attribution_texts(self) -> Attribution:
        return Attribution(commit="", pr="")

    async def get_enhanced_pr_attribution(self) -> Optional[str]:
        return None

    # ---- shell interpolation (BashTool) ---------------------------------
    async def run_shell(self, command: str) -> ShellResult:
        """Execute a shell command for ``!`cmd``` interpolation.

        The default really shells out (bash -lc) with a short timeout, matching
        the CLI's behaviour of inlining live command output into the prompt.
        """

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                return ShellResult(stdout="", stderr="command timed out", interrupted=True)
            return ShellResult(
                stdout=out.decode("utf-8", "replace"),
                stderr=err.decode("utf-8", "replace"),
                interrupted=False,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return ShellResult(stdout="", stderr=str(exc), interrupted=False)

    def can_run_shell(self, command: str) -> bool:
        """Permission gate for shell interpolation (port of hasPermissionsToUseTool).

        Default allows everything; a stricter runtime can deny commands.
        """

        return True

    # ---- conversation state (messages) ----------------------------------
    def messages(self) -> list[Any]:
        return []

    # ---- UI side-effects (local-jsx headless) ---------------------------
    def open_dialog(self, name: str, **data: Any) -> None:
        """Record/perform opening a TUI dialog. No-op by default."""

    def clear_conversation(self) -> None:
        """Port of ``clearConversation`` — start a fresh session."""

    def set_vim_mode(self, enabled: bool) -> None:
        """Toggle vim editing mode."""

    def get_vim_mode(self) -> bool:
        return False

    # ---- capabilities ----------------------------------------------------
    def has_command(self, name: str) -> bool:
        return shutil.which(name) is not None

    def is_non_interactive(self) -> bool:
        """Port of ``getIsNonInteractiveSession()``. Default: interactive."""

        return False

    # ---- /cost helpers ---------------------------------------------------
    # (format_total_cost / is_using_overage declared above)

    # ---- /context, /files ------------------------------------------------
    def context_files(self) -> list[str]:
        """Files currently in context (paths). Port of ``readFileState`` keys."""

        return []

    def context_report(self) -> str:
        """Formatted context-usage report (port of ``formatContextAsMarkdownTable``)."""

        return "Context usage is unavailable in this runtime."

    # ---- /compact --------------------------------------------------------
    def compact_conversation(self, custom_instructions: str) -> Any:
        """Run compaction; returns a CompactionResult-like object. Runtime-bound."""

        return {"summary": "", "userDisplayMessage": None}

    # ---- /heapdump -------------------------------------------------------
    def perform_heap_dump(self) -> dict[str, Any]:
        return {"success": True, "heap_path": "~/Desktop/heap.heapsnapshot", "diag_path": "~/Desktop/diag.json"}

    # ---- /release-notes --------------------------------------------------
    changelog_url = "https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md"

    def release_note_groups(self) -> list[tuple[str, list[str]]]:
        """Recent release-note groups as ``[(version, [notes...])]``."""

        return []

    # ---- /reload-plugins -------------------------------------------------
    def refresh_active_plugins(self) -> dict[str, int]:
        return {
            "enabled_count": 0,
            "command_count": 0,
            "agent_count": 0,
            "hook_count": 0,
            "mcp_count": 0,
            "lsp_count": 0,
            "error_count": 0,
        }

    # ---- /rewind ---------------------------------------------------------
    def open_message_selector(self) -> None:
        self.open_dialog("rewind:message-selector")

    # ---- browser (stickers / install-slack-app / extra-usage) -----------
    def open_browser(self, url: str) -> bool:
        """Open ``url`` in a browser. Default runtime does not actually open."""

        return False

    # ---- /thinkback-play -------------------------------------------------
    def play_thinkback(self) -> str:
        return "Thinkback plugin not installed. Run /think-back first to install it."

    # ---- /vim ------------------------------------------------------------
    def editor_mode(self) -> str:
        return "vim" if self.get_vim_mode() else "normal"

    def set_editor_mode(self, mode: str) -> None:
        self.set_vim_mode(mode == "vim")

    # ---- /voice ----------------------------------------------------------
    def is_voice_mode_enabled(self) -> bool:
        return False

    def is_anthropic_auth_enabled(self) -> bool:
        return self.is_claude_ai_subscriber() or self.has_anthropic_api_key_auth()

    def voice_enabled(self) -> bool:
        return False

    def set_voice_enabled(self, enabled: bool) -> tuple[bool, Optional[str]]:
        """Returns ``(ok, error)``."""

        return True, None

    def recording_available(self) -> tuple[bool, Optional[str]]:
        return True, None

    def voice_stream_available(self) -> bool:
        return True

    def voice_dependencies(self) -> tuple[bool, Optional[str]]:
        """Returns ``(available, install_command)``."""

        return True, None

    # ---- /keybindings ----------------------------------------------------
    def keybinding_customization_enabled(self) -> bool:
        return False

    def open_keybindings(self) -> dict[str, Any]:
        """Returns ``{created: bool, path: str, editor_error: str|None}``."""

        return {"created": True, "path": "~/.claude/keybindings.json", "editor_error": None}

    # ---- /advisor --------------------------------------------------------
    def can_configure_advisor(self) -> bool:
        return False

    def main_loop_model(self) -> str:
        return "opus"

    def advisor_model(self) -> Optional[str]:
        return None

    def set_advisor_model(self, model: Optional[str]) -> None:
        pass

    def model_supports_advisor(self, model: str) -> bool:
        return True

    def is_valid_advisor_model(self, model: str) -> bool:
        return True

    def validate_model(self, model: str) -> tuple[bool, Optional[str]]:
        return True, None

    def normalize_model(self, model: str) -> str:
        return model

    def parse_model(self, model: str) -> str:
        return model

    # ---- /bridge-kick ----------------------------------------------------
    def bridge_debug_available(self) -> bool:
        return False

    def bridge_kick(self, sub: str, a: Optional[str], b: Optional[str]) -> str:
        return f"bridge-kick {sub}: no-op (no bridge handle)"

    # ---- /extra-usage ----------------------------------------------------
    def run_extra_usage(self) -> dict[str, Any]:
        return {"type": "message", "value": "Extra usage is not available."}

    # ---- local-jsx dynamic gates/descriptions ----------------------------
    def is_remote_mode(self) -> bool:
        """Port of ``getIsRemoteMode()``. Gates /session, /remote-env."""

        return False

    def bridge_enabled(self) -> bool:
        """Port of ``isBridgeEnabled()``. Gates /remote-control."""

        return False

    def fast_mode_enabled(self) -> bool:
        """Port of ``isFastModeEnabled()``. Gates + hides /fast."""

        return False

    def fast_mode_model_display(self) -> str:
        """Port of ``FAST_MODE_MODEL_DISPLAY`` used in /fast's dynamic description."""

        return "Fast"

    def passes_eligible(self) -> bool:
        return False

    def passes_has_reward(self) -> bool:
        return False

    def sandbox_supported(self) -> bool:
        """Port of ``SandboxManager.isSupportedPlatform() && isPlatformInEnabledList()``."""

        return False

    def sandbox_status(self) -> str:
        """Port of /sandbox's dynamic status description."""

        return "sandbox disabled (⏎ to configure)"

    def ultrareview_enabled(self) -> bool:
        """Port of ``isUltrareviewEnabled()``. Gates /ultrareview."""

        return False

    def is_supported_desktop_platform(self) -> bool:
        """Port of /desktop's ``isSupportedPlatform`` (darwin, or win32 x64)."""

        p = self.platform()
        if p == "darwin":
            return True
        if p.startswith("win") and self.arch() in ("x86_64", "AMD64", "x64"):
            return True
        return False

    def current_model_display(self) -> str:
        """Port of ``renderModelName(getMainLoopModel())`` for /model's description."""

        return self.main_loop_model()


class RecordingRuntime(CommandRuntime):
    """Test runtime: records side effects, serves canned state, never shells out.

    Construct with keyword overrides for any scalar the commands read, e.g.
    ``RecordingRuntime(user_type='ant', claude_ai=True, shell_outputs={...})``.
    """

    def __init__(
        self,
        *,
        env: Optional[dict[str, str]] = None,
        user_type: Optional[str] = None,
        claude_ai: bool = False,
        api_key_auth: bool = False,
        subscription: Optional[str] = None,
        overage_allowed: bool = False,
        using_overage: bool = False,
        features: Optional[set[str]] = None,
        policies_denied: Optional[set[str]] = None,
        total_cost: str = "Total cost: $1.23",
        version: str = "1.2.3",
        build_time: Optional[str] = None,
        default_branch: str = "main",
        attribution: Optional[Attribution] = None,
        pr_attribution: Optional[str] = None,
        shell_outputs: Optional[dict[str, ShellResult]] = None,
        default_shell: ShellResult = ShellResult(stdout="", stderr="", interrupted=False),
        messages: Optional[list[Any]] = None,
        platform: str = "linux",
        terminal: Optional[str] = None,
        vim_mode: bool = False,
        available_commands: Optional[set[str]] = None,
        undercover: bool = False,
        undercover_text: str = "[undercover] Do not reveal internal tooling.",
        non_interactive: bool = False,
        context_files: Optional[list[str]] = None,
        context_report: str = "Context usage is unavailable in this runtime.",
        browser_success: bool = True,
        release_note_groups: Optional[list[tuple[str, list[str]]]] = None,
        plugin_refresh: Optional[dict[str, int]] = None,
        advisor_model: Optional[str] = None,
        can_configure_advisor: bool = False,
        main_loop_model: str = "opus",
        voice_mode_enabled: bool = False,
        keybindings_enabled: bool = False,
        extra_usage: Optional[dict[str, Any]] = None,
        remote_mode: bool = False,
        fast_mode: bool = False,
    ) -> None:
        self._env = dict(env or {})
        self._user_type = user_type
        self._claude_ai = claude_ai
        self._api_key_auth = api_key_auth
        self._subscription = subscription
        self._overage_allowed = overage_allowed
        self._using_overage = using_overage
        self._features = features or set()
        self._policies_denied = policies_denied or set()
        self._total_cost = total_cost
        self._version = version
        self._build_time = build_time
        self._default_branch = default_branch
        self._attribution = attribution or Attribution()
        self._pr_attribution = pr_attribution
        self._shell_outputs = shell_outputs or {}
        self._default_shell = default_shell
        self._messages = messages if messages is not None else []
        self._platform = platform
        self._terminal = terminal
        self._vim_mode = vim_mode
        self._available_commands = available_commands
        self._undercover = undercover
        self._undercover_text = undercover_text
        self._non_interactive = non_interactive
        self._context_files = context_files or []
        self._context_report = context_report
        self._browser_success = browser_success
        self._release_note_groups = release_note_groups or []
        self._plugin_refresh = plugin_refresh
        self._advisor_model = advisor_model
        self._can_configure_advisor = can_configure_advisor
        self._main_loop_model = main_loop_model
        self._voice_mode_enabled = voice_mode_enabled
        self._keybindings_enabled = keybindings_enabled
        self._extra_usage = extra_usage
        self._remote_mode = remote_mode
        self._fast_mode = fast_mode
        # Recorded side effects, inspectable by tests.
        self.opened_dialogs: list[tuple[str, dict[str, Any]]] = []
        self.shell_calls: list[str] = []
        self.opened_urls: list[str] = []
        self.cleared: bool = False

    # env
    def env(self, key: str) -> Optional[str]:
        return self._env.get(key)

    def user_type(self) -> Optional[str]:
        return self._user_type

    def platform(self) -> str:
        return self._platform

    def terminal(self) -> Optional[str]:
        return self._terminal

    # auth
    def is_claude_ai_subscriber(self) -> bool:
        return self._claude_ai

    def has_anthropic_api_key_auth(self) -> bool:
        return self._api_key_auth

    def subscription_type(self) -> Optional[str]:
        return self._subscription

    def is_overage_provisioning_allowed(self) -> bool:
        return self._overage_allowed

    def is_using_overage(self) -> bool:
        return self._using_overage

    def is_undercover(self) -> bool:
        return self._undercover

    def undercover_instructions(self) -> str:
        return self._undercover_text

    # features / policy
    def feature(self, name: str) -> bool:
        return name in self._features

    def policy_allowed(self, policy: str) -> bool:
        return policy not in self._policies_denied

    # cost / version
    def format_total_cost(self) -> str:
        return self._total_cost

    def version(self) -> str:
        return self._version

    def build_time(self) -> Optional[str]:
        return self._build_time

    # git
    async def get_default_branch(self) -> str:
        return self._default_branch

    def attribution_texts(self) -> Attribution:
        return self._attribution

    async def get_enhanced_pr_attribution(self) -> Optional[str]:
        return self._pr_attribution

    # shell — canned, never real
    async def run_shell(self, command: str) -> ShellResult:
        self.shell_calls.append(command)
        return self._shell_outputs.get(command, self._default_shell)

    # state
    def messages(self) -> list[Any]:
        return self._messages

    # UI
    def open_dialog(self, name: str, **data: Any) -> None:
        self.opened_dialogs.append((name, data))

    def clear_conversation(self) -> None:
        self.cleared = True

    def set_vim_mode(self, enabled: bool) -> None:
        self._vim_mode = enabled

    def get_vim_mode(self) -> bool:
        return self._vim_mode

    def has_command(self, name: str) -> bool:
        if self._available_commands is None:
            return super().has_command(name)
        return name in self._available_commands

    # extended overrides for local-command tests
    def is_non_interactive(self) -> bool:
        return self._non_interactive

    def context_files(self) -> list[str]:
        return list(self._context_files)

    def context_report(self) -> str:
        return self._context_report

    def open_browser(self, url: str) -> bool:
        self.opened_urls.append(url)
        return self._browser_success

    def release_note_groups(self) -> list[tuple[str, list[str]]]:
        return list(self._release_note_groups)

    def refresh_active_plugins(self) -> dict[str, int]:
        if self._plugin_refresh is not None:
            return self._plugin_refresh
        return super().refresh_active_plugins()

    def can_configure_advisor(self) -> bool:
        return self._can_configure_advisor

    def main_loop_model(self) -> str:
        return self._main_loop_model

    def advisor_model(self) -> Optional[str]:
        return self._advisor_model

    def set_advisor_model(self, model: Optional[str]) -> None:
        self._advisor_model = model

    def is_voice_mode_enabled(self) -> bool:
        return self._voice_mode_enabled

    def keybinding_customization_enabled(self) -> bool:
        return self._keybindings_enabled

    def run_extra_usage(self) -> dict[str, Any]:
        if self._extra_usage is not None:
            return self._extra_usage
        return super().run_extra_usage()

    def is_remote_mode(self) -> bool:
        return self._remote_mode

    def fast_mode_enabled(self) -> bool:
        return self._fast_mode
