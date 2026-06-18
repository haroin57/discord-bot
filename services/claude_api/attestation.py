"""CCH attestation: xxHash64-based request body signing.

Replicates the Bun/Zig-level cch computation that the official Claude Code
binary performs in its custom nativeFetch. For Python (non-Bun) clients,
this is implemented as an httpx event hook that patches the serialized
request body before it leaves the process.

Algorithm (from xstarrystar/claude-client-attestation):
    1. System prompt contains "cch=00000" placeholder
    2. SDK serializes the full request body to JSON
    3. xxHash64(body_bytes, seed=CCH_SEED) & 0xFFFFF → 5-hex token
    4. Replace "cch=00000" with "cch=<token>" in the body

The seed 0x6E52736AC806831E was extracted from the compiled Bun binary
and confirmed consistent across versions 2.1.37 → 2.1.92+.
"""

import logging

import xxhash

log = logging.getLogger(__name__)

CCH_SEED: int = 0x6E52736AC806831E
_PLACEHOLDER = b"cch=00000"


def patch_cch_body(body: bytes) -> bytes:
    """Compute and replace the cch=00000 placeholder in a serialized body."""
    if _PLACEHOLDER not in body:
        return body
    digest = xxhash.xxh64(body, seed=CCH_SEED).intdigest()
    token = format(digest & 0xFFFFF, "05x")
    patched = body.replace(_PLACEHOLDER, f"cch={token}".encode(), 1)
    log.debug("cch attestation: %s", token)
    return patched


async def cch_request_hook(request) -> None:
    """httpx async event hook — patches cch in the request body.

    httpx event hooks receive the Request object. The content attribute
    is the raw bytes. We read it, patch, and replace via the internal
    _content attribute (the only way to mutate an httpx.Request body
    after construction).
    """
    body = request.content
    if body and _PLACEHOLDER in body:
        request._content = patch_cch_body(body)
        # Update Content-Length if present (same length, but be safe)
        if "content-length" in request.headers:
            request.headers["content-length"] = str(len(request._content))
