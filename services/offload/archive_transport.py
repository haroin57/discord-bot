"""Bounded, reusable HTTP transport for the durable R2 archive spool.

The caller owns retries and delivery acknowledgement. This module never drops
queue entries, follows redirects, loads credentials from disk, or spawns a CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import httpx

from services.offload.endpoint import validate_endpoint


class ArchiveTransportError(RuntimeError):
    """Sanitized failure safe to persist in spool manifests and logs."""


class ArchiveHTTPTransport:
    def __init__(self, url: str, token: str, *, timeout: float = 60.0):
        self.url = validate_endpoint(url, name="R2_ARCHIVE_HTTP_URL") + "/v1/objects"
        if len(token) < 32 or any(ord(c) < 32 or ord(c) > 126 for c in token):
            raise ValueError("R2_ARCHIVE_HTTP_TOKEN must contain at least 32 printable ASCII characters")
        if not 0 < timeout <= 300:
            raise ValueError("R2_ARCHIVE_HTTP_TIMEOUT_S must be in (0, 300]")
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout, connect=min(10, timeout)),
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.client.close()

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        if response.status_code != 200:
            raise ArchiveTransportError(f"archive HTTP {response.status_code}")

    def upload(self, manifest: dict, payload: Path) -> None:
        size = payload.stat().st_size
        if size != manifest["size"]:
            raise ArchiveTransportError("archive payload size changed")
        headers = {
            "Content-Length": str(size),
            "Content-Type": manifest.get("content_type") or "application/octet-stream",
            "X-Archive-SHA256": manifest["sha256"],
            "X-Archive-Revision": manifest["queue_id"],
        }
        try:
            with payload.open("rb") as body, self.client.stream(
                "PUT", self.url, params={"key": manifest["key"]}, headers=headers, content=body,
            ) as response:
                self._check_status(response)
                acknowledgement = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    acknowledgement.extend(chunk)
                    if len(acknowledgement) > 4096:
                        raise ArchiveTransportError("archive acknowledgement too large")
                data = json.loads(acknowledgement)
                if not isinstance(data, dict) or (
                    data.get("ok") is not True
                    or data.get("sha256") != manifest["sha256"]
                    or data.get("size") != size
                ):
                    raise ArchiveTransportError("archive acknowledgement mismatch")
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeError):
            raise ArchiveTransportError("archive upload failed") from None

    def download(self, key: str, target: Path, *, max_bytes: int = 2**30) -> Path:
        """Stream to a private tempfile; replace destination only after validation."""
        fd, tmp_name = tempfile.mkstemp(prefix=".r2-download-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output, self.client.stream(
                "GET", self.url, params={"key": key}, headers={"Accept-Encoding": "identity"},
            ) as response:
                self._check_status(response)
                raw_length = response.headers.get("Content-Length", "")
                if not raw_length.isdigit() or int(raw_length) > max_bytes:
                    raise ArchiveTransportError("archive download length invalid or too large")
                expected = int(raw_length)
                digest = hashlib.sha256()
                received = 0
                for chunk in response.iter_raw(chunk_size=65536):
                    received += len(chunk)
                    if received > expected or received > max_bytes:
                        raise ArchiveTransportError("archive download exceeded length")
                    digest.update(chunk)
                    output.write(chunk)
                checksum = response.headers.get("X-Archive-SHA256")
                if received != expected or (checksum and digest.hexdigest() != checksum):
                    raise ArchiveTransportError("archive download integrity mismatch")
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp_name, target)
            return target
        except httpx.HTTPError:
            raise ArchiveTransportError("archive download failed") from None
        finally:
            Path(tmp_name).unlink(missing_ok=True)
