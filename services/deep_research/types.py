"""Deep Research の中間状態とデータ型（ADR-0028）.

実行中はorchestratorが保持し、用件に接続した調査はWorkStoreにcheckpointを保存します。
通常チャットのnative sessionやJSONLとは共有しません。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str) -> str:
    """dedup 用の緩い URL 正規化（fragment / 末尾スラッシュ除去）。"""
    if not url:
        return ""
    u = url.strip()
    u = u.split("#", 1)[0]
    if len(u) > 1 and u.endswith("/"):
        u = u[:-1]
    return u


@dataclass
class SourceRef:
    """1 つの情報ソース。sid（S1, S2, …）で本文からインライン参照する。"""

    sid: str
    title: str
    url: str
    source_type: str = "web"  # "web" | "news" | "x" | "other"
    snippet: str = ""
    # EvidenceRegistry が支持判定に使う、検索結果から機械抽出した根拠断片。
    evidence: list[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=utc_now)
    # A source's reporting date is not its retrieval date. Unknown stays null.
    as_of: str | None = None
    fresh_for_s: float | None = None
    version: int = 1


@dataclass
class SubAgentResult:
    """1 サブエージェント（Phase 0 では単一 researcher）の最終要約。"""

    subq: str
    summary_md: str
    cited_sids: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None


@dataclass
class LensResult:
    """1 MAGI レンズ（科学 / 直観 / 懐疑）の lead 出力（FR-M2）。

    ``body`` は lead の構造化テキスト（STANCE / 【...】ブロック）。``stance`` は
    その STANCE 行を軽くパースしたもの（integrator の見出し用）。失敗時は ok=False。
    """

    lens_key: str  # "science" | "intuition" | "skeptic"
    lens_label: str  # "科学（メルキオール）" など表示名
    stance: str  # STANCE: 行（無ければ body 冒頭）
    body: str  # lead の全文（統合役に渡す素材）
    ok: bool = True
    error: str | None = None


@dataclass
class ResearchState:
    """1 回の Deep Research 実行全体の状態。"""

    query: str
    clarified_query: str = ""
    plan: list[str] = field(default_factory=list)
    # normalize_url(url) -> SourceRef。sid は登録順に S1, S2, … と採番。
    sources: dict[str, SourceRef] = field(default_factory=dict)
    results: list[SubAgentResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    tokens_used: int = 0
    tokens_before_resume: int = 0
    phase: str = "init"
    # --- MAGI 推論層（FR-M1/M2）---
    # 中心的問い（Stage1 暫定 → Stage2 研ぎ直し → 承認ゲート確定）と、その「なぜ核心か」。
    central_question: str = ""
    central_question_rationale: str = ""
    # 3レンズ lead の出力（科学/直観/懐疑）。integrator の入力素材。
    lens_outputs: list[LensResult] = field(default_factory=list)
    # リクエスト者の Mem0 長期記憶（recall 済みの整形済みブロック）。clarify/planner/refine/
    # integrator の briefing に注入する。MAGI 3視点には渡さない（独立性を汚さないため）。
    user_memories: str = ""
    work_id: str | None = None
    work_revision: int = 1
    mode: str = "decision"
    created_at: str = field(default_factory=utc_now)
    elapsed_before_resume: float = 0.0
    requirements: dict = field(default_factory=dict)
    evaluations: dict[str, dict] = field(default_factory=dict)
    agent_results: dict[str, dict] = field(default_factory=dict)
    questions: dict[str, dict] = field(default_factory=dict)
    decision_record: dict = field(default_factory=dict)
    learning_record: dict = field(default_factory=dict)
    evidence_quality: dict = field(default_factory=dict)
    completion_criteria: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    completion_status: str = "in_progress"
    incomplete_reason: str = ""
    report: str = ""
    approval_revision: int | None = None
    memory_epoch: int | None = None
    audience_key: str | None = None
    clarification_required: bool = True
    clarification_default: str | None = None

    def snapshot(self) -> dict:
        """Provider independent durable state; no native thread or process clock."""
        payload = asdict(self)
        payload.pop("started_at", None)
        payload["elapsed_before_resume"] = self.elapsed_s()
        # Recalled personal context must be checked again against current policies.
        payload["user_memories"] = ""
        return payload

    @classmethod
    def restore(cls, payload: dict) -> "ResearchState":
        allowed = {item.name for item in fields(cls)} - {"started_at"}
        values = {key: value for key, value in payload.items() if key in allowed}
        values["sources"] = {
            key: SourceRef(**value) for key, value in values.get("sources", {}).items()
        }
        values["results"] = [SubAgentResult(**value) for value in values.get("results", [])]
        values["lens_outputs"] = [LensResult(**value) for value in values.get("lens_outputs", [])]
        values["tokens_before_resume"] = values.get("tokens_used", 0)
        return cls(**values)

    def register_source(
        self,
        *,
        url: str,
        title: str = "",
        source_type: str = "web",
        snippet: str = "",
        as_of: str | None = None,
        fresh_for_s: float | None = None,
    ) -> SourceRef | None:
        """URL を sid 付きで登録（既出なら既存を返す）。無効 URL は None。"""
        norm = normalize_url(url)
        if not norm or not norm.startswith("http"):
            return None
        existing = self.sources.get(norm)
        if existing is not None:
            existing.fetched_at = utc_now()
            snippet_changed = bool(snippet and snippet[:300] != existing.snippet)
            date_changed = as_of is not None and as_of != existing.as_of
            if snippet_changed or date_changed:
                if existing.fresh_for_s is not None or fresh_for_s is not None or date_changed:
                    existing.evidence = []
                existing.version += 1
            if snippet_changed:
                existing.snippet = snippet[:300]
            if as_of is not None:
                existing.as_of = as_of
            if fresh_for_s is not None:
                existing.fresh_for_s = fresh_for_s
            return existing
        sid = f"S{len(self.sources) + 1}"
        ref = SourceRef(
            sid=sid,
            title=(title or norm)[:200],
            url=url,
            source_type=source_type,
            snippet=(snippet or "")[:300],
            as_of=as_of,
            fresh_for_s=fresh_for_s,
        )
        self.sources[norm] = ref
        return ref

    def add_usage(self, usage: dict | None) -> None:
        """run_agent_loop が返す usage_total を累積（予算監視用）。"""
        if not usage:
            return
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            try:
                self.tokens_used += int(usage.get(k, 0) or 0)
            except (TypeError, ValueError):
                pass

    def elapsed_s(self) -> float:
        return self.elapsed_before_resume + time.monotonic() - self.started_at
