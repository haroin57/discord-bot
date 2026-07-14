"""Minimal YAML-ish frontmatter parsing — port of the slices of
``src/utils/frontmatterParser.ts`` + ``markdownConfigLoader.ts`` the commands use.

Only the two behaviours the ported commands rely on are implemented:

* :func:`parse_frontmatter` — split a leading ``---`` fenced block off the body
  and return ``(frontmatter_dict, content)``; no fence => ``({}, text)``.
* :func:`parse_slash_command_tools` — turn an ``allowed-tools`` frontmatter value
  (a comma-separated string, possibly with ``Tool(arg:*)`` parens containing
  commas) into a list, matching ``parseSlashCommandToolsFromFrontmatter``.
"""

from __future__ import annotations

import re
from typing import Any


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return ``(frontmatter, content)``. Frontmatter is a shallow key: value map."""

    m = re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    raw, content = m.group(1), m.group(2)
    fm: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip()
    return fm, content


def parse_slash_command_tools(value: Any) -> list[str]:
    """Split an ``allowed-tools`` value into individual tool patterns.

    Commas inside ``(...)`` — e.g. ``Bash(git diff:*)`` — are not split on; only
    top-level commas separate tools. Mirrors the tolerant parsing upstream.
    """

    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    s = str(value)
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in s:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            tok = "".join(buf).strip()
            if tok:
                out.append(tok)
            buf = []
        else:
            buf.append(ch)
    tok = "".join(buf).strip()
    if tok:
        out.append(tok)
    return out
