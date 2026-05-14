#!/usr/bin/env python3
"""Extract CLI identity from a mitmproxy dump and rewrite the AUTO-CAPTURE
block in services/claude_api/client.py.

Inputs:
    sys.argv[1]: path to a mitmproxy dump produced by mitmdump -w <file>

Outputs:
    - services/claude_api/client.py is rewritten between AUTO-CAPTURE-START
      and AUTO-CAPTURE-END markers
    - prints a JSON summary of what changed to stdout

Exits non-zero if the dump contains no /v1/messages flow or if the
markers cannot be located.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from mitmproxy import io as mio


CLIENT_PY = Path(__file__).resolve().parent.parent / "services" / "claude_api" / "client.py"
MARKER_START = "# AUTO-CAPTURE-START"
MARKER_END = "# AUTO-CAPTURE-END"

# system[0] billing header parses as:
#   x-anthropic-billing-header: cc_version=<v>; cc_entrypoint=<e>; cch=<c>;
_BILLING_RE = re.compile(
    r"cc_version=(?P<v>[^;]+);\s*cc_entrypoint=(?P<e>[^;]+);\s*cch=(?P<c>[^;]+);"
)


def extract_from_dump(dump_path: Path) -> dict:
    """Return the first /v1/messages request's identity fields."""
    with open(dump_path, "rb") as f:
        reader = mio.FlowReader(f)
        for flow in reader.stream():
            req = getattr(flow, "request", None)
            if not req or "/v1/messages" not in req.path:
                continue
            try:
                body = json.loads(req.raw_content.decode("utf-8"))
            except Exception as exc:
                raise SystemExit(f"failed to parse request body: {exc}")
            return _extract_fields(req, body)
    raise SystemExit("no /v1/messages flow found in dump")


def _extract_fields(req, body: dict) -> dict:
    headers = {k.lower(): v for k, v in req.headers.items(multi=False)}
    betas_raw = headers.get("anthropic-beta", "")
    betas = [b.strip() for b in betas_raw.split(",") if b.strip()]

    # system[1] identity text (the second system block); fall back to first if 1-block layout
    system_blocks = body.get("system") or []
    identity_text = ""
    if len(system_blocks) >= 2:
        identity_text = system_blocks[1].get("text", "")
    elif len(system_blocks) == 1:
        identity_text = system_blocks[0].get("text", "")

    # system[0] billing header → cc_version / cc_entrypoint
    cc_version = ""
    cc_entrypoint = ""
    if system_blocks:
        billing = system_blocks[0].get("text", "")
        m = _BILLING_RE.search(billing)
        if m:
            cc_version_full = m.group("v")  # may be "2.1.92.a35" (with suffix)
            # We strip the random hex suffix so the static value stays stable;
            # the suffix is derived dynamically by client.py:get_attribution_header_value.
            cc_version = re.sub(r"\.[0-9a-fA-F]+$", "", cc_version_full)
            cc_entrypoint = m.group("e")

    return {
        "cc_version": cc_version,
        "cc_entrypoint": cc_entrypoint,
        "model_betas": betas,
        "cli_identity_text": identity_text,
    }


def render_block(fields: dict) -> str:
    """Produce the lines that go between the AUTO-CAPTURE markers."""
    lines = [
        MARKER_START,
        f'CC_VERSION = "{fields["cc_version"]}"',
        f'CC_ENTRYPOINT = "{fields["cc_entrypoint"]}"',
        "MODEL_BETAS = [",
    ]
    for b in fields["model_betas"]:
        lines.append(f'    "{b}",')
    lines.append("]")
    # JSON-encode the identity text so any quotes / escapes are safe.
    lines.append(f"CLI_IDENTITY_TEXT = {json.dumps(fields['cli_identity_text'])}")
    lines.append(MARKER_END)
    return "\n".join(lines)


def rewrite_client_py(new_block: str) -> tuple[str, str]:
    """Return (old_block, new_block) after writing to disk."""
    text = CLIENT_PY.read_text(encoding="utf-8")
    start_idx = text.find(MARKER_START)
    end_idx = text.find(MARKER_END)
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        raise SystemExit(f"markers not found in {CLIENT_PY}")
    end_idx_after = end_idx + len(MARKER_END)
    old_block = text[start_idx:end_idx_after]
    if old_block == new_block:
        return old_block, new_block
    new_text = text[:start_idx] + new_block + text[end_idx_after:]
    CLIENT_PY.write_text(new_text, encoding="utf-8")
    return old_block, new_block


def main():
    if len(sys.argv) != 2:
        print("usage: apply_cli_identity.py <mitm_dump>", file=sys.stderr)
        sys.exit(2)
    dump = Path(sys.argv[1])
    if not dump.is_file():
        print(f"no such file: {dump}", file=sys.stderr)
        sys.exit(2)

    fields = extract_from_dump(dump)
    if not all([fields["cc_version"], fields["cc_entrypoint"],
                fields["cli_identity_text"], fields["model_betas"]]):
        print("incomplete extraction:", json.dumps(fields, ensure_ascii=False), file=sys.stderr)
        sys.exit(3)

    new_block = render_block(fields)
    old, new = rewrite_client_py(new_block)
    changed = old != new
    print(json.dumps({
        "changed": changed,
        "extracted": fields,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
