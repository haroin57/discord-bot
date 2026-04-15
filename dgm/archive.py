"""MAP-Elites quality-diversity archive for DGM.

Inspired by Digital Red Queen (Sakana AI) and OpenELM (CarperAI).
Maintains a grid of diverse, high-performing agents indexed by behavioral descriptors.
"""

import ast
import logging
import random
import re

from config import DGM_ARCHIVE_GRID
from dgm.models import Agent

log = logging.getLogger(__name__)

# Patterns detected via regex (for cases where AST alone is insufficient)
_RE_IMPORT_RE = re.compile(r"(?:^|\s)import\s+re(?:\s|$)|from\s+re\s+import")
_RE_USE_RE = re.compile(r"\bre\.\w+")
_RE_ENUMERATE = re.compile(r"\benumerate\s*\(")
_RE_ZIP = re.compile(r"\bzip\s*\(")
_RE_MAP = re.compile(r"\bmap\s*\(")
_RE_FILTER = re.compile(r"\bfilter\s*\(")


class _PatternVisitor(ast.NodeVisitor):
    """AST visitor that detects coding patterns."""

    def __init__(self) -> None:
        self.patterns: set[str] = set()
        self._func_depth = 0

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self.patterns.add("list_comp")
        self.generic_visit(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self.patterns.add("dict_comp")
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.patterns.add("lambda")
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.patterns.add("try_except")
        self.generic_visit(node)

    # Python 3.11+ has TryStar for except*
    def visit_TryStar(self, node: ast.AST) -> None:
        self.patterns.add("try_except")
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.patterns.add("ternary")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self._func_depth > 0:
            self.patterns.add("nested_def")
        self._func_depth += 1
        self.generic_visit(node)
        self._func_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if self._func_depth > 0:
            self.patterns.add("nested_def")
        self._func_depth += 1
        self.generic_visit(node)
        self._func_depth -= 1


def _count_lines(code: str) -> int:
    """Count non-empty, non-comment lines."""
    count = 0
    for line in code.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


def _detect_patterns(code: str) -> set[str]:
    """Detect coding patterns via AST and regex."""
    patterns: set[str] = set()

    # Regex-based detection
    if _RE_IMPORT_RE.search(code) or _RE_USE_RE.search(code):
        patterns.add("regex")
    if _RE_ENUMERATE.search(code) or _RE_ZIP.search(code) or _RE_MAP.search(code) or _RE_FILTER.search(code):
        patterns.add("builtins")

    # AST-based detection
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return patterns

    visitor = _PatternVisitor()
    visitor.visit(tree)
    patterns.update(visitor.patterns)

    return patterns


def compute_niche(agent: Agent, grid_size: int = DGM_ARCHIVE_GRID) -> tuple[int, int]:
    """Compute behavioral descriptors for an agent and map to grid cell.

    Two axes:
    - X: Code complexity (lines of code, bucketed into grid_size bins)
      Range: 0 to grid_size-1. Bucket by LOC: 0-20=0, 20-40=1, 40-60=2, 60+=3 (for grid=4)
    - Y: Pattern diversity (number of distinct coding patterns used)
      Patterns: regex usage, list comprehension, try/except, dict comprehension,
      lambda, recursion (def inside def), ternary, enumerate/zip usage
      Range: 0 to grid_size-1. Bucket by count: 0-1=0, 2-3=1, 4-5=2, 6+=3

    Returns (x, y) grid coordinates.
    """
    code = agent.code
    if not code or not code.strip():
        return (0, 0)

    # X axis: code complexity (LOC)
    loc = _count_lines(code)
    bucket_size = 20  # lines per bucket
    x = min(loc // bucket_size, grid_size - 1)

    # Y axis: pattern diversity
    try:
        patterns = _detect_patterns(code)
    except Exception:
        log.debug("Pattern detection failed for agent %s", agent.id)
        patterns = set()

    pattern_count = len(patterns)
    # Bucket: 0-1=0, 2-3=1, 4-5=2, 6+=3 (for grid=4)
    bucket_width = 2  # patterns per bucket
    y = min(pattern_count // bucket_width, grid_size - 1)

    return (x, y)


class MAPElitesArchive:
    """MAP-Elites archive storing best agent per niche cell."""

    def __init__(self, grid_size: int = DGM_ARCHIVE_GRID):
        self.grid_size = grid_size
        self.grid: dict[tuple[int, int], Agent] = {}  # (x,y) -> Agent

    def try_add(self, agent: Agent) -> bool:
        """Try to add agent to archive. Returns True if added (new cell or better fitness)."""
        niche = compute_niche(agent, self.grid_size)
        agent.niche = niche
        existing = self.grid.get(niche)
        if existing is None or agent.fitness > existing.fitness:
            self.grid[niche] = agent
            return True
        return False

    def sample(self, n: int = 1) -> list[Agent]:
        """Sample n agents from archive, biased toward diversity (from different cells)."""
        if not self.grid:
            return []
        agents = list(self.grid.values())
        return random.sample(agents, min(n, len(agents)))

    def sample_for_inspiration(self, n: int = 3) -> list[Agent]:
        """Sample top n diverse agents for inspiration-based crossover.
        Pick from different niches, preferring higher fitness."""
        if not self.grid:
            return []
        agents = sorted(self.grid.values(), key=lambda a: -a.fitness)
        # Take top agents but ensure niche diversity
        selected: list[Agent] = []
        seen_niches: set[tuple[int, int]] = set()
        for a in agents:
            if a.niche not in seen_niches:
                selected.append(a)
                seen_niches.add(a.niche)
            if len(selected) >= n:
                break
        # If not enough diverse, fill with best remaining
        if len(selected) < n:
            for a in agents:
                if a.id not in {s.id for s in selected}:
                    selected.append(a)
                if len(selected) >= n:
                    break
        return selected

    @property
    def size(self) -> int:
        return len(self.grid)

    @property
    def coverage(self) -> float:
        """Fraction of grid cells filled."""
        total = self.grid_size * self.grid_size
        return len(self.grid) / total

    def best(self) -> Agent | None:
        if not self.grid:
            return None
        return max(self.grid.values(), key=lambda a: a.fitness)

    def all_agents(self) -> list[Agent]:
        return list(self.grid.values())

    def grid_display(self) -> str:
        """Text representation of the grid for Discord display."""
        lines: list[str] = []
        for y in range(self.grid_size - 1, -1, -1):
            row: list[str] = []
            for x in range(self.grid_size):
                agent = self.grid.get((x, y))
                if agent:
                    row.append(f"{agent.fitness:.0%}")
                else:
                    row.append(" -- ")
            lines.append(f"P{y}| " + " | ".join(row))
        lines.append("   " + "  ".join(f" C{x} " for x in range(self.grid_size)))
        lines.append(f"Coverage: {self.coverage:.0%} ({self.size}/{self.grid_size**2} cells)")
        return "\n".join(lines)
