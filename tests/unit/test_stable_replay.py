import pytest

from services.stable_replay import StableReplayWindow


def rows(count):
    return [{"role": "user" if index % 2 == 0 else "assistant", "content": str(index),
             "timestamp": f"synthetic-{index}"} for index in range(count)]


def test_appends_preserve_prefix_until_bound_then_rotate_in_batch():
    window = StableReplayWindow()
    initial = window.select(1, rows(12), minimum_rows=8, extra_rows=4)
    next_rows = window.select(1, rows(14), minimum_rows=8, extra_rows=4)
    assert next_rows[:8] == initial
    assert len(next_rows) == 10
    assert len(window.select(1, rows(16), minimum_rows=8, extra_rows=4)) == 12
    rotated = window.select(1, rows(17), minimum_rows=8, extra_rows=4)
    assert len(rotated) == 8
    assert rotated == rows(17)[-8:]


def test_disabled_extra_retains_exact_legacy_tail():
    window = StableReplayWindow()
    for count in range(8, 20):
        assert window.select(1, rows(count), minimum_rows=8, extra_rows=0) == rows(count)[-8:]


def test_changed_deleted_or_age_filtered_anchor_is_not_resurrected():
    window = StableReplayWindow()
    window.select(1, rows(12), minimum_rows=8, extra_rows=4)
    filtered = rows(14)[6:]
    assert window.select(1, filtered, minimum_rows=8, extra_rows=4) == filtered
    assert window.select(1, [], minimum_rows=8, extra_rows=4) == []
    assert window.select(1, rows(3), minimum_rows=8, extra_rows=4) == rows(3)


def test_keeps_structured_tool_rows_intact_and_does_not_mutate():
    window = StableReplayWindow()
    structured = rows(10)
    structured[-1]["api_messages"] = [{"role": "assistant", "content": [{"type": "thinking", "signature": "synthetic"}]}]
    selected = window.select(1, structured, minimum_rows=8, extra_rows=4)
    assert selected[-1] is structured[-1]
    assert selected[-1]["api_messages"] == structured[-1]["api_messages"]


def test_channel_isolation_and_lru_eviction():
    window = StableReplayWindow(max_channels=1)
    window.select(1, rows(12), minimum_rows=8, extra_rows=4)
    window.select(2, rows(12), minimum_rows=8, extra_rows=4)
    assert len(window.select(1, rows(14), minimum_rows=8, extra_rows=4)) == 8


def test_invalid_bounds_fail_explicitly():
    with pytest.raises(ValueError):
        StableReplayWindow(max_channels=0)
    with pytest.raises(ValueError):
        StableReplayWindow(max_extra_bytes=-1)
    with pytest.raises(ValueError):
        StableReplayWindow().select(1, rows(2), minimum_rows=0, extra_rows=1)


def test_session_loader_uses_extra_probe_and_preserves_prefix(monkeypatch):
    import services.session as session
    captured = []
    source = rows(8)
    def tail(path, maximum, **kwargs):
        captured.append(maximum)
        return source[-maximum:]
    monkeypatch.setattr(session, "MAX_MESSAGES_PER_CHANNEL", 2)
    monkeypatch.setattr(session, "_tail_jsonl", tail)
    monkeypatch.setattr(session, "_stable_api_replay_window", StableReplayWindow())
    monkeypatch.setenv("CLAUDE_API_REPLAY_EXTRA_ROWS", "2")
    first, _, _ = session.load_api_replay_channel(100000000000789)
    source.extend(rows(10)[8:])
    second, _, _ = session.load_api_replay_channel(100000000000789)
    assert second[:len(first)] == first
    assert captured == [7, 7]


def test_large_structured_extra_content_rotates_to_legacy_tail():
    window = StableReplayWindow()
    source = rows(4)
    source[0]["api_messages"] = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "synthetic",
                                      "content": "x" * 125_000}]},
    ]
    initial = window.select(1, source, minimum_rows=4, extra_rows=4)
    source.extend(rows(6)[4:])

    selected = window.select(1, source, minimum_rows=4, extra_rows=4)

    assert selected == source[-4:]
    assert len(initial[0]["api_messages"][0]["content"][0]["content"]) == 125_000


@pytest.mark.parametrize("budget_delta", [-1, 0])
def test_extra_capacity_counts_serialized_utf8_bytes_and_includes_boundary(budget_delta):
    import json

    source = rows(2)
    source[0]["content"] = "あ" * 10
    serialized = json.dumps(source[0], sort_keys=True, ensure_ascii=False)
    extra_size = len(serialized.encode("utf-8"))
    assert extra_size > len(serialized)
    window = StableReplayWindow(max_extra_bytes=extra_size + budget_delta)
    window.select(1, source, minimum_rows=2, extra_rows=1)
    source.extend(rows(3)[2:])

    selected = window.select(1, source, minimum_rows=2, extra_rows=1)

    assert selected == (source[-2:] if budget_delta < 0 else source)


def test_zero_extra_byte_budget_keeps_legacy_tail():
    window = StableReplayWindow(max_extra_bytes=0)
    window.select(1, rows(4), minimum_rows=4, extra_rows=4)

    assert window.select(1, rows(6), minimum_rows=4, extra_rows=4) == rows(6)[-4:]


def test_extra_budget_does_not_trim_required_baseline_rows():
    import json

    source = rows(4)
    extra_size = len(json.dumps(source[0], sort_keys=True, ensure_ascii=False).encode("utf-8"))
    source[-1]["content"] = "x" * 125_000
    window = StableReplayWindow(max_extra_bytes=extra_size)
    window.select(1, source, minimum_rows=4, extra_rows=1)
    source.extend(rows(5)[4:])

    selected = window.select(1, source, minimum_rows=4, extra_rows=1)

    assert selected == source
    assert len(selected[-2]["content"]) == 125_000
