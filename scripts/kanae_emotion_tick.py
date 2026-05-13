#!/usr/bin/env python3
"""Cron entry point for the teleological emotion-driven self-ping.

Replaces the old bash kanae_ping.sh logic. Run by cron every 10 minutes;
the bash script is now a thin exec wrapper so we still benefit from cron's
PATH/environment handling.

Flow (Phase 2/3 telos-driven):
  1. acquire flock; quiet-hours / cooldown gate
  2. load_state from .state/kanae_emotion.json
  3. apply_time_decay for the elapsed time since last update
  4. generate_telos — if None, save_state and exit (no message)
  5. should_suppress — if True, save_state and exit
  6. ask Claude CLI to write the message body, conditioned on telos
  7. POST to the configured CHANNEL_ID (single target)
  8. consecutive_self_initiated++, save_state, touch cooldown sentinel

Failures are logged but never raise — cron should remain reliable.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

# Load .env early so env-derived defaults are populated before anything
# tries to read DISCORD_TOKEN.
def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_env(REPO_DIR / ".env")

# Late imports so .env is in place when emotion params resolve env-driven values.
from services.emotion.params import LATE_NIGHT_HOUR_START, QUIET_HOUR_RANGE  # noqa: E402
from services.emotion.state import (  # noqa: E402
    EmotionState,
    LastInteraction,
    load_state,
    save_state,
)
from services.emotion.lock import LOCK_FILENAME, emotion_lock  # noqa: E402
from services.emotion.telos import Motivation, generate_telos, should_suppress  # noqa: E402
from services.emotion.transition import apply_time_decay  # noqa: E402

# ────────────────────────── constants / paths ───────────────────────────────
# Single delivery target. DM dispatch was removed 2026-05-13; if it ever comes
# back, the haroin user id was 573489206240870410.
CHANNEL_ID = "1490247632847896776"
HISTORY_DIR = REPO_DIR / ".state" / "history"
EMOTION_PATH = REPO_DIR / ".state" / "kanae_emotion.json"
EMOTION_LOCK_PATH = REPO_DIR / ".state" / LOCK_FILENAME
COOLDOWN_S = int(os.environ.get("KANAE_PING_COOLDOWN", "3600"))
TAIL_PER_CHANNEL = 6
CONTEXT_BUDGET = 8000  # ~8KB; we feed less to Claude now since telos already focuses it.
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

JST = timezone(__import__("datetime").timedelta(hours=9))

# XDG state dir (lock + log + cooldown sentinel)
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "discord-bot"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = STATE_DIR / "kanae_ping.log"
LOCK_PATH = STATE_DIR / "kanae_ping.lock"
LAST_SENT_PATH = STATE_DIR / "kanae_ping.last_sent"

# ─────────────────────────── plain logger ──────────────────────────────────
log = logging.getLogger("kanae_ping")
log.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_handler)


# ─────────────────────── helper: acquire flock ─────────────────────────────
def _acquire_lock() -> int | None:
    """Open and lock the cron lock file. Returns fd, or None on contention."""
    fd = os.open(LOCK_PATH, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


# ──────────────────────── context collection ──────────────────────────────
def _collect_history_context() -> str:
    """Aggregate the last N lines from every .state/history/*.jsonl file.

    Mirrors the old bash impl: per-channel tail-N, byte-capped at the end so
    the most recent activity always survives the trim.
    """
    if not HISTORY_DIR.is_dir():
        return "(まだ会話履歴がありません)"
    parts: list[str] = []
    for jf in sorted(HISTORY_DIR.glob("*.jsonl")):
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


# ─────────────────────── claude body generation ───────────────────────────
def _build_message_prompt(state: EmotionState, telos: Motivation, history: str) -> str:
    """Compose the Haiku/Sonnet prompt that produces the literal message body.

    Telos and emotion vector are exposed verbatim so the model knows *why*
    it's writing. The output contract is `{"message": "..."}` so we don't
    waste tokens on a send/skip decision — Python already decided.
    """
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
        f"- satisfaction: {state.satisfaction:.2f} (満足度)\n"
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


def _call_claude(prompt: str) -> str | None:
    """Invoke claude CLI and pull the literal message out of its JSON wrap."""
    cmd = [
        "claude", "-p", prompt,
        "--max-turns", "3",
        "--output-format", "json",
        "--allowed-tools", "",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO_DIR),
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        log.warning("claude CLI timed out")
        return None
    except FileNotFoundError:
        log.warning("claude CLI not on PATH (%s)", os.environ.get("PATH", ""))
        return None
    if proc.returncode != 0:
        log.warning("claude CLI exit=%d stderr=%s", proc.returncode, proc.stderr[:300])
        return None
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.warning("claude CLI returned non-JSON: %s", proc.stdout[:200])
        return None
    inner = outer.get("result", "") if isinstance(outer, dict) else ""
    if not inner:
        log.warning("claude CLI had no .result")
        return None
    # Strip code fences just in case.
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
        log.warning("inner JSON parse failed: %s", inner[:200])
        return None
    msg = body.get("message", "") if isinstance(body, dict) else ""
    msg = str(msg).strip()
    return msg or None


# ─────────────────────────── Discord POST ─────────────────────────────────
def _post_message(bot_token: str, channel: str, message: str) -> bool:
    import urllib.request
    payload = json.dumps({"content": message}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel}/messages",
        data=payload, method="POST",
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
            log.info("POST ch=%s -> HTTP %d", channel, resp.status)
            return ok
    except Exception as exc:
        log.warning("POST ch=%s failed: %s", channel, exc)
        return False


# ──────────────────────────────── main ────────────────────────────────────
def main() -> int:
    bot_token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not bot_token:
        log.error("DISCORD_TOKEN missing")
        return 1

    lock_fd = _acquire_lock()
    if lock_fd is None:
        log.info("skip: another run already in progress")
        return 0

    try:
        now = datetime.now(JST)

        # 1. Quiet hours — refuse even to load state? No: time decay still
        # advances during the night, but we never send and never recompute
        # telos. Update state and exit.
        in_quiet = QUIET_HOUR_RANGE[0] <= now.hour < QUIET_HOUR_RANGE[1]

        # 2. Cooldown — same: tick the state even when cooling down so
        # loneliness keeps accumulating, but skip the LLM call entirely.
        cooldown_active = False
        if LAST_SENT_PATH.exists():
            age = time.time() - LAST_SENT_PATH.stat().st_mtime
            if age < COOLDOWN_S:
                cooldown_active = True

        # 3+4+5. Load → decay → telos → suppress, all under the shared
        # flock so a concurrent bot hook can't replace the file between
        # load and save. The blocking timeout is short because the bot
        # hook itself holds the lock only for cheap I/O.
        try:
            with emotion_lock(EMOTION_LOCK_PATH, blocking=True, timeout=2.0):
                state = load_state(EMOTION_PATH)
                elapsed_hours = 0.0
                if state.updated_at:
                    try:
                        last = datetime.fromisoformat(state.updated_at)
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=JST)
                        elapsed_hours = max(0.0, (now - last).total_seconds() / 3600.0)
                    except (TypeError, ValueError):
                        elapsed_hours = 0.0
                state = apply_time_decay(state, elapsed_hours)
                state.updated_at = now.isoformat()

                if in_quiet:
                    log.info("skip: quiet hours (JST hour=%d); state decayed", now.hour)
                    save_state(state, EMOTION_PATH)
                    return 0

                if cooldown_active:
                    age = int(time.time() - LAST_SENT_PATH.stat().st_mtime)
                    log.info("skip: cooldown active (%ds < %ds); state decayed", age, COOLDOWN_S)
                    save_state(state, EMOTION_PATH)
                    return 0

                telos = generate_telos(state, now)
                if telos is None:
                    log.info(
                        "decision: no_telos val=%.2f lonely=%.2f anx=%.2f sat=%.2f cur=%.2f",
                        state.valence, state.loneliness, state.anxiety,
                        state.satisfaction, state.curiosity,
                    )
                    save_state(state, EMOTION_PATH)
                    return 0

                if should_suppress(state, telos, now):
                    log.info(
                        "decision: suppressed telos=%s urgency=%.2f hr=%d",
                        telos.type, telos.urgency, now.hour,
                    )
                    save_state(state, EMOTION_PATH)
                    return 0

                # Persist decayed state immediately so even if Claude
                # crashes we still observe the time-decay accounting.
                save_state(state, EMOTION_PATH)
        except TimeoutError:
            log.warning("emotion lock contended at start; skipping this tick")
            return 0

        # 6. Compose message body via Claude — long-running, runs *outside*
        # the lock so the bot hook can keep applying conversation events
        # while we wait. The decayed state was already committed above.
        history = _collect_history_context()
        prompt = _build_message_prompt(state, telos, history)
        log.info(
            "decision: send telos=%s urgency=%.2f ctx=%dB",
            telos.type, telos.urgency, len(history),
        )
        message = _call_claude(prompt)
        if not message:
            log.warning("claude returned no body; aborting send")
            return 0

        # 7. POST to the single target channel.
        if DRY_RUN:
            log.info("[DRY_RUN] would send telos=%s body=%r", telos.type, message[:200])
            return 0

        ok_ch = _post_message(bot_token, CHANNEL_ID, message)
        if not ok_ch:
            log.warning("delivery failed; cooldown sentinel untouched")
            return 0

        # 8. Send-success bookkeeping — re-acquire the lock and re-read
        # state so we merge with any concurrent bot-hook update (e.g. a
        # senpai message that landed while Claude was thinking).
        LAST_SENT_PATH.touch()
        try:
            with emotion_lock(EMOTION_LOCK_PATH, blocking=True, timeout=2.0):
                state = load_state(EMOTION_PATH)
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
                save_state(state, EMOTION_PATH)
        except TimeoutError:
            log.warning("emotion lock contended on post-send; counter not bumped")
        log.info("done: channel=%s telos=%s", CHANNEL_ID, telos.type)
        return 0

    except Exception as exc:
        log.exception("unhandled error: %s", exc)
        return 1
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
