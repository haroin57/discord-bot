"""Secret / mention redaction for Discord-bound text."""

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"https://discord(?:app)?\.com/api/webhooks/\d+/[^\s\"'<>]+"), "<webhook>"),
    (re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]{20,}"), "Bearer <redacted>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "<api_key>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "<api_key>"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "<github_token>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "<github_token>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"), "<slack_token>"),
    (re.compile(r"<@!?\d+>"), "<mention>"),
    (re.compile(r"<@&\d+>"), "<mention>"),
    (re.compile(r"@everyone"), "<mention>"),
    (re.compile(r"@here"), "<mention>"),
]


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out
