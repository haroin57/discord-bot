"""動的ツールループのガードレール (ADR-0036).

回数上限を主制御から外す代わりに、これらが実務上のリミッタになる:

- ``ToolLoopDetector`` — 同一 ``(tool_name, 正規化引数)`` の反復（進捗ゼロの
  堂々巡り）をスライディングウィンドウで検知し、warn（1回だけ nudge）→ break
  （checkpoint 離脱）を返す。読み取り系ツールは確認の反復が正常なので閾値を緩める。
- ``RequestBudget`` — リクエスト全体のウォールクロック deadline と累積出力トークン
  footprint を追跡し、soft（wrap-up nudge を1回）/ hard（即 checkpoint 離脱）を返す。

いずれも外部依存なし・時刻注入式で、tool_loop.py から独立して単体テストできる。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass

from config import (
    REQUEST_BUDGET_SOFT_RATIO,
    REQUEST_OUTPUT_TOKEN_BUDGET,
    REQUEST_WALLCLOCK_BUDGET_S,
    RESTRICTED_OUTPUT_TOKEN_BUDGET,
    RESTRICTED_WALLCLOCK_BUDGET_S,
    SUBAGENT_WALLCLOCK_BUDGET_S,
    TOOL_LOOP_READONLY_SLACK,
    TOOL_LOOP_REPEAT_BREAK,
    TOOL_LOOP_REPEAT_WARN,
    TOOL_LOOP_WINDOW,
)


# ── 引数の正規化 ────────────────────────────────────────────────────────────
def normalize_tool_input(tool_input) -> str:
    """ツール引数を安定した文字列に落とす（ハッシュ用）。

    dict はキー順に依存しないよう ``sort_keys`` でシリアライズ。JSON 化できない
    値は ``repr`` にフォールバック。None/欠損は空文字に寄せる。
    """
    if tool_input is None:
        return ""
    try:
        return json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        return repr(tool_input)


def _call_hash(name: str, tool_input) -> str:
    raw = f"{name}\x00{normalize_tool_input(tool_input)}"
    return hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class GuardSignal:
    """ガードの判定結果。``action`` は ``ok`` / ``warn`` / ``break``。"""

    action: str
    reason: str = ""

    @property
    def is_ok(self) -> bool:
        return self.action == "ok"


_OK = GuardSignal("ok")


# ── ループ検知 ──────────────────────────────────────────────────────────────
class ToolLoopDetector:
    """同一 ``(tool, 引数)`` の反復を直近ウィンドウ内でカウントして検知する。

    ``record`` はツール実行ごとに1回呼ぶ。同一ハッシュがウィンドウ内で warn 閾値に
    「到達した瞬間だけ」warn を返し（毎ターン鳴らさない）、break 閾値以上で break。
    読み取り系ツール名 (``readonly_names``) は各閾値に ``readonly_slack`` を上乗せする。
    """

    def __init__(
        self,
        *,
        window: int = TOOL_LOOP_WINDOW,
        warn: int = TOOL_LOOP_REPEAT_WARN,
        brk: int = TOOL_LOOP_REPEAT_BREAK,
        readonly_slack: int = TOOL_LOOP_READONLY_SLACK,
        readonly_names: frozenset[str] | set[str] | None = None,
    ) -> None:
        # window は最低でも break 閾値を数えられる長さを確保する。
        self._window = max(1, window, brk + readonly_slack)
        self._warn = max(2, warn)
        self._brk = max(self._warn + 1, brk)
        self._slack = max(0, readonly_slack)
        self._readonly = frozenset(readonly_names or ())
        self._recent: deque[str] = deque(maxlen=self._window)
        # 既に warn を鳴らしたハッシュ（同一 streak で二重に鳴らさない）。
        self._warned: set[str] = set()

    def _thresholds(self, name: str) -> tuple[int, int]:
        if name in self._readonly:
            return self._warn + self._slack, self._brk + self._slack
        return self._warn, self._brk

    def record(self, name: str, tool_input) -> GuardSignal:
        h = _call_hash(name, tool_input)
        self._recent.append(h)
        count = sum(1 for x in self._recent if x == h)
        warn_at, break_at = self._thresholds(name)
        if count >= break_at:
            return GuardSignal(
                "break",
                f"tool '{name}' repeated {count}x in last {self._window} calls",
            )
        if count >= warn_at and h not in self._warned:
            self._warned.add(h)
            return GuardSignal(
                "warn",
                f"tool '{name}' repeated {count}x — no apparent progress",
            )
        # ウィンドウから溢れて再び新鮮になったら warn 済みフラグを落とす。
        if count < warn_at:
            self._warned.discard(h)
        return _OK

    @classmethod
    def for_role(cls, role: str, *, readonly_names=None) -> "ToolLoopDetector":
        # 閾値はロール非依存（種類は共通・値は予算側で差をつける方針, ADR-0036）。
        return cls(readonly_names=readonly_names)


# ── リクエスト予算 ──────────────────────────────────────────────────────────
@dataclass
class BudgetStatus:
    """予算の状態。``state`` は ``ok`` / ``soft`` / ``hard``。"""

    state: str
    reason: str = ""

    @property
    def is_ok(self) -> bool:
        return self.state == "ok"


_ROLE_WALLCLOCK = {
    "admin": REQUEST_WALLCLOCK_BUDGET_S,
    "restricted": RESTRICTED_WALLCLOCK_BUDGET_S,
    "subagent": SUBAGENT_WALLCLOCK_BUDGET_S,
}
_ROLE_OUTPUT_TOKENS = {
    "admin": REQUEST_OUTPUT_TOKEN_BUDGET,
    "restricted": RESTRICTED_OUTPUT_TOKEN_BUDGET,
    "subagent": REQUEST_OUTPUT_TOKEN_BUDGET,
}


class RequestBudget:
    """ウォールクロック deadline と累積出力トークンを追跡する。

    ``0`` 以下の予算は該当軸を無効化する。時刻は ``now`` 注入式（既定は
    ``time.monotonic``）でテスト可能。
    """

    def __init__(
        self,
        *,
        wallclock_budget_s: float | None,
        output_token_budget: int | None,
        soft_ratio: float = REQUEST_BUDGET_SOFT_RATIO,
        start: float | None = None,
        now=time.monotonic,
    ) -> None:
        self._now = now
        self._start = start if start is not None else now()
        self._wall = wallclock_budget_s if (wallclock_budget_s or 0) > 0 else None
        self._tok_budget = output_token_budget if (output_token_budget or 0) > 0 else None
        self._soft_ratio = min(max(soft_ratio, 0.0), 1.0)
        self._output_tokens = 0
        # soft は1回だけ通知する（毎ターン鳴らさない）。
        self._soft_fired = False

    def add_output_tokens(self, n: int) -> None:
        if n and n > 0:
            self._output_tokens += int(n)

    @property
    def output_tokens(self) -> int:
        return self._output_tokens

    def elapsed_s(self) -> float:
        return self._now() - self._start

    def _axes(self) -> list[tuple[str, float, str]]:
        """(軸名, 使用率, 実測の説明) のリスト。予算無効な軸は含めない。"""
        axes: list[tuple[str, float, str]] = []
        if self._wall is not None:
            frac = self.elapsed_s() / self._wall
            axes.append((
                "wallclock",
                frac,
                f"{self.elapsed_s():.0f}s / {self._wall:.0f}s",
            ))
        if self._tok_budget is not None:
            frac = self._output_tokens / self._tok_budget
            axes.append((
                "output_tokens",
                frac,
                f"{self._output_tokens} / {self._tok_budget} out-tok",
            ))
        return axes

    def status(self) -> BudgetStatus:
        worst_soft: tuple[str, str] | None = None
        for name, frac, detail in self._axes():
            if frac >= 1.0:
                return BudgetStatus("hard", f"{name} budget exhausted ({detail})")
            if frac >= self._soft_ratio and worst_soft is None:
                worst_soft = (name, detail)
        if worst_soft is not None and not self._soft_fired:
            self._soft_fired = True
            return BudgetStatus(
                "soft", f"{worst_soft[0]} budget {int(self._soft_ratio * 100)}%+ ({worst_soft[1]})"
            )
        return BudgetStatus("ok")

    @classmethod
    def for_role(cls, role: str, *, now=time.monotonic, start: float | None = None) -> "RequestBudget":
        return cls(
            wallclock_budget_s=_ROLE_WALLCLOCK.get(role, REQUEST_WALLCLOCK_BUDGET_S),
            output_token_budget=_ROLE_OUTPUT_TOKENS.get(role, REQUEST_OUTPUT_TOKEN_BUDGET),
            now=now,
            start=start,
        )
