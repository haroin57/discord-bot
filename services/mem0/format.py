"""Format retrieved Mem0 memories as a prompt-injection block."""


def format_memories_for_prompt(memories: list[dict]) -> str:
    """Format retrieved memories as a prompt-injection block."""
    if not memories:
        return ""

    lines = ["[Mem0 — この人についての長期記憶]"]
    for m in memories:
        text = m.get("memory", m.get("text", ""))
        if text:
            lines.append(f"- {text}")
    lines.append("[End of Mem0 memories]")
    return "\n".join(lines)
