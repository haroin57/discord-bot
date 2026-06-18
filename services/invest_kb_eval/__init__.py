"""invest_kb evaluation harness — pfnet-style MCQ scoring + golden citation
judge + chitchat false-positive metric.

Public API:
    score_mcq            — run one harness dataset, return per-question result
    grade_citations      — Opus-judge a list of {query, retrieved} pairs
    measure_gate_fpr     — chitchat-set false-positive rate of is_investment_query
"""
from services.invest_kb_eval.mcq import score_mcq
from services.invest_kb_eval.citation import grade_citations
from services.invest_kb_eval.gate import measure_gate_fpr

__all__ = ["score_mcq", "grade_citations", "measure_gate_fpr"]
