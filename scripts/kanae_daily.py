#!/usr/bin/env python3
"""Cron entry point for kanae daily life simulation.

Run every 60 minutes by cron. Generates a daily life entry via Claude CLI
(Opus, session resume), updates the incremental summary via Haiku,
and optionally pushes high-importance entries to Mem0.

Flow:
  1. Load daily journal + summary
  2. Build prompt with context (time, recent entries, summary, conversation)
  3. Claude CLI (Opus, --resume daily session) → new entry
  4. Append to ring buffer, save
  5. Claude CLI (Haiku 4.5, --resume summary session) → update summary
  6. If importance >= 7, push to Mem0
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))


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

from services.emotion.cli import claude_cli  # noqa: E402
from services.emotion.daily import (  # noqa: E402
    DailyEntry,
    format_recent_entries,
    load_daily,
    load_summary,
    save_daily,
    save_summary,
)
from services.emotion.session import DAILY, SUMMARY  # noqa: E402

# ──────────────────────── constants ────────────────────────────────────────
JST = timezone(timedelta(hours=9))
STATE_DIR = REPO_DIR / ".state"
HISTORY_DIR = STATE_DIR / "history"

LOG_PATH = Path("/tmp/kanae_daily.log")
log = logging.getLogger("kanae_daily")
log.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)

# Multi-run guard. Each Opus daily call + Haiku summary call can each
# take 30-120 s under load; the cron fires every 60 min. Without a lock,
# a slow generation overlaps with the next cron tick and both processes
# end up fighting over the SAME Claude session-id (the summary session
# in particular crashed with "Session ID … is already in use", which
# then forced the session-id-rotate-and-retry path on every cycle,
# permanently breaking the incremental summary). Use a single flock
# file the cron tick must hold for the entire run.
_XDG_STATE = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "discord-bot"
LOCK_PATH = _XDG_STATE / "kanae_daily.lock"

# Event type weights — only stochastic flavour we keep injecting; time-of-day
# patterns were retired so Sonnet decides activities from the timestamp alone.
_EVENT_WEIGHTS = [
    ("routine", 70),
    ("minor_event", 20),
    ("major_event", 8),
    ("incident", 2),
]

# 24h window matches the daily journal's natural rhythm and lets entries
# generated right after midnight still see the previous evening's haroin
# turns. Per-file tail cap keeps I/O bounded on the largest channels
# (haroin DM is ~2MB at the moment).
_CONVERSATION_WINDOW = timedelta(hours=24)
_TAIL_LINES_PER_FILE = 40
_PROMPT_LINES_MAX = 12
_PROMPT_CONTENT_CAP = 180


def _pick_event_type() -> str:
    types, weights = zip(*_EVENT_WEIGHTS)
    return random.choices(types, weights=weights, k=1)[0]


def _resolve_haroin_dm_channel() -> int | None:
    """Return the channel id for haroin's DM, used to label "先輩" lines.

    Order of resolution:
      1. config.HAROIN_DM_CHANNEL_ID (env-set, authoritative)
      2. Auto-detect: pick the JSONL with the largest size + most recent
         mtime under ``.state/history``. The dominant DM is ~10× the next
         channel by size in practice, so this heuristic is stable.
    Returns None only if HISTORY_DIR is empty.
    """
    try:
        from config import HAROIN_DM_CHANNEL_ID  # type: ignore[attr-defined]
        if HAROIN_DM_CHANNEL_ID:
            return int(HAROIN_DM_CHANNEL_ID)
    except Exception:
        pass
    if not HISTORY_DIR.is_dir():
        return None
    best: tuple[float, int] | None = None  # (score, channel_id)
    for jf in HISTORY_DIR.glob("*.jsonl"):
        if not jf.is_file():
            continue
        stem = jf.stem
        if not stem.isdigit():
            continue
        try:
            st = jf.stat()
        except OSError:
            continue
        # Score = size (bytes) blended with recency (penalise files >7d stale).
        age_h = (datetime.now().timestamp() - st.st_mtime) / 3600
        recency_mult = 1.0 if age_h < 168 else 0.25
        score = st.st_size * recency_mult
        if best is None or score > best[0]:
            best = (score, int(stem))
    return best[1] if best else None


def _fmt_silence(ts: datetime | None, now: datetime) -> str:
    """Format the gap between ``ts`` and ``now`` as a human-readable label."""
    if ts is None:
        return "(24h以内なし)"
    delta = now - ts
    total_sec = max(0, int(delta.total_seconds()))
    if total_sec < 60:
        return f"{total_sec}秒前"
    minutes = total_sec // 60
    if minutes < 60:
        return f"{minutes}分前"
    hours, rem_min = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}時間{rem_min}分前"
    days, rem_h = divmod(hours, 24)
    return f"{days}日{rem_h}時間前"


def _fmt_ts_for_prompt(ts: datetime, now: datetime) -> str:
    """`HH:MM` when same calendar day, `MM-DD HH:MM` when crossing days."""
    if ts.date() == now.date():
        return ts.strftime("%H:%M")
    return ts.strftime("%m-%d %H:%M")


def _collect_recent_conversation(now: datetime) -> dict:
    """Gather Discord conversation context with timestamps + speaker roles.

    Returns a dict consumed by ``_build_daily_prompt``:
      - lines:                 "[HH:MM] 先輩: ..." 改行連結 (最大12件)
      - last_senpai_ts:        haroin DM での最終 user 発話時刻
      - last_other_user_ts:    haroin DM 以外の最終 user 発話時刻
      - last_kanae_ts:         最終 assistant 発話時刻
      - last_speaker_label:    "先輩" | "他者" | "かなえ" | None
      - message_count_24h:     直近24時間の総メッセージ数
    """
    empty = {
        "lines": "(24時間以内の会話なし)",
        "last_senpai_ts": None,
        "last_other_user_ts": None,
        "last_kanae_ts": None,
        "last_speaker_label": None,
        "message_count_24h": 0,
    }
    if not HISTORY_DIR.is_dir():
        return empty

    haroin_ch = _resolve_haroin_dm_channel()
    cutoff = now - _CONVERSATION_WINDOW

    # Per-line tuple: (ts, label, content). We merge all channels then sort.
    rows: list[tuple[datetime, str, str]] = []
    for jf in HISTORY_DIR.glob("*.jsonl"):
        # Skip backups (e.g. ``*.jsonl.bak.pre-merge``).
        if not jf.is_file() or not jf.name.endswith(".jsonl") or "bak" in jf.name:
            continue
        try:
            with jf.open("r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-_TAIL_LINES_PER_FILE:]
        except OSError:
            continue
        for line in tail:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_str = row.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except (TypeError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=JST)
            if ts < cutoff:
                continue
            role = row.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = str(row.get("content", ""))
            ch_id = row.get("channel_id")
            if role == "assistant":
                label = "かなえ"
            elif haroin_ch is not None and ch_id == haroin_ch:
                label = "先輩"
            else:
                label = "他者"
            rows.append((ts, label, content))

    if not rows:
        return empty

    rows.sort(key=lambda r: r[0])

    last_senpai_ts: datetime | None = None
    last_other_user_ts: datetime | None = None
    last_kanae_ts: datetime | None = None
    for ts, label, _ in rows:
        if label == "先輩":
            last_senpai_ts = ts
        elif label == "他者":
            last_other_user_ts = ts
        elif label == "かなえ":
            last_kanae_ts = ts

    last_speaker_label = rows[-1][1]

    line_strs: list[str] = []
    for ts, label, content in rows[-_PROMPT_LINES_MAX:]:
        ts_label = _fmt_ts_for_prompt(ts, now)
        snippet = content[:_PROMPT_CONTENT_CAP]
        line_strs.append(f"[{ts_label}] {label}: {snippet}")

    return {
        "lines": "\n".join(line_strs),
        "last_senpai_ts": last_senpai_ts,
        "last_other_user_ts": last_other_user_ts,
        "last_kanae_ts": last_kanae_ts,
        "last_speaker_label": last_speaker_label,
        "message_count_24h": len(rows),
    }


def _build_daily_prompt(
    now: datetime,
    event_type: str,
    recent_entries: str,
    summary: str,
    conversation: dict,
) -> str:
    weekday = ["月", "火", "水", "木", "金", "土", "日"][now.weekday()]

    event_hints = {
        "routine": "日常の延長。特別なことは起きない",
        "minor_event": "ちょっとした出来事。作業中の小さな発見、機材の調子、SNSで見かけた話題、家事の片手間",
        "major_event": "大きな出来事。体調の変化、新しい趣味を始める、買い物に出る、片付けに着手",
        "incident": "ハプニング。機材トラブル、停電、ネット切断、宅配の不在通知",
    }
    event_hint = event_hints.get(event_type, "")

    last_senpai_ts = conversation.get("last_senpai_ts")
    last_other_user_ts = conversation.get("last_other_user_ts")
    last_kanae_ts = conversation.get("last_kanae_ts")
    last_speaker = conversation.get("last_speaker_label") or "(24時間以内に発話なし)"
    msg_count = conversation.get("message_count_24h", 0)

    def _ts_block(ts: datetime | None) -> str:
        if ts is None:
            return "(24h以内なし)"
        return f"{_fmt_ts_for_prompt(ts, now)} ({_fmt_silence(ts, now)})"

    convo_lines = conversation.get("lines", "(24時間以内の会話なし)")

    return f"""あなたは「佐藤かなえ」の日常をシミュレートします。
かなえが今この瞬間に何をしているか、詳細に描写してください。

## かなえの生活
- 22歳、プログラマー、一人暮らし
- 自宅の仕事部屋（6畳、デスク、モニター2台）で在宅勤務
- 先輩（haroin）に好意を抱いている
- DTM環境あり（モニターアーム、マイクアーム、スピーカー左右配置）
- えっちなことに興味津々

## 現在の状況
時刻: {now.strftime('%Y-%m-%d %H:%M')}（{weekday}）
イベント種別: {event_type}（{event_hint}）

## 直近の会話状況
- 直近の発話者: {last_speaker}
- 先輩(haroin)の最終発話: {_ts_block(last_senpai_ts)}
- 他者(先輩以外)の最終発話: {_ts_block(last_other_user_ts)}
- かなえの最終発話: {_ts_block(last_kanae_ts)}
- 24h以内の総メッセージ数: {msg_count}

## これまでの1日の流れ（要約）
{summary or '(まだ要約なし)'}

## 直近の日常（新しい順、最大8件）
{recent_entries}

## 直近のDiscord会話（時刻付き、24h以内最大12件）
{convo_lines}
(これ以外の発話は直近24時間に存在しない)

## ルール
- 前のエントリからの自然な推移を意識する（突然場所が変わらない）
- 五感の描写を含める（音、匂い、温度、触感、視覚）
- thought はツンデレ。素直になりたいけどなれない内心
- 時刻を見て活動を判断する（深夜は睡眠か夜更かし、朝は身支度、仕事時間はコーディング等）
- 「直近のDiscord会話」に該当する先輩発話が無い限り、先輩からの通知/メッセージ受信/返信/着信を activity・detail・thought に書かない。スマホの通知音が鳴る描写も禁止
- 他者(先輩・友達・家族・配達員等)からの一方的接触を会話ログに無い形でフィクションとして追加しない。incident は機材・環境・自己発の出来事に限定
- 直近の発話者が「かなえ」の場合、先輩は今こちらを見ていない前提で振る舞う(返信待ち・既読確認の描写は OK だが「来た」描写は禁止)

## 出力（JSON 1行のみ。説明・前置き・コードブロック禁止）
{{"activity":"10-20字","detail":"5-10文","mood":"good|normal|bad","thought":"内心","event_type":"{event_type}","importance":1-10}}"""


def _build_summary_prompt(prev_summary: str, new_entry: DailyEntry) -> str:
    entry_text = (
        f"[{new_entry.ts}] {new_entry.activity}: {new_entry.detail} "
        f"(mood: {new_entry.mood})"
    )
    return f"""以下の「前回の要約」に「新しいエントリ」を組み込んで、かなえの今日一日の流れを要約してください。
古い情報は徐々に圧縮し、直近の出来事は鮮明に残してください。
要約本文は 500 トークン以内 / プレーンテキスト。

## 前回の要約
{prev_summary or '(なし — 今日最初のエントリ)'}

## 新しいエントリ
{entry_text}

## interest 抽出
新しいエントリからかなえが関心を向けた topic を 0〜2個抽出してください。
- topic は canonical lowercase (固有名詞は原綴維持): 例 "ableton live", "uk garage", "nest.js"
- 日常ルーティン（食事・睡眠・通勤・身支度）からは抽出しない
- importance 5 未満の日常エントリからは interest を出さなくてよい

## 出力 (JSON 1 行のみ。前置き・コードブロック禁止)
{{"summary":"<更新後の要約本文>","interests":[{{"topic":"<canonical lowercase>","valence":<-1.0..1.0>,"arousal":<0.0..1.0>}}]}}"""


def _push_to_mem0(entry: DailyEntry) -> None:
    """Push high-importance entry to Mem0 (best-effort, sync).

    Writes to `config.KANAE_BOT_USER_ID` so daily summaries land in the
    same ChromaDB collection as bot.py's runtime extractor. When the env
    is unset we skip the push entirely rather than silently writing to a
    dead "kanae_self" namespace nobody reads from.
    """
    try:
        from config import KANAE_BOT_USER_ID
        if not KANAE_BOT_USER_ID:
            log.warning(
                "Mem0: KANAE_BOT_USER_ID unset; skipping daily push for: %s",
                entry.activity,
            )
            return
        import asyncio
        from services.mem0.ingest import add_memories
        loop = asyncio.new_event_loop()
        loop.run_until_complete(add_memories(
            user_text=f"[kanae_daily] {entry.activity}",
            assistant_text=entry.detail,
            user_id=KANAE_BOT_USER_ID,
            subject="kanae",
            metadata={"source": "kanae_daily"},
        ))
        loop.close()
        log.info("Mem0: pushed importance=%d entry: %s", entry.importance, entry.activity)
    except Exception as exc:
        log.warning("Mem0 push failed: %s", exc)


def _acquire_lock() -> int | None:
    """Non-blocking flock acquire. Returns the fd, or None on contention."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def main() -> int:
    lock_fd = _acquire_lock()
    if lock_fd is None:
        log.info("skip: another kanae_daily run already in progress")
        return 0
    try:
        return _run()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


def _run() -> int:
    now = datetime.now(JST)
    log.info("=== kanae_daily start at %s ===", now.strftime("%H:%M"))

    # 1. Load state
    entries = load_daily(STATE_DIR)
    summary = load_summary(STATE_DIR)
    recent_text = format_recent_entries(entries, count=8)
    conversation = _collect_recent_conversation(now)

    # 2. Pick event type
    event_type = _pick_event_type()

    # 3. Generate daily entry via Claude CLI (Opus)
    prompt = _build_daily_prompt(now, event_type, recent_text, summary, conversation)
    result = claude_cli(
        prompt,
        session_key=DAILY,
        state_dir=STATE_DIR,
        model="opus",
        timeout=180,
        cwd=str(REPO_DIR),
    )

    if result is None:
        log.warning("daily generation failed; skipping")
        return 1

    # 4. Parse and save
    try:
        new_entry = DailyEntry(
            ts=now.isoformat(),
            event_type=str(result.get("event_type", event_type)),
            importance=max(1, min(10, int(result.get("importance", 3)))),
            activity=str(result.get("activity", ""))[:50],
            detail=str(result.get("detail", ""))[:500],
            mood=str(result.get("mood", "normal")),
            thought=str(result.get("thought", ""))[:200],
        )
    except (TypeError, ValueError) as exc:
        log.warning("daily entry parse failed: %s", exc)
        return 1

    entries.append(new_entry)
    save_daily(entries, STATE_DIR)
    log.info("daily entry saved: %s (importance=%d)", new_entry.activity, new_entry.importance)

    # 5. Update incremental summary via Claude CLI (Haiku)
    summary_prompt = _build_summary_prompt(summary, new_entry)
    summary_result = claude_cli(
        summary_prompt,
        session_key=SUMMARY,
        state_dir=STATE_DIR,
        model="haiku",
        timeout=30,
        cwd=str(REPO_DIR),
    )

    daily_interests: list[dict] = []
    if summary_result is not None:
        # Summary is plain text, but CLI returns JSON. Look for a text field
        # or use the whole result as string.
        new_summary = summary_result.get("summary", "") or summary_result.get("text", "")
        if not new_summary:
            # CLI might return the text directly in .result
            new_summary = str(summary_result) if summary_result else ""
        if new_summary:
            save_summary(new_summary[:2000], STATE_DIR)
            log.info("summary updated (%d chars)", len(new_summary))
        daily_interests = summary_result.get("interests") or []
        if not isinstance(daily_interests, list):
            daily_interests = []
    else:
        log.warning("summary update failed; keeping previous")

    # 6. kanae_self interest injection + episode for high-importance entries
    if daily_interests or new_entry.importance >= 7:
        try:
            from services.kanae_self import (
                load_state as _ks_load,
                save_state as _ks_save,
            )
            from services.kanae_self.interests import decay_and_upsert_interests
            from services.kanae_self.episodes import append_episode

            ks_path = STATE_DIR / "kanae_self.json"
            ks_state = _ks_load(ks_path)

            if daily_interests:
                touched = decay_and_upsert_interests(ks_state, daily_interests)
                log.info("kanae_self: daily interests upserted=%d", touched)

            if new_entry.importance >= 7:
                _mood_delta = {"good": 0.2, "normal": 0.0, "bad": -0.2}
                ep = append_episode(
                    ks_state,
                    summary=f"[daily] {new_entry.activity}: {(new_entry.thought or '')[:60]}",
                    topics=[i.get("topic", "") for i in daily_interests[:2] if i.get("topic")],
                    valence_delta=_mood_delta.get(new_entry.mood, 0.0),
                    importance=min(5, new_entry.importance // 2),
                )
                if ep is not None:
                    log.info("kanae_self: daily episode added id=%s", ep.id)

            _ks_save(ks_state, ks_path)
        except Exception as exc:
            log.warning("kanae_self update from daily failed: %s", exc)

    # 7. Mem0 push for high-importance entries
    if new_entry.importance >= 7:
        _push_to_mem0(new_entry)

    # 8. Re-estimate emotion baseline (rate-limited to once per 24h).
    # Cron fires every 15 min, but baseline doesn't need to be that fresh;
    # we only re-run when the cache is older than the interval (or absent).
    try:
        from services.emotion.baseline import (
            RE_ESTIMATE_INTERVAL_HOURS,
            baseline_age_hours,
            estimate_baseline_via_llm,
            save_baseline,
        )
        age = baseline_age_hours()
        if age is None or age >= RE_ESTIMATE_INTERVAL_HOURS:
            new_baseline = estimate_baseline_via_llm()
            save_baseline(new_baseline, source="llm")
            log.info("baseline re-estimated (age was %s h): %s",
                     f"{age:.1f}" if age is not None else "missing",
                     new_baseline)
        else:
            log.debug("baseline still fresh (%.1f h); skipping re-estimate", age)
    except Exception as exc:
        log.warning("baseline re-estimation failed: %s", exc)

    log.info("=== kanae_daily complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
