"""Measure the false-positive rate of the invest_kb gate on a chitchat set."""
from __future__ import annotations

import json


def measure_gate_fpr(chitchat_path: str) -> dict:
    """Each line of chitchat_path is a JSON {"text": "..."} (or just a string).
    Returns aggregate FPR + the offending lines.
    """
    from services.invest_kb.search import is_investment_query

    texts: list[str] = []
    with open(chitchat_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    txt = obj.get("text") or obj.get("query") or ""
                except json.JSONDecodeError:
                    continue
            else:
                txt = line
            if txt:
                texts.append(txt)

    fired = [t for t in texts if is_investment_query(t)]
    n = len(texts) or 1
    return {
        "n": len(texts),
        "fired": len(fired),
        "fpr_pct": round(100 * len(fired) / n, 2),
        "offenders": fired[:10],
    }
