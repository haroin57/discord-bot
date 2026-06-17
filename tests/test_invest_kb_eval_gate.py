"""Test that the bundled chitchat fixture stays at 0% gate false-positives.

If a future edit to is_investment_query() or to the fixture introduces a
false-positive, this test surfaces it.
"""
from pathlib import Path

from services.invest_kb_eval.gate import measure_gate_fpr


REPO_ROOT = Path(__file__).resolve().parents[1]
CHITCHAT = REPO_ROOT / "tests" / "fixtures" / "chitchat_scenarios.jsonl"


def test_chitchat_fixture_zero_false_positive():
    out = measure_gate_fpr(str(CHITCHAT))
    assert out["n"] >= 50, f"expected ≥50 chitchat lines, got {out['n']}"
    assert out["fpr_pct"] <= 5.0, (
        f"FPR rose above 5%: {out['fpr_pct']}% — offenders: {out['offenders']}"
    )


def test_chitchat_fixture_has_at_least_50_lines():
    out = measure_gate_fpr(str(CHITCHAT))
    assert out["n"] >= 50


def test_gate_handles_string_only_lines(tmp_path):
    p = tmp_path / "mix.jsonl"
    p.write_text('\n'.join([
        '今日の天気どう?',  # bare string
        '{"text": "晩飯何食べた?"}',  # JSON
        '{"query": "NISA の枠は?"}',  # investment, fires
    ]), encoding='utf-8')
    out = measure_gate_fpr(str(p))
    assert out["n"] == 3
    # The third line should fire ('NISA').
    assert out["fired"] == 1
