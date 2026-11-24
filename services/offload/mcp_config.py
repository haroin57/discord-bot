"""Replace existing MCP transports without changing their authorization tiers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from services.offload.endpoint import validate_endpoint


def apply_remote_overrides(
    groups: Sequence[dict], raw: str, environ: Mapping[str, str], *, optional: Mapping[str, dict] | None = None,
) -> None:
    """Validate the entire map before changing any group; no implicit fallback.

    JSON stores endpoint URLs and environment variable *names*. Credentials
    remain in the process environment and are never included in error output.
    Unknown keys cannot create a server in a less privileged tier.
    """
    if not raw.strip():
        return
    try:
        entries = json.loads(raw)
    except ValueError:
        raise ValueError("MCP_REMOTE_SERVERS must be a JSON object") from None
    if not isinstance(entries, dict):
        raise ValueError("MCP_REMOTE_SERVERS must be a JSON object")
    destinations: dict[str, dict] = dict(optional or {})
    for group in groups:
        destinations.update({key: group for key in group})
    resolved = {}
    for name, spec in entries.items():
        if name not in destinations:
            raise ValueError("MCP_REMOTE_SERVERS contains an unknown server")
        if not isinstance(spec, dict) or set(spec) - {"url", "headers_env", "bearer_token_env"}:
            raise ValueError("MCP remote entry has invalid fields")
        if not isinstance(spec.get("url"), str):
            raise ValueError("MCP remote entry requires a URL")
        url = validate_endpoint(spec["url"], name="MCP remote endpoint")
        headers = {}
        mapping = spec.get("headers_env", {})
        if not isinstance(mapping, dict):
            raise ValueError("MCP headers_env must be an object")

        def secret(variable):
            if not isinstance(variable, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", variable):
                raise ValueError("MCP credential reference must be an environment variable name")
            value = environ.get(variable, "")
            if not value or any(ord(c) < 32 or ord(c) > 126 for c in value):
                raise ValueError("MCP remote credential is missing or invalid")
            return value

        seen_headers = set()
        for header, variable in mapping.items():
            if (
                not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", header)
                or header.lower() in {"host", "content-length", "connection", "transfer-encoding"}
                or header.lower() in seen_headers
            ):
                raise ValueError("MCP remote header name is invalid")
            seen_headers.add(header.lower())
            headers[header] = secret(variable)
        if "bearer_token_env" in spec:
            if "authorization" in seen_headers:
                raise ValueError("MCP remote Authorization was configured twice")
            headers["Authorization"] = "Bearer " + secret(spec["bearer_token_env"])
        resolved[name] = {"type": "http", "url": url, "headers": headers}
    for name, spec in resolved.items():
        destinations[name][name] = spec
