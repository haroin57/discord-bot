"""Unit tests for services.vscode_sync.parser."""

from services.vscode_sync.parser import parse_event


def _vscode(**kwargs):
    base = {
        "entrypoint": "claude-vscode",
        "sessionId": "abc12345-0000-0000-0000-000000000000",
        "cwd": "/home/ubuntu/discord-bot",
        "gitBranch": "master",
        "timestamp": "2026-05-13T12:00:00.000Z",
    }
    base.update(kwargs)
    return base


def test_non_vscode_entrypoint_dropped():
    raw = _vscode(entrypoint="claude-cli", type="user",
                  message={"role": "user", "content": "hi"})
    assert parse_event(raw) == []


def test_no_entrypoint_dropped():
    raw = {"type": "user", "sessionId": "s", "message": {"content": "hi"}}
    assert parse_event(raw) == []


def test_missing_session_id_dropped():
    raw = _vscode(sessionId="", type="user", message={"content": "hi"})
    assert parse_event(raw) == []


def test_housekeeping_types_dropped():
    for t in ["queue-operation", "ai-title", "last-prompt", "attachment",
              "file-history-snapshot", "system", "summary"]:
        raw = _vscode(type=t)
        assert parse_event(raw) == [], f"event type {t!r} should be dropped"


def test_user_prompt_string_content():
    raw = _vscode(type="user", message={"role": "user", "content": "fix the bug"})
    events = parse_event(raw)
    assert len(events) == 1
    assert events[0].kind == "user_prompt"
    assert events[0].payload["text"] == "fix the bug"
    assert events[0].cwd == "/home/ubuntu/discord-bot"
    assert events[0].git_branch == "master"


def test_user_prompt_long_text_truncated():
    long = "x" * 1000
    events = parse_event(_vscode(type="user", message={"content": long}))
    assert len(events) == 1
    assert events[0].payload["text"].endswith("...")
    assert len(events[0].payload["text"]) <= 500


def test_user_prompt_empty_ignored():
    assert parse_event(_vscode(type="user", message={"content": "   "})) == []


def test_user_prompt_redacts_webhook():
    raw = _vscode(type="user", message={
        "content": "check https://discord.com/api/webhooks/123/abc",
    })
    events = parse_event(raw)
    assert "<webhook>" in events[0].payload["text"]


def test_tool_result_error_emitted():
    raw = _vscode(type="user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": "ENOENT: no such file", "is_error": True},
    ]})
    events = parse_event(raw)
    assert len(events) == 1
    assert events[0].kind == "tool_error"
    assert "ENOENT" in events[0].payload["text"]


def test_tool_result_success_dropped():
    raw = _vscode(type="user", message={"content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": "OK", "is_error": False},
    ]})
    assert parse_event(raw) == []


def test_tool_result_list_content():
    raw = _vscode(type="user", message={"content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": "boom"}], "is_error": True},
    ]})
    events = parse_event(raw)
    assert events[0].kind == "tool_error"
    assert "boom" in events[0].payload["text"]


def test_assistant_thinking_is_noise():
    raw = _vscode(type="assistant", message={
        "role": "assistant",
        "stop_reason": "tool_use",
        "content": [{"type": "thinking", "thinking": "..."}],
    })
    events = parse_event(raw)
    assert len(events) == 1
    assert events[0].kind == "noise"
    assert events[0].payload["label"] == "Thinking"


def test_assistant_bash_tool_use():
    raw = _vscode(type="assistant", message={
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "t", "name": "Bash",
                     "input": {"command": "ls -la"}}],
    })
    events = parse_event(raw)
    assert len(events) == 1
    assert events[0].kind == "tool_call"
    assert events[0].payload["name"] == "Bash"
    assert "ls -la" in events[0].payload["summary"]


def test_assistant_read_tool_use_is_noise():
    raw = _vscode(type="assistant", message={
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "t", "name": "Read",
                     "input": {"file_path": "/tmp/x"}}],
    })
    events = parse_event(raw)
    assert events[0].kind == "noise"
    assert events[0].payload["label"] == "Read"


def test_assistant_write_shows_path():
    raw = _vscode(type="assistant", message={
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "Write",
                     "input": {"file_path": "/tmp/foo.py", "content": "secret"}}],
    })
    events = parse_event(raw)
    assert events[0].kind == "tool_call"
    assert events[0].payload["summary"] == "/tmp/foo.py"
    assert "secret" not in events[0].payload["summary"]


def test_assistant_mcp_tool_keys_only():
    raw = _vscode(type="assistant", message={
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "mcp__brave-search__search",
                     "input": {"query": "secret-search-term", "count": 5}}],
    })
    events = parse_event(raw)
    assert "secret-search-term" not in events[0].payload["summary"]
    assert "query" in events[0].payload["summary"]


def test_assistant_bash_command_redacted():
    raw = _vscode(type="assistant", message={
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "Bash",
                     "input": {"command": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstu' x"}}],
    })
    events = parse_event(raw)
    assert "Bearer <redacted>" in events[0].payload["summary"]


def test_assistant_final_text_emitted():
    raw = _vscode(type="assistant", message={
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Done."}],
    })
    events = parse_event(raw)
    assert events[0].kind == "assistant_text"
    assert events[0].payload["text"] == "Done."


def test_assistant_midturn_text_is_noise():
    raw = _vscode(type="assistant", message={
        "stop_reason": "tool_use",
        "content": [{"type": "text", "text": "Let me check..."}],
    })
    events = parse_event(raw)
    assert events[0].kind == "noise"


def test_assistant_stop_sequence_treated_as_final():
    raw = _vscode(type="assistant", message={
        "stop_reason": "stop_sequence",
        "content": [{"type": "text", "text": "all good"}],
    })
    events = parse_event(raw)
    assert events[0].kind == "assistant_text"


def test_assistant_mixed_blocks():
    raw = _vscode(type="assistant", message={
        "stop_reason": "tool_use",
        "content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}},
        ],
    })
    events = parse_event(raw)
    kinds = [e.kind for e in events]
    assert "noise" in kinds
    assert "tool_call" in kinds


def test_malformed_event_returns_empty():
    assert parse_event({"entrypoint": "claude-vscode", "type": "user"}) == []
    assert parse_event({}) == []
    assert parse_event({"entrypoint": "claude-vscode"}) == []


def test_non_dict_input_returns_empty():
    assert parse_event(None) == []  # type: ignore[arg-type]
    assert parse_event([]) == []   # type: ignore[arg-type]
    assert parse_event("string") == []  # type: ignore[arg-type]


def test_invalid_timestamp_does_not_raise():
    raw = _vscode(type="user", message={"content": "hi"}, timestamp="not-a-date")
    assert len(parse_event(raw)) == 1
