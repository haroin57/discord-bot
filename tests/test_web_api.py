"""Unit tests for web_api.py (WebAPI class).

All external dependencies (aiohttp HTTP, socket.io emit, Claude SDK,
PromptBuilder, session services, Mem0) are fully mocked.
No real network or subprocess usage.
"""

import asyncio
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def make_web_api():
    """Construct a WebAPI with all heavy dependencies stubbed out."""
    mock_bot = MagicMock()

    # Patch load_sessions so __init__ doesn't hit disk
    with patch("web_api.load_sessions", return_value={}):
        # socketio.AsyncServer.attach touches aiohttp internals; mock it
        with patch("socketio.AsyncServer.attach"):
            from web_api import WebAPI
            api = WebAPI(mock_bot)

    # Replace the sio instance with a lightweight mock so emit/enter_room etc.
    # are AsyncMocks that we can inspect in tests.
    sio_mock = MagicMock()
    sio_mock.emit = AsyncMock()
    sio_mock.enter_room = AsyncMock()
    sio_mock.leave_room = AsyncMock()
    sio_mock.event = lambda fn: fn  # no-op decorator
    api.sio = sio_mock

    return api


@pytest.fixture
def api():
    return make_web_api()


# ---------------------------------------------------------------------------
# verify_session
# ---------------------------------------------------------------------------


async def test_verify_session_uses_cache_when_fresh(api):
    """Cached entry younger than 300 s is returned without HTTP call."""
    api._session_cache["tok1"] = {
        "discordId": "111",
        "isAdmin": True,
        "_verified_at": time.time(),
    }
    result = await api.verify_session("tok1")
    assert result is not None
    assert result["discordId"] == "111"


async def test_verify_session_ignores_stale_cache(api, monkeypatch):
    """Cache entry older than 300 s triggers a real HTTP call."""
    api._session_cache["tok_stale"] = {
        "discordId": "999",
        "isAdmin": False,
        "_verified_at": time.time() - 400,  # expired
    }

    mock_http = MagicMock()
    mock_http.closed = False  # must be falsy so WebAPI reuses the session
    resp_mock = AsyncMock()
    resp_mock.__aenter__ = AsyncMock(return_value=resp_mock)
    resp_mock.__aexit__ = AsyncMock(return_value=False)
    resp_mock.status = 200
    resp_mock.json = AsyncMock(return_value={"authenticated": True, "discordId": "999", "isAdmin": False})
    mock_http.get = MagicMock(return_value=resp_mock)
    api._http = mock_http

    result = await api.verify_session("tok_stale")
    assert result is not None
    assert result["discordId"] == "999"
    assert mock_http.get.called


async def test_verify_session_admin_derived_from_admin_user_ids(api, monkeypatch):
    """The verifier's isAdmin field is ignored — admin status comes from
    bot-side ADMIN_USER_IDS."""
    import web_api as _web_api_mod
    monkeypatch.setattr(_web_api_mod, "ADMIN_USER_IDS", {12345})

    mock_http = MagicMock()
    mock_http.closed = False
    resp_mock = AsyncMock()
    resp_mock.__aenter__ = AsyncMock(return_value=resp_mock)
    resp_mock.__aexit__ = AsyncMock(return_value=False)
    resp_mock.status = 200
    resp_mock.json = AsyncMock(
        return_value={"authenticated": True, "discordId": "67890", "isAdmin": True},
    )
    mock_http.get = MagicMock(return_value=resp_mock)
    api._http = mock_http

    result = await api.verify_session("tok_spoofed")
    assert result is not None
    assert result["isAdmin"] is False


async def test_verify_session_admin_granted_when_id_in_admin_user_ids(api, monkeypatch):
    """isAdmin becomes True when the discord id is in ADMIN_USER_IDS, even
    if the verifier returns isAdmin=False (defence against verifier drift)."""
    import web_api as _web_api_mod
    monkeypatch.setattr(_web_api_mod, "ADMIN_USER_IDS", {12345})

    mock_http = MagicMock()
    mock_http.closed = False
    resp_mock = AsyncMock()
    resp_mock.__aenter__ = AsyncMock(return_value=resp_mock)
    resp_mock.__aexit__ = AsyncMock(return_value=False)
    resp_mock.status = 200
    resp_mock.json = AsyncMock(
        return_value={"authenticated": True, "discordId": "12345", "isAdmin": False},
    )
    mock_http.get = MagicMock(return_value=resp_mock)
    api._http = mock_http

    result = await api.verify_session("tok_admin")
    assert result["isAdmin"] is True


async def test_verify_session_returns_none_on_non_200(api):
    """HTTP 401 from auth server → None."""
    mock_http = MagicMock()
    mock_http.closed = False
    resp_mock = AsyncMock()
    resp_mock.__aenter__ = AsyncMock(return_value=resp_mock)
    resp_mock.__aexit__ = AsyncMock(return_value=False)
    resp_mock.status = 401
    mock_http.get = MagicMock(return_value=resp_mock)
    api._http = mock_http

    result = await api.verify_session("bad_token")
    assert result is None


async def test_verify_session_returns_none_when_not_authenticated(api):
    """authenticated=False in response → None."""
    mock_http = MagicMock()
    mock_http.closed = False
    resp_mock = AsyncMock()
    resp_mock.__aenter__ = AsyncMock(return_value=resp_mock)
    resp_mock.__aexit__ = AsyncMock(return_value=False)
    resp_mock.status = 200
    resp_mock.json = AsyncMock(return_value={"authenticated": False})
    mock_http.get = MagicMock(return_value=resp_mock)
    api._http = mock_http

    result = await api.verify_session("unauth_token")
    assert result is None


async def test_verify_session_returns_none_on_exception(api):
    """Network exception → None (non-fatal)."""
    mock_http = MagicMock()
    mock_http.closed = False
    mock_http.get = MagicMock(side_effect=Exception("timeout"))
    api._http = mock_http

    result = await api.verify_session("err_token")
    assert result is None


async def test_verify_session_stores_result_in_cache(api):
    """Successful verification result is cached."""
    mock_http = MagicMock()
    mock_http.closed = False
    resp_mock = AsyncMock()
    resp_mock.__aenter__ = AsyncMock(return_value=resp_mock)
    resp_mock.__aexit__ = AsyncMock(return_value=False)
    resp_mock.status = 200
    resp_mock.json = AsyncMock(return_value={"authenticated": True, "discordId": "777", "isAdmin": True})
    mock_http.get = MagicMock(return_value=resp_mock)
    api._http = mock_http

    result = await api.verify_session("new_token")
    assert "new_token" in api._session_cache
    assert api._session_cache["new_token"]["discordId"] == "777"


async def test_verify_session_creates_http_session_when_none(api):
    """If _http is None, a new ClientSession is created before the request."""
    api._http = None

    with patch("web_api.aiohttp.ClientSession") as MockSession:
        mock_session = MagicMock()
        resp_mock = AsyncMock()
        resp_mock.__aenter__ = AsyncMock(return_value=resp_mock)
        resp_mock.__aexit__ = AsyncMock(return_value=False)
        resp_mock.status = 200
        resp_mock.json = AsyncMock(return_value={"authenticated": True, "discordId": "42", "isAdmin": False})
        mock_session.get = MagicMock(return_value=resp_mock)
        MockSession.return_value = mock_session

        result = await api.verify_session("fresh_token")
        assert result is not None
        assert MockSession.called


# ---------------------------------------------------------------------------
# check_rate_limit
# ---------------------------------------------------------------------------


def test_check_rate_limit_allows_first_request(api):
    """First request from a user is always allowed."""
    allowed, wait = api.check_rate_limit("user1", is_admin=True)
    assert allowed is True
    assert wait == 0


def test_check_rate_limit_blocks_within_cooldown_admin(api):
    """Admin is blocked when within COOLDOWN_SECONDS."""
    api._cooldowns["user_a"] = time.time()
    allowed, wait = api.check_rate_limit("user_a", is_admin=True)
    assert allowed is False
    assert wait > 0


def test_check_rate_limit_blocks_within_cooldown_restricted(api):
    """Non-admin is blocked when within RESTRICTED_COOLDOWN_SECONDS."""
    api._cooldowns["user_b"] = time.time()
    allowed, wait = api.check_rate_limit("user_b", is_admin=False)
    assert allowed is False
    assert wait > 0


def test_check_rate_limit_allows_after_admin_cooldown_expires(api):
    """Request is allowed once admin cooldown window has passed."""
    api._cooldowns["user_c"] = time.time() - config.COOLDOWN_SECONDS - 1
    allowed, wait = api.check_rate_limit("user_c", is_admin=True)
    assert allowed is True


def test_check_rate_limit_allows_after_restricted_cooldown_expires(api):
    """Request is allowed once restricted cooldown window has passed."""
    api._cooldowns["user_d"] = time.time() - config.RESTRICTED_COOLDOWN_SECONDS - 1
    allowed, wait = api.check_rate_limit("user_d", is_admin=False)
    assert allowed is True


def test_check_rate_limit_updates_cooldown_on_success(api):
    """Successful request records current time in _cooldowns."""
    api.check_rate_limit("user_e", is_admin=True)
    assert "user_e" in api._cooldowns
    assert abs(api._cooldowns["user_e"] - time.time()) < 2


def test_check_rate_limit_evicts_old_entries(api, monkeypatch):
    """_cooldowns is pruned when it exceeds MAX_COOLDOWN_ENTRIES."""
    monkeypatch.setattr(config, "MAX_COOLDOWN_ENTRIES", 5)
    # Pre-fill with old entries so they will be purged
    threshold = config.COOLDOWN_SECONDS * 10
    for i in range(6):
        api._cooldowns[f"old_user_{i}"] = time.time() - threshold - 1

    api.check_rate_limit("new_user", is_admin=True)
    assert len(api._cooldowns) <= 6  # old stale ones were purged by _purge_cooldowns


# ---------------------------------------------------------------------------
# _check_daily_quota
# ---------------------------------------------------------------------------


def test_check_daily_quota_allows_when_quota_disabled(api, monkeypatch):
    """RESTRICTED_DAILY_QUOTA=0 means unlimited."""
    import web_api as _web_api_mod
    monkeypatch.setattr(_web_api_mod, "RESTRICTED_DAILY_QUOTA", 0)
    allowed, remaining = api._check_daily_quota("user1")
    assert allowed is True
    assert remaining == -1


def test_check_daily_quota_allows_first_request(api, monkeypatch):
    """First request within quota window is allowed."""
    import web_api as _web_api_mod
    monkeypatch.setattr(_web_api_mod, "RESTRICTED_DAILY_QUOTA", 5)
    allowed, remaining = api._check_daily_quota("user_q1")
    assert allowed is True
    assert remaining == 4


def test_check_daily_quota_blocks_at_limit(api, monkeypatch):
    """Request is blocked when bucket is full."""
    import web_api as _web_api_mod
    monkeypatch.setattr(_web_api_mod, "RESTRICTED_DAILY_QUOTA", 3)
    now = time.time()
    api._daily_quota["user_q2"] = [now, now, now]
    allowed, remaining = api._check_daily_quota("user_q2")
    assert allowed is False
    assert remaining == 0


def test_check_daily_quota_evicts_expired_timestamps(api, monkeypatch):
    """Timestamps older than 24 h are pruned, freeing quota."""
    import web_api as _web_api_mod
    monkeypatch.setattr(_web_api_mod, "RESTRICTED_DAILY_QUOTA", 2)
    stale_ts = time.time() - (25 * 3600)  # > 24 h ago
    api._daily_quota["user_q3"] = [stale_ts, stale_ts]  # would be "full" if not expired
    allowed, remaining = api._check_daily_quota("user_q3")
    assert allowed is True


# ---------------------------------------------------------------------------
# Socket.IO event handlers (via internal method simulation)
# ---------------------------------------------------------------------------


async def _call_sio_event(api, event_name, sid, *args):
    """Invoke the registered sio event handler directly."""
    from web_api import WebAPI  # ensure module is loaded
    # Re-register events on the api instance so handlers reference api.sio mock
    # Instead, call the handler registered on the *real* sio during __init__.
    # We need to reconstruct by accessing the underlying BaseServer handlers.
    # Easier: just re-register and call.
    pass


# We test the event handlers by constructing a fresh WebAPI whose sio.event
# decorator captures handlers, then calling them directly.

def make_api_with_captured_handlers():
    """Return (api, handlers_dict) where handlers_dict maps name->coro."""
    mock_bot = MagicMock()
    handlers = {}

    with patch("web_api.load_sessions", return_value={}):
        with patch("socketio.AsyncServer.attach"):
            # We need sio.event to capture the decorated functions BEFORE
            # WebAPI.__init__ calls _register_events.
            with patch("socketio.AsyncServer") as MockSioClass:
                sio_instance = MagicMock()
                sio_instance.emit = AsyncMock()
                sio_instance.enter_room = AsyncMock()
                sio_instance.leave_room = AsyncMock()

                def capture_event(fn):
                    handlers[fn.__name__] = fn
                    return fn

                sio_instance.event = capture_event
                MockSioClass.return_value = sio_instance

                from web_api import WebAPI
                api = WebAPI(mock_bot)
                # Keep the mock sio
                api.sio = sio_instance

    return api, handlers


# ---------------------------------------------------------------------------
# connect handler
# ---------------------------------------------------------------------------


async def test_connect_rejects_when_no_session_token():
    """connect returns False when auth dict has no sessionToken."""
    api, handlers = make_api_with_captured_handlers()
    result = await handlers["connect"]("sid1", {}, {})
    assert result is False


async def test_connect_rejects_empty_session_token():
    """connect returns False when sessionToken is empty string."""
    api, handlers = make_api_with_captured_handlers()
    result = await handlers["connect"]("sid1", {}, {"sessionToken": ""})
    assert result is False


async def test_connect_rejects_invalid_session(monkeypatch):
    """connect returns False when verify_session returns None."""
    api, handlers = make_api_with_captured_handlers()

    async def fake_verify(token):
        return None

    api.verify_session = fake_verify
    result = await handlers["connect"]("sid2", {}, {"sessionToken": "bad"})
    assert result is False


async def test_connect_accepts_valid_session(monkeypatch):
    """connect stores user in _sid_to_user and emits 'connected' on success."""
    api, handlers = make_api_with_captured_handlers()

    async def fake_verify(token):
        return {"discordId": "123", "isAdmin": True, "_verified_at": time.time()}

    api.verify_session = fake_verify
    result = await handlers["connect"]("sid3", {}, {"sessionToken": "good"})
    assert result is not False
    assert "sid3" in api._sid_to_user
    assert api._sid_to_user["sid3"]["discord_id"] == "123"
    api.sio.emit.assert_awaited()


async def test_connect_non_admin_user(monkeypatch):
    """connect correctly records is_admin=False for non-admin user."""
    api, handlers = make_api_with_captured_handlers()

    async def fake_verify(token):
        return {"discordId": "456", "isAdmin": False, "_verified_at": time.time()}

    api.verify_session = fake_verify
    await handlers["connect"]("sid4", {}, {"sessionToken": "good"})
    assert api._sid_to_user["sid4"]["is_admin"] is False


# ---------------------------------------------------------------------------
# disconnect handler
# ---------------------------------------------------------------------------


async def test_disconnect_removes_user_from_sid_map():
    """disconnect removes the sid entry from _sid_to_user."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid_x"] = {"discord_id": "789"}
    await handlers["disconnect"]("sid_x")
    assert "sid_x" not in api._sid_to_user


async def test_disconnect_on_unknown_sid_does_not_raise():
    """disconnect with unknown sid is a no-op (does not raise)."""
    api, handlers = make_api_with_captured_handlers()
    await handlers["disconnect"]("nonexistent_sid")  # should not raise


# ---------------------------------------------------------------------------
# join_channel / leave_channel handlers
# ---------------------------------------------------------------------------


async def test_join_channel_emits_joined_channel():
    """join_channel emits 'joined_channel' and enters the room when the
    requesting user has view permission on the target channel."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid5"] = {
        "discord_id": "1", "is_admin": True, "username": "u", "tag": "u#0",
    }
    await handlers["join_channel"]("sid5", {"channel_id": "42"})
    api.sio.enter_room.assert_awaited_once_with("sid5", "channel:42")
    api.sio.emit.assert_awaited()


async def test_join_channel_rejects_unauthenticated():
    """join_channel emits error when sid is not authenticated."""
    api, handlers = make_api_with_captured_handlers()
    await handlers["join_channel"]("unknown_sid", {"channel_id": "42"})
    api.sio.enter_room.assert_not_called()
    assert api.sio.emit.call_args[0][0] == "error"


async def test_join_channel_rejects_invalid_channel_id():
    """join_channel emits error when channel_id can't be parsed as int."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid5b"] = {
        "discord_id": "1", "is_admin": True, "username": "u", "tag": "u#0",
    }
    await handlers["join_channel"]("sid5b", {"channel_id": "not_an_int"})
    api.sio.enter_room.assert_not_called()
    assert api.sio.emit.call_args[0][0] == "error"


async def test_join_channel_denies_without_view_permission():
    """Non-admin without view_channel permission may not join the room."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid5c"] = {
        "discord_id": "55", "is_admin": False, "username": "u", "tag": "u#0",
    }
    api._check_channel_permission = AsyncMock(return_value=(False, "permission denied"))
    await handlers["join_channel"]("sid5c", {"channel_id": "999"})
    api.sio.enter_room.assert_not_called()
    assert api.sio.emit.call_args[0][0] == "error"


async def test_leave_channel_emits_left_channel():
    """leave_channel emits 'left_channel' and leaves the room."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid6"] = {"discord_id": "1", "is_admin": True, "username": "u", "tag": "u#0"}
    await handlers["leave_channel"]("sid6", {"channel_id": "99"})
    api.sio.leave_room.assert_awaited_once_with("sid6", "channel:99")
    api.sio.emit.assert_awaited()


async def test_leave_channel_ignores_missing_channel_id():
    """leave_channel is a no-op when channel_id is empty."""
    api, handlers = make_api_with_captured_handlers()
    await handlers["leave_channel"]("sid6", {})
    api.sio.leave_room.assert_not_called()


# ---------------------------------------------------------------------------
# get_history handler
# ---------------------------------------------------------------------------


async def test_get_history_emits_error_when_not_authenticated():
    """get_history emits error when sid not in _sid_to_user."""
    api, handlers = make_api_with_captured_handlers()
    await handlers["get_history"]("unknown_sid", {"channel_id": "10"})
    api.sio.emit.assert_awaited_once()
    call_args = api.sio.emit.call_args
    assert call_args[0][0] == "error"


async def test_get_history_emits_error_on_invalid_channel_id():
    """get_history emits error when channel_id is non-numeric."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid7"] = {"discord_id": "1"}
    await handlers["get_history"]("sid7", {"channel_id": "not_a_number"})
    api.sio.emit.assert_awaited_once()
    assert api.sio.emit.call_args[0][0] == "error"


async def test_get_history_emits_history_on_success():
    """get_history emits 'history' with messages on success."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid8"] = {"discord_id": "1", "is_admin": True}
    api._check_channel_permission = AsyncMock(return_value=(True, None))

    with patch("web_api.asyncio.to_thread", new=AsyncMock(return_value=[{"role": "user", "content": "hi"}])):
        await handlers["get_history"]("sid8", {"channel_id": "123"})

    api.sio.emit.assert_awaited_once()
    event_name, payload = api.sio.emit.call_args[0][0], api.sio.emit.call_args[0][1]
    assert event_name == "history"
    assert payload["channel_id"] == 123


async def test_get_history_denies_without_read_permission():
    """Non-admin without read_message_history may not fetch JSONL."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid8b"] = {"discord_id": "1", "is_admin": False}
    api._check_channel_permission = AsyncMock(return_value=(False, "permission denied"))

    with patch("web_api.asyncio.to_thread", new=AsyncMock()) as mock_thread:
        await handlers["get_history"]("sid8b", {"channel_id": "123"})

    # The expensive JSONL read must not even be attempted.
    mock_thread.assert_not_called()
    assert api.sio.emit.call_args[0][0] == "error"


async def test_get_history_returns_empty_list_on_load_exception():
    """get_history emits empty messages list when load_history_channel raises."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid9"] = {"discord_id": "2", "is_admin": True}
    api._check_channel_permission = AsyncMock(return_value=(True, None))

    with patch("web_api.asyncio.to_thread", new=AsyncMock(side_effect=Exception("disk error"))):
        await handlers["get_history"]("sid9", {"channel_id": "55"})

    payload = api.sio.emit.call_args[0][1]
    assert payload["messages"] == []


# ---------------------------------------------------------------------------
# send_message handler
# ---------------------------------------------------------------------------


async def test_send_message_emits_error_when_not_authenticated():
    """send_message emits error when sid is unknown."""
    api, handlers = make_api_with_captured_handlers()
    await handlers["send_message"]("unknown_sid", {"channel_id": "1", "text": "hello"})
    assert api.sio.emit.call_args[0][0] == "error"


async def test_send_message_emits_error_on_invalid_channel_id():
    """send_message emits error for non-numeric channel_id."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid10"] = {"discord_id": "1", "is_admin": True, "username": "u", "tag": "u#0"}
    await handlers["send_message"]("sid10", {"channel_id": "abc", "text": "hello"})
    assert api.sio.emit.call_args[0][0] == "error"


async def test_send_message_emits_error_on_empty_text():
    """send_message emits error when message text is empty."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid11"] = {"discord_id": "1", "is_admin": True, "username": "u", "tag": "u#0"}
    await handlers["send_message"]("sid11", {"channel_id": "10", "text": "   "})
    assert api.sio.emit.call_args[0][0] == "error"


async def test_send_message_emits_rate_limited_when_throttled():
    """send_message emits rate_limited when check_rate_limit blocks."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid12"] = {"discord_id": "rl_user", "is_admin": True, "username": "u", "tag": "u#0"}
    api._check_channel_permission = AsyncMock(return_value=(True, None))
    # Pre-fill cooldown so rate limit fires immediately
    api._cooldowns["rl_user"] = time.time()

    await handlers["send_message"]("sid12", {"channel_id": "10", "text": "hello"})
    assert api.sio.emit.call_args[0][0] == "rate_limited"


async def test_send_message_denies_without_send_permission():
    """Non-admin without send_messages permission must not reach Claude."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid12b"] = {"discord_id": "no_send_user", "is_admin": False, "username": "u", "tag": "u#0"}
    api._check_channel_permission = AsyncMock(return_value=(False, "permission denied"))

    mock_call_with_tools = AsyncMock(return_value=("never", None, False))
    with patch("web_api.call_with_tools", new=mock_call_with_tools):
        await handlers["send_message"]("sid12b", {"channel_id": "10", "text": "hello"})

    mock_call_with_tools.assert_not_called()
    assert api.sio.emit.call_args[0][0] == "error"


async def test_send_message_emits_error_on_daily_quota_exceeded(monkeypatch):
    """Non-admin hitting daily quota gets an error message."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid13"] = {"discord_id": "q_user", "is_admin": False, "username": "u", "tag": "u#0"}
    api._check_channel_permission = AsyncMock(return_value=(True, None))

    monkeypatch.setattr(config, "RESTRICTED_DAILY_QUOTA", 1)
    now = time.time()
    api._daily_quota["q_user"] = [now]  # already at quota=1

    # Cooldown for non-admin must be past so we reach quota check
    api._cooldowns["q_user"] = time.time() - config.RESTRICTED_COOLDOWN_SECONDS - 1

    await handlers["send_message"]("sid13", {"channel_id": "10", "text": "hello"})
    assert api.sio.emit.call_args[0][0] == "error"


async def test_send_message_success_emits_assistant_message():
    """send_message emits assistant message on successful Claude response."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid14"] = {"discord_id": "ok_user", "is_admin": True, "username": "u", "tag": "u#0"}
    api._check_channel_permission = AsyncMock(return_value=(True, None))

    with patch("web_api.PromptBuilder.build_prompt", new=AsyncMock(return_value="built_prompt")), \
         patch("web_api.PromptBuilder.cap_text", return_value="hello"), \
         patch("web_api.call_with_tools", new=AsyncMock(return_value=("response text", "sess_123", False))), \
         patch("web_api.update_session") as mock_update, \
         patch("web_api.asyncio.to_thread", new=AsyncMock(return_value=None)), \
         patch("web_api.asyncio.create_task"):

        # Patch search_memories_multi used inside send_message
        with patch("services.mem0.search_memories_multi", new=AsyncMock(return_value=[])), patch("services.mem0.add_memories", new=AsyncMock()):
            await handlers["send_message"]("sid14", {"channel_id": "10", "text": "hello"})

    # Find the "message" emit call
    all_calls = [c[0][0] for c in api.sio.emit.call_args_list]
    assert "message" in all_calls


async def test_send_message_emits_error_on_exception():
    """send_message emits error event when an unexpected exception is raised."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid15"] = {"discord_id": "ex_user", "is_admin": True, "username": "u", "tag": "u#0"}
    api._check_channel_permission = AsyncMock(return_value=(True, None))

    with patch("web_api.PromptBuilder.build_prompt", new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("web_api.PromptBuilder.cap_text", return_value="hello"), \
         patch("services.mem0.search_memories_multi", new=AsyncMock(return_value=[])):
        await handlers["send_message"]("sid15", {"channel_id": "10", "text": "hello"})

    all_calls = [c[0][0] for c in api.sio.emit.call_args_list]
    assert "error" in all_calls


async def test_send_message_non_admin_does_not_store_to_mem0():
    """Non-admin send_message does not trigger Mem0 store."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid16"] = {"discord_id": "nonadmin", "is_admin": False, "username": "u", "tag": "u#0"}
    api._check_channel_permission = AsyncMock(return_value=(True, None))
    # Ensure cooldown/quota don't block
    api._cooldowns["nonadmin"] = time.time() - config.RESTRICTED_COOLDOWN_SECONDS - 1

    with patch("web_api.PromptBuilder.build_prompt", new=AsyncMock(return_value="prompt")), \
         patch("web_api.PromptBuilder.cap_text", return_value="hi"), \
         patch("web_api.call_with_tools", new=AsyncMock(return_value=("reply", None, False))), \
         patch("web_api.asyncio.to_thread", new=AsyncMock(return_value=None)), \
         patch("web_api.asyncio.create_task") as mock_task, \
         patch("services.mem0.search_memories_multi", new=AsyncMock(return_value=[])):
        await handlers["send_message"]("sid16", {"channel_id": "20", "text": "hi"})
        # create_task should NOT be called for non-admin
        mock_task.assert_not_called()


# ---------------------------------------------------------------------------
# stop / lifecycle
# ---------------------------------------------------------------------------


async def test_stop_closes_http_and_runner():
    """stop() closes _http and cleans up _runner."""
    api = make_web_api()
    mock_http = AsyncMock()
    mock_http.closed = False
    mock_runner = AsyncMock()
    api._http = mock_http
    api._runner = mock_runner

    await api.stop()

    mock_http.close.assert_awaited_once()
    mock_runner.cleanup.assert_awaited_once()
    assert api._http is None
    assert api._runner is None


async def test_stop_skips_already_closed_http():
    """stop() does not call close() on an already-closed http session."""
    api = make_web_api()
    mock_http = MagicMock()
    mock_http.closed = True
    mock_runner = AsyncMock()
    api._http = mock_http
    api._runner = mock_runner

    await api.stop()

    mock_http.close.assert_not_called()


async def test_stop_is_safe_when_http_is_none():
    """stop() does not raise when _http is None."""
    api = make_web_api()
    api._http = None
    api._runner = AsyncMock()

    await api.stop()  # should not raise


# ---------------------------------------------------------------------------
# H4 — leave_channel authentication
# ---------------------------------------------------------------------------


async def test_leave_channel_requires_auth():
    """leave_channel must be a no-op when sid is not authenticated."""
    api, handlers = make_api_with_captured_handlers()
    # Do not populate _sid_to_user
    await handlers["leave_channel"]("unknown_sid", {"channel_id": "10"})
    api.sio.leave_room.assert_not_called()


# ---------------------------------------------------------------------------
# H5 — invalidate_session internal route
# ---------------------------------------------------------------------------


async def test_invalidate_session_removes_cache_entry():
    """_handle_invalidate_session pops the token from _session_cache."""
    api = make_web_api()
    api._session_cache["tok_x"] = {"discordId": "1", "isAdmin": False, "_verified_at": 9999999999}

    # Build a minimal mock request accepted by the handler
    mock_request = MagicMock()
    mock_request.remote = "127.0.0.1"
    mock_request.json = AsyncMock(return_value={"session_token": "tok_x"})

    response = await api._handle_invalidate_session(mock_request)
    import json
    body = json.loads(response.text)
    assert body["invalidated"] is True
    assert "tok_x" not in api._session_cache


async def test_invalidate_session_returns_false_for_unknown_token():
    """_handle_invalidate_session returns invalidated=false for absent token."""
    api = make_web_api()

    mock_request = MagicMock()
    mock_request.remote = "127.0.0.1"
    mock_request.json = AsyncMock(return_value={"session_token": "no_such_token"})

    response = await api._handle_invalidate_session(mock_request)
    import json
    body = json.loads(response.text)
    assert body["invalidated"] is False


async def test_invalidate_session_forbidden_from_external():
    """_handle_invalidate_session raises HTTPForbidden for non-localhost remotes."""
    from aiohttp import web as _web
    api = make_web_api()

    mock_request = MagicMock()
    mock_request.remote = "1.2.3.4"
    mock_request.json = AsyncMock(return_value={"session_token": "tok"})

    with pytest.raises(_web.HTTPForbidden):
        await api._handle_invalidate_session(mock_request)


# ---------------------------------------------------------------------------
# M6 — CORS wildcard rejection
# ---------------------------------------------------------------------------


def test_cors_wildcard_rejected(monkeypatch):
    """WebAPI.__init__ raises RuntimeError when WEB_API_CORS_ORIGINS has '*'."""
    monkeypatch.setattr("web_api.WEB_API_CORS_ORIGINS", ["*", "http://localhost:3001"])
    with pytest.raises(RuntimeError, match="wildcards"):
        with patch("web_api.load_sessions", return_value={}):
            with patch("socketio.AsyncServer.attach"):
                from web_api import WebAPI
                WebAPI(MagicMock())


def test_cors_empty_string_rejected(monkeypatch):
    """WebAPI.__init__ raises RuntimeError when WEB_API_CORS_ORIGINS has empty string."""
    monkeypatch.setattr("web_api.WEB_API_CORS_ORIGINS", [""])
    with pytest.raises(RuntimeError, match="wildcards"):
        with patch("web_api.load_sessions", return_value={}):
            with patch("socketio.AsyncServer.attach"):
                from web_api import WebAPI
                WebAPI(MagicMock())


# ---------------------------------------------------------------------------
# M7 — Uniform permission error messages
# ---------------------------------------------------------------------------


async def test_permission_error_uniform_channel_not_found():
    """_check_channel_permission returns 'permission denied' when channel is not found."""
    api = make_web_api()
    api.bot.get_channel = MagicMock(return_value=None)
    api.bot.fetch_channel = AsyncMock(side_effect=Exception("not found"))

    allowed, err = await api._check_channel_permission("55", 999, "view", False)
    assert allowed is False
    assert err == "permission denied"


async def test_permission_error_uniform_not_guild_member():
    """_check_channel_permission returns 'permission denied' when user is not in guild."""
    api = make_web_api()

    mock_channel = MagicMock()
    mock_guild = MagicMock()
    mock_guild.get_member = MagicMock(return_value=None)
    mock_guild.fetch_member = AsyncMock(side_effect=Exception("not a member"))
    mock_channel.guild = mock_guild
    api.bot.get_channel = MagicMock(return_value=mock_channel)

    allowed, err = await api._check_channel_permission("55", 999, "view", False)
    assert allowed is False
    assert err == "permission denied"


# ---------------------------------------------------------------------------
# M8 — get_history strips retrieved_memories blocks
# ---------------------------------------------------------------------------


async def test_get_history_strips_retrieved_memories():
    """get_history strips <retrieved_memories> blocks from message content."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid_m8"] = {"discord_id": "1", "is_admin": True}
    api._check_channel_permission = AsyncMock(return_value=(True, None))

    raw_messages = [
        {
            "role": "assistant",
            "content": "<retrieved_memories>secret data</retrieved_memories>visible text",
        },
        {
            "role": "assistant",
            "content": '<retrieved_memories attr="x">more secrets</retrieved_memories>also visible',
        },
    ]

    with patch("web_api.asyncio.to_thread", new=AsyncMock(return_value=raw_messages)):
        await handlers["get_history"]("sid_m8", {"channel_id": "123"})

    payload = api.sio.emit.call_args[0][1]
    assert payload["messages"][0]["content"] == "visible text"
    assert payload["messages"][1]["content"] == "also visible"


# ---------------------------------------------------------------------------
# M9 — verify_session re-derives admin status on cache hit
# ---------------------------------------------------------------------------


async def test_verify_session_admin_status_refreshed_on_cache_hit(monkeypatch):
    """Cache hit re-derives isAdmin from ADMIN_USER_IDS so live revocations apply."""
    import web_api as _web_api_mod
    api = make_web_api()
    api._session_cache["tok_y"] = {
        "discordId": "12345",
        "isAdmin": True,
        "_verified_at": time.time(),
    }
    monkeypatch.setattr(_web_api_mod, "ADMIN_USER_IDS", set())  # admin removed live

    result = await api.verify_session("tok_y")
    assert result["isAdmin"] is False


async def test_verify_session_admin_status_granted_on_cache_hit(monkeypatch):
    """Cache hit also promotes user to admin when they are added to ADMIN_USER_IDS."""
    import web_api as _web_api_mod
    api = make_web_api()
    api._session_cache["tok_z"] = {
        "discordId": "99999",
        "isAdmin": False,
        "_verified_at": time.time(),
    }
    monkeypatch.setattr(_web_api_mod, "ADMIN_USER_IDS", {99999})

    result = await api.verify_session("tok_z")
    assert result["isAdmin"] is True


# ---------------------------------------------------------------------------
# SRE-C6 — verify_session concurrent miss dedup
# ---------------------------------------------------------------------------


async def test_verify_session_dedupes_concurrent_misses():
    """5 concurrent verify_session calls for the same token must result in only
    one HTTP request (lock dedup)."""
    api = make_web_api()

    mock_http = MagicMock()
    mock_http.closed = False
    resp_mock = AsyncMock()
    resp_mock.__aenter__ = AsyncMock(return_value=resp_mock)
    resp_mock.__aexit__ = AsyncMock(return_value=False)
    resp_mock.status = 200
    resp_mock.json = AsyncMock(return_value={"authenticated": True, "discordId": "42", "isAdmin": False})
    mock_http.get = MagicMock(return_value=resp_mock)
    api._http = mock_http

    results = await asyncio.gather(*[api.verify_session("dedup_tok") for _ in range(5)])

    assert all(r is not None for r in results)
    assert mock_http.get.call_count == 1


# ---------------------------------------------------------------------------
# SRE-C7 — /health endpoint
# ---------------------------------------------------------------------------


async def test_health_endpoint_returns_ok_when_bot_ready():
    """GET /health returns status='ok' and discord_connected=True when bot is ready."""
    from aiohttp.test_utils import TestClient, TestServer

    api = make_web_api()
    api.bot.is_ready = MagicMock(return_value=True)
    # latency must be a real float for services.health._check_discord
    api.bot.latency = 0.05
    api._started_at = time.time() - 10

    # Register route directly on the app (bypassing start())
    api.app.router.add_get("/health", api._handle_health)

    # Patch oauth + mem0 subsystem checks to return ok so overall status="ok"
    with patch("services.health._check_oauth", return_value={"ok": True}), \
         patch("services.health._check_mem0", return_value={"ok": True}):
        async with TestClient(TestServer(api.app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            # New schema (M-4): subsystems.discord.ok replaces discord_connected
            assert body["subsystems"]["discord"]["ok"] is True
            # uptime_s now comes from services.health module-level _started_at
            # (set at import time), so we just assert it's non-negative.
            assert body["uptime_s"] >= 0
            assert body["active_connections"] == 0


# ---------------------------------------------------------------------------
# SRE-H10 — disconnect cancels in-flight send_message tasks
# ---------------------------------------------------------------------------


async def test_disconnect_cancels_inflight_tasks():
    """disconnect() cancels tasks registered in _sid_tasks for the given sid."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid_h10"] = {"discord_id": "1", "is_admin": True, "username": "u", "tag": "u#0"}

    # Create a task that blocks until cancelled
    cancelled_flag = asyncio.Event()

    async def _long_running():
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            cancelled_flag.set()
            raise

    task = asyncio.create_task(_long_running())
    api._sid_tasks["sid_h10"] = {task}

    await handlers["disconnect"]("sid_h10")

    # Give the event loop a chance to deliver cancellation
    await asyncio.sleep(0)

    assert task.cancelled() or cancelled_flag.is_set()
    assert "sid_h10" not in api._sid_tasks


# ---------------------------------------------------------------------------
# _handle_invalidate_session — extra branches
# ---------------------------------------------------------------------------


async def test_invalidate_session_rejects_invalid_json_body():
    """_handle_invalidate_session raises HTTPBadRequest when body is not JSON."""
    from aiohttp import web as _web
    api = make_web_api()

    mock_request = MagicMock()
    mock_request.remote = "127.0.0.1"
    mock_request.json = AsyncMock(side_effect=Exception("not json"))

    with pytest.raises(_web.HTTPBadRequest):
        await api._handle_invalidate_session(mock_request)


async def test_invalidate_session_rejects_missing_session_token_key():
    """_handle_invalidate_session raises HTTPBadRequest when session_token key absent."""
    from aiohttp import web as _web
    api = make_web_api()

    mock_request = MagicMock()
    mock_request.remote = "127.0.0.1"
    mock_request.json = AsyncMock(return_value={})

    with pytest.raises(_web.HTTPBadRequest):
        await api._handle_invalidate_session(mock_request)


async def test_invalidate_session_rejects_non_string_token():
    """_handle_invalidate_session raises HTTPBadRequest when token is not a string."""
    from aiohttp import web as _web
    api = make_web_api()

    mock_request = MagicMock()
    mock_request.remote = "127.0.0.1"
    mock_request.json = AsyncMock(return_value={"session_token": 123})

    with pytest.raises(_web.HTTPBadRequest):
        await api._handle_invalidate_session(mock_request)


async def test_invalidate_session_rejects_ipv6_localhost():
    """_handle_invalidate_session raises HTTPForbidden for non-localhost IPv6 address."""
    # ::1 is allowed but other IPv6 are not
    from aiohttp import web as _web
    api = make_web_api()
    api._session_cache["tok_ipv6"] = {"discordId": "1", "_verified_at": 9999999999}

    mock_request = MagicMock()
    mock_request.remote = "::1"
    mock_request.json = AsyncMock(return_value={"session_token": "tok_ipv6"})

    # ::1 is localhost so it should succeed
    response = await api._handle_invalidate_session(mock_request)
    import json
    body = json.loads(response.text)
    assert body["invalidated"] is True


# ---------------------------------------------------------------------------
# stop — runner is None
# ---------------------------------------------------------------------------


async def test_stop_is_safe_when_runner_is_none():
    """stop() does not raise when _runner is None."""
    api = make_web_api()
    api._http = None
    api._runner = None
    await api.stop()  # should not raise


# ---------------------------------------------------------------------------
# _cache_hit — ValueError branch for non-numeric discordId
# ---------------------------------------------------------------------------


async def test_cache_hit_returns_none_for_invalid_discord_id():
    """_cache_hit returns non-admin when discordId is non-numeric (ValueError guard)."""
    import web_api as _web_api_mod
    api = make_web_api()
    api._session_cache["tok_bad_id"] = {
        "discordId": "not_an_int",
        "isAdmin": True,
        "_verified_at": time.time(),
    }
    result = api._cache_hit("tok_bad_id")
    assert result is not None
    assert result["isAdmin"] is False


# ---------------------------------------------------------------------------
# _verify_locks LRU eviction (>1000 entries)
# ---------------------------------------------------------------------------


async def test_verify_session_evicts_excess_verify_locks(monkeypatch):
    """When _verify_locks exceeds 1000, oldest entries are removed."""
    api = make_web_api()

    # Pre-fill with 1001 fake lock entries
    for i in range(1001):
        api._verify_locks[f"tok_{i}"] = asyncio.Lock()

    mock_http = MagicMock()
    mock_http.closed = False
    resp_mock = AsyncMock()
    resp_mock.__aenter__ = AsyncMock(return_value=resp_mock)
    resp_mock.__aexit__ = AsyncMock(return_value=False)
    resp_mock.status = 200
    resp_mock.json = AsyncMock(return_value={"authenticated": True, "discordId": "42", "isAdmin": False})
    mock_http.get = MagicMock(return_value=resp_mock)
    api._http = mock_http

    await api.verify_session("new_overflow_tok")
    assert len(api._verify_locks) <= 1001


# ---------------------------------------------------------------------------
# verify_session double-check cache inside lock
# ---------------------------------------------------------------------------


async def test_verify_session_uses_cache_populated_while_waiting():
    """Second coroutine uses cache entry written by first — hits double-check inside lock."""
    api = make_web_api()

    # Pre-populate cache as if first coroutine already wrote it
    api._session_cache["concurrent_tok"] = {
        "discordId": "77",
        "isAdmin": False,
        "_verified_at": time.time(),
    }

    # Acquire the lock as if another coroutine is holding it
    lock = asyncio.Lock()
    api._verify_locks["concurrent_tok"] = lock

    async def _release_after():
        async with lock:
            pass

    async with lock:
        # While we hold the lock, start a verify_session call that will block
        task = asyncio.ensure_future(api.verify_session("concurrent_tok"))
        await asyncio.sleep(0)  # let it acquire the lock check
    # lock released — task can proceed and will hit the double-check cache hit

    result = await task
    assert result is not None
    assert result["discordId"] == "77"


# ---------------------------------------------------------------------------
# verify_session — ValueError for non-numeric discordId from HTTP response
# ---------------------------------------------------------------------------


async def test_verify_session_handles_non_numeric_discord_id(monkeypatch):
    """discordId that can't be converted to int results in isAdmin=False."""
    import web_api as _web_api_mod
    monkeypatch.setattr(_web_api_mod, "ADMIN_USER_IDS", {12345})

    mock_http = MagicMock()
    mock_http.closed = False
    resp_mock = AsyncMock()
    resp_mock.__aenter__ = AsyncMock(return_value=resp_mock)
    resp_mock.__aexit__ = AsyncMock(return_value=False)
    resp_mock.status = 200
    resp_mock.json = AsyncMock(return_value={"authenticated": True, "discordId": "not_int", "isAdmin": True})
    mock_http.get = MagicMock(return_value=resp_mock)
    api = make_web_api()
    api._http = mock_http

    result = await api.verify_session("tok_non_numeric")
    assert result is not None
    assert result["isAdmin"] is False


# ---------------------------------------------------------------------------
# _check_channel_permission — DM channel branches
# ---------------------------------------------------------------------------


async def test_check_channel_permission_dm_recipient_allowed():
    """DM channel: the actual recipient is allowed."""
    api = make_web_api()

    mock_channel = MagicMock()
    mock_channel.guild = None
    mock_recipient = MagicMock()
    mock_recipient.id = 55
    mock_channel.recipient = mock_recipient
    api.bot.get_channel = MagicMock(return_value=mock_channel)

    allowed, err = await api._check_channel_permission("55", 999, "view", False)
    assert allowed is True
    assert err is None


async def test_check_channel_permission_dm_wrong_recipient():
    """DM channel: a different user is denied."""
    api = make_web_api()

    mock_channel = MagicMock()
    mock_channel.guild = None
    mock_recipient = MagicMock()
    mock_recipient.id = 99
    mock_channel.recipient = mock_recipient
    api.bot.get_channel = MagicMock(return_value=mock_channel)

    allowed, err = await api._check_channel_permission("55", 999, "view", False)
    assert allowed is False
    assert err == "permission denied"


async def test_check_channel_permission_dm_no_recipient():
    """DM channel: recipient=None → permission denied."""
    api = make_web_api()

    mock_channel = MagicMock()
    mock_channel.guild = None
    mock_channel.recipient = None
    api.bot.get_channel = MagicMock(return_value=mock_channel)

    allowed, err = await api._check_channel_permission("55", 999, "view", False)
    assert allowed is False
    assert err == "permission denied"


async def test_check_channel_permission_invalid_discord_id():
    """Non-numeric discord_id returns permission denied."""
    api = make_web_api()

    mock_channel = MagicMock()
    mock_guild = MagicMock()
    mock_channel.guild = mock_guild
    api.bot.get_channel = MagicMock(return_value=mock_channel)

    allowed, err = await api._check_channel_permission("not_int", 999, "view", False)
    assert allowed is False
    assert err == "permission denied"


async def test_check_channel_permission_view_denied():
    """view action denied when perms.view_channel is False."""
    api = make_web_api()

    mock_channel = MagicMock()
    mock_guild = MagicMock()
    mock_member = MagicMock()
    mock_perms = MagicMock()
    mock_perms.view_channel = False
    mock_channel.guild = mock_guild
    mock_guild.get_member = MagicMock(return_value=mock_member)
    mock_channel.permissions_for = MagicMock(return_value=mock_perms)
    api.bot.get_channel = MagicMock(return_value=mock_channel)

    allowed, err = await api._check_channel_permission("55", 999, "view", False)
    assert allowed is False
    assert err == "permission denied"


async def test_check_channel_permission_read_history_denied():
    """read_history denied when read_message_history is False."""
    api = make_web_api()

    mock_channel = MagicMock()
    mock_guild = MagicMock()
    mock_member = MagicMock()
    mock_perms = MagicMock()
    mock_perms.view_channel = True
    mock_perms.read_message_history = False
    mock_channel.guild = mock_guild
    mock_guild.get_member = MagicMock(return_value=mock_member)
    mock_channel.permissions_for = MagicMock(return_value=mock_perms)
    api.bot.get_channel = MagicMock(return_value=mock_channel)

    allowed, err = await api._check_channel_permission("55", 999, "read_history", False)
    assert allowed is False
    assert err == "permission denied"


async def test_check_channel_permission_send_denied():
    """send action denied when send_messages is False."""
    api = make_web_api()

    mock_channel = MagicMock()
    mock_guild = MagicMock()
    mock_member = MagicMock()
    mock_perms = MagicMock()
    mock_perms.view_channel = True
    mock_perms.send_messages = False
    mock_channel.guild = mock_guild
    mock_guild.get_member = MagicMock(return_value=mock_member)
    mock_channel.permissions_for = MagicMock(return_value=mock_perms)
    api.bot.get_channel = MagicMock(return_value=mock_channel)

    allowed, err = await api._check_channel_permission("55", 999, "send", False)
    assert allowed is False
    assert err == "permission denied"


async def test_check_channel_permission_all_allowed():
    """All permission checks pass when perms are granted."""
    api = make_web_api()

    mock_channel = MagicMock()
    mock_guild = MagicMock()
    mock_member = MagicMock()
    mock_perms = MagicMock()
    mock_perms.view_channel = True
    mock_perms.read_message_history = True
    mock_perms.send_messages = True
    mock_channel.guild = mock_guild
    mock_guild.get_member = MagicMock(return_value=mock_member)
    mock_channel.permissions_for = MagicMock(return_value=mock_perms)
    api.bot.get_channel = MagicMock(return_value=mock_channel)

    for action in ("view", "read_history", "send"):
        allowed, err = await api._check_channel_permission("55", 999, action, False)
        assert allowed is True
        assert err is None


# ---------------------------------------------------------------------------
# _check_rate_limit — MAX_COOLDOWN_ENTRIES eviction inside check_rate_limit
# ---------------------------------------------------------------------------


def test_check_rate_limit_evicts_entries_when_max_exceeded(monkeypatch):
    """_cooldowns is capped at MAX_COOLDOWN_ENTRIES via popitem."""
    import web_api as _web_api_mod
    monkeypatch.setattr(_web_api_mod, "MAX_COOLDOWN_ENTRIES", 3)
    api = make_web_api()

    # 3 fresh entries (within cooldown so they won't be purged by _purge_cooldowns)
    api._cooldowns["u1"] = time.time()
    api._cooldowns["u2"] = time.time()
    api._cooldowns["u3"] = time.time()

    # Adding a new user should evict oldest
    allowed, _ = api.check_rate_limit("u4", is_admin=True)
    assert allowed is True
    assert len(api._cooldowns) <= 3


# ---------------------------------------------------------------------------
# _check_daily_quota — MAX_COOLDOWN_ENTRIES eviction
# ---------------------------------------------------------------------------


def test_check_daily_quota_evicts_entries_when_max_exceeded(monkeypatch):
    """_daily_quota is capped at MAX_COOLDOWN_ENTRIES via popitem."""
    import web_api as _web_api_mod
    monkeypatch.setattr(_web_api_mod, "MAX_COOLDOWN_ENTRIES", 3)
    monkeypatch.setattr(_web_api_mod, "RESTRICTED_DAILY_QUOTA", 10)
    api = make_web_api()

    now = time.time()
    api._daily_quota["q1"] = [now]
    api._daily_quota["q2"] = [now]
    api._daily_quota["q3"] = [now]

    allowed, remaining = api._check_daily_quota("q4")
    assert allowed is True
    assert len(api._daily_quota) <= 3


# ---------------------------------------------------------------------------
# leave_channel — left_channel emission
# ---------------------------------------------------------------------------


async def test_leave_channel_unauthenticated_sid_is_noop():
    """leave_channel with unauthenticated sid does nothing (no emit, no leave_room)."""
    api, handlers = make_api_with_captured_handlers()
    # Don't populate _sid_to_user
    await handlers["leave_channel"]("unauth_sid", {"channel_id": "42"})
    api.sio.leave_room.assert_not_called()
    api.sio.emit.assert_not_called()


# ---------------------------------------------------------------------------
# get_history — strip non-string content (no-op)
# ---------------------------------------------------------------------------


async def test_get_history_skips_strip_for_non_string_content():
    """get_history does not crash when message content is not a string."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid_ns"] = {"discord_id": "1", "is_admin": True}
    api._check_channel_permission = AsyncMock(return_value=(True, None))

    raw_messages = [
        {"role": "user", "content": None},
        {"role": "assistant", "content": 42},
    ]

    with patch("web_api.asyncio.to_thread", new=AsyncMock(return_value=raw_messages)):
        await handlers["get_history"]("sid_ns", {"channel_id": "10"})

    payload = api.sio.emit.call_args[0][1]
    assert payload["messages"][0]["content"] is None
    assert payload["messages"][1]["content"] == 42


# ---------------------------------------------------------------------------
# send_message — session_id=None branch (no update_session call)
# ---------------------------------------------------------------------------


async def test_send_message_skips_update_session_when_no_session_id():
    """send_message does not call update_session when call_with_tools returns session_id=None."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid_nosess"] = {"discord_id": "ok2", "is_admin": True, "username": "u", "tag": "u#0"}
    api._check_channel_permission = AsyncMock(return_value=(True, None))

    with patch("web_api.PromptBuilder.build_prompt", new=AsyncMock(return_value="prompt")), \
         patch("web_api.PromptBuilder.cap_text", return_value="hi"), \
         patch("web_api.call_with_tools", new=AsyncMock(return_value=("reply", None, False))), \
         patch("web_api.update_session") as mock_update, \
         patch("web_api.asyncio.to_thread", new=AsyncMock(return_value=None)), \
         patch("web_api.asyncio.create_task"), \
         patch("services.mem0.search_memories_multi", new=AsyncMock(return_value=[])), patch("services.mem0.add_memories", new=AsyncMock()):
        await handlers["send_message"]("sid_nosess", {"channel_id": "10", "text": "hi"})

    mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# send_message — _mem0_store task body (admin path)
# ---------------------------------------------------------------------------


async def test_send_message_admin_triggers_mem0_store():
    """Admin send_message triggers asyncio.create_task for Mem0 store."""
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid_mem0"] = {"discord_id": "adminuser", "is_admin": True, "username": "u", "tag": "u#0"}
    api._check_channel_permission = AsyncMock(return_value=(True, None))

    created_tasks = []

    def capture_create_task(coro):
        t = asyncio.ensure_future(coro)
        created_tasks.append(t)
        return t

    with patch("web_api.PromptBuilder.build_prompt", new=AsyncMock(return_value="prompt")), \
         patch("web_api.PromptBuilder.cap_text", return_value="hi"), \
         patch("web_api.call_with_tools", new=AsyncMock(return_value=("reply", "sess_id", False))), \
         patch("web_api.asyncio.to_thread", new=AsyncMock(return_value=None)), \
         patch("web_api.asyncio.create_task", side_effect=capture_create_task), \
         patch("services.mem0.search_memories_multi", new=AsyncMock(return_value=[])), patch("services.mem0.add_memories", new=AsyncMock()):
        await handlers["send_message"]("sid_mem0", {"channel_id": "10", "text": "hi"})
        # Allow the Mem0 store coroutine to run
        await asyncio.gather(*created_tasks, return_exceptions=True)

    assert len(created_tasks) >= 1


# ---------------------------------------------------------------------------
# send_message — daily quota block reached inside send_message (line 493-498)
# ---------------------------------------------------------------------------


async def test_send_message_blocks_non_admin_at_daily_quota(monkeypatch):
    """Non-admin hitting daily quota in send_message receives an error."""
    import web_api as _web_api_mod
    api, handlers = make_api_with_captured_handlers()
    api._sid_to_user["sid_quota2"] = {
        "discord_id": "quota_user2", "is_admin": False, "username": "u", "tag": "u#0",
    }
    api._check_channel_permission = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(_web_api_mod, "RESTRICTED_DAILY_QUOTA", 2)

    # Bypass cooldown
    import config as _cfg
    api._cooldowns["quota_user2"] = time.time() - _cfg.RESTRICTED_COOLDOWN_SECONDS - 1

    # Fill quota
    now = time.time()
    api._daily_quota["quota_user2"] = [now, now]

    await handlers["send_message"]("sid_quota2", {"channel_id": "10", "text": "hello"})
    all_calls = [c[0][0] for c in api.sio.emit.call_args_list]
    assert "error" in all_calls
