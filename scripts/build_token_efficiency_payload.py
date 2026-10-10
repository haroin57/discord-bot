"""Build a minimal replay overlay from the effective session source."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path


OLD_READ = '''    _validate_channel_id(channel_id)
    rows = _tail_jsonl(
        _history_file(channel_id),
        MAX_MESSAGES_PER_CHANNEL * 2,
        include_context_snapshot=True,
        max_age_days=HISTORY_MAX_AGE_DAYS,
    )
'''
NEW_READ = '''    _validate_channel_id(channel_id)
    extra_rows = max(0, min(MAX_MESSAGES_PER_CHANNEL, int(os.environ.get("CLAUDE_API_REPLAY_EXTRA_ROWS", "0"))))
    minimum_rows = MAX_MESSAGES_PER_CHANNEL * 2
    rows = _tail_jsonl(
        _history_file(channel_id),
        minimum_rows + extra_rows + (1 if extra_rows else 0),
        include_context_snapshot=True,
        max_age_days=HISTORY_MAX_AGE_DAYS,
    )
    if extra_rows:
        rows = _stable_api_replay_window.select(
            channel_id, rows, minimum_rows=minimum_rows, extra_rows=extra_rows,
        )
'''


def build_session(source: str) -> str:
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "load_api_replay_channel")
    lines = source.splitlines(keepends=True)
    function_source = "".join(lines[function.lineno - 1:function.end_lineno])
    if function_source.count(OLD_READ) != 1:
        raise ValueError("effective loader does not match the supported baseline")
    lines[function.lineno - 1:function.end_lineno] = [function_source.replace(OLD_READ, NEW_READ)]
    result = "".join(lines)
    anchor = 'log = logging.getLogger(__name__)\n'
    if result.count(anchor) != 1:
        raise ValueError("logger anchor does not match the supported baseline")
    result = result.replace(anchor, 'from services.stable_replay import StableReplayWindow\n\n' + anchor +
                            '_stable_api_replay_window = StableReplayWindow(max_channels=MAX_SESSIONS)\n', 1)
    updated = ast.parse(result)
    before = {node.name: ast.dump(node) for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    after = {node.name: ast.dump(node) for node in updated.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    if before.keys() != after.keys() or any(before[name] != after[name] for name in before if name != function.name):
        raise ValueError("overlay changed unrelated definitions")
    compile(result, "session.py", "exec")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.baseline.read_text()
    payload = build_session(source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload)
    print(json.dumps({"baseline_sha256": hashlib.sha256(source.encode()).hexdigest(),
                      "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                      "changed_function": "load_api_replay_channel", "default_extra_rows": 0}, indent=2))


if __name__ == "__main__":
    main()
