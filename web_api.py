"""Socket.IO server — bridges Web UI to the Discord bot's Claude backend.

Runs alongside the Discord bot in the same asyncio event loop via
asyncio.gather(). Session auth is delegated to the Next.js API at
http://localhost:3001/api/session/verify.
"""

import asyncio
import logging
import re as _re
import time
from collections import OrderedDict

import aiohttp
import socketio
from aiohttp import web

_MEM_BLOCK_RE = _re.compile(r"<retrieved_memories\b[^>]*>.*?</retrieved_memories>", _re.DOTALL)


def _strip_mem_block(content: str) -> str:
    return _MEM_BLOCK_RE.sub("", content)

from config import (
    ADMIN_USER_IDS,
    COOLDOWN_SECONDS,
    MAX_COOLDOWN_ENTRIES,
    MAX_PROMPT_CHARS,
    MAX_USER_MESSAGE_CHARS,
    RESTRICTED_COOLDOWN_SECONDS,
    RESTRICTED_DAILY_QUOTA,
    WEB_API_CORS_ORIGINS,
    WEB_API_HOST,
    WEB_API_PORT,
)
from services.claude_sdk import call_with_tools
from services.prompt_builder import PromptBuilder
from services.session import load_history_channel, load_sessions, update_session
try:
    from services.correlation import new_request_id, get_request_id
except ImportError:  # pragma: no cover — defensive fallback
    def new_request_id() -> str:
        return ""

    def get_request_id() -> str:
        return ""

log = logging.getLogger(__name__)

_QUOTA_WINDOW_S = 24 * 60 * 60


class WebAPI:
    def __init__(self, bot):
        # M6: reject wildcard CORS before constructing the server
        for origin in WEB_API_CORS_ORIGINS:
            if origin == "*" or origin == "":
                raise RuntimeError(
                    "WEB_API_CORS_ORIGINS must not contain wildcards; "
                    "this disables socket.io credential checks"
                )

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
        # session_token -> asyncio.Lock (SRE-C6: dedup concurrent misses).
        # OrderedDict so eviction is FIFO insert-order, not arbitrary; we
        # only evict on insert so an in-progress lock holder can never have
        # its lock dropped mid-await (review H-SEC-2).
        self._verify_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        # discord_id (str) -> last request epoch seconds
        self._cooldowns: OrderedDict[str, float] = OrderedDict()
        # discord_id (str) -> list[float] of request timestamps within quota window
        self._daily_quota: OrderedDict[str, list[float]] = OrderedDict()

        # Shared sessions dict (channel_id -> sdk session_id) used by call_with_tools
        self._sessions = load_sessions()

        # sid -> discord_id mapping (populated on connect)
        self._sid_to_user: dict[str, dict] = {}

        # sid -> set of in-flight asyncio.Task (SRE-H10)
        self._sid_tasks: dict[str, set[asyncio.Task]] = {}

        self._runner: web.AppRunner | None = None
        self._http: aiohttp.ClientSession | None = None

        # SRE-C7: record start time for /health uptime
        self._started_at: float = time.time()

        self._register_events()

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------
    # H5: internal session invalidation route
    async def _handle_invalidate_session(self, request: web.Request) -> web.Response:
        remote = request.remote
        if remote not in ("127.0.0.1", "::1"):
            raise web.HTTPForbidden()
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest()
        token = body.get("session_token")
        if not isinstance(token, str) or not token:
            raise web.HTTPBadRequest()
        existed = token in self._session_cache
        self._session_cache.pop(token, None)
        return web.json_response({"invalidated": existed})

    # SRE-C7: health endpoint. Delegates to services.health for the rich
    # subsystem snapshot (discord + oauth + mem0) and decorates with
    # web-specific fields (active_connections).
    async def _handle_health(self, request: web.Request) -> web.Response:
        try:
            from services.health import get_health_snapshot
            snap = get_health_snapshot(bot=self.bot)
        except Exception as exc:  # pragma: no cover — health must never fail hard
            log.warning("get_health_snapshot failed, using minimal fallback: %s", exc)
            discord_ok = self.bot.is_ready() if self.bot else False
            uptime = int(time.time() - self._started_at) if self._started_at else 0
            snap = {
                "status": "ok" if discord_ok else "degraded",
                "uptime_s": uptime,
                "subsystems": {"discord": {"ok": discord_ok}},
            }
        # Decorate with web-specific stats
        snap["active_connections"] = len(self._sid_to_user)
        status_code = 200 if snap.get("status") == "ok" else 503
        return web.json_response(snap, status=status_code)

    async def start(self, port: int = WEB_API_PORT, host: str = WEB_API_HOST) -> None:
        self._http = aiohttp.ClientSession()
        # H5: register invalidation route before site is created
        self.app.router.add_post("/internal/invalidate_session", self._handle_invalidate_session)
        # SRE-C7: health route
        self.app.router.add_get("/health", self._handle_health)
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        log.info("WebAPI Socket.IO server listening on %s:%d", host, port)
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
    def _cache_hit(self, session_token: str) -> dict | None:
        """Return cached session if still fresh (<300 s), else None."""
        cached = self._session_cache.get(session_token)
        if cached and time.time() - cached.get("_verified_at", 0) < 300:
            # M9: Re-derive is_admin on every cache hit so ADMIN_USER_IDS changes apply immediately
            try:
                is_admin_live = int(cached["discordId"]) in ADMIN_USER_IDS
            except (KeyError, ValueError):
                is_admin_live = False
            return {**cached, "isAdmin": is_admin_live}
        return None

    async def verify_session(self, session_token: str) -> dict | None:
        """Verify a session token against the Next.js /api/session/verify.

        The Next.js endpoint reads the session cookie and returns:
            {authenticated: true, discordId: str, isAdmin: bool}
        Caches valid results for 300s to avoid hammering the auth endpoint.
        SRE-C6: concurrent misses for the same token are deduplicated via a
        per-token Lock so the HTTP round-trip happens only once.
        """
        hit = self._cache_hit(session_token)
        if hit is not None:
            return hit

        # SRE-C6 / H-SEC-2: bound _verify_locks to 1000 entries with
        # insert-only FIFO eviction. Never evict the lock we are about to
        # acquire, and never evict a lock that another coroutine is already
        # awaiting (acquired) — only evict the OLDEST unlocked entries.
        existed = session_token in self._verify_locks
        if existed:
            lock = self._verify_locks[session_token]
            self._verify_locks.move_to_end(session_token)
        else:
            # Evict before insert so the new lock is the most recently
            # touched and is never selected for eviction in this pass.
            while len(self._verify_locks) >= 1000:
                oldest_key, oldest_lock = next(iter(self._verify_locks.items()))
                if oldest_lock.locked():
                    # Skip locked entries — they have a coroutine awaiting them
                    self._verify_locks.move_to_end(oldest_key)
                    # If every entry is locked we just live with >1000 momentarily
                    if all(v.locked() for v in self._verify_locks.values()):
                        break
                    continue
                self._verify_locks.popitem(last=False)
            lock = asyncio.Lock()
            self._verify_locks[session_token] = lock

        async with lock:
            # Check cache again — another coroutine may have populated it while we waited
            hit = self._cache_hit(session_token)
            if hit is not None:
                return hit

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
                    # Re-derive admin status from the bot's own ADMIN_USER_IDS
                    # instead of trusting the Next.js verifier. The verifier is a
                    # separate process whose config could drift or be tampered
                    # with; admin power inside the bot must be governed by the
                    # bot's own allowlist.
                    discord_id = str(data["discordId"])
                    try:
                        is_admin = int(discord_id) in ADMIN_USER_IDS
                    except ValueError:
                        is_admin = False
                    user = {
                        "discordId": discord_id,
                        "isAdmin": is_admin,
                        "_verified_at": time.time(),
                    }
                    self._session_cache[session_token] = user
                    return user
            except Exception as e:
                log.warning("Session verify failed: %s", e)
                return None

    # -----------------------------------------------------------------------
    # Discord channel permission checks
    # -----------------------------------------------------------------------
    async def _check_channel_permission(
        self,
        discord_id: str,
        channel_id: int,
        action: str,
        is_admin: bool,
    ) -> tuple[bool, str | None]:
        """Verify *discord_id* may perform *action* on *channel_id*.

        Action is one of: ``view``, ``read_history``, ``send``.

        Admin (bot-side ADMIN_USER_IDS) bypasses the Discord-side check so
        admin tooling keeps working even on channels where the admin isn't
        an explicit guild member (e.g. DMs initiated via the Web UI).

        Returns (allowed, error_message).
        """
        if is_admin:
            return True, None

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception as e:
                log.warning("channel %s not resolvable for %s: %s",
                            channel_id, discord_id, e)
                return False, "permission denied"

        guild = getattr(channel, "guild", None)
        if guild is None:
            # DM channels: only the recipient may operate on them.
            recipient = getattr(channel, "recipient", None)
            if recipient is None:
                return False, "permission denied"
            try:
                return (str(recipient.id) == discord_id), (
                    None if str(recipient.id) == discord_id else "permission denied"
                )
            except AttributeError:
                return False, "permission denied"

        try:
            user_id_int = int(discord_id)
        except ValueError:
            return False, "permission denied"
        member = guild.get_member(user_id_int)
        if member is None:
            try:
                member = await guild.fetch_member(user_id_int)
            except Exception:
                log.debug("user %s is not a member of guild for channel %s", discord_id, channel_id)
                return False, "permission denied"

        perms = channel.permissions_for(member)
        if action == "view" and not perms.view_channel:
            return False, "permission denied"
        if action == "read_history" and not (perms.view_channel and perms.read_message_history):
            return False, "permission denied"
        if action == "send" and not (perms.view_channel and perms.send_messages):
            return False, "permission denied"
        return True, None

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
            # SRE-H10: cancel in-flight tasks for this sid
            for t in self._sid_tasks.pop(sid, set()):
                t.cancel()
            log.info("WS disconnected: sid=%s", sid)

        @sio.event
        async def join_channel(sid, data):
            """Join a channel room to receive channel-scoped events."""
            user_info = self._sid_to_user.get(sid)
            if not user_info:
                await sio.emit("error", {"message": "not authenticated"}, to=sid)
                return
            channel_id_raw = data.get("channel_id", "")
            try:
                channel_id_int = int(channel_id_raw)
            except (TypeError, ValueError):
                await sio.emit("error", {"message": "invalid channel_id"}, to=sid)
                return
            allowed, err = await self._check_channel_permission(
                user_info["discord_id"], channel_id_int, "view", user_info["is_admin"],
            )
            if not allowed:
                await sio.emit("error", {"message": err or "permission denied"}, to=sid)
                return
            channel_id = str(channel_id_int)
            await sio.enter_room(sid, f"channel:{channel_id}")
            log.debug("sid=%s joined channel room %s", sid, channel_id)
            await sio.emit("joined_channel", {"channel_id": channel_id}, to=sid)

        @sio.event
        async def leave_channel(sid, data):
            # H4: require authenticated sid
            user_info = self._sid_to_user.get(sid)
            if not user_info:
                return
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

            allowed, err = await self._check_channel_permission(
                user_info["discord_id"], channel_id, "read_history", user_info["is_admin"],
            )
            if not allowed:
                await sio.emit("error", {"message": err or "permission denied"}, to=sid)
                return

            try:
                messages = await asyncio.to_thread(load_history_channel, channel_id)
            except Exception as e:
                log.warning("get_history failed for ch=%s: %s", channel_id, e)
                messages = []

            # M8: strip Mem0 memory blocks so cross-user memories are not leaked
            for msg in messages:
                if isinstance(msg.get("content"), str):
                    msg["content"] = _strip_mem_block(msg["content"])

            await sio.emit("history", {"channel_id": channel_id, "messages": messages}, to=sid)

        @sio.event
        async def send_message(sid, data):
            """Process a user message through Claude and stream status back."""
            # M-1: set per-request correlation ID so downstream logs from
            # call_with_tools, mem0, etc. all carry rid=XXXX.
            new_request_id()
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

            allowed, err = await self._check_channel_permission(
                discord_id, channel_id, "send", is_admin,
            )
            if not allowed:
                await sio.emit("error", {"message": err or "permission denied"}, to=sid)
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

                # Emotion state — admin only, read-only here. load_state is
                # fail-open so failure returns the default EmotionState and
                # the prompt block is still emitted. Non-admins skip
                # entirely so their prompts never leak Kanae's internal
                # state.
                emotion_state = None
                if is_admin:
                    try:
                        from pathlib import Path as _Path
                        from services.emotion.state import load_state as _load_state
                        from config import STATE_DIR as _STATE_DIR
                        emotion_state = await asyncio.to_thread(
                            _load_state, _Path(_STATE_DIR) / "kanae_emotion.json",
                        )
                    except Exception as exc:
                        log.warning("emotion load failed (non-fatal): %s", exc)

                prompt = await PromptBuilder.build_prompt(
                    raw_text=raw_text,
                    sender_name=sender_name,
                    sender_tag=sender_tag,
                    is_admin=is_admin,
                    mem0_user_id=discord_id,
                    mem0_fetch=_mem0_fetch,
                    emotion_state=emotion_state,
                )

                log.info(
                    "WebAPI request: discord_id=%s channel=%d admin=%s len=%d",
                    discord_id, channel_id, is_admin, len(prompt),
                )

                # SRE-H10: track task so disconnect can cancel it
                _loop = asyncio.get_event_loop()
                task = _loop.create_task(call_with_tools(
                    prompt, channel_id, self._sessions,
                    status_callback=status_callback,
                    restricted=not is_admin,
                    raw_user_text=raw_text,
                ))
                self._sid_tasks.setdefault(sid, set()).add(task)
                try:
                    response_text, session_id = await task
                finally:
                    self._sid_tasks.get(sid, set()).discard(task)

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

                    # Emotion feedback — admin only, fire-and-forget.
                    async def _emotion_feedback():
                        try:
                            from services.emotion.feedback import schedule_feedback
                            from config import STATE_DIR as _STATE_DIR
                            await schedule_feedback(
                                user_text=raw_text,
                                assistant_text=response_text,
                                state_dir=_STATE_DIR,
                            )
                        except Exception as exc:
                            log.warning("emotion feedback failed (non-fatal): %s", exc)

                    asyncio.create_task(_emotion_feedback())

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
