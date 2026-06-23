"""TDD Phase 4: the resumed transcript tail must honour MAX_MESSAGES_PER_CHANNEL.

The replay is rebuilt on every non-restricted turn, so its size = per-turn
cost. haroin chose a 250-turn tail (MAX_MESSAGES_PER_CHANNEL=125) on Opus 4.8.
This guards the WIRING (load_api_replay_channel must use the config cap, not a
hardcoded 100) rather than the exact number, which stays a tunable.
"""

import config
from services.session import append_jsonl, load_api_replay_channel

_CID = 100000000000777


class TestReplayCapHonorsConfig:
    def test_replay_capped_at_config_times_two(self, tmp_state_dir):
        # Write well past the cap: (cap+50) pairs = far more than cap*2 messages.
        pairs = config.MAX_MESSAGES_PER_CHANNEL + 50
        for i in range(pairs):
            append_jsonl(_CID, {"role": "user", "content": f"u{i}"})
            append_jsonl(_CID, {"role": "assistant", "content": f"a{i}"})

        history, _snapshot, _ts = load_api_replay_channel(_CID)

        # Exactly the last MAX_MESSAGES_PER_CHANNEL*2 messages survive...
        assert len(history) == config.MAX_MESSAGES_PER_CHANNEL * 2
        # ...and they are the most recent ones (tail preserved, head dropped).
        assert history[-1]["content"] == f"a{pairs - 1}"
        assert history[0]["content"] not in ("u0", "a0")

    def test_under_cap_returns_all(self, tmp_state_dir):
        for i in range(3):
            append_jsonl(_CID, {"role": "user", "content": f"u{i}"})
            append_jsonl(_CID, {"role": "assistant", "content": f"a{i}"})
        history, _s, _t = load_api_replay_channel(_CID)
        assert len(history) == 6


def test_session_ttl_at_least_one_hour():
    # haroin raised this to 60m so a short break doesn't cold-reset the session.
    assert config.SESSION_TTL_S >= 60 * 60
