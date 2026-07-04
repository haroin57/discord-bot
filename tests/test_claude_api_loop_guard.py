"""ADR-0036: ツールループガードレール (loop_guard) の単体テスト。

時刻注入式なので実時間に依存せず決定的に検証できる。
"""
from services.claude_api.loop_guard import (
    RequestBudget,
    ToolLoopDetector,
    normalize_tool_input,
)


class TestNormalizeToolInput:
    def test_dict_key_order_independent(self):
        a = normalize_tool_input({"a": 1, "b": 2})
        b = normalize_tool_input({"b": 2, "a": 1})
        assert a == b

    def test_none_is_empty(self):
        assert normalize_tool_input(None) == ""

    def test_non_json_falls_back_to_repr(self):
        obj = object()
        # 例外を投げずに文字列化できること。
        assert isinstance(normalize_tool_input({"x": obj}), str)


class TestToolLoopDetector:
    def test_distinct_inputs_never_trip(self):
        d = ToolLoopDetector(window=12, warn=3, brk=5)
        for i in range(10):
            assert d.record("Read", {"p": i}).action == "ok"

    def test_warn_then_break_on_repeat(self):
        d = ToolLoopDetector(window=12, warn=3, brk=5)
        actions = [d.record("Write", {"path": "a"}).action for _ in range(6)]
        # 3回目で warn、5回目で break。warn は一度だけ。
        assert actions == ["ok", "ok", "warn", "ok", "break", "break"]

    def test_warn_fires_only_once_per_streak(self):
        d = ToolLoopDetector(window=20, warn=3, brk=99)
        actions = [d.record("Write", {"path": "a"}).action for _ in range(6)]
        assert actions.count("warn") == 1

    def test_readonly_gets_slack(self):
        # readonly は warn/break に slack を加算 (warn=3+3=6, break=5+3=8)。
        d = ToolLoopDetector(
            window=20, warn=3, brk=5, readonly_slack=3, readonly_names={"Read"},
        )
        actions = [d.record("Read", {"p": "same"}).action for _ in range(9)]
        assert actions[5] == "warn"   # 6回目
        assert actions[7] == "break"  # 8回目

    def test_window_evicts_old_calls(self):
        # ウィンドウ外に溢れた反復はカウントに含めない → 常に window 個ぶん離して
        # 呼べば同一ハッシュが同居せず警告しない。window は break 閾値以上を確保
        # する下限があるので、それを尊重した形で検証する。
        d = ToolLoopDetector(window=3, warn=2, brk=3, readonly_slack=0)
        # A,B,C,A,B,C,... は window=3 では各ハッシュ最大1回しか同居しない。
        seq = ["A", "B", "C", "A", "B", "C", "A", "B", "C"]
        actions = [d.record("Read", {"k": k}).action for k in seq]
        assert all(a == "ok" for a in actions)

    def test_for_role_builds_with_readonly(self):
        d = ToolLoopDetector.for_role("admin", readonly_names={"Read", "Glob"})
        assert d.record("Read", {"p": 1}).action == "ok"


class TestRequestBudget:
    def _clock(self, t):
        return lambda: t[0]

    def test_ok_within_budget(self):
        t = [0.0]
        b = RequestBudget(
            wallclock_budget_s=100, output_token_budget=1000,
            soft_ratio=0.8, start=0.0, now=self._clock(t),
        )
        assert b.status().state == "ok"

    def test_output_token_soft_then_hard(self):
        t = [0.0]
        b = RequestBudget(
            wallclock_budget_s=0, output_token_budget=1000,
            soft_ratio=0.8, start=0.0, now=self._clock(t),
        )
        b.add_output_tokens(850)
        assert b.status().state == "soft"
        # soft は一度だけ。
        assert b.status().state == "ok"
        b.add_output_tokens(200)  # 1050 / 1000
        assert b.status().state == "hard"

    def test_wallclock_hard(self):
        t = [0.0]
        b = RequestBudget(
            wallclock_budget_s=40, output_token_budget=0,
            soft_ratio=0.8, start=0.0, now=self._clock(t),
        )
        assert b.status().state == "ok"
        t[0] = 33  # 33/40 = 82.5% → soft
        assert b.status().state == "soft"
        t[0] = 41  # 超過 → hard
        assert b.status().state == "hard"

    def test_zero_budget_disables_axis(self):
        t = [0.0]
        b = RequestBudget(
            wallclock_budget_s=0, output_token_budget=0,
            soft_ratio=0.8, start=0.0, now=self._clock(t),
        )
        b.add_output_tokens(10**9)
        t[0] = 10**9
        # 両軸無効 → 常に ok。
        assert b.status().state == "ok"

    def test_hard_reason_names_axis(self):
        t = [0.0]
        b = RequestBudget(
            wallclock_budget_s=0, output_token_budget=100,
            soft_ratio=0.8, start=0.0, now=self._clock(t),
        )
        b.add_output_tokens(200)
        st = b.status()
        assert st.state == "hard"
        assert "output_tokens" in st.reason

    def test_for_role_restricted_tighter_than_admin(self):
        admin = RequestBudget.for_role("admin", start=0.0, now=lambda: 0.0)
        restricted = RequestBudget.for_role("restricted", start=0.0, now=lambda: 0.0)
        # restricted の方が締まっている（同じ経過時間で先に hard になる）。
        admin_hard_at = admin._wall  # type: ignore[attr-defined]
        restricted_hard_at = restricted._wall  # type: ignore[attr-defined]
        assert restricted_hard_at < admin_hard_at
