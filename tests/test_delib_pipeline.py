"""熟考パイプライン P2-P5 の単体テスト（router / verifier / planner /
scratchpad / second_opinion / magi_lite）。LLM 呼び出しは全て mock。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.claude_api import router as rt
from services.claude_api import verifier as vf
from services.claude_api import planner as pl
from services.claude_api import magi_lite as ml
from services.claude_api import second_opinion as so


def _resp(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


# ---------------------------------------------------------------------------
# P2: router
# ---------------------------------------------------------------------------

class TestRouter:
    @pytest.mark.parametrize("text,tier", [
        ("じっくり考えて、この設計どう思う？", "deliberate"),   # force marker
        ("さくっと教えて: このAPIの使い方を調査して", "chat"),  # negative marker 優先
        ("この2つのライブラリを比較して検討して", "deliberate"),  # task kw
        ("おはよ", "chat"),                                     # short no kw
        ("ん？", "chat"),
    ])
    def test_tier_table(self, text, tier):
        assert rt.route_request(text).tier == tier

    def test_judgment_detection(self):
        assert rt.route_request("AとBどっちがいいと思う？").judgment is True
        assert rt.route_request("今日の天気は？").judgment is False

    def test_long_text_escalates(self):
        d = rt.route_request("あ" * 350)
        assert d.tier == "deliberate"

    def test_attachments_add_score(self):
        base = rt.route_request("これ見て", has_attachments=False).score
        with_att = rt.route_request("これ見て", has_attachments=True).score
        assert with_att > base


# ---------------------------------------------------------------------------
# P2: verifier — verdict parse / fail-open / prompts / judge gate
# ---------------------------------------------------------------------------

class TestVerdictParse:
    def test_good_json(self):
        v = vf._parse_verdict(
            '{"pass": false, "issues": [{"claim": "X", "problem": "Y", '
            '"severity": "high"}]}')
        assert v.ok is False and len(v.issues) == 1
        assert v.issues[0].severity == "high"

    def test_fenced_json_with_preamble(self):
        v = vf._parse_verdict(
            "検証しました。\n```json\n{\"pass\": true, \"issues\": []}\n```")
        assert v.ok is True and not v.degraded

    def test_broken_json_fails_open(self):
        v = vf._parse_verdict("just prose, no json")
        assert v.ok is True and v.degraded is True

    def test_pass_true_with_issues_is_fail(self):
        v = vf._parse_verdict(
            '{"pass": true, "issues": [{"claim": "c", "problem": "p"}]}')
        assert v.ok is False  # issues があれば pass 扱いしない


class TestVerifyDraft:
    @pytest.mark.asyncio
    async def test_verify_uses_mini_loop_and_parses(self, monkeypatch):
        from services.claude_api.tool_loop import AgentLoopResult
        fake = AsyncMock(return_value=AgentLoopResult(
            text='{"pass": false, "issues": [{"claim": "数値相違", '
                 '"problem": "ledger と不一致", "severity": "high"}]}',
            synthetic_session_id="x", active_mcp_keys=[],
        ))
        monkeypatch.setattr("services.claude_api.tool_loop.run_agent_loop", fake)
        v = await vf.verify_draft(
            user_request="req", draft="draft", evidence_digest="[E1] ...",
            evidence_root="/tmp", api_client=object(), channel_id=1,
            model="claude-fable-5", extra_betas=["context-1m-2025-08-07", "x"],
        )
        assert v.ok is False and v.issues[0].claim == "数値相違"
        # context-1m は落として呼ぶ
        _, kwargs = fake.call_args
        assert all(not b.startswith("context-1m") for b in kwargs["extra_betas"])
        # tools は Read のみ
        assert [t["name"] for t in kwargs["tools_for_api"]] == ["Read"]

    @pytest.mark.asyncio
    async def test_verify_exception_fails_open(self, monkeypatch):
        monkeypatch.setattr(
            "services.claude_api.tool_loop.run_agent_loop",
            AsyncMock(side_effect=RuntimeError("down")),
        )
        v = await vf.verify_draft(
            user_request="r", draft="d", evidence_digest="", evidence_root=None,
            api_client=object(), channel_id=1, model="m", extra_betas=[],
        )
        assert v.ok is True and v.degraded is True


class TestJudgeGate:
    def _ev(self, n):
        return SimpleNamespace(records=[object()] * n)

    def test_should_run_judge_conditions(self):
        assert vf.should_run_judge("x" * 500, self._ev(2), tier="chat",
                                   restricted=False)
        assert not vf.should_run_judge("x" * 500, self._ev(2), tier="deliberate",
                                       restricted=False)
        assert not vf.should_run_judge("x" * 500, None, tier="chat",
                                       restricted=False)
        assert not vf.should_run_judge("x" * 500, self._ev(0), tier="chat",
                                       restricted=False)
        assert not vf.should_run_judge("short", self._ev(2), tier="chat",
                                       restricted=False)
        assert not vf.should_run_judge("x" * 500, self._ev(2), tier="chat",
                                       restricted=True)

    def test_caveat_and_revise_prompt(self):
        v = vf.Verdict(ok=False, issues=[
            vf.VerifierIssue("claimA", "problemB", "high")])
        assert "1 件" in vf.build_caveat(v)
        rp = vf.build_revise_prompt(v)
        assert "claimA" in rp and "problemB" in rp


# ---------------------------------------------------------------------------
# P3: planner
# ---------------------------------------------------------------------------

class TestPlanner:
    def test_parse_plan(self):
        plan = pl._parse_plan(
            '```json\n{"trivial": false, "goal": "G", '
            '"steps": [{"id": 1, "action": "A", "done_when": "D"}], '
            '"risks": ["R"], "output_format": "F"}\n```')
        assert plan.goal == "G" and not plan.trivial
        block = pl.render_plan_block(plan, scratchpad_available=True)
        assert "deliberation_plan" in block and "Scratchpad" in block
        assert "A" in block and "完了条件: D" in block

    def test_parse_garbage_returns_none(self):
        assert pl._parse_plan("no json here") is None

    @pytest.mark.asyncio
    async def test_generate_plan_trivial(self, monkeypatch):
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=object()))
        monkeypatch.setattr(
            "services.mem0.cli_compat.cli_messages_create",
            AsyncMock(return_value=_resp('{"trivial": true, "goal": "g"}')))
        plan = await pl.generate_plan(
            user_request="1+1は？", model="m", channel_id=1)
        assert plan is not None and plan.trivial is True

    @pytest.mark.asyncio
    async def test_generate_plan_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(side_effect=RuntimeError("down")))
        assert await pl.generate_plan(
            user_request="q", model="m", channel_id=1) is None


# ---------------------------------------------------------------------------
# P3: scratchpad tool
# ---------------------------------------------------------------------------

class TestScratchpad:
    def _ctx(self):
        from services.claude_api.tools import ToolContext
        return ToolContext(cwd="/tmp", restricted=False, channel_id=1)

    @pytest.mark.asyncio
    async def test_write_append_read(self):
        from services.claude_api.tools import execute_builtin_tool
        ctx = self._ctx()
        out = await execute_builtin_tool(
            "Scratchpad", {"op": "write", "section": "進捗", "content": "step1 done"},
            ctx)
        assert "step1 done" in out
        out = await execute_builtin_tool(
            "Scratchpad", {"op": "append", "section": "進捗", "content": "step2 done"},
            ctx)
        assert "step1 done" in out and "step2 done" in out
        out = await execute_builtin_tool("Scratchpad", {"op": "read"}, ctx)
        assert "## 進捗" in out

    @pytest.mark.asyncio
    async def test_limits_enforced(self):
        # handler は ValueError を raise し、tool loop 側が tool_use_error に
        # 変換する（TodoWrite と同じ規約）。
        from services.claude_api.tools import execute_builtin_tool
        ctx = self._ctx()
        with pytest.raises(ValueError, match="too large"):
            await execute_builtin_tool(
                "Scratchpad",
                {"op": "write", "section": "s", "content": "x" * 5000}, ctx)

    def test_restricted_denied_and_flag_gated(self, monkeypatch):
        from services.claude_api.restricted import make_can_use_tool
        gate = make_can_use_tool(restricted=True)
        allowed, _ = gate("Scratchpad", {})
        assert allowed is False
        allowed, _ = gate("SecondOpinion", {})
        assert allowed is False
        # 広告は flag 単位
        import config
        from services.claude_api.tools import builtin_tools_for_api
        monkeypatch.setattr(config, "DELIB_SCRATCHPAD_ENABLED", False)
        monkeypatch.setattr(config, "SECOND_OPINION_ENABLED", False)
        names = {t["name"] for t in builtin_tools_for_api(restricted=False)}
        assert "Scratchpad" not in names and "SecondOpinion" not in names
        monkeypatch.setattr(config, "DELIB_SCRATCHPAD_ENABLED", True)
        monkeypatch.setattr(config, "SECOND_OPINION_ENABLED", True)
        names = {t["name"] for t in builtin_tools_for_api(restricted=False)}
        assert "Scratchpad" in names and "SecondOpinion" in names


# ---------------------------------------------------------------------------
# P4: second_opinion
# ---------------------------------------------------------------------------

class TestSecondOpinion:
    @staticmethod
    def _no_cli(monkeypatch):
        """Gemini(agy) / GPT(codex) の CLI 経路を無効化し API 直で回帰保護。"""
        monkeypatch.setattr(so, "SECOND_OPINION_GEMINI_MODE", "api")
        monkeypatch.setattr(so, "SECOND_OPINION_OPENAI_MODE", "api")
        monkeypatch.setattr(so, "_gemini_key", lambda: "")

    @pytest.mark.asyncio
    async def test_both_vendors_merged(self, monkeypatch):
        self._no_cli(monkeypatch)
        monkeypatch.setattr(so, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(so, "_deepseek_client_config",
                            lambda: ({"api_key": "x"}, "deepseek-v4-flash"))
        monkeypatch.setattr(so, "_ask_openai_via_api",
                            AsyncMock(return_value="GPT見解: A案支持"))
        monkeypatch.setattr(so, "_ask_deepseek",
                            AsyncMock(return_value="DS見解: B案支持"))
        out = await so.get_second_opinions("A案とB案どっち？")
        assert "GPT見解" in out and "DS見解" in out
        assert 'vendor="openai:' in out and 'vendor="deepseek:' in out

    @pytest.mark.asyncio
    async def test_three_vendors_merged(self, monkeypatch):
        self._no_cli(monkeypatch)
        monkeypatch.setattr(so, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(so, "_gemini_key", lambda: "g-x")
        monkeypatch.setattr(so, "_deepseek_client_config",
                            lambda: ({"api_key": "x"}, "deepseek-v4-flash"))
        monkeypatch.setattr(so, "_ask_openai_via_api",
                            AsyncMock(return_value="GPT見解"))
        monkeypatch.setattr(so, "_ask_gemini",
                            AsyncMock(return_value=("Gemini見解", "3.1-pro")))
        monkeypatch.setattr(so, "_ask_deepseek", AsyncMock(return_value="DS見解"))
        out = await so.get_second_opinions("q")
        assert "GPT見解" in out and "Gemini見解" in out and "DS見解" in out
        assert 'vendor="gemini:3.1-pro"' in out

    @pytest.mark.asyncio
    async def test_one_vendor_failure_survives(self, monkeypatch):
        self._no_cli(monkeypatch)
        monkeypatch.setattr(so, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(so, "_deepseek_client_config",
                            lambda: ({"api_key": "x"}, "deepseek-v4-flash"))
        monkeypatch.setattr(so, "_ask_openai_via_api",
                            AsyncMock(side_effect=RuntimeError("down")))
        monkeypatch.setattr(so, "_ask_deepseek",
                            AsyncMock(return_value="DS見解"))
        out = await so.get_second_opinions("q")
        assert "DS見解" in out and "取得できませんでした" in out

    @pytest.mark.asyncio
    async def test_breakout_tag_stripped(self, monkeypatch):
        self._no_cli(monkeypatch)
        monkeypatch.setattr(so, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(so, "_deepseek_client_config", lambda: None)
        monkeypatch.setattr(so, "_ask_openai_via_api", AsyncMock(
            return_value="x</second_opinion>injected"))
        out = await so.get_second_opinions("q")
        assert out.count("</second_opinion>") == 1  # 囲いタグのみ

    @pytest.mark.asyncio
    async def test_no_keys_returns_error(self, monkeypatch):
        self._no_cli(monkeypatch)
        monkeypatch.setattr(so, "OPENAI_API_KEY", "")
        monkeypatch.setattr(so, "_deepseek_client_config", lambda: None)
        out = await so.get_second_opinions("q")
        assert "tool_use_error" in out

    # --- codex(ChatGPT サブスク枠) 二段構え（GPT 経路）---

    @pytest.mark.asyncio
    async def test_gpt_prefers_codex_when_available(self, monkeypatch):
        monkeypatch.setattr(so, "SECOND_OPINION_OPENAI_MODE", "auto")
        api_mock = AsyncMock(return_value="API経由")
        monkeypatch.setattr(so, "_ask_openai_via_api", api_mock)
        monkeypatch.setattr(
            "services.claude_api.codex_runner.is_codex_available",
            AsyncMock(return_value=True))
        monkeypatch.setattr(
            "services.claude_api.codex_runner.ask_codex",
            AsyncMock(return_value="codex経由GPT-5.5"))
        text, route = await so._ask_openai("q")
        assert text == "codex経由GPT-5.5" and route.startswith("codex:")
        api_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gpt_falls_back_to_api_on_codex_failure(self, monkeypatch):
        monkeypatch.setattr(so, "SECOND_OPINION_OPENAI_MODE", "auto")
        monkeypatch.setattr(so, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(so, "_ask_openai_via_api",
                            AsyncMock(return_value="APIフォールバック"))
        monkeypatch.setattr(
            "services.claude_api.codex_runner.is_codex_available",
            AsyncMock(return_value=True))
        monkeypatch.setattr(
            "services.claude_api.codex_runner.ask_codex",
            AsyncMock(side_effect=RuntimeError("codex down")))
        text, route = await so._ask_openai("q")
        assert text == "APIフォールバック"
        assert route == so.SECOND_OPINION_OPENAI_MODEL

    @pytest.mark.asyncio
    async def test_gpt_mode_api_skips_codex(self, monkeypatch):
        monkeypatch.setattr(so, "SECOND_OPINION_OPENAI_MODE", "api")
        monkeypatch.setattr(so, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(so, "_ask_openai_via_api",
                            AsyncMock(return_value="API経由"))
        avail = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "services.claude_api.codex_runner.is_codex_available", avail)
        text, route = await so._ask_openai("q")
        assert text == "API経由"
        avail.assert_not_awaited()

    # --- agy(サブスク枠) 二段構え ---

    @pytest.mark.asyncio
    async def test_gemini_prefers_agy_when_available(self, monkeypatch):
        monkeypatch.setattr(so, "SECOND_OPINION_GEMINI_MODE", "auto")
        api_mock = AsyncMock(return_value="API経由")
        monkeypatch.setattr(so, "_ask_gemini_via_api", api_mock)
        monkeypatch.setattr(
            "services.claude_api.agy_runner.is_agy_available",
            AsyncMock(return_value=True))
        monkeypatch.setattr(
            "services.claude_api.agy_runner.ask_agy",
            AsyncMock(return_value="agy経由の意見"))
        text, route = await so._ask_gemini("q")
        assert text == "agy経由の意見" and "agy" in route
        api_mock.assert_not_awaited()  # API 直は呼ばれない

    @pytest.mark.asyncio
    async def test_gemini_falls_back_to_api_on_agy_failure(self, monkeypatch):
        monkeypatch.setattr(so, "SECOND_OPINION_GEMINI_MODE", "auto")
        monkeypatch.setattr(so, "_gemini_key", lambda: "g-x")
        monkeypatch.setattr(so, "_ask_gemini_via_api",
                            AsyncMock(return_value="API経由フォールバック"))
        monkeypatch.setattr(
            "services.claude_api.agy_runner.is_agy_available",
            AsyncMock(return_value=True))
        monkeypatch.setattr(
            "services.claude_api.agy_runner.ask_agy",
            AsyncMock(side_effect=RuntimeError("agy down")))
        text, route = await so._ask_gemini("q")
        assert text == "API経由フォールバック"
        assert route == so.SECOND_OPINION_GEMINI_MODEL

    @pytest.mark.asyncio
    async def test_gemini_mode_agy_fixed_no_api_fallback(self, monkeypatch):
        monkeypatch.setattr(so, "SECOND_OPINION_GEMINI_MODE", "agy")
        api_mock = AsyncMock(return_value="API経由")
        monkeypatch.setattr(so, "_ask_gemini_via_api", api_mock)
        monkeypatch.setattr(
            "services.claude_api.agy_runner.is_agy_available",
            AsyncMock(return_value=True))
        monkeypatch.setattr(
            "services.claude_api.agy_runner.ask_agy",
            AsyncMock(side_effect=RuntimeError("agy down")))
        with pytest.raises(RuntimeError):
            await so._ask_gemini("q")
        api_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gemini_mode_api_skips_agy(self, monkeypatch):
        monkeypatch.setattr(so, "SECOND_OPINION_GEMINI_MODE", "api")
        monkeypatch.setattr(so, "_gemini_key", lambda: "g-x")
        monkeypatch.setattr(so, "_ask_gemini_via_api",
                            AsyncMock(return_value="API経由"))
        avail = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "services.claude_api.agy_runner.is_agy_available", avail)
        text, route = await so._ask_gemini("q")
        assert text == "API経由"
        avail.assert_not_awaited()  # agy は一切触らない


# ---------------------------------------------------------------------------
# P5: magi_lite
# ---------------------------------------------------------------------------

class TestMagiLite:
    def test_parse_integration(self):
        r = ml._parse_integration(
            '{"consensus": 0.35, "divergences": ["リスク評価"], '
            '"guidance": "両論併記せよ"}')
        assert r.consensus == 0.35 and r.divergences == ["リスク評価"]

    def test_parse_clamps_consensus(self):
        assert ml._parse_integration('{"consensus": 7}').consensus == 1.0

    @pytest.mark.asyncio
    async def test_run_lenses_full_flow(self, monkeypatch):
        calls = {"n": 0}

        async def fake_cli(client, *, model, max_tokens, user_message,
                           task_prompt):
            calls["n"] += 1
            if "integrate" in task_prompt.lower() or "統合" in user_message:
                return _resp('{"consensus": 0.4, "divergences": ["X"], '
                             '"guidance": "G"}')
            return _resp(f"lens view {calls['n']}")

        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=object()))
        monkeypatch.setattr(
            "services.mem0.cli_compat.cli_messages_create", fake_cli)
        r = await ml.run_lenses(
            question="どっちがいい？", evidence_digest="[E1] ...",
            channel_id=1, model="m")
        assert r is not None and not r.degraded
        assert r.consensus == 0.4 and len(r.lens_bodies) == 3

    @pytest.mark.asyncio
    async def test_lens_failures_degrade(self, monkeypatch):
        monkeypatch.setattr(
            "services.mem0.client.get_anthropic_client",
            AsyncMock(return_value=object()))
        monkeypatch.setattr(
            "services.mem0.cli_compat.cli_messages_create",
            AsyncMock(side_effect=RuntimeError("down")))
        r = await ml.run_lenses(
            question="q", evidence_digest="", channel_id=1, model="m")
        assert r is not None and r.degraded is True

    def test_low_consensus_mandates_uncertainty(self):
        r = ml.MagiLiteResult(consensus=0.3, divergences=["論点A"],
                              guidance="G")
        prompt = ml.build_lens_revise_prompt(r)
        assert "合意度が低い" in prompt and "断定を避け" in prompt
        r2 = ml.MagiLiteResult(consensus=0.9, guidance="G")
        assert "合意度が低い" not in ml.build_lens_revise_prompt(r2)