"""mitmproxy addon that records request shape without credentials or prompts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


ALLOWED_HEADERS = {
    "user-agent", "x-app", "anthropic-version", "anthropic-beta",
    "anthropic-dangerous-direct-browser-access", "x-stainless-lang",
    "x-stainless-package-version", "x-stainless-runtime",
    "x-stainless-runtime-version", "x-stainless-os", "x-stainless-arch",
    "x-stainless-timeout", "accept", "accept-encoding", "accept-language",
    "sec-fetch-mode", "content-type",
}
ALLOWED_BODY_FIELDS = {
    "model", "max_tokens", "thinking", "output_config", "context_management",
    "tool_choice", "stream", "temperature", "top_p", "top_k",
}


def summarize_request(body: dict, headers) -> dict:
    summary = {key: body[key] for key in sorted(ALLOWED_BODY_FIELDS) if key in body}
    summary["keys"] = sorted(body)
    summary["system"] = [
        {
            "type": block.get("type"),
            "text_length": len(block.get("text", "")),
            "text_sha256": hashlib.sha256(block.get("text", "").encode()).hexdigest(),
            "cache_control": block.get("cache_control"),
        }
        for block in body.get("system", []) if isinstance(block, dict)
    ]
    summary["messages"] = [
        {
            "role": message.get("role"),
            "types": [block.get("type") for block in message["content"] if isinstance(block, dict)]
            if isinstance(message.get("content"), list) else ["text"],
        }
        for message in body.get("messages", [])
    ]
    summary["tools"] = [
        {"type": tool.get("type", "custom"), "name": tool.get("name"), "keys": sorted(tool)}
        for tool in body.get("tools", [])
    ]
    summary["metadata_keys"] = sorted(body.get("metadata", {}))
    return {
        "headers": {key.lower(): value for key, value in headers.items() if key.lower() in ALLOWED_HEADERS},
        "body": summary,
    }


def request(flow) -> None:
    incoming = flow.request
    if incoming.host != "api.anthropic.com" or incoming.path.split("?", 1)[0] != "/v1/messages":
        return
    try:
        body = json.loads(incoming.content)
    except (ValueError, TypeError):
        return
    flow.metadata["request_shape"] = summarize_request(body, incoming.headers)


def response(flow) -> None:
    summary = flow.metadata.get("request_shape")
    if summary is None:
        return
    summary["http_status"] = flow.response.status_code
    output = Path(os.environ["CLAUDE_REQUEST_CAPTURE_OUTPUT"])
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(summary) + "\n")
