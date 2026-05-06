"""Socket.IO server — bridges Web UI to the Discord bot's Claude backend.

Runs alongside the Discord bot in the same asyncio event loop via
asyncio.gather(). Session auth is delegated to the Next.js API at
http://localhost:3001/api/session/verify.
"""

import asyncio
import logging
import time
from collections import OrderedDict

import aiohttp
import socketio
from aiohttp import web

from config import (
    ADMIN_USER_IDS,
    COOLDOWN_SECONDS,
    MAX_COOLDOWN_ENTRIES,
    MAX_PROMPT_CHARS,
    MAX_USER_MESSAGE_CHARS,
    RESTRICTED_COOLDOWN_SECONDS,
    RESTRICTED_DAILY_QUOTA,
    WEB_API_CORS_ORIGINS,
    WEB_API_PORT,
)
from services.claude_sdk import call_with_tools
from services.prompt_builder import PromptBuilder
from services.session import load_history_channel, load_sessions, update_session

log = logging.getLogger(__name__)

_QUOTA_WINDOW_S = 24 * 60 * 60


class WebAPI:
    def __init__(self, bot):
        self.bot = bot
        self.sio = socketio.AsyncServer(
            async_mode="aiohttp",
            cors_allowed_origins=WEB_API_CORS_ORIGINS,
            logger=False,
            engineio_logger=False,
        )
        self.app = web.Application()
        self.sio.attach(self.app)

        # session_token -> {discord_id, username, ...}
        self._session_cache: dict[str, dict] = {}
        # discord_id (str) -> last request epoch seconds
        self._cooldowns: OrderedDict[str, float] = OrderedDict()
        # discord_id (str) -> list[float] of request timestamps within quota window
        self._daily_quota: OrderedDict[str, list[float]] = OrderedDict()

        # Shared sessions dict (channel_id -> sdk session_id) used by call_with_tools
        self._sessions = load_sessions()

        # sid -> discord_id mapping (populated on connect)
        self._sid_to_user: dict[str, dict] = {}

        self._runner: web.AppRunner | None = None
        self._http: aiohttp.ClientSession | None = None

        self._register_events()

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------
    async def start(self, port: int = WEB_API_PORT) -> None:
        self._http = aiohttp.ClientSession()
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", port)
        await site.start()
        log.info("WebAPI Socket.IO server listening on port %d", port)
        # Keep running until cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()
            self._http = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        log.info("WebAPI stopped")

    # -----------------------------------------------------------------------
    # Session verification
    # -----------------------------------------------------------------------
    async def verify_session(self, session_token: str) -> dict | None:
        """Verify a session token against the Next.js /api/session/verify.

        The Next.js endpoint reads the session cookie and returns:
            {authenticated: true, discordId: str, isAdmin: bool}
        Caches valid results for 300s to avoid hammering the auth endpoint.
        """
        cached = self._session_cache.get(session_token)
        if cached:
            if time.time() - cached.get("_verified_at", 0) < 300:
                return cached

        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()

        try:
            async with self._http.get(
                "http://localhost:3001/api/session/verify",
                cookies={"next-auth.session-token": session_token},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not data.get("authenticated"):
                    return None
                user = {
                    "discordId": data["discordId"],
                    "isAdmin": data.get("isAdmin", False),
                    "_verified_at": time.time(),
                }
                self._session_cache[session_token] = user
                return user
        except Exception as e:
            log.warning("Session verify failed: %s", e)
            return None

    # -----------------------------------------------------------------------
    # Rate limiting
    # -----------------------------------------------------------------------
    def _purge_cooldowns(self) -> None:
        now = time.time()
        threshold = COOLDOWN_SECONDS * 10
        while self._cooldowns:
            oldest_id, oldest_ts = next(iter(self._cooldowns.items()))
            if now - oldest_ts <= threshold:
                break
            del self._cooldowns[oldest_id]

    def check_rate_limit(self, discord_id: str, is_admin: bool) -> tuple[bool, int]:
        """Return (allowed, wait_seconds). Updates the cooldown table on success."""
        self._purge_cooldowns()
        now = time.time()
        last = self._cooldowns.get(discord_id, 0)
        effective_cooldown = COOLDOWN_SECONDS if is_admin else RESTRICTED_COOLDOWN_SECONDS
        if now - last < effective_cooldown:
            remaining = int(effective_cooldown - (now - last))
            return False, remaining

        self._cooldowns[discord_id] = now
        self._cooldowns.move_to_end(discord_id)
        while len(self._cooldowns) > MAX_COOLDOWN_ENTRIES:
            self._cooldowns.popitem(last=False)
        return True, 0

    def _check_daily_quota(self, discord_id: str) -> tuple[bool, int]:
        """Return (allowed, remaining). Evicts expired entries lazily."""
        if RESTRICTED_DAILY_QUOTA <= 0:
            return True, -1
        now = time.time()
        bucket = self._daily_quota.get(discord_id, [])
        bucket = [t for t in bucket if now - t < _QUOTA_WINDOW_S]
        if len(bucket) >= RESTRICTED_DAILY_QUOTA:
            self._daily_quota[discord_id] = bucket
            return False, 0
        bucket.append(now)
        self._daily_quota[discord_id] = bucket
        self._daily_quota.move_to_end(discord_id)
        while len(self._daily_quota) > MAX_COOLDOWN_ENTRIES:
            self._daily_quota.popitem(last=False)
        return True, RESTRICTED_DAILY_QUOTA - len(bucket)

    # -----------------------------------------------------------------------
    # Socket.IO event registration
    # -----------------------------------------------------------------------
    def _register_events(self) -> None:
        sio = self.sio

        @sio.event
        async def connect(sid, environ, auth):
            """Authenticate and register the Socket.IO client."""
            token = (auth or {}).get("sessionToken", "")
            if not token:
                log.warning("WS connect rejected: no sessionToken (sid=%s)", sid)
                return False  # reject

            user = await self.verify_session(token)
            if user is None:
                log.warning("WS connect rejected: invalid session (sid=%s)", sid)
                return False

            discord_id = str(user.get("discordId", ""))
            is_admin = user.get("isAdmin", False)
            self._sid_to_user[sid] = {
                "discord_id": discord_id,
                "username": discord_id,  # Web UI doesn't have display name yet
                "tag": discord_id,
                "is_admin": is_admin,
            }
            log.info("WS connected: sid=%s discord_id=%s admin=%s", sid, discord_id, is_admin)
            await sio.emit("connected", {"status": "ok", "discord_id": discord_id}, to=sid)

        @sio.event
        async def disconnect(sid):
            self._sid_to_user.pop(sid, None)
            log.info("WS disconnected: sid=%s", sid)

        @sio.event
        async def join_channel(sid, data):
            """Join a channel room to receive channel-scoped events."""
            channel_id = str(data.get("channel_id", ""))
            if not channel_id:
                return
            await sio.enter_room(sid, f"channel:{channel_id}")
            log.debug("sid=%s joined channel room %s", sid, channel_id)
            await sio.emit("joined_channel", {"channel_id": channel_id}, to=sid)

        @sio.event
        async def leave_channel(sid, data):
            channel_id = str(data.get("channel_id", ""))
            if not channel_id:
                return
            await sio.leave_room(sid, f"channel:{channel_id}")
            await sio.emit("left_channel", {"channel_id": channel_id}, to=sid)

        @sio.event
        async def get_history(sid, data):
            """Return the recent JSONL history for a channel."""
            user_info = self._sid_to_user.get(sid)
            if not user_info:
                await sio.emit("error", {"message": "not authenticated"}, to=sid)
                return

            channel_id_raw = data.get("channel_id")
            try:
                channel_id = int(channel_id_raw)
            except (TypeError, ValueError):
                await sio.emit("error", {"message": "invalid channel_id"}, to=sid)
                return

            try:
                messages = await asyncio.to_thread(load_history_channel, channel_id)
            except Exception as e:
                log.warning("get_history failed for ch=%s: %s", channel_id, e)
                messages = []

            await sio.emit("history", {"channel_id": channel_id, "messages": messages}, to=sid)

        @sio.event
        async def send_message(sid, data):
            """Process a user message through Claude and stream status back."""
            user_info = self._sid_to_user.get(sid)
            if not user_info:
                await sio.emit("error", {"message": "not authenticated"}, to=sid)
                return

            discord_id = user_info["discord_id"]
            is_admin = user_info["is_admin"]
            sender_name = user_info["username"]
            sender_tag = user_info["tag"]

            channel_id_raw = data.get("channel_id")
            raw_text = str(data.get("text", "")).strip()

            try:
                channel_id = int(channel_id_raw)
            except (TypeError, ValueError):
                await sio.emit("error", {"message": "invalid channel_id"}, to=sid)
                return

            if not raw_text:
                await sio.emit("error", {"message": "empty message"}, to=sid)
                return

            # Rate limiting
            allowed, wait_s = self.check_rate_limit(discord_id, is_admin)
            if not allowed:
                await sio.emit(
                    "rate_limited",
                    {"wait_seconds": wait_s},
                    to=sid,
                )
                return

            # Daily quota (non-admin only)
            if not is_admin:
                quota_ok, remaining = self._check_daily_quota(discord_id)
                if not quota_ok:
                    await sio.emit(
                        "error",
                        {"message": "今日の利用上限に達しました。また明日来てください。"},
                        to=sid,
                    )
                    return

            raw_text = PromptBuilder.cap_text(raw_text, MAX_USER_MESSAGE_CHARS)

            # Status callback — relays Claude progress to the Web UI
            async def status_callback(text: str) -> None:
                await sio.emit(
                    "status",
                    {"phase": text, "channel_id": channel_id},
                    to=sid,
                )

            await sio.emit("status", {"phase": "⏳ 考え中...", "channel_id": channel_id}, to=sid)

            try:
                from services.mem0_service import search_memories_multi

                async def _mem0_fetch(queries, user_id):
                    return await search_memories_multi(queries, user_id, limit=8)

                prompt = await PromptBuilder.build_prompt(
                    raw_text=raw_text,
                    sender_name=sender_name,
                    sender_tag=sender_tag,
                    is_admin=is_admin,
                    mem0_user_id=discord_id,
                    mem0_fetch=_mem0_fetch,
                )

                log.info(
                    "WebAPI request: discord_id=%s channel=%d admin=%s len=%d",
                    discord_id, channel_id, is_admin, len(prompt),
                )

                response_text, session_id = await call_with_tools(
                    prompt, channel_id, self._sessions,
                    status_callback=status_callback,
                    restricted=not is_admin,
                    raw_user_text=raw_text,
                )

                if session_id:
                    await asyncio.to_thread(
                        update_session, self._sessions, channel_id, session_id
                    )

                await sio.emit(
                    "message",
                    {
                        "channel_id": channel_id,
                        "role": "assistant",
                        "content": response_text,
                    },
                    to=sid,
                )

                # Broadcast to all clients in the same channel room so other
                # Web UI tabs see the new message without polling.
                await sio.emit(
                    "message",
                    {
                        "channel_id": channel_id,
                        "role": "assistant",
                        "content": response_text,
                        "from_user": sender_name,
                    },
                    room=f"channel:{channel_id}",
                    skip_sid=sid,
                )

                # Store to Mem0 for admins (mirrors bot.py behaviour)
                if is_admin:
                    async def _mem0_store():
                        try:
                            from services.mem0_service import add_memories
                            msgs = [
                                {"role": "user", "content": raw_text},
                                {"role": "assistant", "content": response_text},
                            ]
                            await add_memories(msgs, discord_id, metadata={
                                "channel_id": str(channel_id),
                                "source": "web_ui",
                            })
                        except Exception as exc:
                            log.debug("Mem0 store failed (non-fatal): %s", exc)

                    asyncio.create_task(_mem0_store())

            except Exception:
                log.exception(
                    "Error processing web message: discord_id=%s channel=%d",
                    discord_id, channel_id,
                )
                await sio.emit(
                    "error",
                    {"message": "❌ エラーが発生しました。"},
                    to=sid,
                )
