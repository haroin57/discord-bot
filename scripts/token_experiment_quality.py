"""Strict and semantic quality gates for synthetic token-efficiency experiments."""

from __future__ import annotations

import hashlib
import json
import re


EXPECTED_ANSWER = {"code": "SILVER-42", "retry_limit": 7}
_JSON_FENCE = re.compile(r"\A```(?:json)?\s*\n?(.*?)\s*```\Z", re.DOTALL)


def assess_replay_answer(text: str, stop_reason: str | None) -> dict:
    answer = None
    failure_class = ""
    json_error = None
    try:
        answer = json.loads(text)
    except json.JSONDecodeError as error:
        failure_class = "empty_response" if not text.strip() else "invalid_json"
        json_error = {"line": error.lineno, "column": error.colno, "message": error.msg}
        fenced = _JSON_FENCE.fullmatch(text.strip())
        if fenced is not None:
            try:
                answer = json.loads(fenced.group(1))
                failure_class = "json_code_fence"
            except json.JSONDecodeError:
                pass
    semantic_pass = answer == EXPECTED_ANSWER
    schema_pass = (
        isinstance(answer, dict)
        and set(answer) == set(EXPECTED_ANSWER)
        and isinstance(answer["code"], str)
        and type(answer["retry_limit"]) is int
    )
    if not failure_class and not schema_pass:
        failure_class = "unexpected_json_schema"
    elif not failure_class and not semantic_pass:
        failure_class = "unexpected_json_value"
    if stop_reason == "refusal":
        failure_class = "refusal"
    elif stop_reason == "max_tokens":
        failure_class = "output_truncated"
    elif stop_reason != "end_turn":
        failure_class = "unexpected_stop_reason"
    return {
        "quality_pass": not failure_class,
        "semantic_quality_pass": semantic_pass and stop_reason == "end_turn",
        "failure_class": failure_class,
        "json_error": json_error,
        "response_chars": len(text),
        "response_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def assess_memory_representations(case: dict, representations: list[str], *, recall: bool) -> dict:
    valid_shape = (
        isinstance(representations, list)
        and len(representations) >= 2
        and all(isinstance(item, str) for item in representations)
    )
    generated = " ".join(representations) if valid_shape else ""
    hypothetical = representations[-2] if valid_shape else ""
    missing = [term for term in case["required"] if term.casefold() not in generated.casefold()]
    unsupported = [
        term for term in case.get("forbidden_hypothesis", [])
        if term.casefold() in hypothetical.casefold()
    ]
    return {
        "missing_terms": missing,
        "unsupported_hypothesis_terms": unsupported,
        "representation_shape_valid": valid_shape,
        "quality_pass": valid_shape and recall and not missing and not unsupported,
    }
