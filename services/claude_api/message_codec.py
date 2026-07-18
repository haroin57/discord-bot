"""Serialization and replay normalization for Claude message history."""


def _block_to_dict(block) -> dict | None:
    """Convert an Anthropic content block to a replayable dictionary."""
    btype = getattr(block, "type", None)
    if btype == "fallback":
        def _endpoint(name: str) -> dict:
            value = getattr(block, name, None)
            if value is None and name == "from":
                value = getattr(block, "from_", None)
            if value is None and hasattr(block, "model_dump"):
                value = block.model_dump(by_alias=True).get(name)
            model = (
                value.get("model")
                if isinstance(value, dict)
                else getattr(value, "model", None)
            )
            return {"model": model or ""}

        return {
            "type": "fallback",
            "from": _endpoint("from"),
            "to": _endpoint("to"),
        }
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if btype == "tool_use":
        out = {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", None) or {},
        }
        caller = getattr(block, "caller", None)
        if caller is not None:
            if hasattr(caller, "model_dump"):
                out["caller"] = caller.model_dump()
            else:
                out["caller"] = caller
        return out
    if btype == "thinking":
        out = {
            "type": "thinking",
            "thinking": getattr(block, "thinking", ""),
        }
        sig = getattr(block, "signature", None)
        if sig is not None:
            out["signature"] = sig
        return out
    if btype == "redacted_thinking":
        out = {"type": "redacted_thinking"}
        data = getattr(block, "data", None)
        if data is not None:
            out["data"] = data
        return out
    if btype == "server_tool_use":
        out = {
            "type": "server_tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", None) or {},
        }
        caller = getattr(block, "caller", None)
        if caller is not None:
            out["caller"] = (
                caller.model_dump() if hasattr(caller, "model_dump") else caller
            )
        return out
    if btype in ("tool_search_tool_result", "advisor_tool_result"):
        return {
            "type": btype,
            "tool_use_id": getattr(block, "tool_use_id", ""),
            "content": _serialize_search_result_content(
                getattr(block, "content", None)
            ),
        }
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": btype or "unknown"}


def _response_blocks_to_dict(blocks) -> list[dict]:
    """Serialize a response using Anthropic's fallback continuation rules."""
    replayable = list(blocks or [])
    fallback_at = -1
    for index, block in enumerate(replayable):
        if getattr(block, "type", None) == "fallback":
            fallback_at = index

    paired_server_ids = {
        getattr(block, "tool_use_id", None)
        for block in replayable
        if getattr(block, "tool_use_id", None)
    }
    filtered = []
    for index, block in enumerate(replayable):
        btype = getattr(block, "type", None)
        if index < fallback_at:
            if btype in (
                "thinking", "redacted_thinking", "connector_text", "tool_use",
            ):
                continue
            if (
                btype == "server_tool_use"
                and getattr(block, "id", None) not in paired_server_ids
            ):
                continue
        filtered.append(block)
    return [
        serialized
        for serialized in (_block_to_dict(block) for block in filtered)
        if serialized is not None
    ]


def _serialize_search_result_content(content) -> "list[dict] | dict":
    """Serialize tool-search result content to the replayable request shape."""
    if content is None:
        return []
    if not isinstance(content, (list, tuple)):
        err_code = getattr(content, "error_code", None)
        if err_code is not None:
            return {"type": "tool_search_tool_result_error", "error_code": err_code}
        if isinstance(content, dict):
            return content
        return []
    out: list[dict] = []
    for item in content:
        itype = getattr(item, "type", None) or (
            item.get("type") if isinstance(item, dict) else None
        )
        if itype == "tool_reference":
            name = getattr(item, "tool_name", None) or (
                item.get("tool_name") if isinstance(item, dict) else ""
            )
            out.append({"type": "tool_reference", "tool_name": name or ""})
        elif itype == "text":
            text = getattr(item, "text", None) or (
                item.get("text") if isinstance(item, dict) else ""
            )
            out.append({"type": "text", "text": text or ""})
        elif isinstance(item, dict):
            out.append(item)
    return out


def _join_final_text(final_message) -> str:
    parts = []
    for block in getattr(final_message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


_EMPTY_CONTENT_PLACEHOLDER = [{"type": "text", "text": "(続けてください)"}]


def _sanitize_content_blocks(content):
    """Apply fallback continuation rules to serialized content blocks."""
    if not isinstance(content, list):
        return content

    fallback_at = -1
    for index, block in enumerate(content):
        if isinstance(block, dict) and block.get("type") == "fallback":
            fallback_at = index
    if fallback_at < 0:
        return content

    paired_server_ids = {
        block.get("tool_use_id")
        for block in content
        if isinstance(block, dict) and block.get("tool_use_id")
    }
    out = []
    for index, block in enumerate(content):
        btype = block.get("type") if isinstance(block, dict) else None
        if index < fallback_at:
            if btype in (
                "thinking", "redacted_thinking", "connector_text", "tool_use",
            ):
                continue
            if btype == "server_tool_use" and block.get("id") not in paired_server_ids:
                continue
        if btype == "fallback":
            out.append({
                "type": "fallback",
                "from": {"model": (block.get("from") or {}).get("model", "")},
                "to": {"model": (block.get("to") or {}).get("model", "")},
            })
        else:
            out.append(block)
    return out


_TOOL_SEARCH_BLOCK_TYPES = (
    "server_tool_use", "tool_search_tool_result", "advisor_tool_result",
)
_SERVER_TOOL_RESULT_TYPES = ("tool_search_tool_result", "advisor_tool_result")


def strip_tool_search_blocks(content):
    """Remove Tool Search blocks before replay when the feature is disabled."""
    if not isinstance(content, list):
        return content
    return [
        block
        for block in content
        if not (
            isinstance(block, dict)
            and block.get("type") in _TOOL_SEARCH_BLOCK_TYPES
        )
    ]


def _has_unpaired_server_tool_use(content) -> bool:
    """Return whether a server tool-use block has no matching result."""
    server_ids: set[str] = set()
    result_ids: set[str] = set()
    for block in content or []:
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype == "server_tool_use":
            block_id = getattr(block, "id", None) or (
                block.get("id") if isinstance(block, dict) else None
            )
            if block_id:
                server_ids.add(block_id)
        elif btype in _SERVER_TOOL_RESULT_TYPES:
            result_id = getattr(block, "tool_use_id", None) or (
                block.get("tool_use_id") if isinstance(block, dict) else None
            )
            if result_id:
                result_ids.add(result_id)
    return bool(server_ids - result_ids)


def _normalize_history_for_api(
    history: list[dict], *, strip_tool_search: bool = False,
) -> list[dict]:
    """Normalize history to the API's ``role`` and non-empty ``content`` shape."""
    out: list[dict] = []
    for item in history:
        content = _sanitize_content_blocks(item["content"])
        if strip_tool_search:
            content = strip_tool_search_blocks(content)
        if content == "" or content == []:
            content = _EMPTY_CONTENT_PLACEHOLDER
        out.append({"role": item["role"], "content": content})
    return out
