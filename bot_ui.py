"""Status / spinner UI controller for on_message (R2).

Owns the single status message (⏳ 考え中... → spinner → final edit/delete)
that previously lived as ~250 lines of closures + shared locals inside
on_message. State moves into fields; the edit/animate logic is verbatim.

Design (external review consensus, 2026-07-04): the renderer never touches
spinner internals — it calls ``await status.reposition()`` after appending
content, and this class owns everything about the status message.
"""

from __future__ import annotations

import asyncio
import logging
import time

import discord

from services.claude_api.progress import ProgressSpinner, _fmt_tokens, SPINNER_EMOJI
from services.correlation import get_request_id

log = logging.getLogger(__name__)

STATUS_INTERVAL_S = 2.0
SPIN_INTERVAL_S = 1.0  # ≈ Discord's sustainable single-message edit rate


class StatusController:
    """Latest-wins status updates + CLI-style animated spinner.

    One instance per on_message invocation. All Discord writes to the status
    message go through this object, so there is exactly one writer.
    """

    def __init__(self, message: "discord.Message", *, track_task) -> None:
        self.message = message
        self.status_msg: "discord.Message | None" = None
        self._track_task = track_task

        # Status updater (latest-wins, 2s rate limit).
        self._last_status_update = 0.0
        self._pending_status: dict = {"text": None, "task": None}

        # Claude-Code-CLI-style animated spinner. CADENCE = 1 char / second —
        # a HARD Discord limit (per-message edit bucket ~5/5s ≈ 1/s sustained);
        # see scripts/spinner_capture.py + measure_edit_ratelimit.py for the
        # empirical freezes at faster cadences. Continuous motion comes from
        # SPINNER_EMOJI (animated application emoji, free).
        self._spinner = ProgressSpinner()
        self.prog: dict = {"tokens": 0}   # live output-token count (set by tool_loop)
        self.spin: dict = {"on": False, "action": "", "start": 0.0}
        # Vestigial guard, kept False: status_msg is the spinner only — the
        # response is always a fresh reply now (in-place-repurpose removed
        # 2026-06-22: it made the spinner vanish at generation start and put
        # the reply in the wrong timeline slot).
        self.consumed = {"v": False}
        self._animator_task: "asyncio.Task | None" = None

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def initial_reply(self, *, was_queued: bool, wait_pos: int = 1) -> None:
        try:
            if was_queued:
                self.status_msg = await self.message.reply(f"⏳ 待機中({wait_pos}番目)")
            else:
                self.status_msg = await self.message.reply("⏳ 考え中...")
        except discord.errors.HTTPException:
            self.status_msg = None

    def spin_start(self, *, mode: str) -> None:
        """Start the animator; it owns status_msg until spin_stop()."""
        self.spin["on"] = True
        self.spin["start"] = time.monotonic()
        coro = self._animate_gif() if mode == "gif" else self._animate()
        self._animator_task = self._track_task(coro, name="status_spinner")

    def spin_stop(self) -> None:
        """Hand status_msg back to the normal (throttled) flow."""
        self.spin["on"] = False
        if self._animator_task is not None:
            self._animator_task.cancel()
            self._animator_task = None

    async def reposition(self) -> None:
        """Keep the spinner pinned at the BOTTOM of the reply while generating.

        When a new content message is appended below it, re-post the spinner
        so it stays the last message. Order: send NEW → reassign status_msg →
        delete OLD, so the animator (which reads status_msg every frame) never
        edits a just-deleted message (the stray frame between is swallowed by
        the HTTPException guards). No-op once generation ends or if the
        spinner is gone.
        """
        if not self.spin["on"] or self.status_msg is None:
            return
        old = self.status_msg
        try:
            new = await self.message.channel.send(self._spin_body())
        except discord.errors.HTTPException:
            return
        self.status_msg = new
        try:
            await old.delete()
        except discord.errors.HTTPException:
            pass

    async def edit_final(self, text: str) -> None:
        """Terminal edit (abort/error paths). No-op when no status message."""
        if self.status_msg is None:
            return
        try:
            await self.status_msg.edit(content=text)
        except discord.errors.HTTPException:
            pass

    def cancel_pending(self) -> None:
        """Cancel any pending delayed status edit (success path, pre-delete)."""
        pending = self._pending_status.get("task")
        if pending and not pending.done():
            pending.cancel()
            self._pending_status["task"] = None

    async def delete_if_alive(self) -> None:
        """Success path: remove the status message (unless repurposed)."""
        if self.status_msg is not None and not self.consumed["v"]:
            try:
                await self.status_msg.delete()
            except discord.errors.HTTPException:
                pass

    # ── status text (latest-wins, throttled) ───────────────────────────────
    async def update(self, text: str) -> None:
        """Latest-wins status updater with 2s rate limit.

        Never drops the most recent text: if called within the cool-down,
        schedule a delayed edit (cancelling any previously pending one).
        Discord message limit is 2000 chars — truncate to stay safe.
        """
        if len(text) > 1990:
            text = text[:1990] + "…"
        # While the animator owns status_msg, route phase/tool labels into its
        # transient "action" line instead of editing directly (avoids two
        # writers fighting over one message). Plain thinking/generating
        # indicators clear the action so the shimmer shows through.
        if self.spin["on"]:
            self.spin["action"] = "" if text[:1] in ("💭", "📝") else text
            return
        now_mono = time.monotonic()
        elapsed = now_mono - self._last_status_update
        if elapsed >= STATUS_INTERVAL_S:
            pending = self._pending_status.get("task")
            if pending and not pending.done():
                pending.cancel()
                self._pending_status["task"] = None
            self._last_status_update = now_mono
            # Fire-and-forget so the streaming loop isn't blocked on Discord I/O.
            self._track_task(self._do_edit(text), name="status_edit")
            return
        # Too soon: remember latest text, schedule a single delayed edit.
        self._pending_status["text"] = text
        pending = self._pending_status.get("task")
        if pending and not pending.done():
            pending.cancel()
        self._pending_status["task"] = self._track_task(
            self._delayed_edit(text, STATUS_INTERVAL_S - elapsed),
            name="status_delayed_edit",
        )

    async def _do_edit(self, text: str) -> None:
        if self.status_msg is None or self.consumed["v"]:
            # status_msg was repurposed as the response body — don't clobber it.
            return
        try:
            await self.status_msg.edit(content=text)
        except discord.errors.HTTPException:
            pass

    async def _delayed_edit(self, text: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._last_status_update = time.monotonic()
        self._pending_status["task"] = None
        await self._do_edit(text)

    # ── spinner rendering ───────────────────────────────────────────────────
    def _spin_body(self) -> str:
        elapsed = time.monotonic() - self.spin["start"]
        action = self.spin["action"]
        if action:
            prefix = f"{SPINNER_EMOJI} " if SPINNER_EMOJI else ""
            body = f"{prefix}{action}  ({int(elapsed)}s"
            if self.prog["tokens"]:
                body += f" · ↑ {_fmt_tokens(self.prog['tokens'])} tokens"
            return body + ")"
        return self._spinner.render(
            elapsed_s=elapsed, tokens=self.prog["tokens"], interrupt_hint="/abort で中断",
        )

    async def _spin_edit(self, text: str) -> None:
        try:
            if self.status_msg is not None:
                await self.status_msg.edit(content=text)
        except discord.errors.HTTPException:
            pass

    async def _animate(self) -> None:
        next_frame = time.monotonic()
        while self.spin["on"]:
            if self.status_msg is not None:
                # Serial: await this edit, THEN advance one char. Stepping only
                # after the edit lands guarantees each visible frame moves the
                # bold window by exactly one position (never skips → no
                # stutter), with zero task-spawn churn.
                try:
                    await self._spin_edit(self._spin_body())
                except asyncio.CancelledError:
                    break
                self._spinner.step()
            # Wall-clock cadence: pace to SPIN_INTERVAL_S when edits are fast;
            # if an edit ran long, delay clamps to 0 and the grid resyncs (the
            # loop then runs at the edit rate, still one step per frame).
            next_frame += SPIN_INTERVAL_S
            delay = next_frame - time.monotonic()
            if delay < 0:
                next_frame = time.monotonic()
                delay = 0.0
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _animate_gif(self) -> None:
        # Option B (SPINNER_MODE=gif): the WHOLE status line (gerund word +
        # elapsed/tokens) is baked into one seamlessly-looping bold-shimmer GIF
        # attachment with empty message content — so it's a single block (no
        # line break) at Discord's text size. The client loops the GIF for free,
        # re-uploaded once a second (≈ Discord's sustainable edit rate) with a
        # fresh word + ticking counters; each GIF is a one-second forward bold
        # sweep, so the word visibly shimmers and the seconds advance every tick.
        # While a tool runs (spin["action"]) we switch to a plain text line and
        # drop the GIF, restoring it when the action clears.
        import os as _os
        from services.claude_api.progress import pick_word
        from services.claude_api.spinner_gif import render_status_gif, _CACHE_DIR
        REFRESH_S = 1.0
        gif_path = _os.path.join(_CACHE_DIR, f"status_{self.message.channel.id}.gif")
        state = {"mode": None, "action": None, "last_refresh": 0.0}
        next_frame = time.monotonic()

        def _meta() -> str:
            elapsed = int(time.monotonic() - self.spin["start"])
            parts = [f"{elapsed}s"]
            if self.prog["tokens"]:
                parts.append(f"↑ {_fmt_tokens(self.prog['tokens'])} tokens")
            parts.append("/abort で中断")
            return f"({' · '.join(parts)})"

        async def _refresh_gif(meta: str) -> None:
            try:
                path = await asyncio.to_thread(
                    render_status_gif, pick_word(), meta, gif_path,
                )
            except Exception as exc:
                log.warning("[rid=%s] status gif render failed: %s",
                            get_request_id(), exc)
                await self.status_msg.edit(content=f"⏳ {meta}")
                state["mode"] = "text"
                return
            f = discord.File(path)
            try:
                await self.status_msg.edit(content="", attachments=[f])
                state["mode"] = "gif"
                state["last_refresh"] = time.monotonic()
            finally:
                f.close()

        while self.spin["on"]:
            if self.status_msg is not None:
                meta = _meta()
                action = self.spin["action"]
                try:
                    if action:
                        # tool running → plain text line, drop the gif
                        changed = state["mode"] != "action" or state["action"] != action
                        if changed or time.monotonic() - state["last_refresh"] >= REFRESH_S:
                            if state["mode"] == "gif":
                                # clear the gif attachment when leaving gif mode
                                await self.status_msg.edit(content=f"{action}  {meta}",
                                                           attachments=[])
                            else:
                                await self.status_msg.edit(content=f"{action}  {meta}")
                            state["mode"] = "action"
                            state["action"] = action
                            state["last_refresh"] = time.monotonic()
                    else:
                        state["action"] = None
                        # Refresh every tick (~1s): new sweep + ticking counters.
                        await _refresh_gif(meta)
                except discord.errors.HTTPException:
                    pass
                except asyncio.CancelledError:
                    break
            next_frame += SPIN_INTERVAL_S
            delay = next_frame - time.monotonic()
            if delay < 0:
                next_frame = time.monotonic()
                delay = 0.0
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
