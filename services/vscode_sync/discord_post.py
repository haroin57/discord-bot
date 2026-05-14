"""Direct bot.send() poster for VSCode sync — replaces the webhook flow.

The daemon takes an opaque async callable, so this module is the only
place that imports discord.py. Tests can pass any async callable.
"""

import logging
from typing import Awaitable, Callable

import discord

log = logging.getLogger(__name__)

PostCallback = Callable[[str], Awaitable[None]]


def make_post_callback(bot: discord.Client, thread_id: int) -> PostCallback:
    """Return an async post(content) callable bound to a Discord thread.

    The channel object is resolved on first call and cached. Subsequent
    sends reuse it. Permission / not-found errors are logged and swallowed
    so the daemon never crashes on a transient Discord issue.
    """
    cache: dict[str, discord.abc.Messageable | None] = {"channel": None}

    async def _resolve() -> discord.abc.Messageable | None:
        ch = bot.get_channel(thread_id)
        if ch is not None:
            return ch  # type: ignore[return-value]
        try:
            return await bot.fetch_channel(thread_id)  # type: ignore[return-value]
        except discord.NotFound:
            log.warning("vscode_sync: thread %s not found", thread_id)
        except discord.Forbidden:
            log.warning("vscode_sync: no permission to access thread %s", thread_id)
        except discord.HTTPException as e:
            log.warning("vscode_sync: fetch_channel(%s) failed: %s", thread_id, e)
        return None

    async def post(content: str) -> None:
        if not content:
            return
        channel = cache["channel"]
        if channel is None:
            channel = await _resolve()
            if channel is None:
                return
            cache["channel"] = channel
        try:
            await channel.send(
                content[:2000],
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True,
            )
        except discord.Forbidden:
            log.warning("vscode_sync: send forbidden on thread %s", thread_id)
            cache["channel"] = None
        except discord.HTTPException as e:
            log.warning("vscode_sync: send failed on thread %s: %s", thread_id, e)

    return post
