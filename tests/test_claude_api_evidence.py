"""Tests for services/claude_api/evidence.py（熟考 P1: tool 呼び出し台帳）。"""

import asyncio
import os
import time

import pytest

from services.claude_api import evidence as ev


@pytest.fixture
def tmp_evidence_root(tmp_path, monkeypatch):
    root = tmp_path / "tool_results"
    monkeypatch.setattr(ev, "EVIDENCE_ROOT", str(root))
    return root


class TestEvidenceAdd:
    @pytest.mark.asyncio
    async def test_small_result_no_raw_file(self, tmp_evidence_root):
        idx = ev.EvidenceIndex(channel_id=1)
        rec = await idx.add(
            seq=idx.next_seq(), turn=1, tool="mcp__x__y",
            tool_input={"q": "a"}, content="short", is_error=False,
        )
        assert rec.raw_path is None
        assert rec.note == "short"
        assert rec.raw_chars == 5

    @pytest.mark.asyncio
    async def test_large_result_saved_to_disk(self, tmp_evidence_root, monkeypatch):
        monkeypatch.setattr(ev, "DELIB_EVIDENCE_RAW_MIN_CHARS", 10)
        idx = ev.EvidenceIndex(channel_id=42)
        body = "x" * 500
        rec = await idx.add(
            seq=idx.next_seq(), turn=2, tool="mcp__brave-search__brave_web_search",
            tool_input={"query": "q"}, content=body, is_error=False,
        )
        assert rec.raw_path is not None
        with open(rec.raw_path, encoding="utf-8") as f:
            assert f.read() == body
        assert "42" in rec.raw_path  # channel dir

    @pytest.mark.asyncio
    async def test_list_content_flattened(self, tmp_evidence_root):
        idx = ev.EvidenceIndex(channel_id=1)
        content = [
            {"type": "text", "text": "hello"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "world"},
        ]
        rec = await idx.add(
            seq=idx.next_seq(), turn=1, tool="Read",
            tool_input={}, content=content, is_error=False,
        )
        assert "hello" in rec.note and "[image]" in rec.note and "world" in rec.note

    @pytest.mark.asyncio
    async def test_error_result_not_saved(self, tmp_evidence_root, monkeypatch):
        monkeypatch.setattr(ev, "DELIB_EVIDENCE_RAW_MIN_CHARS", 1)
        idx = ev.EvidenceIndex(channel_id=1)
        rec = await idx.add(
            seq=idx.next_seq(), turn=1, tool="Bash",
            tool_input={}, content="<tool_use_error>boom</tool_use_error>",
            is_error=True,
        )
        assert rec.is_error and rec.raw_path is None

    @pytest.mark.asyncio
    async def test_disk_failure_is_nonfatal(self, tmp_evidence_root, monkeypatch):
        monkeypatch.setattr(ev, "DELIB_EVIDENCE_RAW_MIN_CHARS", 1)

        def boom(path, text):
            raise OSError("disk full")

        monkeypatch.setattr(ev, "_write_raw_sync", boom)
        idx = ev.EvidenceIndex(channel_id=1)
        rec = await idx.add(
            seq=idx.next_seq(), turn=1, tool="mcp__x__y",
            tool_input={}, content="data" * 100, is_error=False,
        )
        assert rec.raw_path is None  # 継続、raise しない


class TestDigest:
    @pytest.mark.asyncio
    async def test_digest_format_and_truncation(self, tmp_evidence_root):
        idx = ev.EvidenceIndex(channel_id=1)
        for i in range(3):
            await idx.add(
                seq=idx.next_seq(), turn=1, tool=f"mcp__t__tool{i}",
                tool_input={"i": i}, content=f"result body {i} " * 30,
                is_error=False,
            )
        d = idx.digest(max_chars=6000)
        assert "[E1]" in d and "[E3]" in d
        assert "input:" in d and "note:" in d
        # 予算が極端に小さくてもヘッダ行は全件残る
        small = idx.digest(max_chars=10)
        assert "[E1]" in small and "[E3]" in small

    def test_empty_digest(self):
        assert ev.EvidenceIndex(channel_id=1).digest() == "(no tool calls)"


class TestCleanup:
    def test_ttl_cleanup(self, tmp_evidence_root):
        d = tmp_evidence_root / "1"
        d.mkdir(parents=True)
        old = d / "old.txt"
        new = d / "new.txt"
        old.write_text("old")
        new.write_text("new")
        past = time.time() - 999999
        os.utime(old, (past, past))
        removed = ev.cleanup_stale_evidence(ttl_s=86400)
        assert removed == 1
        assert not old.exists() and new.exists()


class TestSeqOrdering:
    @pytest.mark.asyncio
    async def test_gather_preserves_dispatch_order(self, tmp_evidence_root):
        """gather 並列でも next_seq() の同期採番で block 順が保たれる。"""
        idx = ev.EvidenceIndex(channel_id=1)

        async def one(i):
            seq = idx.next_seq()
            await asyncio.sleep(0.01 * (3 - i))  # 逆順に完了させる
            return await idx.add(seq=seq, turn=1, tool=f"t{i}",
                                 tool_input={}, content=f"c{i}", is_error=False)

        # 実運用と同じく「coroutine 生成時に seq を確保」する形を模す
        async def wrapper(i):
            return await one(i)

        recs = await asyncio.gather(*(wrapper(i) for i in range(3)))
        # 完了順は逆でも、digest は seq 順 = dispatch 順
        d = idx.digest()
        assert d.index("[E1]") < d.index("[E2]") < d.index("[E3]")
        assert sorted(r.seq for r in recs) == [1, 2, 3]