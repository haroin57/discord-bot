"""Provider-neutral response requirements and lossless presentation planning.

This module only interprets preferences. An ``effect`` intent is never an
authorization to execute an external action, and ``immediate`` never waives
verification. Persist ``ResponsePreferences.to_dict()`` with the work record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal, TypedDict, cast

ResultIntent = Literal["conclusion", "compare", "learn", "create", "effect"]
EvidenceLevel = Literal["contextual", "current", "independent"]
WaitPreference = Literal["immediate", "partial", "patient"]
Verbosity = Literal["brief", "normal", "detailed"]


class _PreferenceValues(TypedDict, total=False):
    intent: ResultIntent
    evidence: EvidenceLevel
    wait: WaitPreference
    verbosity: Verbosity


_VALUES = {
    "intent": {"conclusion", "compare", "learn", "create", "effect"},
    "evidence": {"contextual", "current", "independent"},
    "wait": {"immediate", "partial", "patient"},
    "verbosity": {"brief", "normal", "detailed"},
}


@dataclass(frozen=True)
class ResponsePreferences:
    intent: ResultIntent = "conclusion"
    evidence: EvidenceLevel = "contextual"
    wait: WaitPreference = "immediate"
    verbosity: Verbosity = "normal"

    def __post_init__(self) -> None:
        for name, allowed in _VALUES.items():
            value = getattr(self, name)
            if not isinstance(value, str) or value not in allowed:
                raise ValueError(f"Invalid response preference {name}: {value!r}")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: Mapping[str, object]) -> ResponsePreferences:
        """Read persisted known fields, accepting unrelated future metadata."""
        return cls(**cast(_PreferenceValues, {key: values[key] for key in _VALUES if key in values}))


def _coerce(
    preferences: ResponsePreferences | Mapping[str, object] | None,
) -> ResponsePreferences:
    if preferences is None:
        return ResponsePreferences()
    if isinstance(preferences, ResponsePreferences):
        return preferences
    return ResponsePreferences.from_dict(preferences)


def _has(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def infer_preferences(
    text: str,
    current: ResponsePreferences | Mapping[str, object] | None = None,
    *,
    overrides: Mapping[str, object] | None = None,
) -> ResponsePreferences:
    """Merge explicitly expressed dimensions without resetting the others.

    Inference precedence within a dimension is deterministic. Independent
    verification wins over contextual/latest wording, partial delivery wins
    over waiting, and brevity wins over generic detail wording. A structured
    override is authoritative and is validated rather than silently dropped.
    This deliberately does not infer permanent user preferences: the caller
    decides whether to store the result for this turn, work, or user.
    """
    text = text or ""
    values: dict[str, object] = {}

    # Specific requested deliverables take precedence over generic explanation.
    if _has(text, r"送信して|送って|予約して|購入して|通知して|起こして|電源.{0,8}(?:入れ|切っ)|\b(?:send|schedule|purchase|notify)\b"):
        values["intent"] = "effect"
    elif _has(text, r"実装|作って|作成|書いて|執筆|修正して|直して|下書き|\b(?:implement|create|draft|write|build|fix)\b"):
        values["intent"] = "create"
    elif _has(text, r"比較|選ぶ|どちら|どっち|候補|長所と短所|メリット.{0,8}デメリット|\b(?:compare|comparison|choose|versus)\b"):
        values["intent"] = "compare"
    elif _has(text, r"結論|答えだけ|回答だけ|要点|\b(?:conclusion|answer only|bottom line)\b"):
        values["intent"] = "conclusion"
    elif _has(text, r"学び|学習|理解|仕組み|教えて|解説|順番に|\b(?:learn|understand|explain|teach)\b"):
        values["intent"] = "learn"

    if _has(text, r"じっくり|熟考|徹底的|網羅的|深掘り|ちゃんと調べ|十分に調べ|十分な検証|精査|裏取り|一次資料|独立.{0,12}検証|検証して|\b(?:carefully|thoroughly|verify|independent verification|fact.check)\b"):
        values["evidence"] = "independent"
    elif _has(text, r"最新|現時点|現在の|今日の|今の価格|更新情報|調べたうえで|確認したうえで|\b(?:latest|up.to.date|current information|check sources)\b"):
        values["evidence"] = "current"
    elif _has(text, r"この文脈だけ|手元の情報だけ|既知の情報だけ|検索は不要|検索しないで|\b(?:provided context only|no browsing|without searching)\b"):
        values["evidence"] = "contextual"

    if _has(text, r"まず.{0,16}(?:途中|暫定|速報|要点|結果)|途中結果|途中経過も|随時報告|\b(?:partial results?|provisional|progress first)\b"):
        values["wait"] = "partial"
    elif _has(text, r"さくっと|パッと|すぐ(?:に)?(?:答|返|教)|今すぐ|即答|急いで|\b(?:quickly|immediately|right away)\b"):
        values["wait"] = "immediate"
    elif _has(text, r"時間.{0,8}(?:かけ|掛け|使っ)|急がない|待てます|待てる|じっくり|熟考|徹底的|\b(?:take your time|no rush|patiently|thoroughly)\b"):
        values["wait"] = "patient"

    if _has(text, r"一言で|一文で|手短に|簡潔|短く|結論だけ|要点だけ|ざっくりでいい|\b(?:briefly|brief|concise|one sentence|answer only|summary only)\b"):
        values["verbosity"] = "brief"
    elif _has(text, r"詳しく|詳説|詳細に|丁寧に説明|順を追って|省略せず|\b(?:detailed|in detail|step.by.step|comprehensive explanation)\b"):
        values["verbosity"] = "detailed"
    elif _has(text, r"説明量は(?:普通|標準)|標準の長さ|いつもの長さ|\b(?:normal length|standard detail)\b"):
        values["verbosity"] = "normal"

    if overrides is not None:
        unknown = set(overrides) - set(_VALUES)
        if unknown:
            raise ValueError(f"Unknown response preference overrides: {sorted(unknown)}")
        values.update(overrides)
    return replace(_coerce(current), **cast(_PreferenceValues, values))


def build_prompt_guidance(
    preferences: ResponsePreferences | Mapping[str, object],
) -> str:
    """Render independent requirements without claiming tools already ran."""
    prefs = _coerce(preferences)
    intent = {
        "conclusion": "結論と、その結論が成り立つ条件を先に示します。",
        "compare": "比較する候補、必須条件、評価軸、採用理由を示します。",
        "learn": "理解済みと未理解の論点を分け、説明と確認問題を必要に応じて示します。",
        "create": "成果物と、実施した検証および残る制約を示します。",
        "effect": "許可済みの操作範囲を確認し、依頼受付・送信・相手側受付・結果確認を区別します。意図の分類は操作の許可にはなりません。",
    }[prefs.intent]
    evidence = {
        "contextual": "現在の文脈に基づいて回答します。根拠が不足する断定は避け、未確認事項を示します。",
        "current": "時間に依存する情報は現時点の資料で確認し、出典・取得日時・対象時点を残します。確認できなければ未確認と示します。",
        "independent": "独立した根拠または独立した検証を用い、反証と条件の取りこぼしを確認します。実施できない検証は未確認と示します。短い表示の指定でこの水準を下げません。",
    }[prefs.evidence]
    wait = {
        "immediate": "利用者が次に判断できる回答を早く返します。必要な検証が終わらなければ暫定と明示し、完了と扱いません。",
        "partial": "判断に役立つ到達点が得られた時に途中結果を示します。暫定版と確定版を区別し、結論が変われば変更点を示します。",
        "patient": "必要な調査と検証に時間を使います。進捗は実際に完了した作業と未解決事項に基づいて示します。",
    }[prefs.wait]
    verbosity = {
        "brief": "最初の表示を短い結論・重要な根拠・未確定事項・必要な次の操作に絞ります。条件や留保を意味が変わる形で省略しません。長い本文は別の成果物にまとめます。",
        "normal": "最初に結論または到達点を示し、判断に必要な根拠と未確定事項を続けます。",
        "detailed": "要点を先に示したうえで、詳細な根拠・条件・説明を続けます。比較・学習・作成それぞれに適した形式にします。",
    }[prefs.verbosity]
    return "\n".join((
        "[応答の要件]",
        f"得たい結果: {intent}",
        f"根拠の水準: {evidence}",
        f"待ち時間: {wait}",
        f"表示量: {verbosity}",
        "内部の思考過程やツール名を進捗の代わりに表示しません。保存・配信・予約は実際に成功した範囲だけを完了と表現します。",
    ))


@dataclass(frozen=True)
class Presentation:
    """A delivery plan; callers must deliver the artifact or use full_text.

    ``artifact_content`` preserves the original body byte-for-byte as text.
    No upload or archival success is implied by constructing this object.
    ``display_text`` may be an explicitly labelled excerpt when no trustworthy
    model/user-provided summary is available. It is never a hidden truncation.
    """

    display_text: str
    full_text: str
    artifact_content: str | None
    provisional: bool = False
    revision: int | None = None

    @property
    def artifact_required(self) -> bool:
        return self.artifact_content is not None


def _opening_paragraph(text: str, limit: int) -> str:
    """Select a complete prose paragraph, avoiding cut code/table structures."""
    stripped = text.strip()
    for paragraph in re.split(r"\n\s*\n", stripped):
        paragraph = paragraph.strip()
        # A code fence, heading, or table is not a stand-alone summary.
        if not paragraph or all(line.startswith("#") for line in paragraph.splitlines()):
            continue
        if paragraph.startswith(("```", "~~~", "|")):
            return ""
        if len(paragraph) <= limit and "```" not in paragraph and "~~~" not in paragraph:
            return paragraph
        # Do not skip an overlong opening argument and promote a later
        # paragraph to a summary. A safe generic display is preferable.
        return ""
    return ""


def prepare_presentation(
    content: str,
    preferences: ResponsePreferences | Mapping[str, object],
    *,
    summary: str | None = None,
    uncertainties: Sequence[str] = (),
    next_action: str | None = None,
    provisional: bool = False,
    revision: int | None = None,
    previous_revision: int | None = None,
    change_summary: str | None = None,
    display_limit: int = 1400,
) -> Presentation:
    """Keep mobile first-view text useful while preserving the full report.

    ``summary`` must include material conditions and qualifications; this pure
    helper cannot prove semantic equivalence. Without one, it labels a complete
    opening paragraph as an excerpt. Explicit uncertainties and next actions
    are always included, even when they exceed the preferred display limit.
    The caller owns message splitting, artifact upload, and delivery receipts.
    """
    if display_limit < 80:
        raise ValueError("display_limit must be at least 80")
    if revision is not None and revision < 1:
        raise ValueError("revision must be positive")
    if previous_revision is not None and (
        previous_revision < 1 or revision is None or previous_revision >= revision
    ):
        raise ValueError("previous_revision must precede revision")
    prefs = _coerce(preferences)
    target = min(display_limit, 600) if prefs.verbosity == "brief" else display_limit
    artifact = len(content) > target or bool(summary and summary.strip() != content.strip())
    if artifact:
        if summary and len(summary.strip()) <= target:
            body = summary.strip()
        else:
            excerpt = _opening_paragraph(content, target)
            body = f"本文の冒頭抜粋です。\n{excerpt}" if excerpt else "本文は長いため、全文を別の成果物として提示する必要があります。"
    else:
        body = content

    labels: list[str] = []
    if provisional:
        labels.append("暫定結果です。")
    if revision is not None:
        labels.append(f"成果物の版は{revision}です。")
    if previous_revision is not None:
        labels.append(f"前の版{previous_revision}を更新します。")
    if change_summary:
        labels.append(f"変更点: {change_summary}")
    display_parts = [*labels, body]
    if uncertainties:
        display_parts.append("未確定事項:\n" + "\n".join(f"- {item}" for item in uncertainties))
    if next_action:
        display_parts.append(f"次の操作: {next_action}")
    return Presentation(
        display_text="\n\n".join(part for part in display_parts if part),
        full_text=content,
        artifact_content=content if artifact else None,
        provisional=provisional,
        revision=revision,
    )
