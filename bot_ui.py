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
from services.job_state import JobPhase
from utils.markdown_parser import (
    TableSegment,
    MathSegment,
    split_settled,
    first_block_span,
)
from utils.math_renderer import render_math_image
from utils.response_sanitizer import strip_numeric_inline_code
from utils.table_renderer import render_table_image

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


class LiveRenderer:
    """In-stream renderer for the model's live narration (R3).

    Extracted verbatim from the ~260 lines of closures that lived inside
    on_message. Owns the incremental table/$$/$ block rendering, editable
    live-tail prose drafts, the held open-block placeholder, and the
    debounced parallel tool-call notice. One instance per on_message call.

    Design (external review consensus, 2026-07-04):
      * ``settled`` stays a monotonically increasing INTEGER offset
        (``self._emitted["upto"]``), never a string.
      * The renderer never touches spinner internals — it calls
        ``await self._status.reposition()`` after appending content.
      * Behaviour is byte-for-byte the original: throttle intervals, split
        limits, placeholder wording, image cleanup, exception handling and
        log strings are all preserved. No new locking / queueing.

    ``split_message`` is injected (not imported) because the test-suite
    patches ``bot.split_message``; on_message binds the (possibly patched)
    module global and passes it in at construction time.
    """

    # Same constants the closures used (kept verbatim).
    LIVE_DRAFT_LIMIT = 1880
    STREAM_EDIT_INTERVAL_S = 1.5
    DISCORD_HARD_CAP = 1990
    TOOL_BATCH_WINDOW_S = 0.08
    TOOL_NOTICE_MAX_LINES = 8
    SCRATCH_PREVIEW_TAIL_CHARS = 1500

    def __init__(
        self,
        message: "discord.Message",
        status: "StatusController",
        *,
        track_task,
        job,
        split_message,
    ) -> None:
        self.message = message
        self._status = status
        self._track_task = track_task
        self._job = job
        self._split_message = split_message

        # ── shared streaming state (was nonlocal / dict cells in on_message) ──
        self._live_sent_any = {"v": False}
        self._acc = {"committed": "", "last_edit": 0.0}   # raw prose across all turns
        self._emitted = {"upto": 0}                        # raw offset finalized (immutable)
        self._draft_msgs: list["discord.Message"] = []     # editable live-tail prose msgs
        self._placeholder: dict = {"msg": None}            # held open-block placeholder
        self._rendered_images: list[str] = []              # temp image paths to clean up
        self._first_reply = {"done": False}                # first content msg replies to user
        self._render_lock = asyncio.Lock()

        # ── parallel tool-call notice state ──────────────────────────────────
        self._tool_notice_state: dict = {"msg": None, "lines": [], "task": None}

        # ── Scratchpad live-preview state ─────────────────────────────────────
        # One live message per turn showing the in-progress Scratchpad write,
        # throttled like stream_preview. On block close it collapses into a
        # 1-line subtext summary instead of leaving the full draft behind.
        self._scratch_state: dict = {
            "msg": None, "last_edit": 0.0, "section": None, "summary": [],
        }

    # ── public read-only accessors (bot.py reads these after the turn) ───────
    @property
    def live_sent_any(self) -> bool:
        return self._live_sent_any["v"]

    @property
    def committed(self) -> str:
        return self._acc["committed"]

    @property
    def rendered_images(self) -> list[str]:
        return self._rendered_images

    @property
    def tool_notice_task(self):
        return self._tool_notice_state.get("task")

    @property
    def tool_notice_msg(self):
        return self._tool_notice_state.get("msg")

    def clear_tool_notice_msg(self) -> None:
        self._tool_notice_state["msg"] = None

    # ── content sending ─────────────────────────────────────────────────────
    async def _send_content(self, text: str | None = None, *, file=None):
        """Send one content message; the first one replies to the user's.

        The response is ALWAYS a fresh message (reply for the first chunk,
        channel.send after) — never an in-place edit of the spinner's
        status_msg. Repurposing status_msg made the spinner vanish the
        instant generation began and, worse, dropped the reply into the
        spinner's OLD timeline slot, so a reply sent mid-generation landed
        above newer messages (haroin, 2026-06-22). The spinner keeps
        running in its own status_msg and is deleted when the turn ends.
        """
        try:
            if not self._first_reply["done"]:
                self._first_reply["done"] = True
                m = await (self.message.reply(file=file) if file is not None
                           else self.message.reply(text))
            else:
                m = await (self.message.channel.send(file=file) if file is not None
                           else self.message.channel.send(text))
        except discord.errors.HTTPException as exc:
            log.warning("[rid=%s] content send failed: %s", get_request_id(), exc)
            return None
        self._live_sent_any["v"] = True
        await self._status.reposition()
        return m

    def _disp(self, text: str) -> str:
        # Display-only transform: strip numeric inline-code backticks (only
        # removes chars, never shifts the RAW offsets the streamer tracks)
        # and hard-cap to stay under Discord's limit.
        text = strip_numeric_inline_code(text)
        if len(text) > self.DISCORD_HARD_CAP:
            text = text[:self.DISCORD_HARD_CAP - 1] + "…"
        return text

    async def _draft_live(self, prose: str) -> None:
        """Render trailing prose into the editable draft messages, edits in
        place and appends as the tail grows. Idempotent (skips unchanged)."""
        prose = (prose or "").strip("\n")
        if not prose.strip():
            return
        chunks = await asyncio.to_thread(
            self._split_message, prose, self.LIVE_DRAFT_LIMIT, numbered=False,
        )
        for i, chunk in enumerate(chunks):
            disp = self._disp(chunk)
            if not disp.strip():
                continue
            if i < len(self._draft_msgs):
                existing = self._draft_msgs[i]
                if getattr(existing, "content", None) == disp:
                    continue
                try:
                    await existing.edit(content=disp)
                except discord.errors.HTTPException:
                    pass
            else:
                sent = await self._send_content(disp)
                if sent is None:
                    return
                self._draft_msgs.append(sent)

    async def _finalize_text(self, prose: str) -> None:
        """Freeze the live-tail prose as immutable messages: render it once
        more, then stop tracking those messages (no further edits). Reuses
        the existing draft messages — never re-sends, so no duplication."""
        await self._draft_live(prose)
        self._draft_msgs.clear()

    async def _show_placeholder(self) -> None:
        if self._placeholder.get("msg") is None:
            self._placeholder["msg"] = await self._send_content("📊 描画中…")

    async def _hide_placeholder(self) -> None:
        ph = self._placeholder.get("msg")
        if ph is not None:
            try:
                await ph.delete()
            except discord.errors.HTTPException:
                pass
            self._placeholder["msg"] = None

    async def _emit_block(self, seg) -> None:
        """Render a Table/Math segment to an image (immutable, in order).
        Removes the held placeholder first; falls back to text on failure."""
        img_path = None
        try:
            if isinstance(seg, TableSegment):
                img_path = await render_table_image(seg.markdown, caption=seg.caption)
            elif isinstance(seg, MathSegment):
                img_path = await render_math_image(seg.latex, display_mode=seg.display_mode)
        except Exception as exc:
            log.warning("[rid=%s] block render failed: %s", get_request_id(), exc)
            img_path = None
        # Drop the placeholder right before placing the final content so the
        # image lands at the same position (a placeholder→image swap, never
        # a raw-markdown→image swap).
        await self._hide_placeholder()
        if img_path:
            self._rendered_images.append(img_path)
            f = discord.File(img_path)
            try:
                await self._send_content(file=f)
            finally:
                f.close()
            return
        # Render failed → raw-text fallback at the same position.
        if isinstance(seg, TableSegment):
            fallback = seg.markdown
        else:
            fallback = (f"```\n{seg.latex}\n```" if seg.display_mode
                        else f"`{seg.latex}`")
        for chunk in await asyncio.to_thread(
            self._split_message, fallback, self.LIVE_DRAFT_LIMIT, numbered=False,
        ):
            await self._send_content(self._disp(chunk))

    async def render_incremental(self, full_text: str, *, final: bool = False,
                                 force: bool = False) -> None:
        full_text = full_text or ""
        async with self._render_lock:
            now_mono = time.monotonic()
            if not final and not force and \
                    (now_mono - self._acc["last_edit"]) < self.STREAM_EDIT_INTERVAL_S:
                return
            self._acc["last_edit"] = now_mono

            if final:
                settled_end = len(full_text)
                is_open = False
            else:
                # 純 CPU の regex 関数。to_thread の dispatch コストの方が
                # 実行コストより高いので直接呼ぶ（毎 tick 実行される）。
                settled, _pending, is_open = split_settled(full_text)
                settled_end = len(settled)

            # Emit every closed block in [_emitted, settled_end] in order.
            # Bounded loop: each iteration must advance _emitted (end > 0),
            # but guard against a pathological zero-width span so the event
            # loop can never spin here.
            _guard = 0
            while _guard < 500:
                _guard += 1
                region = full_text[self._emitted["upto"]:settled_end]
                span = first_block_span(region)
                if span is None:
                    break
                start, end, seg = span
                if end <= 0:
                    break  # never advances → bail (defensive)
                prose_before = region[:start]
                if prose_before.strip():
                    await self._finalize_text(prose_before)
                else:
                    self._draft_msgs.clear()  # freeze any stale draft before block
                await self._emit_block(seg)
                self._emitted["upto"] += end

            # Trailing region after the last emitted block.
            if final:
                tail = full_text[self._emitted["upto"]:]
                if tail.strip():
                    await self._finalize_text(tail)
                await self._hide_placeholder()
                return
            if is_open:
                # Prose before the open block (settled) is shown live; the
                # open block is held behind the placeholder until it closes.
                await self._draft_live(full_text[self._emitted["upto"]:settled_end])
                await self._show_placeholder()
            else:
                await self._hide_placeholder()
                await self._draft_live(full_text[self._emitted["upto"]:])

    async def stream_preview(self, partial_turn_text: str) -> None:
        # In-progress text of the CURRENT turn appended to everything
        # already committed. Throttled (the turn-final incremental_send
        # forces a render; the post-stream final flush is authoritative).
        sep = "\n\n" if self._acc["committed"] else ""
        await self.render_incremental(
            self._acc["committed"] + sep + (partial_turn_text or ""),
        )

    async def incremental_send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        sep = "\n\n" if self._acc["committed"] else ""
        self._acc["committed"] = self._acc["committed"] + sep + text
        log.info(
            "[rid=%s] live commit: +%d chars committed=%d",
            get_request_id(), len(text), len(self._acc["committed"]),
        )
        await self.render_incremental(self._acc["committed"], force=True)

    # ── parallel tool-call notice ────────────────────────────────────────────
    async def flush_tool_notice(self) -> None:
        lines = self._tool_notice_state.get("lines") or []
        if not lines:
            return
        self._tool_notice_state["lines"] = []
        shown = lines[:self.TOOL_NOTICE_MAX_LINES]
        extra = len(lines) - len(shown)
        payload = "\n".join(shown)
        if extra > 0:
            payload += f"\n-# …他 {extra} 件"
        if len(payload) > 1990:
            payload = payload[:1960] + "\n…(truncated)"
        existing = self._tool_notice_state.get("msg")
        try:
            if existing is None:
                self._tool_notice_state["msg"] = await self.message.channel.send(payload)
                await self._status.reposition()
                return
            await existing.edit(content=payload)
        except discord.errors.NotFound:
            try:
                self._tool_notice_state["msg"] = await self.message.channel.send(payload)
                await self._status.reposition()
            except Exception as exc:
                log.warning("tool notice re-post failed: %s", exc)
        except Exception as exc:
            log.warning("tool notice send/edit failed: %s", exc)

    async def tool_invocation_send(self, tool_name: str, tool_input: dict) -> None:
        self._job.phase = JobPhase.TOOL_RUNNING
        try:
            from services.claude_api.status import tool_oneline
            line = tool_oneline(tool_name, tool_input)
        except Exception as exc:
            log.warning("tool notice format failed: %s", exc)
            return
        if not line:
            return
        self._tool_notice_state["lines"].append(line)
        # Debounce: collapse a burst of parallel tool calls into one edit by
        # (re)scheduling the flush TOOL_BATCH_WINDOW_S after the latest call.
        pending = self._tool_notice_state.get("task")
        if pending and not pending.done():
            pending.cancel()

        async def _delayed_flush():
            try:
                await asyncio.sleep(self.TOOL_BATCH_WINDOW_S)
            except asyncio.CancelledError:
                return
            await self.flush_tool_notice()

        self._tool_notice_state["task"] = self._track_task(
            _delayed_flush(), name="tool_notice_flush"
        )

    # ── Scratchpad live preview ──────────────────────────────────────────────
    async def scratch_stream_preview(self, section: str, content: str, op: str) -> None:
        """Live-render an in-progress Scratchpad write so the channel isn't
        silent while the model works out its scratch notes (tool_loop.py
        forwards ``input_json`` stream events here). Throttled the same as
        prose streaming; a single message is edited in place per turn."""
        now_mono = time.monotonic()
        if (now_mono - self._scratch_state["last_edit"]) < self.STREAM_EDIT_INTERVAL_S:
            return
        self._scratch_state["last_edit"] = now_mono
        self._scratch_state["section"] = section
        tail = (content or "")[-self.SCRATCH_PREVIEW_TAIL_CHARS:]
        quoted = "\n".join(f"> {line}" for line in tail.splitlines()) or "> …"
        payload = self._disp(f"-# 💭 メモ「{section}」を記述中…\n{quoted}")
        existing = self._scratch_state.get("msg")
        try:
            if existing is None:
                self._scratch_state["msg"] = await self.message.channel.send(payload)
                await self._status.reposition()
                return
            await existing.edit(content=payload)
        except discord.errors.NotFound:
            try:
                self._scratch_state["msg"] = await self.message.channel.send(payload)
                await self._status.reposition()
            except Exception as exc:
                log.warning("scratch preview re-post failed: %s", exc)
        except Exception as exc:
            log.warning("scratch preview send/edit failed: %s", exc)

    async def scratch_finalize(self) -> None:
        """Collapse the live Scratchpad draft into a 1-line subtext summary
        once its content_block closes, so the finished write doesn't leave a
        multi-line quote block cluttering the channel."""
        section = self._scratch_state.get("section")
        existing = self._scratch_state.get("msg")
        if section is None or existing is None:
            return
        self._scratch_state["summary"].append(section)
        lines = [f"-# 💭 メモ「{s}」" for s in self._scratch_state["summary"]]
        payload = "\n".join(lines)
        try:
            await existing.edit(content=payload)
        except Exception as exc:
            log.warning("scratch preview collapse failed: %s", exc)
        self._scratch_state["section"] = None
