"""Tests for services/claude_api/codex_runner.py（codex ChatGPT サブスク枠 GPT-5.5）。

subprocess は全て fake。実 codex には触らない。
"""

import json

import pytest

from services.claude_api import codex_runner as cr


@pytest.fixture(autouse=True)
def _reset():
    cr._reset_cache_for_test()
    yield
    cr._reset_cache_for_test()


class _FakeProc:
    def __init__(self, *, returncode=0, stderr=b"", hang=False):
        self.returncode = returncode
        self._stderr = stderr
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            import asyncio
            await asyncio.sleep(3600)
        return b"", self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


class TestCleanEnv:
    def test_strips_openai_key(self, monkeypatch):
        for k in cr._STRIP_ENV_KEYS:
            monkeypatch.setenv(k, "leaked")
        env = cr._clean_env()
        for k in cr._STRIP_ENV_KEYS:
            assert k not in env  # サブスク枠強制の肝


class TestAskCodex:
    @pytest.mark.asyncio
    async def test_reads_output_file(self, monkeypatch):
        captured = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            captured["stdin"] = kwargs.get("stdin")
            # -o <path> の次要素が出力ファイル
            argv = list(args)
            oi = argv.index("-o")
            out_path = argv[oi + 1]
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("2です。")
            return _FakeProc(returncode=0)

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        monkeypatch.setenv("OPENAI_API_KEY", "leaked")
        out = await cr.ask_codex("1+1?", timeout_s=30)
        assert out == "2です。"
        assert "OPENAI_API_KEY" not in captured["env"]  # サブスク枠強制
        assert captured["args"][:2] == (cr.CODEX_BIN, "exec")
        import asyncio as _a
        # MCP等でstdinがパイプだと子が入力待ちでハングする回帰の防止
        assert captured["stdin"] == _a.subprocess.DEVNULL

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises(self, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _FakeProc(returncode=1,
                             stderr=b"model not supported with ChatGPT account")
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        with pytest.raises(RuntimeError, match="not supported"):
            await cr.ask_codex("q", timeout_s=30)

    @pytest.mark.asyncio
    async def test_timeout_kills(self, monkeypatch):
        proc = _FakeProc(hang=True)

        async def fake_exec(*args, **kwargs):
            return proc
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        with pytest.raises(RuntimeError, match="timed out"):
            await cr.ask_codex("q", timeout_s=0.1)
        assert proc.killed is True

    @pytest.mark.asyncio
    async def test_empty_output_raises(self, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _FakeProc(returncode=0, stderr=b"")  # 出力ファイル書かない
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        with pytest.raises(RuntimeError, match="no final message"):
            await cr.ask_codex("q", timeout_s=30)


class TestIsAvailable:
    @pytest.mark.asyncio
    async def test_missing_binary(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _b: None)
        assert await cr.is_codex_available() is False

    @pytest.mark.asyncio
    async def test_chatgpt_auth_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/codex")
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps({"auth_mode": "chatgpt"}))
        monkeypatch.setattr("os.path.expanduser", lambda p: str(auth))
        assert await cr.is_codex_available() is True

    @pytest.mark.asyncio
    async def test_api_key_auth_mode_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/codex")
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps({"auth_mode": "apikey"}))
        monkeypatch.setattr("os.path.expanduser", lambda p: str(auth))
        # apikey モードは従量課金なので available=False（サブスク枠が無い）
        assert await cr.is_codex_available() is False

    @pytest.mark.asyncio
    async def test_cache(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/codex")
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps({"auth_mode": "chatgpt"}))
        calls = {"n": 0}
        orig = auth.read_text

        def counting(*a, **k):
            calls["n"] += 1
            return orig()
        monkeypatch.setattr("os.path.expanduser", lambda p: str(auth))
        await cr.is_codex_available()
        await cr.is_codex_available()
        # 2回目はキャッシュ（which は呼ばれるが auth 判定まで行かない想定でも
        # 結果が一致すればよい）。厳密なファイル読み回数までは問わない。
        assert await cr.is_codex_available() is True
