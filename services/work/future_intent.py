"""Explicit reminder requests with source-time-based Japanese date resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ReminderIntent:
    run_at: float | None
    content: str
    error: str = ""


def parse_reminder(text: str, *, source_time: datetime | None = None) -> ReminderIntent | None:
    if not re.search(r"(?:通知して|知らせて|リマインドして)[。.]?$", text.strip()):
        return None
    if "出たら" in text or "変わったら" in text:
        return None
    now = source_time or datetime.now(ZoneInfo("Asia/Tokyo"))
    now = now.replace(tzinfo=ZoneInfo("Asia/Tokyo")) if now.tzinfo is None else now.astimezone(ZoneInfo("Asia/Tokyo"))
    body = re.sub(r"(?:通知して|知らせて|リマインドして)[。.]?$", "", text.strip()).strip()
    relative = re.match(r"(\d{1,4})(分|時間|日)後(?:に|、|\s)*", body)
    if relative:
        seconds = int(relative.group(1)) * {"分": 60, "時間": 3600, "日": 86400}[relative.group(2)]
        if seconds <= 0:
            return ReminderIntent(None, "", "将来の日時を指定してください。")
        target = now + timedelta(seconds=seconds)
        content = body[relative.end():]
    else:
        match = re.match(r"(?:(今日|明日|明後日)(?:の)?|(?P<date>\d{4}-\d{2}-\d{2})[ T])(?:朝|午前|午後)?(?P<hour>\d{1,2})(?:時(?P<min>\d{1,2})?分?|:(?P<minute>\d{2}))(?:に|、|\s)*", body)
        if not match:
            return ReminderIntent(None, "", "通知する日時を指定してください。例: 明日9時に会議を知らせて。")
        try:
            day = datetime.fromisoformat(match.group("date")).replace(tzinfo=now.tzinfo) if match.group("date") else now + timedelta(days={"今日": 0, "明日": 1, "明後日": 2}[match.group(1)])
            hour = int(match.group("hour"))
            if "午後" in match.group(0) and hour < 12:
                hour += 12
            if "午前" in match.group(0) and hour == 12:
                hour = 0
            target = day.replace(hour=hour, minute=int(match.group("min") or match.group("minute") or 0), second=0, microsecond=0)
        except ValueError:
            return ReminderIntent(None, "", "通知日時が有効ではありません。")
        content = body[match.end():]
    content = re.sub(r"(?:を|について)$", "", content).strip()
    if not content:
        return ReminderIntent(None, "", "通知する内容を指定してください。")
    return ReminderIntent(target.timestamp(), content)
