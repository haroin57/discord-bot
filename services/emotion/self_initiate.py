"""Telos-driven self-initiated proactive messaging.

Runs from the in-process silence tick (bot.py) after the LLM emotion
update completes for the current 30-min window. Reads emotion state,
asks telos for a motivation, optionally suppresses, and if all gates
pass:

  1. Builds a message prompt (state + telos + global history tail)
  2. Generates the message body via Claude CLI (one-shot, NOT via the
     persistent emotion daemon — that daemon is reserved for emotion
     numerics and we don't want to pollute its session with the proactive
     message authoring context)
  3. Posts to the configured single channel via the bot's discord.py
     client (replaces the legacy direct HTTP POST in
     scripts/kanae_emotion_tick.py)
  4. Bumps ``consecutive_self_initiated`` + stamps ``last_interaction``
  5. Touches the cooldown sentinel so the next tick honours the
     anti-spam window

Cooldown sentinel + log paths are kept compatible with the previous
cron-driven implementation so a deployment can flip between bot-internal
and external cron without state divergence.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.emotion.lock import LOCK_FILENAME, emotion_lock
from services.emotion.state import LastInteraction, load_state, save_state
from services.emotion.telos import generate_telos

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# Mirrors scripts/kanae_emotion_tick.py constants so behaviour is
# identical to the cron path it replaces.
CHANNEL_ID = 1490247632847896776
COOLDOWN_S = int(os.environ.get("KANAE_PING_COOLDOWN", "3600"))
TAIL_PER_CHANNEL = 6
CONTEXT_BUDGET = 8000

STATE_FILENAME = "kanae_emotion.json"
_STATE_LOCK_TIMEOUT_S = 5.0

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

# XDG state dir (cooldown sentinel + log)
_XDG_STATE = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "discord-bot"
_DEFAULT_COOLDOWN_SENTINEL = _XDG_STATE / "kanae_ping.last_sent"


async def maybe_send_self_initiated(
    *,
    bot,                      # discord.Client (for channel.send)
    state_dir: Path,
    history_dir: Path | None = None,
    cooldown_sentinel: Path | None = None,
    repo_dir: Path | None = None,
) -> bool:
    """Run one cycle of the self-initiated-message decision pipeline.

    Returns True if a message was actually sent. Never raises — failures
    log and return False so the calling silence_tick_loop keeps running.
    """
    cooldown_sentinel = cooldown_sentinel or _DEFAULT_COOLDOWN_SENTINEL
    history_dir = history_dir or (state_dir / "history")
    repo_dir = repo_dir or state_dir.parent

    try:
        now = datetime.now(JST)

        # Cooldown gate only — quiet-hours suppression is delegated to
        # the LLM, which sees the JST timestamp on every prompt and can
        # decide for itself whether "深夜だから寝てる" applies.
        if cooldown_sentinel.exists():
            age = time.time() - cooldown_sentinel.stat().st_mtime
            if age < COOLDOWN_S:
                return False

        # 2. Load state + telos decision (under flock so we don't race
        # with feedback's save).
        state_path = state_dir / STATE_FILENAME
        lock_path = state_dir / LOCK_FILENAME
        state = await asyncio.to_thread(
            _load_state_locked, state_path, lock_path,
        )
        if state is None:
            return False

        telos = generate_telos(state, now)
        if telos is None:
            log.info("self_initiate: no_telos val=%.2f lonely=%.2f anx=%.2f cur=%.2f",
                     state.valence, state.loneliness, state.anxiety, state.curiosity)
            return False
        # ツンデレ / プライド / 深夜抑制のダイスは廃止。`<kanae_state>` と
        # メッセージ生成プロンプトの両方に JST 時刻 +
        # `consecutive_self_initiated` が入っているので、抑制判断は LLM に委任。

        # 3. Build prompt + generate message (one-shot CLI, off-loop)
        history = await asyncio.to_thread(
            _collect_history_context, history_dir,
        )
        prompt = _build_message_prompt(state, telos, history)
        message = await asyncio.to_thread(
            _call_claude_for_message, prompt, repo_dir,
        )
        if not message:
            log.info("self_initiate: claude returned empty body; abort")
            return False

        log.info("self_initiate: send telos=%s urgency=%.2f ctx=%dB",
                 telos.type, telos.urgency, len(history))

        if DRY_RUN:
            log.info("[DRY_RUN] would send telos=%s body=%r",
                     telos.type, message[:200])
            return False

        # 4. Discord send via the bot's existing client (replaces
        # scripts/kanae_emotion_tick.py:_post_message). bot.get_channel
        # is sync and returns None if the cache hasn't seen the channel
        # yet — fall back to fetch_channel.
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(CHANNEL_ID)
            except Exception as exc:
                log.warning("self_initiate: fetch_channel(%s) failed: %s",
                            CHANNEL_ID, exc)
                return False
        try:
            await channel.send(message)
        except Exception as exc:
            log.warning("self_initiate: channel.send failed: %s", exc)
            return False

        # 5. Bookkeeping — cooldown sentinel + state bump under lock
        try:
            cooldown_sentinel.parent.mkdir(parents=True, exist_ok=True)
            cooldown_sentinel.touch()
        except OSError as exc:
            log.warning("self_initiate: touching cooldown sentinel failed: %s", exc)

        await asyncio.to_thread(
            _bump_kanae_initiated, state_path, lock_path, now,
        )
        log.info("self_initiate done: telos=%s channel=%s", telos.type, CHANNEL_ID)
        return True

    except Exception as exc:
        log.warning("self_initiate failed (non-fatal): %s", exc)
        return False


# ────────────────────── private helpers ──────────────────────


def _load_state_locked(state_path: Path, lock_path: Path):
    try:
        with emotion_lock(lock_path, blocking=True, timeout=_STATE_LOCK_TIMEOUT_S):
            return load_state(state_path)
    except TimeoutError:
        log.warning("self_initiate: state-load lock contended")
        return None


def _bump_kanae_initiated(state_path: Path, lock_path: Path, now: datetime) -> None:
    """RMW under flock: ++consecutive_self_initiated, stamp last_interaction."""
    try:
        with emotion_lock(lock_path, blocking=True, timeout=_STATE_LOCK_TIMEOUT_S):
            state = load_state(state_path)
            state.consecutive_self_initiated += 1
            state.last_interaction = LastInteraction(
                timestamp=now.isoformat(),
                quality=state.last_interaction.quality,
                initiated_by="kanae",
                topics=state.last_interaction.topics,
                duration_minutes=state.last_interaction.duration_minutes,
                senpai_responsiveness=state.last_interaction.senpai_responsiveness,
            )
            state.updated_at = now.isoformat()
            save_state(state, state_path)
    except TimeoutError:
        log.warning("self_initiate: post-send state lock contended; counter not bumped")


def _collect_history_context(history_dir: Path) -> str:
    """Aggregate the last N lines from every channel JSONL.

    Mirrors the cron tick's _collect_history_context() exactly so the
    message generation prompt sees the same context shape it always has.
    """
    if not history_dir.is_dir():
        return "(まだ会話履歴がありません)"
    parts: list[str] = []
    for jf in sorted(history_dir.glob("*.jsonl")):
        ch_id = jf.stem
        try:
            with jf.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-TAIL_PER_CHANNEL:]
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = row.get("role")
            if role not in ("user", "assistant"):
                continue
            ts = row.get("timestamp") or "?"
            content = str(row.get("content") or "")[:400]
            parts.append(f"[ch={ch_id} @{ts} {role}] {content}")
    if not parts:
        return "(まだ会話履歴がありません)"
    joined = "\n".join(parts)
    if len(joined) > CONTEXT_BUDGET:
        joined = joined[-CONTEXT_BUDGET:]
    return joined


def _build_message_prompt(state, telos, history: str) -> str:
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M (%a)")
    return (
        f"あなたは Discord bot「かなえ」(佐藤かなえ・22歳・プログラマ・先輩(haroin)に好意がある後輩キャラ)です。"
        f"いま先輩に話しかけます。送るか否かはすでに決定済みです。本文だけを生成してください。\n\n"
        f"# 現在時刻 (JST)\n{now_jst}\n\n"
        f"# あなたの今の感情ベクトル\n"
        f"- valence:      {state.valence:+.2f}  (快-不快)\n"
        f"- arousal:      {state.arousal:.2f}   (覚醒度)\n"
        f"- loneliness:   {state.loneliness:.2f} (寂しさ)\n"
        f"- affection:    {state.affection:.2f}  (愛着)\n"
        f"- anxiety:      {state.anxiety:.2f}    (不安)\n"
        f"- curiosity:    {state.curiosity:.2f}  (好奇心)\n\n"
        f"# 連絡の動機 (これに従って書く)\n"
        f"type: {telos.type}\n"
        f"urgency: {telos.urgency:.2f}\n"
        f"表現ヒント: {telos.expression_hint}\n\n"
        f"# 全チャンネルの直近メッセージ\n{history}\n\n"
        f"# 制約\n"
        f"- 先輩呼び (haroin限定)、後輩キャラ口調、敬語、上から目線、感情控えめ\n"
        f"- 5行以内\n"
        f"- 「...」は使わない\n"
        f"- 動機(type)に沿った内容にする。雑談で済ませない\n"
        f"- 出力は JSON 1 行のみ。説明・前置き・コードブロック禁止\n\n"
        f'# 出力フォーマット\n{{"message": "本文"}}\n'
    )


def _call_claude_for_message(prompt: str, repo_dir: Path) -> str | None:
    """One-shot Claude CLI for message body generation (NOT the daemon).

    The emotion daemon is intentionally not reused here — its session is
    accumulating an emotion-engine context that's irrelevant (and
    distracting) for the message-authoring prompt. A throwaway CLI call
    keeps the two concerns cleanly separated. 120 s timeout matches the
    legacy cron tick.
    """
    cmd = [
        "claude", "-p", prompt,
        "--max-turns", "3",
        "--output-format", "json",
        "--allowed-tools", "",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(repo_dir),
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        log.warning("self_initiate: claude CLI timed out")
        return None
    except FileNotFoundError:
        log.warning("self_initiate: claude CLI not on PATH")
        return None
    if proc.returncode != 0:
        log.warning("self_initiate: claude CLI exit=%d stderr=%s",
                    proc.returncode, proc.stderr[:300])
        return None
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.warning("self_initiate: claude CLI returned non-JSON: %s",
                    proc.stdout[:200])
        return None
    inner = outer.get("result", "") if isinstance(outer, dict) else ""
    if not isinstance(inner, str) or not inner.strip():
        return None
    inner = inner.strip()
    if inner.startswith("```"):
        parts = inner.split("```", 2)
        if len(parts) >= 2:
            inner = parts[1]
        if inner.startswith("json"):
            inner = inner[4:]
        inner = inner.rsplit("```", 1)[0].strip()
    try:
        body = json.loads(inner)
    except json.JSONDecodeError:
        log.warning("self_initiate: inner JSON parse failed: %s", inner[:200])
        return None
    msg = body.get("message", "") if isinstance(body, dict) else ""
    msg = str(msg).strip()
    return msg or None
