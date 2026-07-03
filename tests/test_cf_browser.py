"""Tests for utils/cf_browser.py — the Cloudflare Browser Rendering REST client.

aiohttp is mocked in the unittest.mock style used elsewhere in the suite
(see tests/test_oauth_manager.py): a fake ClientSession whose .post returns an
async-context-manager wrapping a fake response.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_session(responses):
    """Build a fake aiohttp.ClientSession that yields ``responses`` in order.

    Each element of ``responses`` is a MagicMock with ``.status`` and awaitable
    ``.read()`` / ``.text()``. The session itself is an async context manager
    (``async with aiohttp.ClientSession(...) as s``), and each ``.post(...)`` is
    also an async context manager.
    """
    it = iter(responses)

    def _post(*args, **kwargs):
        resp = next(it)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm, session


def _resp(status, *, body=b"", text=""):
    r = MagicMock()
    r.status = status
    r.read = AsyncMock(return_value=body)
    r.text = AsyncMock(return_value=text)
    return r


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------

def test_is_configured_true_with_token():
    import utils.cf_browser as cb
    with patch.object(cb, "CF_BROWSER_RENDERING_TOKEN", "tok"), \
         patch.object(cb, "CF_ACCOUNT_ID", "acct"):
        assert cb.is_configured() is True


def test_is_configured_false_without_token():
    import utils.cf_browser as cb
    with patch.object(cb, "CF_BROWSER_RENDERING_TOKEN", ""), \
         patch.object(cb, "CF_ACCOUNT_ID", "acct"):
        assert cb.is_configured() is False


# ---------------------------------------------------------------------------
# render_screenshot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_png_bytes_on_200():
    import utils.cf_browser as cb
    session_cm, _ = _fake_session([_resp(200, body=b"\x89PNGdata")])
    with patch.object(cb, "CF_BROWSER_RENDERING_TOKEN", "tok"), \
         patch.object(cb, "CF_ACCOUNT_ID", "acct"), \
         patch("aiohttp.ClientSession", return_value=session_cm):
        result = await cb.render_screenshot("<html></html>", selector=".x")
    assert result == b"\x89PNGdata"


@pytest.mark.asyncio
async def test_returns_none_when_unconfigured():
    import utils.cf_browser as cb
    with patch.object(cb, "CF_BROWSER_RENDERING_TOKEN", ""), \
         patch.object(cb, "CF_ACCOUNT_ID", "acct"):
        result = await cb.render_screenshot("<html></html>")
    assert result is None


@pytest.mark.asyncio
async def test_200_empty_body_returns_none():
    import utils.cf_browser as cb
    session_cm, _ = _fake_session([_resp(200, body=b"")])
    with patch.object(cb, "CF_BROWSER_RENDERING_TOKEN", "tok"), \
         patch.object(cb, "CF_ACCOUNT_ID", "acct"), \
         patch("aiohttp.ClientSession", return_value=session_cm):
        result = await cb.render_screenshot("<html></html>")
    assert result is None


@pytest.mark.asyncio
async def test_400_does_not_retry():
    import utils.cf_browser as cb
    session_cm, session = _fake_session([_resp(400, text="bad request")])
    with patch.object(cb, "CF_BROWSER_RENDERING_TOKEN", "tok"), \
         patch.object(cb, "CF_ACCOUNT_ID", "acct"), \
         patch.object(cb, "CF_BROWSER_RENDERING_RETRIES", 2), \
         patch("aiohttp.ClientSession", return_value=session_cm):
        result = await cb.render_screenshot("<html></html>")
    assert result is None
    # 4xx (non-429) is not transient → exactly one POST.
    assert session.post.call_count == 1


@pytest.mark.asyncio
async def test_429_retries_then_succeeds():
    import utils.cf_browser as cb
    session_cm, session = _fake_session([
        _resp(429, text="rate limited"),
        _resp(200, body=b"PNG2"),
    ])
    with patch.object(cb, "CF_BROWSER_RENDERING_TOKEN", "tok"), \
         patch.object(cb, "CF_ACCOUNT_ID", "acct"), \
         patch.object(cb, "CF_BROWSER_RENDERING_RETRIES", 1), \
         patch("aiohttp.ClientSession", return_value=session_cm), \
         patch("asyncio.sleep", new=AsyncMock()):
        result = await cb.render_screenshot("<html></html>")
    assert result == b"PNG2"
    assert session.post.call_count == 2


@pytest.mark.asyncio
async def test_5xx_exhausts_retries_returns_none():
    import utils.cf_browser as cb
    session_cm, session = _fake_session([
        _resp(503, text="unavailable"),
        _resp(503, text="unavailable"),
    ])
    with patch.object(cb, "CF_BROWSER_RENDERING_TOKEN", "tok"), \
         patch.object(cb, "CF_ACCOUNT_ID", "acct"), \
         patch.object(cb, "CF_BROWSER_RENDERING_RETRIES", 1), \
         patch("aiohttp.ClientSession", return_value=session_cm), \
         patch("asyncio.sleep", new=AsyncMock()):
        result = await cb.render_screenshot("<html></html>")
    assert result is None
    assert session.post.call_count == 2


@pytest.mark.asyncio
async def test_network_exception_returns_none():
    import aiohttp
    import utils.cf_browser as cb

    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch.object(cb, "CF_BROWSER_RENDERING_TOKEN", "tok"), \
         patch.object(cb, "CF_ACCOUNT_ID", "acct"), \
         patch.object(cb, "CF_BROWSER_RENDERING_RETRIES", 0), \
         patch("aiohttp.ClientSession", return_value=session_cm), \
         patch("asyncio.sleep", new=AsyncMock()):
        result = await cb.render_screenshot("<html></html>")
    assert result is None


@pytest.mark.asyncio
async def test_request_body_shape():
    """The POST body carries html + selector + gotoOptions/viewport as expected."""
    import utils.cf_browser as cb
    session_cm, session = _fake_session([_resp(200, body=b"PNG")])
    with patch.object(cb, "CF_BROWSER_RENDERING_TOKEN", "tok"), \
         patch.object(cb, "CF_ACCOUNT_ID", "acct"), \
         patch("aiohttp.ClientSession", return_value=session_cm):
        await cb.render_screenshot(
            "<html>hi</html>",
            selector=".math-container",
            device_scale_factor=2,
            wait_for_network_idle=True,
        )
    _, kwargs = session.post.call_args
    body = kwargs["json"]
    assert body["html"] == "<html>hi</html>"
    assert body["selector"] == ".math-container"
    assert body["viewport"]["deviceScaleFactor"] == 2
    assert body["viewport"]["width"] > 0 and body["viewport"]["height"] > 0
    assert body["gotoOptions"]["waitUntil"] == "networkidle0"
    # Authorization header carries the bearer token.
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
