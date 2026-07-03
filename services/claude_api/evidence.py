"""Evidence index — per-request の tool 呼び出し台帳（熟考パイプライン P1）。

全 tool 呼び出しを {seq, tool, input要約, 蒸留ノート or 原文head, 原文パス, is_error}
として台帳化する。用途:
  - result distillation（services/claude_api/distill.py）の置き場
  - verifier / judge-lite（P2）の検証対象
  - MAGI-lite digest（P5）の証拠ベース
原文は .state/tool_results/{channel_id}/ に保存し、モデル/verifier は Read で
いつでも遡れる（蒸留ノートは要約であって根拠の代替ではない）。

障害設計: 台帳は観測層であり、いかなる失敗も tool loop へ raise しない。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field

from config import (
    DELIB_EVIDENCE_NOTE_CHARS,
    DELIB_EVIDENCE_RAW_MIN_CHARS,
    DELIB_EVIDENCE_TTL_S,
    STATE_DIR,
)

log = logging.getLogger(__name__)

EVIDENCE_ROOT = os.path.join(STATE_DIR, "tool_results")

_SAFE_TOOL_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _content_to_text(content) -> str:
    """tool_result content（str | list[dict]）をプレーンテキスト化する。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif b.get("type") == "image":
                    parts.append("[image]")
        return "\n".join(parts)
    return str(content)


def _write_raw_sync(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        f = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise
    with f:
        f.write(text)


@dataclass
class EvidenceRecord:
    seq: int
    turn: int
    tool: str
    input_summary: str
    is_error: bool
    raw_chars: int
    raw_path: str | None
    note: str
    distilled: bool = False


@dataclass
class EvidenceIndex:
    channel_id: int
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    records: list[EvidenceRecord] = field(default_factory=list)
    _seq: int = 0

    def next_seq(self) -> int:
        """dispatch 順の採番。asyncio.gather 前の同期区間で呼ぶこと。"""
        self._seq += 1
        return self._seq

    def request_dir(self) -> str:
        return os.path.join(EVIDENCE_ROOT, str(self.channel_id))

    async def add(
        self, *, seq: int, turn: int, tool: str, tool_input: dict,
        content, is_error: bool,
    ) -> EvidenceRecord:
        """tool 実行結果を台帳に追加する。失敗しても raise しない。"""
        try:
            text = _content_to_text(content)
        except Exception:  # noqa: BLE001
            text = "(unserializable)"
        try:
            input_summary = json.dumps(
                tool_input or {}, ensure_ascii=False, sort_keys=True,
            )[:200]
        except Exception:  # noqa: BLE001
            input_summary = str(tool_input)[:200]

        raw_path: str | None = None
        if not is_error and len(text) >= DELIB_EVIDENCE_RAW_MIN_CHARS:
            tool_slug = _SAFE_TOOL_RE.sub("_", tool)[-48:]
            candidate = os.path.join(
                self.request_dir(),
                f"{self.request_id}_{seq:03d}_{tool_slug}.txt",
            )
            try:
                await asyncio.to_thread(_write_raw_sync, candidate, text)
                raw_path = candidate
            except Exception as exc:  # noqa: BLE001
                log.warning("evidence raw save failed (non-fatal): %s", exc)

        rec = EvidenceRecord(
            seq=seq, turn=turn, tool=tool, input_summary=input_summary,
            is_error=is_error, raw_chars=len(text), raw_path=raw_path,
            note=text[:DELIB_EVIDENCE_NOTE_CHARS],
        )
        self.records.append(rec)
        return rec

    def attach_note(self, rec: EvidenceRecord, note: str, *, distilled: bool) -> None:
        rec.note = (note or "")[: max(DELIB_EVIDENCE_NOTE_CHARS * 2, 1400)]
        rec.distilled = distilled

    def digest(self, max_chars: int = 6000) -> str:
        """verifier / judge / MAGI 共用の証拠ダイジェスト。

        ヘッダ行は全 record 残し、note は古い record から切り詰める
        （直近の根拠ほど濃く残す）。
        """
        if not self.records:
            return "(no tool calls)"
        ordered = sorted(self.records, key=lambda x: x.seq)
        headers: dict[int, str] = {}
        for r in ordered:
            status = "ERROR" if r.is_error else "ok"
            raw = f" raw={r.raw_path}" if r.raw_path else ""
            headers[r.seq] = (
                f"[E{r.seq}] turn={r.turn} {r.tool} {status} chars={r.raw_chars}{raw}\n"
                f"  input: {r.input_summary}"
            )
        # note の割当: 新しい record から順に budget を詰める
        budget = max_chars - sum(len(h) + 1 for h in headers.values())
        notes: dict[int, str] = {}
        for r in sorted(ordered, key=lambda x: -x.seq):
            if budget <= 0:
                break
            note = r.note.strip()
            if not note:
                continue
            take = note[: max(0, min(len(note), budget))]
            notes[r.seq] = take
            budget -= len(take) + 10
        out = []
        for r in ordered:
            out.append(headers[r.seq])
            if r.seq in notes:
                out.append(f"  note: {notes[r.seq]}")
        return "\n".join(out)


# --- TTL 掃除（プロセスあたり初回 new_index 時に1度だけ走らせる） -----------
_cleanup_started = False


def cleanup_stale_evidence(ttl_s: int | None = None) -> int:
    """mtime が TTL を超えた raw ファイルを削除する（同期・to_thread 前提）。"""
    ttl = DELIB_EVIDENCE_TTL_S if ttl_s is None else ttl_s
    cutoff = time.time() - ttl
    removed = 0
    if not os.path.isdir(EVIDENCE_ROOT):
        return 0
    for root, _dirs, files in os.walk(EVIDENCE_ROOT):
        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    if removed:
        log.info("evidence cleanup: removed %d stale raw files", removed)
    return removed


def new_index(channel_id: int) -> EvidenceIndex:
    global _cleanup_started
    if not _cleanup_started:
        _cleanup_started = True
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(asyncio.to_thread(cleanup_stale_evidence))
        except RuntimeError:
            pass  # 同期文脈（テスト等）では掃除しない
    return EvidenceIndex(channel_id=channel_id)
