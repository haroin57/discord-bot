"""Tests for services.claude_api.tools — Read/Write/Edit/Bash/Glob/Grep."""

import asyncio
import os
import signal
import tempfile

import pytest

import services.claude_api.tools as tools_module
from services.claude_api.tools import (
    BASH_OUTPUT_CAP_BYTES,
    BUILTIN_TOOLS,
    READ_MAX_BYTES,
    READ_MAX_LINES,
    ToolContext,
    _resolve_under,
    builtin_tools_for_api,
    execute_builtin_tool,
)


class _BlockingBashStream:
    def __init__(self, chunks=()):
        self._chunks = list(chunks)

    async def read(self, _size):
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.Future()


class _FakeBashProcessGroup:
    def __init__(self, *, stdout=(), stderr=(), kill_exit_delay=0.0):
        self.proc = self._Process(stdout=stdout, stderr=stderr)
        self.spawned_pgid = self.proc.pid
        self.group_alive = True
        self.started = asyncio.Event()
        self.term_sent = asyncio.Event()
        self.kill_sent = asyncio.Event()
        self.signals = []
        self.probes = 0
        self.kill_exit_delay = kill_exit_delay

    class _Process:
        pid = 4242

        def __init__(self, *, stdout, stderr):
            self.stdout = _BlockingBashStream(stdout)
            self.stderr = _BlockingBashStream(stderr)
            self.returncode = None
            self.exited = asyncio.Event()
            self.wait_calls = 0

        async def wait(self):
            self.wait_calls += 1
            await self.exited.wait()
            return self.returncode

    async def create_subprocess_exec(self, *_args, **kwargs):
        assert kwargs["start_new_session"] is True
        self.started.set()
        return self.proc

    def killpg(self, pgid, sig):
        assert pgid == self.spawned_pgid
        if sig == 0:
            self.probes += 1
            if not self.group_alive:
                raise ProcessLookupError
            return

        self.signals.append((pgid, sig))
        if sig == signal.SIGTERM:
            # The leader exits cooperatively, but a descendant deliberately
            # remains in the same process group until SIGKILL arrives.
            self.proc.returncode = -signal.SIGTERM
            self.proc.exited.set()
            self.term_sent.set()
        elif sig == signal.SIGKILL:
            self.kill_sent.set()
            if self.kill_exit_delay:
                asyncio.get_running_loop().call_later(
                    self.kill_exit_delay,
                    setattr,
                    self,
                    "group_alive",
                    False,
                )
            else:
                self.group_alive = False


def _install_fake_bash(
    monkeypatch,
    *,
    stdout=(),
    stderr=(),
    grace=0.01,
    kill_grace=0.05,
    kill_exit_delay=0.0,
):
    group = _FakeBashProcessGroup(
        stdout=stdout,
        stderr=stderr,
        kill_exit_delay=kill_exit_delay,
    )
    monkeypatch.setattr(
        tools_module.asyncio,
        "create_subprocess_exec",
        group.create_subprocess_exec,
    )
    monkeypatch.setattr(tools_module.os, "killpg", group.killpg)
    monkeypatch.setattr(tools_module, "_BASH_CANCEL_GRACE_S", grace)
    monkeypatch.setattr(tools_module, "_BASH_KILL_GRACE_S", kill_grace)
    monkeypatch.setattr(tools_module, "_BASH_GROUP_POLL_S", 0.001)
    return group


def _active_bash_cleanup_tasks():
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("claude-bash-")
    ]


@pytest.fixture
def tmpdir_ctx():
    with tempfile.TemporaryDirectory() as tmp:
        yield ToolContext(cwd=tmp, restricted=False, channel_id=1)


@pytest.fixture
def tmpdir_ctx_restricted():
    # restricted=True is what every non-admin caller gets in production
    # (entry.py derives `restricted` from is_admin). The cwd jail is a
    # restricted-mode guard, so cwd-escape rejection must be tested here,
    # not via the admin (restricted=False) fixture which drops the jail by
    # design (e.g. an admin reading ~/manga).
    with tempfile.TemporaryDirectory() as tmp:
        yield ToolContext(cwd=tmp, restricted=True, channel_id=1)


class TestResolveUnder:
    def test_relative_ok(self, tmpdir_ctx):
        out = _resolve_under(tmpdir_ctx.cwd, "a/b.txt")
        assert out.startswith(os.path.realpath(tmpdir_ctx.cwd))

    def test_absolute_within(self, tmpdir_ctx):
        target = os.path.join(tmpdir_ctx.cwd, "x.txt")
        out = _resolve_under(tmpdir_ctx.cwd, target)
        assert out.endswith("x.txt")

    def test_escape_rejected(self, tmpdir_ctx):
        with pytest.raises(ValueError, match="escapes cwd"):
            _resolve_under(tmpdir_ctx.cwd, "../escape.txt")

    def test_absolute_outside_rejected(self, tmpdir_ctx):
        with pytest.raises(ValueError, match="escapes cwd"):
            _resolve_under(tmpdir_ctx.cwd, "/etc/passwd")

    def test_symlink_escape_rejected(self, tmpdir_ctx):
        link = os.path.join(tmpdir_ctx.cwd, "link")
        os.symlink("/etc", link)
        with pytest.raises(ValueError):
            _resolve_under(tmpdir_ctx.cwd, "link/passwd")


class TestRead:
    @pytest.mark.asyncio
    async def test_read_simple(self, tmpdir_ctx):
        p = os.path.join(tmpdir_ctx.cwd, "a.txt")
        with open(p, "w") as fh:
            fh.write("hello\nworld\n")
        out = await execute_builtin_tool("Read", {"file_path": "a.txt"}, tmpdir_ctx)
        assert "hello" in out and "world" in out

    @pytest.mark.asyncio
    async def test_read_missing_file_path(self, tmpdir_ctx):
        with pytest.raises(ValueError):
            await execute_builtin_tool("Read", {}, tmpdir_ctx)

    @pytest.mark.asyncio
    async def test_read_too_large(self, tmpdir_ctx):
        p = os.path.join(tmpdir_ctx.cwd, "big.txt")
        with open(p, "wb") as fh:
            fh.write(b"x" * (READ_MAX_BYTES + 1))
        with pytest.raises(ValueError, match="too large"):
            await execute_builtin_tool("Read", {"file_path": "big.txt"}, tmpdir_ctx)

    @pytest.mark.asyncio
    async def test_read_truncates_long_files(self, tmpdir_ctx):
        p = os.path.join(tmpdir_ctx.cwd, "long.txt")
        with open(p, "w") as fh:
            fh.write("\n".join(f"line{i}" for i in range(READ_MAX_LINES + 50)))
        out = await execute_builtin_tool("Read", {"file_path": "long.txt"}, tmpdir_ctx)
        assert "[showing lines 1-2000 of 2050; continue with offset=2000]" in out

    @pytest.mark.asyncio
    async def test_read_rejects_escape_via_symlink(self, tmpdir_ctx_restricted):
        # A symlink whose realpath escapes cwd is rejected at the path
        # gate (before the file is opened). O_NOFOLLOW alone wouldn't be
        # enough — it only protects the final path component.
        # Uses the restricted (non-admin) context, where the cwd jail applies.
        link = os.path.join(tmpdir_ctx_restricted.cwd, "escape")
        os.symlink("/etc", link)
        with pytest.raises(ValueError, match="escapes"):
            await execute_builtin_tool(
                "Read",
                {"file_path": "escape/passwd"},
                tmpdir_ctx_restricted,
            )


class TestWrite:
    @pytest.mark.asyncio
    async def test_write_creates_file(self, tmpdir_ctx):
        result = await execute_builtin_tool(
            "Write",
            {"file_path": "new.txt", "content": "abc"},
            tmpdir_ctx,
        )
        assert "wrote 3 bytes" in result
        with open(os.path.join(tmpdir_ctx.cwd, "new.txt")) as fh:
            assert fh.read() == "abc"

    @pytest.mark.asyncio
    async def test_write_outside_cwd_rejected(self, tmpdir_ctx_restricted):
        # Restricted (non-admin) context: writes outside the cwd jail are rejected.
        with pytest.raises(ValueError):
            await execute_builtin_tool(
                "Write",
                {"file_path": "/tmp/evil.txt", "content": "x"},
                tmpdir_ctx_restricted,
            )

    @pytest.mark.asyncio
    async def test_write_permission_0600(self, tmpdir_ctx):
        await execute_builtin_tool(
            "Write",
            {"file_path": "p.txt", "content": "x"},
            tmpdir_ctx,
        )
        mode = os.stat(os.path.join(tmpdir_ctx.cwd, "p.txt")).st_mode & 0o777
        assert mode == 0o600


class TestEdit:
    @pytest.mark.asyncio
    async def test_edit_unique_replace(self, tmpdir_ctx):
        p = os.path.join(tmpdir_ctx.cwd, "f.txt")
        with open(p, "w") as fh:
            fh.write("foo bar baz")
        await execute_builtin_tool("Read", {"file_path": "f.txt"}, tmpdir_ctx)
        await execute_builtin_tool(
            "Edit",
            {"file_path": "f.txt", "old_string": "bar", "new_string": "BAR"},
            tmpdir_ctx,
        )
        with open(p) as fh:
            assert fh.read() == "foo BAR baz"

    @pytest.mark.asyncio
    async def test_edit_ambiguous_fails(self, tmpdir_ctx):
        p = os.path.join(tmpdir_ctx.cwd, "f.txt")
        with open(p, "w") as fh:
            fh.write("xx xx xx")
        await execute_builtin_tool("Read", {"file_path": "f.txt"}, tmpdir_ctx)
        with pytest.raises(ValueError, match="occurs"):
            await execute_builtin_tool(
                "Edit",
                {"file_path": "f.txt", "old_string": "xx", "new_string": "y"},
                tmpdir_ctx,
            )

    @pytest.mark.asyncio
    async def test_edit_replace_all(self, tmpdir_ctx):
        p = os.path.join(tmpdir_ctx.cwd, "f.txt")
        with open(p, "w") as fh:
            fh.write("a a a")
        await execute_builtin_tool("Read", {"file_path": "f.txt"}, tmpdir_ctx)
        await execute_builtin_tool(
            "Edit",
            {
                "file_path": "f.txt",
                "old_string": "a",
                "new_string": "b",
                "replace_all": True,
            },
            tmpdir_ctx,
        )
        with open(p) as fh:
            assert fh.read() == "b b b"

    @pytest.mark.asyncio
    async def test_edit_not_found(self, tmpdir_ctx):
        p = os.path.join(tmpdir_ctx.cwd, "f.txt")
        with open(p, "w") as fh:
            fh.write("hello")
        await execute_builtin_tool("Read", {"file_path": "f.txt"}, tmpdir_ctx)
        with pytest.raises(ValueError, match="not found"):
            await execute_builtin_tool(
                "Edit",
                {"file_path": "f.txt", "old_string": "missing", "new_string": "x"},
                tmpdir_ctx,
            )


class TestBash:
    @pytest.mark.asyncio
    async def test_bash_runs_command(self, tmpdir_ctx):
        out = await execute_builtin_tool(
            "Bash",
            {"command": "echo hi"},
            tmpdir_ctx,
        )
        assert "hi" in out
        assert "exit code: 0" in out

    @pytest.mark.asyncio
    async def test_bash_pipe_works(self, tmpdir_ctx):
        # Shell feature — pipe must work via /bin/bash -c
        out = await execute_builtin_tool(
            "Bash",
            {"command": "printf 'a\\nb\\n' | wc -l"},
            tmpdir_ctx,
        )
        assert "2" in out

    @pytest.mark.asyncio
    async def test_bash_env_filtered(self, tmpdir_ctx, monkeypatch):
        monkeypatch.setenv("SECRET_THING", "leakme")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        out = await execute_builtin_tool(
            "Bash",
            {"command": 'echo "$SECRET_THING"'},
            tmpdir_ctx,
        )
        # Should be empty since SECRET_THING is not on allowlist
        assert "leakme" not in out

    @pytest.mark.asyncio
    async def test_bash_timeout(self, tmpdir_ctx):
        # Timeout no longer raises (audit #10): the buffered partial output is
        # returned with a timed-out marker so npm test / pip install runs keep
        # their diagnostics.
        out = await execute_builtin_tool(
            "Bash",
            {"command": "echo partial; sleep 5", "timeout": 1},
            tmpdir_ctx,
        )
        assert "partial" in out
        assert "timed out after" in out
        assert "exit code" not in out

    @pytest.mark.asyncio
    async def test_bash_outer_timeout_terminates_and_reaps_process_group(
        self,
        tmpdir_ctx,
        monkeypatch,
    ):
        group = _install_fake_bash(monkeypatch)

        call = asyncio.create_task(
            execute_builtin_tool(
                "Bash",
                {"command": "sleep forever"},
                tmpdir_ctx,
            ),
        )
        await group.started.wait()
        # The cleanup path must keep using the PGID captured at spawn, even if
        # the mutable process object changes later.
        group.proc.pid = 9999

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(call, timeout=0.01)

        assert group.signals == [
            (group.spawned_pgid, signal.SIGTERM),
            (group.spawned_pgid, signal.SIGKILL),
        ]
        assert group.probes > 0
        assert group.proc.wait_calls == 1
        assert not group.group_alive
        assert not _active_bash_cleanup_tasks()

    @pytest.mark.asyncio
    async def test_bash_double_cancel_waits_for_cleanup_and_reraises_first(
        self,
        tmpdir_ctx,
        monkeypatch,
    ):
        group = _install_fake_bash(monkeypatch, grace=0.02)
        call = asyncio.create_task(
            execute_builtin_tool(
                "Bash",
                {"command": "sleep forever"},
                tmpdir_ctx,
            ),
        )
        await group.started.wait()

        call.cancel("first cancellation")
        await group.term_sent.wait()
        call.cancel("second cancellation")

        with pytest.raises(asyncio.CancelledError) as caught:
            await call

        assert caught.value.args == ("first cancellation",)
        assert group.kill_sent.is_set()
        assert group.proc.wait_calls == 1
        assert not group.group_alive
        assert not _active_bash_cleanup_tasks()

    @pytest.mark.asyncio
    async def test_bash_internal_timeout_uses_group_cleanup_and_keeps_output(
        self,
        tmpdir_ctx,
        monkeypatch,
    ):
        group = _install_fake_bash(
            monkeypatch,
            stdout=(b"partial stdout\n",),
            stderr=(b"partial stderr\n",),
        )

        out = await execute_builtin_tool(
            "Bash",
            {"command": "sleep forever", "timeout": 1},
            tmpdir_ctx,
        )

        assert group.signals == [
            (group.spawned_pgid, signal.SIGTERM),
            (group.spawned_pgid, signal.SIGKILL),
        ]
        assert group.proc.wait_calls == 1
        assert not group.group_alive
        assert not _active_bash_cleanup_tasks()
        assert "partial stdout" in out
        assert "--- stderr ---\npartial stderr" in out
        assert "timed out after 1s" in out
        assert "exit code" not in out

    @pytest.mark.asyncio
    async def test_bash_waits_for_group_disappearance_after_sigkill(
        self,
        monkeypatch,
    ):
        group = _install_fake_bash(
            monkeypatch,
            grace=0.001,
            kill_grace=0.1,
            kill_exit_delay=0.02,
        )

        await tools_module._terminate_bash_process_group(
            group.proc,
            group.spawned_pgid,
        )

        assert group.kill_sent.is_set()
        assert not group.group_alive
        assert group.probes > 1
        assert group.proc.wait_calls == 1
        assert not _active_bash_cleanup_tasks()

    @pytest.mark.asyncio
    async def test_bash_output_capped(self, tmpdir_ctx):
        # Generate huge output
        out = await execute_builtin_tool(
            "Bash",
            {"command": f"yes x | head -c {BASH_OUTPUT_CAP_BYTES * 2}"},
            tmpdir_ctx,
        )
        # Output portion alone must not exceed the cap (the trailing
        # "exit code" line is appended separately).
        stdout_section = out.split("--- stderr ---")[0].rsplit("--- exit", 1)[0]
        # Middle-cut keeps head+tail halves plus a "[N bytes truncated]"
        # marker line between them (audit #13).
        assert len(stdout_section) <= BASH_OUTPUT_CAP_BYTES + 64
        assert "bytes truncated" in out


class TestGlob:
    @pytest.mark.asyncio
    async def test_glob_finds_files(self, tmpdir_ctx):
        os.makedirs(os.path.join(tmpdir_ctx.cwd, "sub"))
        with open(os.path.join(tmpdir_ctx.cwd, "sub", "a.py"), "w") as fh:
            fh.write("")
        out = await execute_builtin_tool(
            "Glob",
            {"pattern": "**/*.py"},
            tmpdir_ctx,
        )
        assert "a.py" in out

    @pytest.mark.asyncio
    async def test_glob_no_match(self, tmpdir_ctx):
        out = await execute_builtin_tool(
            "Glob",
            {"pattern": "*.nope"},
            tmpdir_ctx,
        )
        assert "no matches" in out


class TestGrep:
    @pytest.mark.asyncio
    async def test_grep_finds_pattern(self, tmpdir_ctx):
        p = os.path.join(tmpdir_ctx.cwd, "f.py")
        with open(p, "w") as fh:
            fh.write("def needle():\n    pass\n")
        out = await execute_builtin_tool(
            "Grep",
            {"pattern": "needle", "output_mode": "content"},
            tmpdir_ctx,
        )
        assert "needle" in out and ":1:" in out

    @pytest.mark.asyncio
    async def test_grep_no_match(self, tmpdir_ctx):
        with open(os.path.join(tmpdir_ctx.cwd, "f.py"), "w") as fh:
            fh.write("x = 1")
        out = await execute_builtin_tool(
            "Grep",
            {"pattern": "missing"},
            tmpdir_ctx,
        )
        assert "no matches" in out


class TestRegistry:
    def test_admin_advertises_all(self, monkeypatch):
        # 熟考 P3/P4 tools はフラグ単位で広告される。env に依存しないよう固定。
        import config

        monkeypatch.setattr(config, "DELIB_SCRATCHPAD_ENABLED", False)
        monkeypatch.setattr(config, "SECOND_OPINION_ENABLED", False)
        out = builtin_tools_for_api(restricted=False)
        names = {t["name"] for t in out}
        flag_gated = {"Scratchpad", "SecondOpinion"}
        assert names == set(BUILTIN_TOOLS.keys()) - flag_gated
        monkeypatch.setattr(config, "SECOND_OPINION_ENABLED", True)
        names_on = {t["name"] for t in builtin_tools_for_api(restricted=False)}
        assert "SecondOpinion" in names_on

    def test_restricted_advertises_none(self):
        assert builtin_tools_for_api(restricted=True) == []

    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self, tmpdir_ctx):
        with pytest.raises(KeyError):
            await execute_builtin_tool("Nope", {}, tmpdir_ctx)

    def test_todo_write_advertised(self):
        out = builtin_tools_for_api(restricted=False)
        assert "TodoWrite" in {t["name"] for t in out}

    def test_task_create_alias_resolves(self):
        from services.claude_api.tools import BUILTIN_TOOL_ALIASES

        assert BUILTIN_TOOL_ALIASES["TaskCreate"] == "TodoWrite"
        assert BUILTIN_TOOL_ALIASES["task_create"] == "TodoWrite"
        assert BUILTIN_TOOL_ALIASES["todo_write"] == "TodoWrite"


class TestTodoWrite:
    @pytest.mark.asyncio
    async def test_replace_semantics(self, tmpdir_ctx):
        todos1 = [
            {"content": "step A", "status": "in_progress", "activeForm": "Doing A"},
            {"content": "step B", "status": "pending", "activeForm": "Doing B"},
        ]
        out1 = await execute_builtin_tool("TodoWrite", {"todos": todos1}, tmpdir_ctx)
        assert "2 todos" in out1
        assert len(tmpdir_ctx.todos) == 2

        # Replace with a smaller list — old items should be gone, not merged.
        todos2 = [
            {"content": "step C", "status": "pending", "activeForm": "Doing C"},
        ]
        await execute_builtin_tool("TodoWrite", {"todos": todos2}, tmpdir_ctx)
        assert len(tmpdir_ctx.todos) == 1
        assert tmpdir_ctx.todos[0]["content"] == "step C"

    @pytest.mark.asyncio
    async def test_empty_clears(self, tmpdir_ctx):
        tmpdir_ctx.todos = [
            {"content": "x", "status": "pending", "activeForm": "X"},
        ]
        out = await execute_builtin_tool("TodoWrite", {"todos": []}, tmpdir_ctx)
        assert "cleared" in out
        assert tmpdir_ctx.todos == []

    @pytest.mark.asyncio
    async def test_rejects_invalid_status(self, tmpdir_ctx):
        with pytest.raises(ValueError, match="status"):
            await execute_builtin_tool(
                "TodoWrite",
                {"todos": [{"content": "x", "status": "done", "activeForm": "X"}]},
                tmpdir_ctx,
            )

    @pytest.mark.asyncio
    async def test_rejects_empty_content(self, tmpdir_ctx):
        with pytest.raises(ValueError, match="content"):
            await execute_builtin_tool(
                "TodoWrite",
                {"todos": [{"content": "", "status": "pending", "activeForm": "X"}]},
                tmpdir_ctx,
            )

    @pytest.mark.asyncio
    async def test_rejects_multiple_in_progress(self, tmpdir_ctx):
        with pytest.raises(ValueError, match="in_progress"):
            await execute_builtin_tool(
                "TodoWrite",
                {
                    "todos": [
                        {"content": "a", "status": "in_progress", "activeForm": "A"},
                        {"content": "b", "status": "in_progress", "activeForm": "B"},
                    ]
                },
                tmpdir_ctx,
            )

    @pytest.mark.asyncio
    async def test_rejects_non_list(self, tmpdir_ctx):
        with pytest.raises(ValueError, match="array"):
            await execute_builtin_tool("TodoWrite", {"todos": "nope"}, tmpdir_ctx)

    @pytest.mark.asyncio
    async def test_alias_TaskCreate_routes_to_TodoWrite(self, tmpdir_ctx):
        # Mis-named call from a model that learned the old TaskCreate name
        # from the previous CLI default prompt must still dispatch to
        # TodoWrite via BUILTIN_TOOL_ALIASES.
        out = await execute_builtin_tool(
            "TaskCreate",
            {"todos": [{"content": "x", "status": "pending", "activeForm": "X"}]},
            tmpdir_ctx,
        )
        assert "1 todos" in out
        assert len(tmpdir_ctx.todos) == 1
