"""DGM data models."""

from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Agent:
    """A coding agent in the evolutionary population."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    generation: int = 0
    parent_ids: list[str] = field(default_factory=list)
    code: str = ""           # Python source containing a solve() function
    strategy: str = ""       # Natural language description of approach
    fitness: float = 0.0     # 0.0 to 1.0 weighted score
    scores: dict[str, float] = field(default_factory=dict)  # per-challenge scores
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    run_id: str = ""
    niche: tuple[int, int] = (0, 0)  # MAP-Elites grid coordinates (complexity, pattern_count)


@dataclass
class Challenge:
    """A coding challenge used for fitness evaluation."""
    id: str
    name: str
    description: str          # problem statement
    function_name: str        # function the agent must implement
    test_cases: list[tuple] = field(default_factory=list)  # [(args_tuple, expected), ...]
    difficulty: str = "medium"  # easy/medium/hard


@dataclass
class SandboxResult:
    """Result of running agent code in sandbox."""
    passed: int = 0
    total: int = 0
    pass_rate: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    timed_out: bool = False


@dataclass
class EvolutionRun:
    """Metadata for a single evolution run."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    population_size: int = 4
    max_generations: int = 3
    current_generation: int = 0
    best_fitness: float = 0.0
    status: str = "running"  # running/completed/stopped
