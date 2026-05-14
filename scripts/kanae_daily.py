#!/usr/bin/env python3
"""Cron entry point for kanae daily life simulation.

Run every 15 minutes by cron. Generates a daily life entry via Claude CLI
(Sonnet 4, session resume), updates the incremental summary via Haiku,
and optionally pushes high-importance entries to Mem0.

Flow:
  1. Load daily journal + summary
  2. Build prompt with context (time, recent entries, summary, conversation)
  3. Claude CLI (Sonnet 4, --resume daily session) → new entry
  4. Append to ring buffer, save
  5. Claude CLI (Haiku 4.5, --resume summary session) → update summary
  6. If importance >= 7, push to Mem0
"""

from __future__ import annotations

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

# Time patterns
_TIME_PATTERNS = {
    (0, 7): "睡眠中。たまに寝返りを打ったり、夢を見たりする",
    (7, 9): "朝の準備。目覚まし、洗顔、着替え、朝食",
    (9, 12): "午前の仕事。コーディング、コードレビュー、ミーティング",
    (12, 14): "昼休み。自炊か買い物、昼寝、SNS",
    (14, 18): "午後の仕事。集中コーディング、調査、ドキュメント",
    (18, 21): "夕方〜夜。夕食準備、食事、片付け、DTM、ゲーム、アニメ",
    (21, 24): "夜。風呂、リラックス、読書、ネットサーフィン、就寝準備",
}

# Event type weights
_EVENT_WEIGHTS = [
    ("routine", 70),
    ("minor_event", 20),
    ("major_event", 8),
    ("incident", 2),
]


def _pick_event_type() -> str:
    types, weights = zip(*_EVENT_WEIGHTS)
    return random.choices(types, weights=weights, k=1)[0]


def _get_time_pattern(hour: int) -> str:
    for (start, end), desc in _TIME_PATTERNS.items():
        if start <= hour < end:
            return desc
    return "自由時間"


def _collect_recent_conversation() -> str:
    """Gather recent conversation context from history files."""
    if not HISTORY_DIR.is_dir():
        return "(会話履歴なし)"
    now = datetime.now(JST)
    cutoff = now - timedelta(hours=4)
    parts: list[str] = []
    for jf in sorted(HISTORY_DIR.glob("*.jsonl")):
        try:
            with jf.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-6:]
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
            ts_str = row.get("timestamp")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=JST)
                    if ts < cutoff:
                        continue
                except (TypeError, ValueError):
                    pass
            role = row.get("role", "")
            content = str(row.get("content", ""))[:200]
            if role in ("user", "assistant"):
                who = "先輩" if role == "user" else "かなえ"
                parts.append(f"{who}: {content}")

    if not parts:
        return "(4時間以内の会話なし)"
    return "\n".join(parts[-8:])


def _build_daily_prompt(
    now: datetime,
    event_type: str,
    recent_entries: str,
    summary: str,
    conversation: str,
) -> str:
    weekday = ["月", "火", "水", "木", "金", "土", "日"][now.weekday()]
    hour = now.hour
    time_pattern = _get_time_pattern(hour)

    event_hints = {
        "routine": "日常の延長。特別なことは起きない",
        "minor_event": "ちょっとした出来事。同僚からの連絡、小さな発見、軽いトラブル",
        "major_event": "大きな出来事。友達と出かける、体調崩す、新しい趣味を始める",
        "incident": "ハプニング。思わぬ来客、機材トラブル、嬉しい知らせ",
    }
    event_hint = event_hints.get(event_type, "")

    return f"""あなたは「佐藤かなえ」の日常をシミュレートします。
かなえが今この瞬間に何をしているか、詳細に描写してください。

## かなえの生活
- 22歳、プログラマー、一人暮らし
- 自宅の仕事部屋（6畳、デスク、モニター2台）で在宅勤務
- 先輩（haroin）に好意を抱いている
- 毎日エナジードリンクを飲む（1日1本に減らす予定）
- DTM環境あり（モニターアーム、マイクアーム、スピーカー左右配置）
- えっちなことに興味津々

## 時間帯別パターン
{time_pattern}

## 現在の状況
時刻: {now.strftime('%Y-%m-%d %H:%M')}（{weekday}）
イベント種別: {event_type}（{event_hint}）

## これまでの1日の流れ（要約）
{summary or '(まだ要約なし)'}

## 直近の日常（新しい順、最大8件）
{recent_entries}

## 先輩との最近の会話
{conversation}

## ルール
- 前のエントリからの自然な推移を意識する（突然場所が変わらない）
- 五感の描写を含める（音、匂い、温度、触感、視覚）
- thought はツンデレ。素直になりたいけどなれない内心
- 睡眠中（0-7時）は基本的に寝ている。時々夢を見る

## 出力（JSON 1行のみ。説明・前置き・コードブロック禁止）
{{"activity":"10-20字","detail":"5-10文","mood":"good|normal|bad","thought":"内心","event_type":"{event_type}","importance":1-10}}"""


def _build_summary_prompt(prev_summary: str, new_entry: DailyEntry) -> str:
    entry_text = (
        f"[{new_entry.ts}] {new_entry.activity}: {new_entry.detail} "
        f"(mood: {new_entry.mood})"
    )
    return f"""以下の「前回の要約」に「新しいエントリ」を組み込んで、かなえの今日一日の流れを要約してください。
古い情報は徐々に圧縮し、直近の出来事は鮮明に残してください。
500トークン以内で出力してください。コードブロック・前置き禁止。プレーンテキストのみ。

## 前回の要約
{prev_summary or '(なし — 今日最初のエントリ)'}

## 新しいエントリ
{entry_text}

## 出力
要約テキストのみ。"""


def _push_to_mem0(entry: DailyEntry) -> None:
    """Push high-importance entry to Mem0 (best-effort, sync)."""
    try:
        import asyncio
        from services.mem0.ingest import add_memories
        loop = asyncio.new_event_loop()
        loop.run_until_complete(add_memories(
            user_text=f"[kanae_daily] {entry.activity}",
            assistant_text=entry.detail,
            user_id="kanae_self",
            metadata={"source": "kanae_daily"},
        ))
        loop.close()
        log.info("Mem0: pushed importance=%d entry: %s", entry.importance, entry.activity)
    except Exception as exc:
        log.warning("Mem0 push failed: %s", exc)


def main() -> int:
    now = datetime.now(JST)
    log.info("=== kanae_daily start at %s ===", now.strftime("%H:%M"))

    # 1. Load state
    entries = load_daily(STATE_DIR)
    summary = load_summary(STATE_DIR)
    recent_text = format_recent_entries(entries, count=8)
    conversation = _collect_recent_conversation()

    # 2. Pick event type
    event_type = _pick_event_type()

    # 3. Generate daily entry via Claude CLI (Sonnet 4)
    prompt = _build_daily_prompt(now, event_type, recent_text, summary, conversation)
    result = claude_cli(
        prompt,
        session_key=DAILY,
        state_dir=STATE_DIR,
        model="sonnet",
        timeout=90,
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
    else:
        log.warning("summary update failed; keeping previous")

    # 6. Mem0 push for high-importance entries
    if new_entry.importance >= 7:
        _push_to_mem0(new_entry)

    log.info("=== kanae_daily complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
