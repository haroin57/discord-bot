import ast

import pytest

from scripts.build_token_efficiency_payload import OLD_READ, build_session


BASELINE = '''import logging
log = logging.getLogger(__name__)
def untouched(value):
    return value
def load_api_replay_channel(channel_id):
    from config import HISTORY_MAX_AGE_DAYS
''' + OLD_READ + '''    return rows
'''


def test_payload_preserves_unrelated_definitions_and_opt_in_default():
    result = build_session(BASELINE)
    def unchanged(source):
        return ast.dump(next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "untouched"))
    assert unchanged(result) == unchanged(BASELINE)
    assert '"CLAUDE_API_REPLAY_EXTRA_ROWS", "0"' in result
    assert "max_age_days=HISTORY_MAX_AGE_DAYS" in result
    assert "_stable_api_replay_window.select" in result


def test_payload_rejects_unknown_or_already_changed_baseline():
    with pytest.raises(ValueError):
        build_session(BASELINE.replace("MAX_MESSAGES_PER_CHANNEL * 2", "unsupported_limit"))
    with pytest.raises(ValueError):
        build_session(build_session(BASELINE))
