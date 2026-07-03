"""Deep Research モード（/research）— 多段 Web 調査で引用付きレポート（ADR-0028）.

正本: docs/deep-research-mode.md。Phase 0 = 単一エージェント + 必須明確化。
"""

from .orchestrator import (
    DeepResearchDeps,
    available_research_keys,
    has_required_web_backend,
    is_active_research_thread,
    run_deep_research,
)

__all__ = [
    "DeepResearchDeps",
    "available_research_keys",
    "has_required_web_backend",
    "is_active_research_thread",
    "run_deep_research",
]
