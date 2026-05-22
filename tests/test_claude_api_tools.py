"""Tests for services.claude_api.tools — Read/Write/Edit/Bash/Glob/Grep."""

import os
import tempfile

import pytest

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


@pytest.fixture
def tmpdir_ctx():
    with tempfile.TemporaryDirectory() as tmp:
        yield ToolContext(cwd=tmp, restricted=False, channel_id=1)


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
        assert "[truncated" in out

    @pytest.mark.asyncio
    async def test_read_rejects_escape_via_symlink(self, tmpdir_ctx):
        # A symlink whose realpath escapes cwd is rejected at the path
        # gate (before the file is opened). O_NOFOLLOW alone wouldn't be
        # enough — it only protects the final path component.
        link = os.path.join(tmpdir_ctx.cwd, "escape")
        os.symlink("/etc", link)
        with pytest.raises(ValueError, match="escapes"):
            await execute_builtin_tool(
                "Read", {"file_path": "escape/passwd"}, tmpdir_ctx,
            )


class TestWrite:
    @pytest.mark.asyncio
    async def test_write_creates_file(self, tmpdir_ctx):
        result = await execute_builtin_tool(
            "Write", {"file_path": "new.txt", "content": "abc"}, tmpdir_ctx,
        )
        assert "wrote 3 bytes" in result
        with open(os.path.join(tmpdir_ctx.cwd, "new.txt")) as fh:
            assert fh.read() == "abc"

    @pytest.mark.asyncio
    async def test_write_outside_cwd_rejected(self, tmpdir_ctx):
        with pytest.raises(ValueError):
            await execute_builtin_tool(
                "Write", {"file_path": "/tmp/evil.txt", "content": "x"}, tmpdir_ctx,
            )

    @pytest.mark.asyncio
    async def test_write_permission_0600(self, tmpdir_ctx):
        await execute_builtin_tool(
            "Write", {"file_path": "p.txt", "content": "x"}, tmpdir_ctx,
        )
        mode = os.stat(os.path.join(tmpdir_ctx.cwd, "p.txt")).st_mode & 0o777
        assert mode == 0o600


class TestEdit:
    @pytest.mark.asyncio
    async def test_edit_unique_replace(self, tmpdir_ctx):
        p = os.path.join(tmpdir_ctx.cwd, "f.txt")
        with open(p, "w") as fh:
            fh.write("foo bar baz")
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
        await execute_builtin_tool(
            "Edit",
            {"file_path": "f.txt", "old_string": "a", "new_string": "b", "replace_all": True},
            tmpdir_ctx,
        )
        with open(p) as fh:
            assert fh.read() == "b b b"

    @pytest.mark.asyncio
    async def test_edit_not_found(self, tmpdir_ctx):
        p = os.path.join(tmpdir_ctx.cwd, "f.txt")
        with open(p, "w") as fh:
            fh.write("hello")
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
            "Bash", {"command": "echo hi"}, tmpdir_ctx,
        )
        assert "hi" in out
        assert "exit code: 0" in out

    @pytest.mark.asyncio
    async def test_bash_pipe_works(self, tmpdir_ctx):
        # Shell feature — pipe must work via /bin/bash -c
        out = await execute_builtin_tool(
            "Bash", {"command": "printf 'a\\nb\\n' | wc -l"}, tmpdir_ctx,
        )
        assert "2" in out

    @pytest.mark.asyncio
    async def test_bash_env_filtered(self, tmpdir_ctx, monkeypatch):
        monkeypatch.setenv("SECRET_THING", "leakme")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        out = await execute_builtin_tool(
            "Bash", {"command": "echo \"$SECRET_THING\""}, tmpdir_ctx,
        )
        # Should be empty since SECRET_THING is not on allowlist
        assert "leakme" not in out

    @pytest.mark.asyncio
    async def test_bash_timeout(self, tmpdir_ctx):
        with pytest.raises(TimeoutError):
            await execute_builtin_tool(
                "Bash", {"command": "sleep 5", "timeout": 0.2}, tmpdir_ctx,
            )

    @pytest.mark.asyncio
    async def test_bash_output_capped(self, tmpdir_ctx):
        # Generate huge output
        out = await execute_builtin_tool(
            "Bash", {"command": f"yes x | head -c {BASH_OUTPUT_CAP_BYTES * 2}"},
            tmpdir_ctx,
        )
        # Output portion alone must not exceed the cap (the trailing
        # "exit code" line is appended separately).
        stdout_section = out.split("--- stderr ---")[0].rsplit("--- exit", 1)[0]
        assert len(stdout_section) <= BASH_OUTPUT_CAP_BYTES + 16


class TestGlob:
    @pytest.mark.asyncio
    async def test_glob_finds_files(self, tmpdir_ctx):
        os.makedirs(os.path.join(tmpdir_ctx.cwd, "sub"))
        with open(os.path.join(tmpdir_ctx.cwd, "sub", "a.py"), "w") as fh:
            fh.write("")
        out = await execute_builtin_tool(
            "Glob", {"pattern": "**/*.py"}, tmpdir_ctx,
        )
        assert "a.py" in out

    @pytest.mark.asyncio
    async def test_glob_no_match(self, tmpdir_ctx):
        out = await execute_builtin_tool(
            "Glob", {"pattern": "*.nope"}, tmpdir_ctx,
        )
        assert "no matches" in out


class TestGrep:
    @pytest.mark.asyncio
    async def test_grep_finds_pattern(self, tmpdir_ctx):
        p = os.path.join(tmpdir_ctx.cwd, "f.py")
        with open(p, "w") as fh:
            fh.write("def needle():\n    pass\n")
        out = await execute_builtin_tool(
            "Grep", {"pattern": "needle"}, tmpdir_ctx,
        )
        assert "needle" in out and ":1:" in out

    @pytest.mark.asyncio
    async def test_grep_no_match(self, tmpdir_ctx):
        with open(os.path.join(tmpdir_ctx.cwd, "f.py"), "w") as fh:
            fh.write("x = 1")
        out = await execute_builtin_tool(
            "Grep", {"pattern": "missing"}, tmpdir_ctx,
        )
        assert "no matches" in out


class TestRegistry:
    def test_admin_advertises_all(self):
        out = builtin_tools_for_api(restricted=False)
        names = {t["name"] for t in out}
        assert names == set(BUILTIN_TOOLS.keys())

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
                {"todos": [
                    {"content": "a", "status": "in_progress", "activeForm": "A"},
                    {"content": "b", "status": "in_progress", "activeForm": "B"},
                ]},
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
