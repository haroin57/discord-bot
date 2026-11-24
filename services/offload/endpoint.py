"""Validation shared by explicitly configured service endpoints."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def validate_endpoint(value: str, *, name: str) -> str:
    """Require HTTPS except literal loopback addresses used for local tests.

    Redirects must also be disabled by callers. Credentials belong in headers,
    never in URLs, which transport exceptions and access logs may contain.
    """
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise ValueError(f"{name} must be a valid HTTPS URL") from None
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) <= 32 for char in value)
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
    ):
        raise ValueError(f"{name} requires HTTPS (HTTP allowed only on loopback), without URL credentials/query/fragment")
    return value
