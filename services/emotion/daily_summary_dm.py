"""Hourly daily-summary DM to senpai.

Every hour, summarize the past hour of kanae_daily entries (typically
4 entries at the 15-min cadence) and DM the summary to haroin.

The summary call uses a throwaway Claude CLI session so the emotion
daemon's context isn't polluted with summarisation prompts. If no
entries exist in the last hour (cron lag, fresh bot restart), the cycle
quietly skips.

A missed cycle (bot down at the hour boundary) is *not* backfilled —
the next tick simply summarises whatever happened in its own previous
hour. Senpai will see a slightly shorter recap rather than a duplicate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.emotion.daily import load_daily

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# haroin (先輩) — recipient of the hourly recap DM.
HAROIN_USER_ID = 573489206240870410

# Loop interval. 1h matches the requested cadence; the entry window is
# also 1h so consecutive recaps don't overlap.
INTERVAL_S = 3600

_SUMMARY_PROMPT = """以下は「佐藤かなえ」(後輩キャラ・先輩haroinに好意あり) の直近1時間の日常ログです。
これを先輩への DM として送る短い近況報告にまとめてください。

## ログ
{entries}

## 制約
- 後輩キャラ口調、敬語、感情控えめ、上から目線も少し
- 2-4 行
- 「...」は使わない
- かなえ視点の一人称で報告 (「〜してました」「〜です」)
- 出力は本文のみ。前置き・コードブロック・JSON 禁止
"""


async def hourly_summary_dm_loop(bot, state_dir: Path, repo_dir: Path) -> None:
    """Forever loop: every hour DM senpai a recap of kanae's last hour."""
    while True:
        try:
            await asyncio.sleep(INTERVAL_S)
        except asyncio.CancelledError:
            return
        try:
            await _run_one_cycle(bot, state_dir, repo_dir)
        except Exception as exc:
            log.warning("hourly summary DM iteration failed: %s", exc)


async def _run_one_cycle(bot, state_dir: Path, repo_dir: Path) -> None:
    now = datetime.now(JST)
    cutoff = now - timedelta(hours=1)

    entries = await asyncio.to_thread(load_daily, state_dir)
    recent = [e for e in entries if _entry_is_recent(e, cutoff)]
    if not recent:
        log.info("hourly_summary_dm: no entries in last hour; skip")
        return

    text = _format_entries(recent)
    summary = await asyncio.to_thread(_generate_summary, text, repo_dir)
    if not summary:
        log.info("hourly_summary_dm: summary generation returned empty")
        return

    user = bot.get_user(HAROIN_USER_ID)
    if user is None:
        try:
            user = await bot.fetch_user(HAROIN_USER_ID)
        except Exception as exc:
            log.warning("hourly_summary_dm: fetch_user failed: %s", exc)
            return
    if user is None:
        log.warning("hourly_summary_dm: user %s unresolved", HAROIN_USER_ID)
        return

    try:
        await user.send(summary)
        log.info("hourly_summary_dm sent (%d entries, %d chars)",
                 len(recent), len(summary))
    except Exception as exc:
        log.warning("hourly_summary_dm: user.send failed: %s", exc)


def _entry_is_recent(entry, cutoff: datetime) -> bool:
    ts_str = getattr(entry, "ts", "") or ""
    if not ts_str:
        return False
    try:
        ts = datetime.fromisoformat(ts_str)
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=JST)
    return ts >= cutoff


def _format_entries(entries) -> str:
    lines = []
    for e in entries:
        lines.append(
            f"[{e.ts}] activity={e.activity} | mood={e.mood} | "
            f"thought={e.thought} | detail={e.detail[:200]}"
        )
    return "\n".join(lines)


def _generate_summary(entries_text: str, repo_dir: Path) -> str | None:
    """One-shot Claude CLI Haiku call. Throwaway session per call.

    Returns the plain-text body (already stripped of code fences) or
    None on any failure.
    """
    prompt = _SUMMARY_PROMPT.format(entries=entries_text)
    session_id = str(uuid.uuid4())
    cmd = [
        "claude", "-p", prompt,
        "--session-id", session_id,
        "--print",
        "--output-format", "json",
        "--model", "haiku",
        "--allowed-tools", "",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(repo_dir),
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        log.warning("hourly_summary_dm: claude CLI timed out")
        return None
    except FileNotFoundError:
        log.warning("hourly_summary_dm: claude CLI not on PATH")
        return None
    if proc.returncode != 0:
        log.warning("hourly_summary_dm: claude CLI exit=%d stderr=%s",
                    proc.returncode, proc.stderr[:300])
        return None
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.warning("hourly_summary_dm: claude CLI returned non-JSON")
        return None
    result = outer.get("result", "") if isinstance(outer, dict) else ""
    if not isinstance(result, str):
        return None
    body = result.strip()
    if body.startswith("```"):
        parts = body.split("```", 2)
        if len(parts) >= 2:
            body = parts[1]
        if body.startswith("json"):
            body = body[4:]
        body = body.rsplit("```", 1)[0].strip()
    return body or None
