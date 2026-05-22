"""LLM-derived rest-anchor for the silence-tick mean-reversion target.

Replaces the previously hardcoded EMOTION_BASELINE dict. The baseline is
estimated by Claude (Haiku) from kanae's persona prompt + recent context
(emotion journal, daily journal+summary, Mem0 memories, previous baseline),
cached in .state/kanae_baseline.json, and re-estimated daily by the
kanae_daily cron.

The "rest anchor" is intentionally allowed to drift over weeks as kanae's
relationship with haroin evolves, but per-call echo-chamber risk is
mitigated by explicit prompt language ("the previous baseline is a
reference, not a target — re-derive from persona").

When the cache is missing and LLM estimation fails, callers receive None
and silence-tick mean-reversion is skipped entirely — the LLM's per-tick
emotion output stands on its own without being pulled toward any anchor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.emotion.params import BOUNDS
from utils.atomic_write import atomic_write_json

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

REPO_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_STATE_DIR = REPO_DIR / ".state"
BASELINE_FILENAME = "kanae_baseline.json"
RE_ESTIMATE_INTERVAL_HOURS = 24.0

# Truncation budgets — keep total prompt under ~4-5k tokens so Haiku
# responds quickly. CLAUDE.md is huge so we slice rather than send whole.
PERSONA_MAX_CHARS = 6000
EMOTION_JOURNAL_RECENT_N = 20
DAILY_RECENT_N = 12
MEM0_RESULTS_LIMIT = 8
DAILY_SUMMARY_MAX_CHARS = 1500

NUMERIC_FIELDS = (
    "valence", "arousal", "loneliness", "affection", "curiosity", "anxiety",
)

GLOBAL_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"
PROJECT_CLAUDE_MD = REPO_DIR / "CLAUDE.md"

# Mem0 search queries used to pull persona-relevant long-term memories.
# Multiple queries broaden recall vs a single bag-of-keywords string.
_MEM0_QUERIES = (
    "かなえの性格と感情の傾向",
    "先輩への好意と距離感",
    "最近の出来事と気分",
)


# ---------------------------------------------------------------------------
# context collectors
# ---------------------------------------------------------------------------


def _load_persona_text() -> str:
    """Read global + project CLAUDE.md, return combined text capped at limit."""
    parts: list[str] = []
    for path in (GLOBAL_CLAUDE_MD, PROJECT_CLAUDE_MD):
        try:
            text = path.read_text(encoding="utf-8")
            parts.append(f"--- {path} ---\n{text}")
        except OSError as exc:
            log.debug("persona file unreadable %s: %s", path, exc)
    combined = "\n\n".join(parts)
    if len(combined) > PERSONA_MAX_CHARS:
        combined = combined[:PERSONA_MAX_CHARS] + "\n…(truncated)"
    return combined


def _format_emotion_journal(state_dir: Path) -> str:
    """Format the last N entries of kanae_emotion.json's journal."""
    try:
        from services.emotion.state import load_state
        state = load_state(state_dir / "kanae_emotion.json")
    except Exception as exc:
        log.debug("emotion state unreadable: %s", exc)
        return "(感情ジャーナル取得失敗)"
    journal = list(state.emotion_journal)[-EMOTION_JOURNAL_RECENT_N:]
    if not journal:
        return "(まだ感情ジャーナルなし)"
    lines: list[str] = []
    for e in journal:
        ts = str(e.get("ts", ""))[:16]
        v = e.get("v", 0)
        ar = e.get("ar", 0)
        lo = e.get("lo", 0)
        af = e.get("af", 0)
        cu = e.get("cu", 0)
        ax = e.get("ax", 0)
        lines.append(
            f"{ts} v{v:+.2f} ar{ar:.2f} lo{lo:.2f} af{af:.2f} cu{cu:.2f} ax{ax:.2f}"
        )
    return "\n".join(lines)


def _format_daily_context(state_dir: Path) -> tuple[str, str]:
    """Return (recent_entries_text, summary_text)."""
    try:
        from services.emotion.daily import load_daily, load_summary
        entries = load_daily(state_dir)
        summary = load_summary(state_dir)
    except Exception as exc:
        log.debug("daily journal unreadable: %s", exc)
        return "(日常ジャーナル取得失敗)", ""
    recent = entries[-DAILY_RECENT_N:] if entries else []
    if not recent:
        recent_text = "(まだ日常ジャーナルなし)"
    else:
        lines: list[str] = []
        for e in recent:
            ts = (e.ts or "")[:16]
            activity = (e.activity or "")[:40]
            mood = e.mood or "normal"
            thought = (e.thought or "")[:80]
            lines.append(f"{ts} [{mood}] {activity} ｜ {thought}")
        recent_text = "\n".join(lines)
    if summary and len(summary) > DAILY_SUMMARY_MAX_CHARS:
        summary = summary[:DAILY_SUMMARY_MAX_CHARS] + "…(truncated)"
    return recent_text, summary or "(要約なし)"


async def _async_mem0_search() -> list[dict]:
    """Search Mem0 with the persona-relevant query set. Returns merged hits."""
    try:
        from config import KANAE_BOT_USER_ID
        from services.mem0.search import search_memories_multi
    except Exception as exc:
        log.debug("mem0 unavailable: %s", exc)
        return []
    if not KANAE_BOT_USER_ID:
        return []
    try:
        return await search_memories_multi(
            list(_MEM0_QUERIES),
            user_id=KANAE_BOT_USER_ID,
            limit=MEM0_RESULTS_LIMIT,
        )
    except Exception as exc:
        log.warning("mem0 search for baseline failed: %s", exc)
        return []


def _format_mem0_context() -> str:
    """Run Mem0 search from sync context and format hits as bullet list."""
    try:
        hits = asyncio.run(_async_mem0_search())
    except RuntimeError:
        # An event loop is already running (caller is async). Fall through
        # with empty — async callers can pre-fetch if they want hits.
        log.debug("mem0 search skipped (event loop already running)")
        return "(Mem0コンテキストなし)"
    except Exception as exc:
        log.warning("mem0 search wrapper failed: %s", exc)
        return "(Mem0取得失敗)"
    if not hits:
        return "(Mem0関連記憶なし)"
    lines: list[str] = []
    for h in hits[: MEM0_RESULTS_LIMIT]:
        memo = str(h.get("memory") or h.get("text") or "").replace("\n", " ")
        if memo:
            lines.append(f"- {memo[:200]}")
    return "\n".join(lines) if lines else "(Mem0関連記憶なし)"


def _format_previous_baseline(state_dir: Path) -> str:
    """Show previous baseline + age, with explicit anti-anchoring caveat."""
    p = state_dir / BASELINE_FILENAME
    if not p.is_file():
        return "(過去 baseline なし — 初回推定)"
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        inner = data.get("baseline") or {}
        ts = data.get("generated_at", "")
        age_str = ""
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                age = datetime.now(timezone.utc) - dt
                age_str = f"（{age.total_seconds() / 3600:.1f}時間前）"
            except ValueError:
                pass
        axes = " ".join(
            f"{a}={float(inner.get(a, 0)):+.2f}" for a in NUMERIC_FIELDS
        )
        return f"{axes} {age_str}"
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        log.debug("previous baseline unreadable: %s", exc)
        return "(過去 baseline 読み込み失敗)"


# ---------------------------------------------------------------------------
# prompt construction
# ---------------------------------------------------------------------------


def _build_prompt(
    *,
    persona: str,
    emotion_journal: str,
    daily_recent: str,
    daily_summary: str,
    mem0_context: str,
    previous_baseline: str,
    now: datetime,
) -> str:
    weekday = ["月", "火", "水", "木", "金", "土", "日"][now.weekday()]
    return f"""あなたは「佐藤かなえ」の長期的な感情 baseline (rest anchor) を推定する役割です。

baseline は「会話も刺激もなく一人で過ごしている時に、長期平均としてかなえが落ち着く先の値」です。
個々のイベントによる短期的な振れではなく、persona の核と最近の文脈から導かれる安定値を出してください。
baseline は日次で再推定されるため、関係性や生活の変化に応じてゆっくり変動してかまいません。

## 現在時刻
{now.strftime('%Y-%m-%d %H:%M')}（{weekday}）JST

## persona 定義（最重要）
<persona>
{persona}
</persona>

## 過去 baseline（参考。これに引きずられず persona から再判断せよ）
<previous_baseline note="echo-chamber 防止: 過去値を肯定する方向に固着しないこと">
{previous_baseline}
</previous_baseline>

## 最近の感情ジャーナル（短期の振れ。baseline ではなく state の履歴）
<emotion_journal note="これは『最近どう揺れたか』。rest anchor とは区別せよ">
{emotion_journal}
</emotion_journal>

## 直近の日常ジャーナル
<daily_recent note="行動と内心のスナップショット">
{daily_recent}
</daily_recent>

## 日常要約
<daily_summary>
{daily_summary}
</daily_summary>

## Mem0 長期記憶（persona・関係性の長期傾向）
<mem0>
{mem0_context}
</mem0>

## 推定ルール
1. persona の核（性格・関係性・生活）から rest anchor を導く
2. 過去 baseline は参考だが、追従する義務はない。明確な根拠があれば変更してよい
3. 短期の感情ジャーナルの振れに引っ張られない。それは state であって anchor ではない
4. ただし、長期的な変化（先輩との関係が深まった・離れた、生活が変わった等）が
   Mem0 / daily に観測されているなら、baseline をその方向に動かしてよい
5. 各軸の動きは前回比で大きくても ±0.15 程度に留める（日次再推定なので緩やかに）

## 軸の範囲
- valence: [-1, 1]（負=不機嫌、正=機嫌よし）
- arousal: [0, 1]（覚醒度）
- loneliness: [0, 1]（寂しさ）
- affection: [0, 1]（先輩への好意）
- curiosity: [0, 1]（興味）
- anxiety: [0, 1]（不安）

## 出力（JSON 1行のみ。説明・前置き・コードブロック禁止）
{{"baseline":{{"valence":float,"arousal":float,"loneliness":float,"affection":float,"curiosity":float,"anxiety":float}},"rationale":"1-2行で推定理由（前回からの変化と根拠を含める）"}}
"""


def _collect_context(state_dir: Path) -> dict:
    """Gather every input slice into one dict, ready for _build_prompt()."""
    daily_recent, daily_summary = _format_daily_context(state_dir)
    return {
        "persona": _load_persona_text(),
        "emotion_journal": _format_emotion_journal(state_dir),
        "daily_recent": daily_recent,
        "daily_summary": daily_summary,
        "mem0_context": _format_mem0_context(),
        "previous_baseline": _format_previous_baseline(state_dir),
        "now": datetime.now(JST),
    }


# ---------------------------------------------------------------------------
# disk I/O
# ---------------------------------------------------------------------------


def _baseline_path(state_dir: Path) -> Path:
    return state_dir / BASELINE_FILENAME


def load_baseline(state_dir: Path | None = None) -> dict[str, float] | None:
    """Load cached baseline from disk. Returns None if missing or corrupt."""
    p = _baseline_path(state_dir or DEFAULT_STATE_DIR)
    if not p.is_file():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("baseline load failed (%s); treating as missing", exc)
        return None
    if not isinstance(data, dict):
        return None
    inner = data.get("baseline")
    if not isinstance(inner, dict):
        return None
    try:
        return {axis: float(inner[axis]) for axis in NUMERIC_FIELDS}
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("baseline schema invalid (%s); treating as missing", exc)
        return None


def save_baseline(
    baseline: dict[str, float],
    state_dir: Path | None = None,
    *,
    source: str = "llm",
) -> None:
    """Persist a baseline dict to .state/kanae_baseline.json atomically."""
    state_dir = state_dir or DEFAULT_STATE_DIR
    payload = {
        "baseline": {axis: float(baseline[axis]) for axis in NUMERIC_FIELDS},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    atomic_write_json(_baseline_path(state_dir), payload)


def baseline_age_hours(state_dir: Path | None = None) -> float | None:
    """Return age of cached baseline in hours, or None if absent / unparseable."""
    p = _baseline_path(state_dir or DEFAULT_STATE_DIR)
    if not p.is_file():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("generated_at")
        if not isinstance(ts, str):
            return None
        dt = datetime.fromisoformat(ts)
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 3600.0
    except (OSError, json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> dict:
    """Pull the outermost {...} object out of arbitrary model text.

    Claude sometimes prefixes its reply with a polite preamble ("既に前の
    ターンで出力済みです：") even when the prompt says JSON-only. A strict
    json.loads() on the whole string fails on those. Greedy regex captures
    the outermost brace span and we parse just that.
    """
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```", 2)
        if len(parts) >= 2:
            text = parts[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object found in response: {text[:200]!r}")
    return json.loads(match.group(0))


def _call_claude_cli(
    prompt: str, *, model: str, timeout: int,
) -> dict:
    """One-shot Claude CLI call. No session resume — baseline is stateless.

    Each invocation is a fresh persona-only question; reusing a session
    leaks unrelated context and causes Claude to prepend "already answered
    in a previous turn" preambles, breaking JSON parsing.
    """
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"baseline LLM timeout after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("claude CLI not found on PATH") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"baseline LLM exit={proc.returncode}: {proc.stderr[:200]}"
        )
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"baseline outer JSON parse failed: {exc}") from exc
    inner = outer.get("result", "") if isinstance(outer, dict) else ""
    if not inner:
        raise RuntimeError("baseline: empty .result from claude CLI")
    return _extract_json_object(inner)


def _validate(raw: dict) -> dict[str, float]:
    """Coerce LLM output into a clamped 6-axis dict. Raises on missing axis."""
    inner = raw.get("baseline")
    if not isinstance(inner, dict):
        raise ValueError("response missing 'baseline' dict")
    out: dict[str, float] = {}
    for axis in NUMERIC_FIELDS:
        if axis not in inner:
            raise ValueError(f"baseline missing axis: {axis}")
        val = float(inner[axis])
        lo, hi = BOUNDS[axis]
        out[axis] = max(lo, min(hi, val))
    return out


def estimate_baseline_via_llm(
    state_dir: Path | None = None,
    *,
    model: str = "haiku",
    timeout: int = 120,
) -> dict[str, float]:
    """Ask Claude to estimate kanae's rest-anchor. Raises on failure.

    Pulls persona, recent emotion/daily journal, Mem0 memories, and previous
    baseline before building the prompt — see `_collect_context()`.
    """
    state_dir = state_dir or DEFAULT_STATE_DIR
    ctx = _collect_context(state_dir)
    prompt = _build_prompt(**ctx)
    raw = _call_claude_cli(prompt, model=model, timeout=timeout)
    return _validate(raw)


def get_or_estimate_baseline(
    state_dir: Path | None = None,
) -> dict[str, float]:
    """Load cached baseline, or estimate via LLM and cache if missing.

    Raises on LLM failure when the cache is empty.
    """
    cached = load_baseline(state_dir)
    if cached is not None:
        return cached
    baseline = estimate_baseline_via_llm(state_dir)
    save_baseline(baseline, state_dir)
    return baseline
