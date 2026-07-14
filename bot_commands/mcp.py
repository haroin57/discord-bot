"""/mcp management UI backed by the free-code command dispatcher."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from bot_commands._deps import CommandDeps
from free_code_commands.runtime import CommandRuntime
from services.mcp_control import get_mcp_control
from services.session import get_session

log = logging.getLogger(__name__)

_SERVERS_PER_PAGE = 10
_TOOLS_PER_PAGE = 15


def _mcp_command():
    from free_code_commands.commands import jsx_commands

    return next(command for command in jsx_commands.COMMANDS if command.name == "mcp")


class _DiscordMcpRuntime(CommandRuntime):
    def __init__(self, channel_id: int | None, user_id: int) -> None:
        self.channel_id = channel_id
        self.user_id = user_id

    def mcp_selected_keys(self) -> list[str]:
        if self.channel_id is None:
            return []
        info = (
            get_session(self.channel_id, self.user_id)
            or get_session(self.channel_id, None)
        )
        return list(info.mcp_server_keys) if info is not None else []


def _state_words(row: dict) -> str:
    persistent = "enabled" if row.get("enabled") else "disabled"
    selected = "selected" if row.get("selected") else "not selected"
    probe = row.get("probe_status") or "unprobed"
    return f"{persistent} | {selected} | {probe}"


class _McpServerSelect(discord.ui.Select):
    def __init__(self, owner: "_McpView") -> None:
        self.owner = owner
        rows = owner.page_rows()
        options = [
            discord.SelectOption(
                label=str(row["key"])[:100],
                value=str(row["key"]),
                description=(
                    f"{_state_words(row)} | {row.get('transport', 'stdio')}"
                )[:100],
                default=row["key"] == owner.selected_key,
            )
            for row in rows
        ]
        if not options:
            options = [discord.SelectOption(label="No configured servers", value="_none")]
        super().__init__(
            placeholder="Select an MCP server",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
            disabled=not rows,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value != "_none":
            self.owner.selected_key = value
            self.owner.mode = "detail"
            self.owner.tool_page = 0
        self.owner.refresh()
        await interaction.response.edit_message(
            content=self.owner.render(),
            view=self.owner,
        )


class _McpView(discord.ui.View):
    def __init__(
        self,
        runtime: _DiscordMcpRuntime,
        *,
        owner_id: int,
        selected_key: str | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self.runtime = runtime
        self.owner_id = owner_id
        self.selected_key = selected_key
        self.mode = "overview"
        self.server_page = 0
        self.tool_page = 0
        self.notice = ""
        self.rows: list[dict[str, Any]] = []
        self.message = None
        self.refresh()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the user who opened this panel can operate it.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def page_count(self) -> int:
        return max(1, (len(self.rows) + _SERVERS_PER_PAGE - 1) // _SERVERS_PER_PAGE)

    def page_rows(self) -> list[dict]:
        start = self.server_page * _SERVERS_PER_PAGE
        return self.rows[start:start + _SERVERS_PER_PAGE]

    def selected_row(self) -> dict | None:
        return next(
            (row for row in self.rows if row["key"] == self.selected_key),
            None,
        )

    def refresh(self) -> None:
        self.rows = self.runtime.mcp_list()
        self.server_page = min(self.server_page, self.page_count() - 1)
        keys = {row["key"] for row in self.rows}
        if self.selected_key not in keys:
            self.selected_key = self.page_rows()[0]["key"] if self.page_rows() else None

        for item in list(self.children):
            if isinstance(item, _McpServerSelect):
                self.remove_item(item)
        self.add_item(_McpServerSelect(self))
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        row = self.selected_row()
        self.toggle.disabled = row is None
        if row is not None:
            self.toggle.label = "Disable" if row.get("enabled") else "Enable"
            self.toggle.style = (
                discord.ButtonStyle.danger
                if row.get("enabled")
                else discord.ButtonStyle.success
            )
        self.tools.disabled = row is None
        self.probe.disabled = row is None
        self.previous.disabled = not self._can_previous()
        self.next.disabled = not self._can_next()

    def _tool_page_count(self) -> int:
        if self.selected_key is None:
            return 1
        names = self.runtime.mcp_tool_names(self.selected_key)
        return max(1, (len(names) + _TOOLS_PER_PAGE - 1) // _TOOLS_PER_PAGE)

    def _can_previous(self) -> bool:
        page = self.tool_page if self.mode == "tools" else self.server_page
        return page > 0

    def _can_next(self) -> bool:
        if self.mode == "tools":
            return self.tool_page + 1 < self._tool_page_count()
        return self.server_page + 1 < self.page_count()

    def render(self) -> str:
        prefix = f"{self.notice}\n\n" if self.notice else ""
        if self.mode == "tools":
            return prefix + self._render_tools()
        if self.mode == "detail":
            return prefix + self._render_detail()
        return prefix + self._render_overview()

    def _render_overview(self) -> str:
        lines = [
            "MCP management",
            "enabled/disabled: persistent setting",
            "selected/not selected: current conversation session",
            "reachable/failed: latest temporary probe",
            "",
        ]
        for row in self.page_rows():
            lines.append(
                f"- {row['key']}: {_state_words(row)} | "
                f"{row.get('transport', 'stdio')} | {row.get('tool_count', 0)} tools"
            )
        lines.append(f"\nServers page {self.server_page + 1}/{self.page_count()}")
        return "\n".join(lines)[:1950]

    def _render_detail(self) -> str:
        row = self.selected_row()
        if row is None:
            return self._render_overview()
        lines = [
            f"MCP server: {row['key']}",
            f"- persistent: {'enabled' if row.get('enabled') else 'disabled'}",
            f"- session: {'selected' if row.get('selected') else 'not selected'}",
            f"- latest probe: {row.get('probe_status', 'unprobed')}",
            f"- transport: {row.get('transport', 'stdio')}",
            f"- tier: {row.get('tier', 'other')}",
            f"- known tools: {row.get('tool_count', 0)}",
        ]
        if row.get("error"):
            lines.append(f"- probe error: {row['error']}")
        return "\n".join(lines)[:1950]

    def _render_tools(self) -> str:
        if self.selected_key is None:
            return self._render_overview()
        names = self.runtime.mcp_tool_names(self.selected_key)
        pages = self._tool_page_count()
        self.tool_page = min(self.tool_page, pages - 1)
        start = self.tool_page * _TOOLS_PER_PAGE
        shown = names[start:start + _TOOLS_PER_PAGE]
        lines = [f"MCP tools: {self.selected_key} ({len(names)})", ""]
        lines.extend(f"- {name[:140]}" for name in shown)
        if not shown:
            lines.append("- No known tools. Run Probe to fetch the live list.")
        lines.append(f"\nTools page {self.tool_page + 1}/{pages}")
        return "\n".join(lines)[:1950]

    async def _run(self, args: str):
        return await _mcp_command().call(args, self.runtime)

    @discord.ui.button(label="Overview", style=discord.ButtonStyle.secondary, row=1)
    async def overview(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "overview"
        self.notice = ""
        self.refresh()
        await interaction.response.edit_message(content=self.render(), view=self)

    @discord.ui.button(label="Tools", style=discord.ButtonStyle.secondary, row=1)
    async def tools(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "tools"
        self.tool_page = 0
        self.notice = ""
        self.refresh()
        await interaction.response.edit_message(content=self.render(), view=self)

    @discord.ui.button(label="Probe", style=discord.ButtonStyle.primary, row=1)
    async def probe(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_key is None:
            return
        await interaction.response.defer()
        outcome = await self._run(f"reconnect {self.selected_key}")
        self.notice = outcome.message or ""
        self.mode = "detail"
        self.refresh()
        await interaction.edit_original_response(content=self.render(), view=self)

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.danger, row=1)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = self.selected_row()
        if row is None:
            return
        action = "disable" if row.get("enabled") else "enable"
        outcome = await self._run(f"{action} {row['key']}")
        self.notice = outcome.message or ""
        self.mode = "detail"
        self.refresh()
        await interaction.response.edit_message(content=self.render(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=2)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.mode == "tools":
            self.tool_page = max(0, self.tool_page - 1)
        else:
            self.server_page = max(0, self.server_page - 1)
        self.refresh()
        await interaction.response.edit_message(content=self.render(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=2)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.mode == "tools":
            self.tool_page = min(self._tool_page_count() - 1, self.tool_page + 1)
        else:
            self.server_page = min(self.page_count() - 1, self.server_page + 1)
        self.refresh()
        await interaction.response.edit_message(content=self.render(), view=self)


async def _server_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    needle = (current or "").lower()
    keys = list(get_mcp_control().server_keys)
    action = getattr(getattr(interaction, "namespace", None), "action", "")
    if isinstance(action, app_commands.Choice):
        action = action.value
    if action in {"enable", "disable"}:
        keys.insert(0, "all")
    return [
        app_commands.Choice(name=key, value=key)
        for key in keys
        if needle in key.lower()
    ][:_SERVERS_PER_PAGE * 2 + 5]


async def _handle(
    interaction: discord.Interaction,
    deps: CommandDeps,
    action: str,
    server: str,
) -> None:
    if not deps.is_admin(interaction.user):
        await interaction.response.send_message("admin only", ephemeral=True)
        return

    action = (action or "status").lower()
    server = (server or "").strip()
    if action != "status" and not server:
        await interaction.response.send_message(
            f"server is required for {action}.",
            ephemeral=True,
        )
        return

    runtime = _DiscordMcpRuntime(interaction.channel_id, interaction.user.id)
    args = "" if action == "status" else f"{action} {server}"
    outcome = await _mcp_command().call(args, runtime)
    selected = server if server and server != "all" else None
    view = _McpView(runtime, owner_id=interaction.user.id, selected_key=selected)
    view.notice = outcome.message or ""
    if action == "tools":
        view.mode = "tools"
    elif action != "status":
        view.mode = "detail"
    view.refresh()
    await interaction.response.send_message(
        view.render(),
        view=view,
        ephemeral=True,
    )
    try:
        view.message = await interaction.original_response()
    except (AttributeError, discord.HTTPException):
        pass


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    async def handler(
        interaction: discord.Interaction,
        action: str = "status",
        server: str = "",
    ):
        await _handle(interaction, deps, action, server)

    handler = app_commands.describe(
        action="Operation to perform",
        server="Configured MCP server; all is valid for enable/disable",
    )(handler)
    handler = app_commands.choices(action=[
        app_commands.Choice(name="status", value="status"),
        app_commands.Choice(name="tools", value="tools"),
        app_commands.Choice(name="reconnect", value="reconnect"),
        app_commands.Choice(name="enable", value="enable"),
        app_commands.Choice(name="disable", value="disable"),
    ])(handler)
    handler = app_commands.autocomplete(server=_server_autocomplete)(handler)
    tree.command(
        name="mcp",
        description="Manage MCP servers (admin only)",
    )(handler)
