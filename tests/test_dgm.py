"""Comprehensive tests for the DGM (Darwin Goedel Machine) module."""

import asyncio
import json
import os
import sqlite3
import textwrap
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from dgm.models import Agent, Challenge, EvolutionRun, SandboxResult


# ===================================================================
# 1. dgm/models.py
# ===================================================================

class TestAgent:
    def test_default_id_is_uuid(self):
        a = Agent()
        uuid.UUID(a.id)  # raises if not valid uuid

    def test_unique_ids(self):
        a1, a2 = Agent(), Agent()
        assert a1.id != a2.id

    def test_default_values(self):
        a = Agent()
        assert a.generation == 0
        assert a.parent_ids == []
        assert a.code == ""
        assert a.strategy == ""
        assert a.fitness == 0.0
        assert a.scores == {}
        assert a.run_id == ""
        assert a.niche == (0, 0)

    def test_created_at_is_iso(self):
        a = Agent()
        assert "T" in a.created_at  # ISO format contains T

    def test_field_initialization(self):
        a = Agent(
            id="abc",
            generation=5,
            parent_ids=["p1", "p2"],
            code="def solve(): pass",
            strategy="brute force",
            fitness=0.85,
            scores={"c1": 1.0},
            run_id="run-1",
            niche=(2, 3),
        )
        assert a.id == "abc"
        assert a.generation == 5
        assert a.parent_ids == ["p1", "p2"]
        assert a.fitness == 0.85
        assert a.niche == (2, 3)

    def test_mutable_defaults_not_shared(self):
        a1, a2 = Agent(), Agent()
        a1.parent_ids.append("x")
        assert a2.parent_ids == []
        a1.scores["c"] = 1.0
        assert a2.scores == {}


class TestChallenge:
    def test_initialization(self):
        c = Challenge(
            id="test",
            name="Test Challenge",
            description="Do something",
            function_name="solve",
            test_cases=[(("a",), "b")],
            difficulty="hard",
        )
        assert c.id == "test"
        assert c.name == "Test Challenge"
        assert c.function_name == "solve"
        assert c.difficulty == "hard"
        assert len(c.test_cases) == 1

    def test_defaults(self):
        c = Challenge(id="x", name="X", description="d", function_name="f")
        assert c.test_cases == []
        assert c.difficulty == "medium"


class TestSandboxResult:
    def test_defaults(self):
        r = SandboxResult()
        assert r.passed == 0
        assert r.total == 0
        assert r.pass_rate == 0.0
        assert r.stdout == ""
        assert r.stderr == ""
        assert r.error is None
        assert r.timed_out is False

    def test_with_values(self):
        r = SandboxResult(passed=3, total=5, pass_rate=0.6, timed_out=True, error="oops")
        assert r.passed == 3
        assert r.total == 5
        assert r.pass_rate == 0.6
        assert r.timed_out is True
        assert r.error == "oops"


class TestEvolutionRun:
    def test_defaults(self):
        r = EvolutionRun()
        uuid.UUID(r.id)
        assert r.finished_at is None
        assert r.population_size == 4
        assert r.max_generations == 3
        assert r.current_generation == 0
        assert r.best_fitness == 0.0
        assert r.status == "running"

    def test_status_field(self):
        r = EvolutionRun(status="completed")
        assert r.status == "completed"


# ===================================================================
# 2. dgm/challenges.py
# ===================================================================

class TestBuiltinChallenges:
    def test_returns_list(self):
        from dgm.challenges import get_builtin_challenges
        challenges = get_builtin_challenges()
        assert isinstance(challenges, list)

    def test_count(self):
        from dgm.challenges import get_builtin_challenges
        assert len(get_builtin_challenges()) == 8

    def test_each_has_required_fields(self):
        from dgm.challenges import get_builtin_challenges
        for c in get_builtin_challenges():
            assert isinstance(c, Challenge)
            assert c.id
            assert c.name
            assert len(c.test_cases) > 0
            assert c.function_name
            assert c.difficulty in ("easy", "medium", "hard")

    def test_unique_ids(self):
        from dgm.challenges import get_builtin_challenges
        ids = [c.id for c in get_builtin_challenges()]
        assert len(ids) == len(set(ids))


# ===================================================================
# 3. dgm/archive.py
# ===================================================================

class TestCountLines:
    def test_empty_string(self):
        from dgm.archive import _count_lines
        assert _count_lines("") == 0

    def test_only_comments(self):
        from dgm.archive import _count_lines
        assert _count_lines("# comment\n# another") == 0

    def test_only_blank_lines(self):
        from dgm.archive import _count_lines
        assert _count_lines("\n\n\n") == 0

    def test_mixed(self):
        from dgm.archive import _count_lines
        code = "x = 1\n# comment\n\ny = 2\n"
        assert _count_lines(code) == 2

    def test_real_code(self):
        from dgm.archive import _count_lines
        code = textwrap.dedent("""\
            def solve(n):
                # helper
                if n == 0:
                    return 1
                return n * solve(n - 1)
        """)
        assert _count_lines(code) == 4


class TestDetectPatterns:
    def test_list_comp(self):
        from dgm.archive import _detect_patterns
        assert "list_comp" in _detect_patterns("[x for x in range(10)]")

    def test_dict_comp(self):
        from dgm.archive import _detect_patterns
        assert "dict_comp" in _detect_patterns("{k: v for k, v in items}")

    def test_lambda(self):
        from dgm.archive import _detect_patterns
        assert "lambda" in _detect_patterns("f = lambda x: x + 1")

    def test_try_except(self):
        from dgm.archive import _detect_patterns
        code = "try:\n    pass\nexcept:\n    pass"
        assert "try_except" in _detect_patterns(code)

    def test_ternary(self):
        from dgm.archive import _detect_patterns
        assert "ternary" in _detect_patterns("x = 1 if True else 0")

    def test_regex_import(self):
        from dgm.archive import _detect_patterns
        assert "regex" in _detect_patterns("import re")

    def test_regex_usage(self):
        from dgm.archive import _detect_patterns
        assert "regex" in _detect_patterns("re.match(pattern, s)")

    def test_builtins_enumerate(self):
        from dgm.archive import _detect_patterns
        assert "builtins" in _detect_patterns("for i, x in enumerate(lst): pass")

    def test_builtins_zip(self):
        from dgm.archive import _detect_patterns
        assert "builtins" in _detect_patterns("list(zip(a, b))")

    def test_builtins_map(self):
        from dgm.archive import _detect_patterns
        assert "builtins" in _detect_patterns("list(map(int, items))")

    def test_builtins_filter(self):
        from dgm.archive import _detect_patterns
        assert "builtins" in _detect_patterns("list(filter(None, items))")

    def test_nested_def(self):
        from dgm.archive import _detect_patterns
        code = "def outer():\n    def inner():\n        pass"
        assert "nested_def" in _detect_patterns(code)

    def test_syntax_error_returns_partial(self):
        from dgm.archive import _detect_patterns
        # regex patterns still detected even if AST fails
        code = "import re\nthis is bad syntax ((("
        patterns = _detect_patterns(code)
        assert "regex" in patterns

    def test_no_patterns(self):
        from dgm.archive import _detect_patterns
        assert _detect_patterns("x = 1") == set()


class TestComputeNiche:
    def test_empty_code(self):
        from dgm.archive import compute_niche
        agent = Agent(code="")
        assert compute_niche(agent) == (0, 0)

    def test_whitespace_only(self):
        from dgm.archive import compute_niche
        agent = Agent(code="   \n  \n  ")
        assert compute_niche(agent) == (0, 0)

    def test_short_simple_code(self):
        from dgm.archive import compute_niche
        agent = Agent(code="x = 1\ny = 2")
        # 2 lines -> bucket 0 (0-19), no patterns -> bucket 0
        assert compute_niche(agent, grid_size=4) == (0, 0)

    def test_complex_code(self):
        from dgm.archive import compute_niche
        # Generate code with 25 lines and multiple patterns
        lines = ["def outer():"]
        lines.append("    def inner(): pass")  # nested_def
        lines.append("    x = [i for i in range(10)]")  # list_comp
        lines.append("    y = lambda a: a")  # lambda
        lines.append("    z = 1 if True else 0")  # ternary
        # Pad to >20 lines
        for i in range(20):
            lines.append(f"    v{i} = {i}")
        code = "\n".join(lines)
        niche = compute_niche(Agent(code=code), grid_size=4)
        assert niche[0] >= 1  # >20 lines
        assert niche[1] >= 1  # multiple patterns


class TestMAPElitesArchive:
    def test_try_add_new_cell(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        agent = Agent(code="x = 1", fitness=0.5)
        assert archive.try_add(agent) is True
        assert archive.size == 1

    def test_try_add_better_replaces(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        a1 = Agent(code="x = 1", fitness=0.3)
        a2 = Agent(code="x = 1", fitness=0.8)  # same niche, better
        archive.try_add(a1)
        assert archive.try_add(a2) is True
        assert archive.best().fitness == 0.8

    def test_try_add_worse_rejected(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        a1 = Agent(code="x = 1", fitness=0.8)
        a2 = Agent(code="x = 1", fitness=0.3)  # same niche, worse
        archive.try_add(a1)
        assert archive.try_add(a2) is False
        assert archive.best().fitness == 0.8

    def test_sample_empty(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        assert archive.sample() == []

    def test_sample_populated(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        archive.try_add(Agent(code="x = 1", fitness=0.5))
        result = archive.sample(1)
        assert len(result) == 1

    def test_sample_for_inspiration_empty(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        assert archive.sample_for_inspiration() == []

    def test_sample_for_inspiration_populated(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        # Add agents in different niches
        archive.try_add(Agent(code="x = 1", fitness=0.5))
        archive.try_add(Agent(code="\n".join(["x = " + str(i) for i in range(25)]), fitness=0.8))
        result = archive.sample_for_inspiration(n=2)
        assert len(result) <= 2

    def test_size_property(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        assert archive.size == 0
        archive.try_add(Agent(code="x = 1", fitness=0.5))
        assert archive.size == 1

    def test_coverage_empty(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        assert archive.coverage == 0.0

    def test_coverage_partial(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        archive.try_add(Agent(code="x = 1", fitness=0.5))
        # 1 / 16 cells
        assert archive.coverage == pytest.approx(1 / 16)

    def test_best_empty(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        assert archive.best() is None

    def test_best_returns_highest_fitness(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        archive.try_add(Agent(code="x = 1", fitness=0.3))
        lines = "\n".join(["x = " + str(i) for i in range(25)])
        archive.try_add(Agent(code=lines, fitness=0.9))
        assert archive.best().fitness == 0.9

    def test_all_agents(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        archive.try_add(Agent(code="x = 1", fitness=0.5))
        agents = archive.all_agents()
        assert len(agents) == 1

    def test_grid_display_returns_string(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        archive.try_add(Agent(code="x = 1", fitness=0.5))
        display = archive.grid_display()
        assert isinstance(display, str)
        assert "Coverage" in display

    def test_grid_display_empty(self):
        from dgm.archive import MAPElitesArchive
        archive = MAPElitesArchive(grid_size=4)
        display = archive.grid_display()
        assert "0%" in display


# ===================================================================
# 4. dgm/sandbox.py
# ===================================================================

class TestCheckImports:
    def test_allowed_import_math(self):
        from dgm.sandbox import check_imports
        assert check_imports("import math") is None

    def test_allowed_import_re(self):
        from dgm.sandbox import check_imports
        assert check_imports("import re") is None

    def test_banned_import_os(self):
        from dgm.sandbox import check_imports
        result = check_imports("import os")
        assert result is not None
        assert "os" in result

    def test_banned_import_sys(self):
        from dgm.sandbox import check_imports
        result = check_imports("import sys")
        assert result is not None
        assert "sys" in result

    def test_banned_import_subprocess(self):
        from dgm.sandbox import check_imports
        result = check_imports("import subprocess")
        assert result is not None
        assert "subprocess" in result

    def test_banned_from_import(self):
        from dgm.sandbox import check_imports
        result = check_imports("from os import path")
        assert result is not None
        assert "os" in result

    def test_syntax_error(self):
        from dgm.sandbox import check_imports
        result = check_imports("def ((( bad")
        assert result is not None
        assert "SyntaxError" in result

    def test_no_imports(self):
        from dgm.sandbox import check_imports
        assert check_imports("x = 1 + 2") is None

    def test_unknown_module(self):
        from dgm.sandbox import check_imports
        result = check_imports("import numpy")
        assert result is not None
        assert "Unknown module" in result

    def test_multiple_imports_one_banned(self):
        from dgm.sandbox import check_imports
        result = check_imports("import math\nimport os")
        assert result is not None
        assert "os" in result

    def test_dotted_import_banned(self):
        from dgm.sandbox import check_imports
        result = check_imports("import os.path")
        assert result is not None


@pytest.mark.integration
class TestRunSandboxed:
    """Spawns a real `python` subprocess per test (DGM sandbox isolation).

    Each test is +2–5 s wall-clock and the timeout test forces a 1 s stall.
    Excluded from the default `pytest` run via the `integration` marker;
    opt in with `pytest -m integration` or `pytest -m ""`.
    """

    @pytest.mark.asyncio
    async def test_successful_run(self):
        from dgm.sandbox import run_sandboxed
        agent_code = "def solve(x): return x * 2"
        test_code = textwrap.dedent("""\
            _passed = 0
            _total = 1
            if solve(3) == 6:
                _passed += 1
            print(f"PASSED {_passed}/{_total}")
        """)
        result = await run_sandboxed(agent_code, test_code)
        assert result.passed == 1
        assert result.total == 1
        assert result.pass_rate == 1.0
        assert result.error is None

    @pytest.mark.asyncio
    async def test_syntax_error_in_agent(self):
        from dgm.sandbox import run_sandboxed
        result = await run_sandboxed("def ((( bad", "print('hi')")
        assert result.error is not None
        assert "SyntaxError" in result.error

    @pytest.mark.asyncio
    async def test_banned_import_rejected(self):
        from dgm.sandbox import run_sandboxed
        result = await run_sandboxed("import os", "print('hi')")
        assert result.error is not None
        assert "os" in result.error

    @pytest.mark.asyncio
    async def test_timeout(self, monkeypatch):
        monkeypatch.setattr("dgm.sandbox.DGM_SANDBOX_TIMEOUT_S", 1)
        from dgm.sandbox import run_sandboxed
        agent_code = textwrap.dedent("""\
            import time
            def solve():
                time.sleep(30)
        """)
        # time is not in ALLOWED_MODULES, so it would fail on import check
        # Use a busy loop instead with allowed code
        agent_code = "def solve(): pass"
        test_code = textwrap.dedent("""\
            while True:
                pass
        """)
        result = await run_sandboxed(agent_code, test_code)
        # Should either timeout or exit with error
        assert result.timed_out or result.error is not None

    @pytest.mark.asyncio
    async def test_runtime_error_in_agent(self):
        from dgm.sandbox import run_sandboxed
        agent_code = "def solve(x): return 1 / 0"
        test_code = textwrap.dedent("""\
            _passed = 0
            _total = 1
            try:
                if solve(1) == 1:
                    _passed += 1
            except:
                pass
            print(f"PASSED {_passed}/{_total}")
        """)
        result = await run_sandboxed(agent_code, test_code)
        assert result.passed == 0
        assert result.total == 1


# ===================================================================
# 5. dgm/evaluator.py
# ===================================================================

class TestGenerateTestCode:
    def test_generates_valid_python(self):
        from dgm.evaluator import generate_test_code
        c = Challenge(
            id="test", name="Test", description="d",
            function_name="solve",
            test_cases=[(("hello",), "HELLO")],
        )
        code = generate_test_code(c)
        assert "solve" in code
        assert "PASSED" in code
        assert "_total = 1" in code

    def test_multiple_test_cases(self):
        from dgm.evaluator import generate_test_code
        c = Challenge(
            id="test", name="Test", description="d",
            function_name="add",
            test_cases=[((1, 2), 3), ((0, 0), 0)],
        )
        code = generate_test_code(c)
        assert "_total = 2" in code

    def test_generated_code_is_parseable(self):
        import ast
        from dgm.evaluator import generate_test_code
        c = Challenge(
            id="test", name="Test", description="d",
            function_name="solve",
            test_cases=[(("a",), "b"), ((1,), 2)],
        )
        code = generate_test_code(c)
        # Should parse without error
        ast.parse(code)


class TestEvaluateAgent:
    @pytest.mark.asyncio
    async def test_all_passing(self):
        from dgm.evaluator import evaluate_agent
        agent = Agent(code="def solve(x): return x")
        challenges = [
            Challenge(
                id="c1", name="C1", description="d",
                function_name="solve",
                test_cases=[(("a",), "a")],
                difficulty="easy",
            ),
        ]
        mock_result = SandboxResult(passed=1, total=1, pass_rate=1.0)
        with patch("dgm.evaluator.run_sandboxed", new_callable=AsyncMock, return_value=mock_result):
            fitness, scores = await evaluate_agent(agent, challenges)
        assert fitness == 1.0
        assert scores["c1"] == 1.0

    @pytest.mark.asyncio
    async def test_all_failing(self):
        from dgm.evaluator import evaluate_agent
        agent = Agent(code="def solve(x): return x")
        challenges = [
            Challenge(
                id="c1", name="C1", description="d",
                function_name="solve",
                test_cases=[(("a",), "b")],
                difficulty="easy",
            ),
        ]
        mock_result = SandboxResult(error="Exit code 1")
        with patch("dgm.evaluator.run_sandboxed", new_callable=AsyncMock, return_value=mock_result):
            fitness, scores = await evaluate_agent(agent, challenges)
        assert fitness == 0.0
        assert scores["c1"] == 0.0

    @pytest.mark.asyncio
    async def test_mixed_results_weighted(self):
        from dgm.evaluator import evaluate_agent
        agent = Agent(code="def solve(x): return x")
        challenges = [
            Challenge(
                id="easy1", name="Easy", description="d",
                function_name="solve", test_cases=[(("a",), "a")],
                difficulty="easy",
            ),
            Challenge(
                id="hard1", name="Hard", description="d",
                function_name="solve", test_cases=[(("a",), "a")],
                difficulty="hard",
            ),
        ]
        results = [
            SandboxResult(passed=1, total=1, pass_rate=1.0),  # easy passes
            SandboxResult(passed=0, total=1, pass_rate=0.0),  # hard fails
        ]

        call_count = 0

        async def mock_run(agent_code, test_code):
            nonlocal call_count
            r = results[call_count]
            call_count += 1
            return r

        with patch("dgm.evaluator.run_sandboxed", side_effect=mock_run):
            fitness, scores = await evaluate_agent(agent, challenges)

        # easy weight=1.0, hard weight=3.0
        # total_score = 1.0*1.0 + 0.0*3.0 = 1.0
        # total_weight = 1.0 + 3.0 = 4.0
        # fitness = 1.0 / 4.0 = 0.25
        assert fitness == pytest.approx(0.25)
        assert scores["easy1"] == 1.0
        assert scores["hard1"] == 0.0

    @pytest.mark.asyncio
    async def test_no_challenges(self):
        from dgm.evaluator import evaluate_agent
        agent = Agent(code="pass")
        fitness, scores = await evaluate_agent(agent, [])
        assert fitness == 0.0
        assert scores == {}


# ===================================================================
# 6. dgm/formatter.py
# ===================================================================

class TestAgentEmbed:
    def test_returns_embed(self):
        from dgm.formatter import agent_embed
        a = Agent(fitness=0.5, strategy="test")
        embed = agent_embed(a)
        assert isinstance(embed, discord.Embed)

    def test_color_green_high_fitness(self):
        from dgm.formatter import agent_embed
        a = Agent(fitness=0.8)
        embed = agent_embed(a)
        assert embed.color == discord.Color.green()

    def test_color_gold_medium_fitness(self):
        from dgm.formatter import agent_embed
        a = Agent(fitness=0.5)
        embed = agent_embed(a)
        assert embed.color == discord.Color.gold()

    def test_color_red_low_fitness(self):
        from dgm.formatter import agent_embed
        a = Agent(fitness=0.2)
        embed = agent_embed(a)
        assert embed.color == discord.Color.red()

    def test_color_boundary_07(self):
        from dgm.formatter import agent_embed
        a = Agent(fitness=0.7)
        embed = agent_embed(a)
        assert embed.color == discord.Color.green()

    def test_color_boundary_04(self):
        from dgm.formatter import agent_embed
        a = Agent(fitness=0.4)
        embed = agent_embed(a)
        assert embed.color == discord.Color.gold()

    def test_with_rank(self):
        from dgm.formatter import agent_embed
        a = Agent(fitness=0.5)
        embed = agent_embed(a, rank=1)
        assert "#1" in embed.title

    def test_with_scores(self):
        from dgm.formatter import agent_embed
        a = Agent(fitness=0.5, scores={"c1": 1.0, "c2": 0.0})
        embed = agent_embed(a)
        # scores field exists
        field_names = [f.name for f in embed.fields]
        assert "Scores" in field_names


class TestCodeEmbed:
    def test_returns_embed(self):
        from dgm.formatter import code_embed
        a = Agent(code="def solve(): pass", fitness=0.5)
        embed = code_embed(a)
        assert isinstance(embed, discord.Embed)
        assert "```python" in embed.description

    def test_truncation(self):
        from dgm.formatter import code_embed
        a = Agent(code="x = 1\n" * 2000, fitness=0.5)
        embed = code_embed(a)
        assert "truncated" in embed.description
        assert len(embed.description) < 4100


class TestPopulationEmbed:
    def test_without_run(self):
        from dgm.formatter import population_embed
        agents = [Agent(fitness=0.5), Agent(fitness=0.8)]
        embed = population_embed(agents)
        assert isinstance(embed, discord.Embed)
        assert "Population Status" in embed.title

    def test_with_run(self):
        from dgm.formatter import population_embed
        agents = [Agent(fitness=0.5)]
        run = EvolutionRun(current_generation=2, max_generations=5)
        embed = population_embed(agents, run=run)
        field_names = [f.name for f in embed.fields]
        assert "Evolution" in field_names

    def test_empty_agents(self):
        from dgm.formatter import population_embed
        embed = population_embed([])
        field_values = [f.value for f in embed.fields]
        assert any("No agents" in v for v in field_values)


class TestRunEmbed:
    def test_running_status(self):
        from dgm.formatter import run_embed
        run = EvolutionRun(status="running")
        embed = run_embed(run)
        assert isinstance(embed, discord.Embed)
        assert embed.color == discord.Color.blue()

    def test_completed_status(self):
        from dgm.formatter import run_embed
        run = EvolutionRun(status="completed")
        embed = run_embed(run)
        assert embed.color == discord.Color.green()

    def test_stopped_status(self):
        from dgm.formatter import run_embed
        run = EvolutionRun(status="stopped")
        embed = run_embed(run)
        assert embed.color == discord.Color.orange()

    def test_with_finished_at(self):
        from dgm.formatter import run_embed
        run = EvolutionRun(status="completed", finished_at="2024-01-01T00:00:00")
        embed = run_embed(run)
        field_names = [f.name for f in embed.fields]
        assert "Finished" in field_names

    def test_without_finished_at(self):
        from dgm.formatter import run_embed
        run = EvolutionRun(status="running")
        embed = run_embed(run)
        field_names = [f.name for f in embed.fields]
        assert "Finished" not in field_names


class TestArchiveEmbed:
    def test_basic(self):
        from dgm.formatter import archive_embed
        embed = archive_embed("grid text", 5, 0.3)
        assert isinstance(embed, discord.Embed)
        assert "grid text" in embed.description
        assert embed.color == discord.Color.dark_teal()


class TestLineageText:
    def test_empty(self):
        from dgm.formatter import lineage_text
        assert lineage_text([]) == "No lineage found."

    def test_single_agent(self):
        from dgm.formatter import lineage_text
        a = Agent(generation=0, fitness=0.5)
        text = lineage_text([a])
        assert a.id[:8] in text
        assert "origin" in text

    def test_multiple_agents(self):
        from dgm.formatter import lineage_text
        a1 = Agent(id="parent-1-id", generation=0, fitness=0.3)
        a2 = Agent(generation=1, fitness=0.7, parent_ids=["parent-1-id"])
        text = lineage_text([a2, a1])
        assert "parent-1" in text


# ===================================================================
# 7. dgm/llm.py (helper functions only)
# ===================================================================

class TestExtractCode:
    def test_python_code_block(self):
        from dgm.llm import _extract_code
        text = "Here is code:\n```python\ndef solve(): pass\n```\nDone."
        assert _extract_code(text) == "def solve(): pass"

    def test_generic_code_block(self):
        from dgm.llm import _extract_code
        text = "Here:\n```\ndef solve(): pass\n```"
        assert _extract_code(text) == "def solve(): pass"

    def test_no_code_block(self):
        from dgm.llm import _extract_code
        text = "def solve(): pass"
        assert _extract_code(text) == "def solve(): pass"

    def test_multiline_code_block(self):
        from dgm.llm import _extract_code
        text = "```python\ndef a():\n    return 1\n\ndef b():\n    return 2\n```"
        code = _extract_code(text)
        assert "def a():" in code
        assert "def b():" in code

    def test_prefers_python_block_over_generic(self):
        from dgm.llm import _extract_code
        text = "```python\ndef correct(): pass\n```\n```\ndef wrong(): pass\n```"
        assert "correct" in _extract_code(text)


class TestExtractStrategy:
    def test_with_strategy_line(self):
        from dgm.llm import _extract_strategy
        text = "STRATEGY: Use regex for parsing\n```python\ncode\n```"
        assert _extract_strategy(text) == "Use regex for parsing"

    def test_case_insensitive(self):
        from dgm.llm import _extract_strategy
        text = "Strategy: My approach\ncode"
        assert _extract_strategy(text) == "My approach"

    def test_without_strategy(self):
        from dgm.llm import _extract_strategy
        text = "Just some code\ndef solve(): pass"
        assert _extract_strategy(text, "fallback") == "fallback"

    def test_empty_fallback(self):
        from dgm.llm import _extract_strategy
        assert _extract_strategy("no strategy here") == ""


class TestChallengesSummary:
    def test_formatting(self):
        from dgm.llm import _challenges_summary
        challenges = [
            Challenge(
                id="c1", name="C1", description="Do something",
                function_name="solve",
                test_cases=[(("input",), "output")],
                difficulty="easy",
            ),
        ]
        summary = _challenges_summary(challenges)
        assert "solve" in summary
        assert "Do something" in summary
        assert "easy" in summary
        assert "input" in summary

    def test_empty_challenges(self):
        from dgm.llm import _challenges_summary
        assert _challenges_summary([]) == ""


class TestIdentifyCommonFailures:
    def test_no_failures(self):
        from dgm.llm import _identify_common_failures
        agents = [Agent(scores={"c1": 1.0, "c2": 1.0})]
        challenges = [
            Challenge(id="c1", name="C1", description="d", function_name="f1"),
            Challenge(id="c2", name="C2", description="d", function_name="f2"),
        ]
        result = _identify_common_failures(agents, challenges)
        assert "None" in result

    def test_with_failures(self):
        from dgm.llm import _identify_common_failures
        agents = [
            Agent(scores={"c1": 0.5, "c2": 1.0}),
            Agent(scores={"c1": 0.0, "c2": 0.8}),
        ]
        challenges = [
            Challenge(id="c1", name="C1", description="d", function_name="solve_c1"),
            Challenge(id="c2", name="C2", description="d", function_name="solve_c2"),
        ]
        result = _identify_common_failures(agents, challenges)
        assert "solve_c1" in result
        assert "2/2" in result  # both agents fail c1

    def test_empty_population(self):
        from dgm.llm import _identify_common_failures
        result = _identify_common_failures([], [])
        assert "None" in result


# ===================================================================
# 8. dgm/persistence.py
# ===================================================================

class TestPersistence:
    """Test persistence using a temporary database."""

    @pytest.fixture(autouse=True)
    def setup_tmp_db(self, tmp_path, monkeypatch):
        """Set up a temporary database for each test."""
        import threading
        db_path = str(tmp_path / "test_dgm.db")
        monkeypatch.setattr("dgm.persistence.DGM_DB_PATH", db_path)
        # Clear thread-local connection so it re-creates with new path
        local = None
        import dgm.persistence as pm
        pm._local = threading.local()
        self.db_path = db_path
        self.pm = pm

    def _init_sync(self):
        self.pm._init_db_sync()

    @pytest.mark.asyncio
    async def test_init_db(self):
        await self.pm.init_db()
        # Verify tables exist
        conn = sqlite3.connect(self.db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert "agents" in table_names
        assert "runs" in table_names
        conn.close()

    @pytest.mark.asyncio
    async def test_save_and_load_agent(self):
        await self.pm.init_db()
        agent = Agent(
            id="test-agent-1",
            generation=2,
            parent_ids=["p1"],
            code="def solve(): pass",
            strategy="brute force",
            fitness=0.75,
            scores={"c1": 1.0},
            run_id="run-1",
            niche=(1, 2),
        )
        await self.pm.save_agent(agent)
        loaded = await self.pm.load_agents_by_run("run-1")
        assert len(loaded) == 1
        a = loaded[0]
        assert a.id == "test-agent-1"
        assert a.generation == 2
        assert a.parent_ids == ["p1"]
        assert a.code == "def solve(): pass"
        assert a.strategy == "brute force"
        assert a.fitness == 0.75
        assert a.scores == {"c1": 1.0}
        assert a.run_id == "run-1"
        assert a.niche == (1, 2)

    @pytest.mark.asyncio
    async def test_save_and_load_run(self):
        await self.pm.init_db()
        run = EvolutionRun(
            id="run-1",
            population_size=4,
            max_generations=5,
            current_generation=2,
            best_fitness=0.8,
            status="running",
        )
        await self.pm.save_run(run)
        loaded = await self.pm.load_run("run-1")
        assert loaded is not None
        assert loaded.id == "run-1"
        assert loaded.population_size == 4
        assert loaded.max_generations == 5
        assert loaded.current_generation == 2
        assert loaded.best_fitness == 0.8
        assert loaded.status == "running"

    @pytest.mark.asyncio
    async def test_load_nonexistent_run(self):
        await self.pm.init_db()
        assert await self.pm.load_run("nonexistent") is None

    @pytest.mark.asyncio
    async def test_load_best_agent(self):
        await self.pm.init_db()
        a1 = Agent(id="a1", fitness=0.3, run_id="run-1")
        a2 = Agent(id="a2", fitness=0.9, run_id="run-1")
        a3 = Agent(id="a3", fitness=0.5, run_id="run-1")
        for a in [a1, a2, a3]:
            await self.pm.save_agent(a)
        best = await self.pm.load_best_agent("run-1")
        assert best is not None
        assert best.id == "a2"
        assert best.fitness == 0.9

    @pytest.mark.asyncio
    async def test_load_best_agent_nonexistent_run(self):
        await self.pm.init_db()
        assert await self.pm.load_best_agent("nope") is None

    @pytest.mark.asyncio
    async def test_load_agent_lineage(self):
        await self.pm.init_db()
        grandparent = Agent(id="gp", generation=0, parent_ids=[], run_id="r")
        parent = Agent(id="p", generation=1, parent_ids=["gp"], run_id="r")
        child = Agent(id="c", generation=2, parent_ids=["p"], run_id="r")
        for a in [grandparent, parent, child]:
            await self.pm.save_agent(a)
        lineage = await self.pm.load_agent_lineage("c")
        ids = [a.id for a in lineage]
        assert "c" in ids
        assert "p" in ids
        assert "gp" in ids

    @pytest.mark.asyncio
    async def test_load_agent_lineage_nonexistent(self):
        await self.pm.init_db()
        lineage = await self.pm.load_agent_lineage("nonexistent")
        assert lineage == []

    @pytest.mark.asyncio
    async def test_load_latest_run(self):
        await self.pm.init_db()
        r1 = EvolutionRun(id="r1", started_at="2024-01-01T00:00:00")
        r2 = EvolutionRun(id="r2", started_at="2024-06-01T00:00:00")
        await self.pm.save_run(r1)
        await self.pm.save_run(r2)
        latest = await self.pm.load_latest_run()
        assert latest is not None
        assert latest.id == "r2"

    @pytest.mark.asyncio
    async def test_load_latest_run_empty(self):
        await self.pm.init_db()
        assert await self.pm.load_latest_run() is None

    @pytest.mark.asyncio
    async def test_save_agent_upsert(self):
        await self.pm.init_db()
        agent = Agent(id="a1", fitness=0.3, run_id="r1")
        await self.pm.save_agent(agent)
        agent.fitness = 0.9
        await self.pm.save_agent(agent)
        agents = await self.pm.load_agents_by_run("r1")
        assert len(agents) == 1
        assert agents[0].fitness == 0.9

    @pytest.mark.asyncio
    async def test_load_agents_by_run_empty(self):
        await self.pm.init_db()
        assert await self.pm.load_agents_by_run("empty-run") == []

    @pytest.mark.asyncio
    async def test_save_run_upsert(self):
        await self.pm.init_db()
        run = EvolutionRun(id="r1", status="running")
        await self.pm.save_run(run)
        run.status = "completed"
        run.finished_at = "2024-01-01T12:00:00"
        await self.pm.save_run(run)
        loaded = await self.pm.load_run("r1")
        assert loaded.status == "completed"
        assert loaded.finished_at == "2024-01-01T12:00:00"


# ===================================================================
# 9. dgm/engine.py (helper functions only)
# ===================================================================

class TestTournamentSelect:
    def test_returns_max_fitness(self):
        from dgm.engine import tournament_select
        agents = [Agent(fitness=f) for f in [0.1, 0.5, 0.9, 0.3]]
        # With k=4, samples all, must return best
        winner = tournament_select(agents, k=4)
        assert winner.fitness == 0.9

    def test_single_agent(self):
        from dgm.engine import tournament_select
        a = Agent(fitness=0.5)
        assert tournament_select([a], k=1).fitness == 0.5

    def test_k_larger_than_population(self):
        from dgm.engine import tournament_select
        agents = [Agent(fitness=0.3), Agent(fitness=0.7)]
        winner = tournament_select(agents, k=10)
        assert winner.fitness == 0.7


class TestCurriculumFilter:
    def test_early_easy_only(self):
        from dgm.engine import _curriculum_filter
        challenges = [
            Challenge(id="e", name="E", description="d", function_name="f", difficulty="easy"),
            Challenge(id="m", name="M", description="d", function_name="f", difficulty="medium"),
            Challenge(id="h", name="H", description="d", function_name="f", difficulty="hard"),
        ]
        # generation 0 out of 10 -> progress = 0.0 -> easy only
        result = _curriculum_filter(challenges, 0, 10)
        assert all(c.difficulty == "easy" for c in result)

    def test_mid_easy_medium(self):
        from dgm.engine import _curriculum_filter
        challenges = [
            Challenge(id="e", name="E", description="d", function_name="f", difficulty="easy"),
            Challenge(id="m", name="M", description="d", function_name="f", difficulty="medium"),
            Challenge(id="h", name="H", description="d", function_name="f", difficulty="hard"),
        ]
        # generation 5 out of 10 -> progress = 0.5 -> easy+medium
        result = _curriculum_filter(challenges, 5, 10)
        difficulties = {c.difficulty for c in result}
        assert "easy" in difficulties
        assert "medium" in difficulties
        assert "hard" not in difficulties

    def test_late_all_difficulties(self):
        from dgm.engine import _curriculum_filter
        challenges = [
            Challenge(id="e", name="E", description="d", function_name="f", difficulty="easy"),
            Challenge(id="m", name="M", description="d", function_name="f", difficulty="medium"),
            Challenge(id="h", name="H", description="d", function_name="f", difficulty="hard"),
        ]
        # generation 8 out of 10 -> progress = 0.8 -> all
        result = _curriculum_filter(challenges, 8, 10)
        difficulties = {c.difficulty for c in result}
        assert difficulties == {"easy", "medium", "hard"}

    def test_single_generation_returns_all(self):
        from dgm.engine import _curriculum_filter
        challenges = [
            Challenge(id="h", name="H", description="d", function_name="f", difficulty="hard"),
        ]
        # max_generations=1 -> returns all
        result = _curriculum_filter(challenges, 0, 1)
        assert len(result) == 1

    def test_boundary_030(self):
        from dgm.engine import _curriculum_filter
        challenges = [
            Challenge(id="e", name="E", description="d", function_name="f", difficulty="easy"),
            Challenge(id="m", name="M", description="d", function_name="f", difficulty="medium"),
        ]
        # progress = 3/10 = 0.3 -> still easy only
        result = _curriculum_filter(challenges, 3, 10)
        assert all(c.difficulty == "easy" for c in result)

    def test_boundary_060(self):
        from dgm.engine import _curriculum_filter
        challenges = [
            Challenge(id="e", name="E", description="d", function_name="f", difficulty="easy"),
            Challenge(id="m", name="M", description="d", function_name="f", difficulty="medium"),
            Challenge(id="h", name="H", description="d", function_name="f", difficulty="hard"),
        ]
        # progress = 6/10 = 0.6 -> easy+medium
        result = _curriculum_filter(challenges, 6, 10)
        difficulties = {c.difficulty for c in result}
        assert "hard" not in difficulties

    def test_fallback_when_no_match(self):
        from dgm.engine import _curriculum_filter
        challenges = [
            Challenge(id="h", name="H", description="d", function_name="f", difficulty="hard"),
        ]
        # Early: easy only, but no easy challenges -> fallback to all
        result = _curriculum_filter(challenges, 0, 10)
        assert len(result) == 1


class TestComputeMutationRate:
    def test_short_history(self):
        from dgm.engine import _compute_mutation_rate
        assert _compute_mutation_rate([0.5]) == 0.5
        assert _compute_mutation_rate([]) == 0.5

    def test_stagnating(self):
        from dgm.engine import _compute_mutation_rate
        # No improvement -> increase mutation
        history = [0.5, 0.5, 0.505]
        rate = _compute_mutation_rate(history, base_rate=0.5)
        assert rate == pytest.approx(0.7)

    def test_improving(self):
        from dgm.engine import _compute_mutation_rate
        # Big improvement -> decrease mutation (more crossover)
        history = [0.3, 0.4, 0.5]
        rate = _compute_mutation_rate(history, base_rate=0.5)
        # improvement = 0.5 - 0.3 = 0.2 > 0.05
        assert rate == pytest.approx(0.3)

    def test_capped_at_max(self):
        from dgm.engine import _compute_mutation_rate
        # Stagnating with already high base rate
        history = [0.5, 0.5, 0.5]
        rate = _compute_mutation_rate(history, base_rate=0.8)
        assert rate <= 0.9

    def test_capped_at_min(self):
        from dgm.engine import _compute_mutation_rate
        # Improving with already low base rate
        history = [0.1, 0.2, 0.3]
        rate = _compute_mutation_rate(history, base_rate=0.3)
        assert rate >= 0.2

    def test_exactly_two_items(self):
        from dgm.engine import _compute_mutation_rate
        # len=2, recent = last 3 -> [0.5, 0.5], improvement = 0.0 < 0.01 -> stagnate
        history = [0.5, 0.5]
        rate = _compute_mutation_rate(history, base_rate=0.5)
        assert rate == pytest.approx(0.7)


class TestEngineState:
    def test_is_running_default(self, monkeypatch):
        import dgm.engine as engine
        monkeypatch.setattr(engine, "_current_run", None)
        assert engine.is_running() is False

    def test_is_running_with_running_run(self, monkeypatch):
        import dgm.engine as engine
        run = EvolutionRun(status="running")
        monkeypatch.setattr(engine, "_current_run", run)
        assert engine.is_running() is True

    def test_is_running_with_completed_run(self, monkeypatch):
        import dgm.engine as engine
        run = EvolutionRun(status="completed")
        monkeypatch.setattr(engine, "_current_run", run)
        assert engine.is_running() is False

    def test_get_current_run(self, monkeypatch):
        import dgm.engine as engine
        monkeypatch.setattr(engine, "_current_run", None)
        assert engine.get_current_run() is None
        run = EvolutionRun()
        monkeypatch.setattr(engine, "_current_run", run)
        assert engine.get_current_run() is run

    def test_get_archive(self, monkeypatch):
        import dgm.engine as engine
        monkeypatch.setattr(engine, "_current_archive", None)
        assert engine.get_archive() is None

    def test_request_stop(self, monkeypatch):
        import dgm.engine as engine
        event = asyncio.Event()
        monkeypatch.setattr(engine, "_stop_event", event)
        assert not event.is_set()
        engine.request_stop()
        assert event.is_set()

    def test_request_stop_no_event(self, monkeypatch):
        import dgm.engine as engine
        monkeypatch.setattr(engine, "_stop_event", None)
        # Should not raise
        engine.request_stop()
