"""Restricted-mode policy gate for the direct-API path.

Restricted mode (non-admin Discord callers) operates on a hard deny list
enforced by the policy gate that tool_loop calls before executing any tool.
This is defense-in-depth — restricted callers also have ``tools=[]`` passed
to the API so the model never sees a tool to call in the first place.

The deny list mirrors services/claude_sdk.py:_RESTRICTED_DENY_LIST. Read is
NOT exposed; image attachments are inlined into the prompt as ImageBlocks
by entry.py (see Codex v2 #3) so the model can describe them without any
filesystem access.
"""

from typing import Callable

RESTRICTED_DENY_LIST: tuple[str, ...] = (
    "Bash", "BashOutput", "KillBash",
    "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep",
    "WebFetch", "WebSearch",
    "Task", "Agent", "SlashCommand", "ExitPlanMode", "TodoWrite",
    "Read",  # image attachments go inline; Read would let non-admin read fs
)

RESTRICTED_MAX_TURNS = 8


def make_can_use_tool(
    restricted: bool,
    allowed_tools: list[str] | None = None,
) -> Callable[[str, dict], tuple[bool, str]]:
    """Build a ``can_use_tool(name, input) -> (bool, reason)`` callback.

    Semantics:
      - ``restricted=True``: empty allow list, full deny list, plus the same
        deny list as defense-in-depth so a regression in the allow path can
        only ever broaden by one bug, not two.
      - ``restricted=False`` with ``allowed_tools=None``: allow everything
        (admin / bypassPermissions equivalent).
      - ``restricted=False`` with ``allowed_tools=[...]``: explicit allow
        list (used by tests and the side-question feature later).
    """
    deny = set(RESTRICTED_DENY_LIST) if restricted else set()
    allow_set = set(allowed_tools) if allowed_tools is not None else None

    def gate(tool_name: str, _tool_input: dict) -> tuple[bool, str]:
        # MCP names look like "mcp__server__tool" — strip the prefix for the
        # deny check so we still catch e.g. "mcp__shell__Bash" if someone
        # accidentally exposes one through an MCP server.
        bare = tool_name.split("__", 2)[-1] if "__" in tool_name else tool_name
        if bare in deny or tool_name in deny:
            return False, "denied by policy"
        if allow_set is not None:
            if not allow_set:
                return False, "no tools allowed"
            if bare not in allow_set and tool_name not in allow_set:
                return False, "not in allow list"
        return True, ""

    return gate
