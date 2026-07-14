"""Feature-flag-gated commands — free-code's conditionally-registered slash commands.

In ``commands.ts`` these are spread into ``COMMANDS`` only when a ``bun:bundle``
``feature('FLAG')`` (or a GrowthBook gate) is on, e.g.
``...(forkCmd ? [forkCmd] : [])``. Each port keeps that gate as
``is_enabled_fn=lambda rt: rt.feature('FLAG')`` (ANDed with any extra condition),
so the command stays disabled/hidden until the flag is set — matching upstream.

Sources present in this distribution are ported with real metadata:
``remote-setup/index.ts`` (``web-setup``), ``bridge/index.ts`` (``remote-control``),
``brief.ts``, ``ultraplan.tsx``.

The other flag-gated commands are **eliminated from this build**: the bundler strips
disabled-feature code, so their source dirs (``fork/``, ``buddy/``, ``peers/``,
``workflows/``, ``assistant/``, ``proactive.ts``, ``torch.ts``,
``remoteControlServer/``) are not present. They are represented as metadata shells —
canonical name + flag gate + a headless dialog ``call`` — so the enumeration is
complete and dispatch/gating can be verified, with a comment marking that the real
implementation lives behind the flag and is absent here.
"""

from __future__ import annotations

from ..runtime import CommandRuntime
from ..types import JsxOutcome, LocalJsxCommand

_CCR_TERMS_URL = "https://code.claude.com/docs/en/claude-code-on-the-web"


def _dialog(name: str):
    def _call(args: str, rt: CommandRuntime) -> JsxOutcome:
        rt.open_dialog(name, args=args)
        return JsxOutcome(dialog=name)

    return _call


def _gated(name: str, description: str, enabled, **kw) -> LocalJsxCommand:
    kw.setdefault("call_fn", _dialog(name))
    kw["is_enabled_fn"] = enabled
    return LocalJsxCommand(name=name, description=description, **kw)


COMMANDS: list[LocalJsxCommand] = [
    # remote-setup/index.ts — commands.ts gates on feature('CCR_REMOTE_SETUP');
    # the command's own isEnabled adds a GrowthBook flag + policy check.
    _gated(
        "web-setup",
        "Setup Claude Code on the web (requires connecting your GitHub account)",
        lambda rt: rt.feature("CCR_REMOTE_SETUP")
        and rt.feature("tengu_cobalt_lantern")
        and rt.policy_allowed("allow_remote_sessions"),
        availability=["claude-ai"],
        is_hidden_fn=lambda rt: not rt.policy_allowed("allow_remote_sessions"),
    ),
    # bridge/index.ts — feature('BRIDGE_MODE') && isBridgeEnabled(); immediate.
    _gated(
        "remote-control",
        "Connect this terminal for remote-control sessions",
        lambda rt: rt.feature("BRIDGE_MODE") and rt.bridge_enabled(),
        aliases=["rc"],
        argument_hint="[name]",
        immediate=True,
        is_hidden_fn=lambda rt: not (rt.feature("BRIDGE_MODE") and rt.bridge_enabled()),
    ),
    # brief.ts — feature('KAIROS') || feature('KAIROS_BRIEF'); GB enable_slash_command.
    _gated(
        "brief",
        "Toggle brief-only mode",
        lambda rt: rt.feature("KAIROS") or rt.feature("KAIROS_BRIEF"),
    ),
    # ultraplan.tsx — feature('ULTRAPLAN'); the command's own isEnabled is always true.
    _gated(
        "ultraplan",
        "~10–30 min · Claude Code on the web drafts an advanced plan you can edit and "
        f"approve. See {_CCR_TERMS_URL}",
        lambda rt: rt.feature("ULTRAPLAN"),
        argument_hint="<prompt>",
    ),
    # ---- eliminated-from-build shells (source stripped; gated behind a flag) ----
    # fork/index.ts (absent) — feature('FORK_SUBAGENT'); when on, /branch drops its
    # 'fork' alias (see jsx_commands branch) and this owns /fork.
    _gated(
        "fork",
        "Fork the current conversation into a sub-agent",
        lambda rt: rt.feature("FORK_SUBAGENT"),
        argument_hint="[prompt]",
    ),
    # buddy/index.ts (absent) — feature('BUDDY').
    _gated("buddy", "Pair with a buddy agent", lambda rt: rt.feature("BUDDY")),
    # peers/index.ts (absent) — feature('UDS_INBOX').
    _gated("peers", "Manage peer inbox sessions", lambda rt: rt.feature("UDS_INBOX")),
    # workflows/index.ts (absent) — feature('WORKFLOW_SCRIPTS'); badged kind='workflow'.
    _gated(
        "workflows",
        "List and run workflow scripts",
        lambda rt: rt.feature("WORKFLOW_SCRIPTS"),
        kind="workflow",
    ),
    # assistant/index.ts (absent) — feature('KAIROS').
    _gated("assistant", "Kairos assistant", lambda rt: rt.feature("KAIROS")),
    # proactive.ts (absent) — feature('PROACTIVE') || feature('KAIROS').
    _gated(
        "proactive",
        "Toggle proactive mode",
        lambda rt: rt.feature("PROACTIVE") or rt.feature("KAIROS"),
    ),
    # torch.ts (absent) — feature('TORCH').
    _gated("torch", "Torch mode", lambda rt: rt.feature("TORCH")),
    # remoteControlServer/index.ts (absent) — feature('DAEMON') && feature('BRIDGE_MODE').
    _gated(
        "remote-control-server",
        "Run the remote-control server for this session",
        lambda rt: rt.feature("DAEMON") and rt.feature("BRIDGE_MODE"),
    ),
]
