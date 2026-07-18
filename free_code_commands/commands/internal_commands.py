"""Internal-only (ant) commands — ``INTERNAL_ONLY_COMMANDS`` from ``commands.ts``.

These are spread into the registry only when ``USER_TYPE === 'ant' && !IS_DEMO``.
In THIS distribution their implementations are **stubbed out**: every dir's
``index.js`` is the same two-line shim —

    export default { isEnabled: () => false, isHidden: true, name: 'stub' }

— i.e. the real ant-only tooling was stripped from the public build. So there is no
logic to translate; the faithful port is the stub itself: disabled, hidden, and
ant-gated. Each command is registered under its **canonical** name (recovered from
the ``commands.ts`` import — the public build collapses them all to ``name:'stub'``)
so the enumeration is complete and the registry can still resolve/gate them, with
``is_enabled_fn`` returning ``False`` to mirror the upstream ``isEnabled: () => false``.

Already ported elsewhere (NOT re-stubbed here): ``commit``, ``commit-push-pr``
(prompt_commands), ``version``, ``bridge-kick`` (local_commands), ``init-verifiers``
(prompt_commands).
"""

from __future__ import annotations

from ..registry import _local_command, _metadata_catalog
from ..runtime import CommandRuntime
from ..types import LocalCommandResult

# Verbatim upstream stub shape, for reference / assertions.
STUB_MESSAGE = "(internal ant-only command — implementation stripped from this build)"


def _stub(name: str, *, extra_flag=None, **kw):
    """Build a disabled+hidden ant-gated stub matching the upstream shim.

    ``extra_flag`` documents a command that is *also* behind a bun feature flag
    upstream (e.g. force-snip → HISTORY_SNIP). It does not change enablement — the
    stub's ``isEnabled: () => false`` dominates — but is recorded for fidelity.
    """

    def _call(args: str, rt: CommandRuntime) -> LocalCommandResult:
        return LocalCommandResult.text(STUB_MESSAGE)

    return _local_command(
        name=name,
        description=kw.pop("description", "Internal (ant-only) command"),
        supports_non_interactive=False,
        _is_hidden=True,
        # Mirrors the stub's `isEnabled: () => false`; ant-gating is moot since
        # this is always false, but the intent (ant + stripped) is in the docstring.
        is_enabled_fn=lambda rt: False,
        call_fn=_call,
        loaded_from="commands_DEPRECATED",
        **kw,
    )


# Canonical names recovered from commands.ts's INTERNAL_ONLY_COMMANDS imports.
_DEFINITIONS = (
    _stub("backfill-sessions"),  # backfill-sessions/index.js
    _stub("break-cache"),  # break-cache/index.js
    _stub("bughunter"),  # bughunter/index.js
    _stub("ctx_viz"),  # ctx_viz/index.js
    _stub("good-claude"),  # good-claude/index.js
    _stub("issue"),  # issue/index.js
    _stub("mock-limits"),  # mock-limits/index.js
    _stub("reset-limits"),  # reset-limits/index.js (default export)
    # reset-limits/index.js also exports resetLimitsNonInteractive (same stub); it
    # would collide by name with the above, so it is intentionally not duplicated.
    _stub("onboarding"),  # onboarding/index.js
    _stub("share"),  # share/index.js
    _stub("summary"),  # summary/index.js
    _stub("teleport"),  # teleport/index.js
    _stub("ant-trace"),  # ant-trace/index.js
    _stub("perf-issue"),  # perf-issue/index.js
    _stub("env"),  # env/index.js
    _stub("oauth-refresh"),  # oauth-refresh/index.js
    _stub("debug-tool-call"),  # debug-tool-call/index.js
    _stub("agents-platform"),  # agents-platform/index.js (ant + source absent)
    _stub("autofix-pr"),  # autofix-pr/index.js
    # Also feature-gated upstream (kept as ant stubs here):
    _stub("force-snip", extra_flag="HISTORY_SNIP"),  # force-snip.js
    _stub("subscribe-pr", extra_flag="KAIROS_GITHUB_WEBHOOKS"),  # subscribe-pr.js
)

COMMANDS = _metadata_catalog(_DEFINITIONS)
