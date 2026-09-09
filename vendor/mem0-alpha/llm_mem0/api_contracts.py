"""Pure, provider-independent input and outcome contracts for MCP adapters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import islice
import json
import math
import re

MAX_INPUT_CHARS = 128_000
MAX_OUTPUT_CHARS = 24_000
MAX_IDENTIFIER_CHARS = 256
MAX_SOURCE_IDS = 100


def identifier(value: str | None, name: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > MAX_IDENTIFIER_CHARS:
        raise ValueError(f"{name} must be text of at most {MAX_IDENTIFIER_CHARS} characters")
    cleaned = value.strip()
    if any(ord(char) < 32 for char in cleaned) or (required and not cleaned):
        raise ValueError(f"invalid {name}")
    return cleaned


def validate_input_text(**values: str) -> None:
    for name, value in values.items():
        if not isinstance(value, str) or len(value) > MAX_INPUT_CHARS:
            raise ValueError(f"{name} must be text of at most {MAX_INPUT_CHARS} characters")


def epoch_seconds(value: int | float | str, name: str) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"invalid {name}")
    if isinstance(value, str):
        if len(value) > 80:
            raise ValueError(f"invalid {name}")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            value = parsed.timestamp()
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError(f"{name} requires Unix seconds or timezone-aware ISO8601") from exc
    if (not isinstance(value, (int, float)) or not 0 <= value <= 253402300799
            or not math.isfinite(value)):
        raise ValueError(f"{name} is outside the supported time range")
    return value


def provenance_metadata(
    *, request_id: str | None = None, source_id: str | None = None,
    source_message_ids: list[str] | None = None, source_author_id: str | None = None,
    source_timestamp: int | float | str | None = None, channel_id: str | None = None,
    valid_from: int | float | str | None = None, valid_to: int | float | str | None = None,
) -> dict:
    """Validate explicit provenance, never infer identity or retry IDs."""
    result = {}
    for name, value in (("request_id", request_id), ("source_author_id", source_author_id), ("channel_id", channel_id)):
        cleaned = identifier(value, name)
        if cleaned:
            result[name] = cleaned
    source = identifier(source_id, "source_id")
    if source_message_ids is not None and (not isinstance(source_message_ids, list) or len(source_message_ids) > MAX_SOURCE_IDS):
        raise ValueError(f"source_message_ids must contain at most {MAX_SOURCE_IDS} identifiers")
    sources = [identifier(value, "source_message_id", required=True) for value in (source_message_ids or [])]
    if source:
        sources.insert(0, source)
    sources = list(dict.fromkeys(sources))
    if len(sources) > MAX_SOURCE_IDS:
        raise ValueError("too many source identifiers")
    if sources:
        result["source_message_ids"] = json.dumps(sources, ensure_ascii=False)
    if source_timestamp is not None:
        epoch_seconds(source_timestamp, "source_timestamp")
        result["source_timestamp"] = source_timestamp
    for name, value in (("valid_from", valid_from), ("valid_to", valid_to)):
        if value is not None:
            result[name] = epoch_seconds(value, name)
    if "valid_from" in result and "valid_to" in result and result["valid_from"] > result["valid_to"]:
        raise ValueError("valid_from must not exceed valid_to")
    return result


@dataclass(frozen=True)
class AddOutcome:
    status: str
    stored: int = 0
    failed: int = 0


def classify_add_outcome(result) -> AddOutcome:
    if not isinstance(result, dict):
        return AddOutcome("unavailable" if result is None else "unknown")
    rows = result.get("results")
    count = len(rows) if isinstance(rows, list) else 0
    failed = result.get("failed", 0)
    if isinstance(failed, bool) or not isinstance(failed, int) or failed < 0:
        return AddOutcome("unknown", count)
    status = result.get("status")
    if result.get("error") or status == "error":
        return AddOutcome("error", count, failed)
    if status == "partial" or failed:
        return AddOutcome("partial", count, failed)
    valid_rows = isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
    if status == "ok" and (rows is None or valid_rows):
        return AddOutcome("ok", count)
    if "status" not in result and valid_rows:
        return AddOutcome("ok", count)
    return AddOutcome("unknown", count)


def add_failure(outcome: AddOutcome, result=None, request_id: str | None = None) -> str:
    retry_id = request_id
    if isinstance(result, dict):
        retry_id = result.get("continuation_id") or result.get("request_id") or retry_id
    retry = "retry with the same request_id/source identifiers"
    if isinstance(retry_id, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", retry_id):
        retry = f"retry request_id={retry_id} with the same input/source identifiers"
    return (f"ingestion incomplete ({outcome.status}; stored={outcome.stored}; failed={outcome.failed}); "
            f"verify the result and {retry}")


def search_mode(mode: str | None, smart: bool) -> str:
    if mode is None or mode == "":
        return "smart" if smart else "vector"
    if not isinstance(mode, str) or mode not in {"vector", "hybrid", "smart"}:
        raise ValueError("mode must be vector, hybrid, or smart")
    return mode


def page_offset(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
        raise ValueError("offset must be between 0 and 10000")
    return value


def bound_output(text: str) -> str:
    marker = "\n(output truncated; narrow the query or page size)"
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS - len(marker)] + marker


def bounded_details(value, scrub=lambda text: text, *, _depth=0, _budget=None):
    """Bound cardinality/depth before JSON serialization; text stays untrusted."""
    if _budget is None:
        _budget = [MAX_OUTPUT_CHARS // 2]
    if _depth > 4 or _budget[0] <= 0:
        return "[depth limit]"
    _budget[0] -= 8
    if isinstance(value, str):
        text = scrub(value[:max(0, min(4000, _budget[0]))])
        _budget[0] -= len(text)
        return text
    if isinstance(value, dict):
        result = {}
        for key, item in islice(value.items(), 40):
            if _budget[0] <= 0:
                break
            safe_key = str(scrub(str(key)[:120]))
            _budget[0] -= len(safe_key)
            result[safe_key] = bounded_details(item, scrub, _depth=_depth + 1, _budget=_budget)
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value[:100]:
            if _budget[0] <= 0:
                break
            result.append(bounded_details(item, scrub, _depth=_depth + 1, _budget=_budget))
        return result
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return bounded_details(str(value), scrub, _depth=_depth, _budget=_budget)


def details_json(key: str, value, scrub=lambda text: text, **metadata) -> str:
    """Return valid bounded JSON, not a truncated invalid JSON document."""
    rows = bounded_details(value, scrub)
    payload = {key: rows, **metadata}
    if isinstance(value, list) and isinstance(rows, list) and len(rows) < len(value):
        payload["truncated"] = True
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    if isinstance(rows, list):
        payload["truncated"] = True
        while rows and len(text) > MAX_OUTPUT_CHARS:
            rows.pop()
            text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        return text
    return json.dumps({key: [], "truncated": True}, ensure_ascii=False)
