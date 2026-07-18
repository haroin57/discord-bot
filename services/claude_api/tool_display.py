"""Discord rendering helpers for tool invocation previews."""


_TOOL_DETAIL_INLINE_CAP = 1900
_TOOL_DETAIL_FIELD_CAP = 700


def _cap_inline(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + "\n...(truncated)"


def _format_tool_invocation_for_display(
    tool_name: str,
    tool_input: dict,
) -> str | None:
    """Build a Discord-friendly notice of an about-to-execute tool call."""
    tool_input = tool_input or {}

    if tool_name == "Bash":
        cmd = str(tool_input.get("command", "")).rstrip()
        if not cmd:
            return None
        timeout_hint = tool_input.get("timeout")
        header = "🔧 **Bash 実行**"
        if timeout_hint:
            header += f" (timeout={timeout_hint}s)"
        return f"{header}\n```bash\n{_cap_inline(cmd, _TOOL_DETAIL_INLINE_CAP)}\n```"

    if tool_name == "Read":
        file_path = tool_input.get("file_path", "")
        if not file_path:
            return None
        return f"🔧 **Read** `{file_path}`"

    if tool_name == "Write":
        file_path = tool_input.get("file_path", "")
        content = str(tool_input.get("content", ""))
        if not file_path:
            return None
        return (
            f"🔧 **Write** `{file_path}`\n"
            f"```\n{_cap_inline(content, _TOOL_DETAIL_INLINE_CAP)}\n```"
        )

    if tool_name == "Edit":
        file_path = tool_input.get("file_path", "")
        old = str(tool_input.get("old_string", ""))
        new = str(tool_input.get("new_string", ""))
        if not file_path:
            return None
        old_preview = _cap_inline(old, _TOOL_DETAIL_FIELD_CAP)
        new_preview = _cap_inline(new, _TOOL_DETAIL_FIELD_CAP)
        old_lines = "\n".join("- " + line for line in old_preview.splitlines() or [""])
        new_lines = "\n".join("+ " + line for line in new_preview.splitlines() or [""])
        replace_all = " (replace_all)" if tool_input.get("replace_all") else ""
        return (
            f"🔧 **Edit** `{file_path}`{replace_all}\n"
            f"```diff\n{old_lines}\n{new_lines}\n```"
        )

    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        if not pattern:
            return None
        return f"🔧 **Glob** `{pattern}`"

    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        glob_filter = tool_input.get("glob")
        if not pattern:
            return None
        if glob_filter:
            return f"🔧 **Grep** `{pattern}` (in `{glob_filter}`)"
        return f"🔧 **Grep** `{pattern}`"

    if tool_name == "TodoWrite":
        todos = tool_input.get("todos") or []
        if not isinstance(todos, list):
            return None
        lines = ["📝 **Update Todos**"]
        for todo in todos:
            if not isinstance(todo, dict):
                continue
            status = todo.get("status")
            if status == "completed":
                lines.append(f"  ☒ ~~{str(todo.get('content', ''))}~~")
            elif status == "in_progress":
                active = todo.get("activeForm") or todo.get("content", "")
                lines.append(f"  ☐ **{str(active)}**")
            else:
                lines.append(f"  ☐ {str(todo.get('content', ''))}")
        return _cap_inline("\n".join(lines), _TOOL_DETAIL_INLINE_CAP)

    if tool_input:
        import json as _json

        try:
            args_str = _json.dumps(tool_input, ensure_ascii=False)
        except (TypeError, ValueError):
            args_str = str(tool_input)
        return (
            f"🔧 **{tool_name}**\n"
            f"```json\n{_cap_inline(args_str, _TOOL_DETAIL_INLINE_CAP)}\n```"
        )
    return f"🔧 **{tool_name}**"
