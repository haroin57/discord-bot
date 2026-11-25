"""Conservative, deterministic intake for work controls in ordinary conversation.

Reply binding and ownership are resolved by the durable store, never by these
text heuristics. A nearby task alone does not make an unrelated message an edit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkIntent:
    action: str
    text: str
    work_id: str | None = None
    question_id: str | None = None


_ID = re.compile(r"(?:用件|案件|work)\s*[:：#]?\s*(w[-_][a-zA-Z0-9_-]+)", re.I)
_QUESTION = re.compile(r"(?:回答|answer)\s*[:：#]?\s*(q[-_][a-zA-Z0-9_-]+)\s*[:：]?\s*(.+)", re.I | re.S)
_NEW = re.compile(
    r"比較(?:して|を|してください)|調(?:べて|査して|査を)|検討して|検証して|分析して|"
    r"設計して|計画(?:して|を立て)|実装して|作(?:って|成して)|まとめて|"
    r"熟考して|深掘りして|レポート|research\b|compare\b|implement\b", re.I,
)
_AMEND = re.compile(
    r"^(?:追記|訂正|条件(?:を|は|の)|予算(?:を|は)|期限(?:を|は)|変更|追加|"
    r"やっぱり|さっきの|先ほどの|それは|代わりに|amend\b|change\b)|"
    r"(?:に変更|に訂正|も条件に|は除外|を除外|以内にして|以下にして|上限にして)", re.I,
)


def classify_work_intent(text: str) -> WorkIntent:
    value = (text or "").strip()
    match = _ID.search(value)
    work_id = match.group(1) if match else None
    body = (value[:match.start()] + value[match.end():]).strip(" :：、。") if match else value
    if re.fullmatch(r"(?:(?:昨日|前回|先ほど|さっき|この前)の)?(?:(?:比較|調査|用件|案件)の)?続き(?:を)?(?:お願いします|お願い|進めて|やって|再開して)?[。.]?", body):
        return WorkIntent("resume", body, work_id)
    question = _QUESTION.fullmatch(body)
    if question:
        return WorkIntent("answer", question.group(2).strip(), work_id, question.group(1))
    if re.match(r"^(?:別件(?:ですが)?|ところで|ついでに|btw\b|aside\b)", body, re.I):
        return WorkIntent("aside", body, work_id)
    if re.fullmatch(r"(?:用件|案件)(?:一覧|を一覧|を見せて)?[。.]?", body):
        return WorkIntent("list", body, work_id)
    controls = (
        ("cancel", r"(?:これ|この用件|この案件)?(?:を)?(?:取り消して|取消|キャンセル|中止して|中止|cancel)"),
        ("pause", r"(?:一旦|いったん)?(?:休止|停止して|止めて|待って|保留|pause)"),
        ("resume", r"(?:続き(?:を)?(?:お願い|やって|進めて)?|再開(?:して)?|resume)"),
        ("status", r"(?:進捗(?:は|を教えて)?|状況(?:は|を教えて)?|どうなった|どこまで(?:進んだ)?|status)"),
    )
    for action, pattern in controls:
        if re.fullmatch(pattern + r"[。.?？]?", body, re.I):
            return WorkIntent(action, body, work_id)
    if _AMEND.search(body):
        return WorkIntent("amend", body, work_id)
    if _NEW.search(body):
        return WorkIntent("new", body, work_id)
    if work_id:
        return WorkIntent("followup", body, work_id)
    return WorkIntent("chat", value)
