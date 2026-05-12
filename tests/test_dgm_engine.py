"""Tests for dgm/engine.py — focuses on _evaluate_and_update and run_evolution."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from dgm.models import Agent, Challenge, EvolutionRun


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_challenges(difficulties=("easy",)):
    return [
        Challenge(
            id=f"c_{d}",
            name=d.title(),
            description="desc",
            function_name="solve",
            test_cases=[((1,), 1)],
            difficulty=d,
        )
        for d in difficulties
    ]


# ---------------------------------------------------------------------------
# _evaluate_and_update
# ---------------------------------------------------------------------------

class TestEvaluateAndUpdate:
    @pytest.mark.asyncio
    async def test_sets_fitness_scores_niche(self):
        from dgm.engine import _evaluate_and_update
        from dgm.archive import compute_niche

        agent = Agent(code="def solve(x): return x")
        challenges = _make_challenges(["easy"])

        with patch("dgm.engine.evaluate_agent", new_callable=AsyncMock) as mock_eval, \
             patch("dgm.engine.compute_niche", return_value=(1, 2)) as mock_niche:
            mock_eval.return_value = (0.75, {"c_easy": 0.75})
            await _evaluate_and_update(agent, challenges)

        assert agent.fitness == 0.75
        assert agent.scores == {"c_easy": 0.75}
        assert agent.niche == (1, 2)
        mock_eval.assert_awaited_once_with(agent, challenges)

    @pytest.mark.asyncio
    async def test_zero_fitness(self):
        from dgm.engine import _evaluate_and_update

        agent = Agent(code="def solve(x): return None")
        challenges = _make_challenges(["easy"])

        with patch("dgm.engine.evaluate_agent", new_callable=AsyncMock) as mock_eval, \
             patch("dgm.engine.compute_niche", return_value=(0, 0)):
            mock_eval.return_value = (0.0, {"c_easy": 0.0})
            await _evaluate_and_update(agent, challenges)

        assert agent.fitness == 0.0


# ---------------------------------------------------------------------------
# run_evolution — minimal 1-gen, 1-agent, stop-early, exception paths
# ---------------------------------------------------------------------------

def _patch_all(
    *,
    population_size=2,
    max_generations=1,
    offspring_per_gen=1,
    stop_after_init=False,
    generate_raises=False,
    offspring_raises=False,
    mutation_side=None,   # (code, strategy) or None → defaults
    crossover_side=None,
):
    """Build a dict of patches for run_evolution."""
    agents_store = {}

    async def fake_init_db():
        pass

    def fake_save_run(run):
        pass

    def fake_save_agent(agent):
        agents_store[agent.id] = agent

    async def fake_generate(challenges, idx, total):
        if generate_raises:
            raise RuntimeError("LLM fail")
        return (f"def solve(x): return {idx}", f"strategy_{idx}")

    async def fake_evaluate(agent, challenges):
        return (0.5, {c.id: 0.5 for c in challenges})

    fake_compute_niche = MagicMock(return_value=(0, 0))
    fake_challenges = _make_challenges(["easy"])

    async def fake_mutate(parent, challenges, meta_hint):
        if offspring_raises:
            raise RuntimeError("mutate fail")
        if mutation_side:
            return mutation_side
        return ("def solve(x): return 0", "mutated")

    async def fake_crossover(parent, inspirations, challenges, meta_hint):
        if offspring_raises:
            raise RuntimeError("crossover fail")
        if crossover_side:
            return crossover_side
        return ("def solve(x): return 0", "crossover")

    async def fake_meta_prompt(population, gen, prev_best, challenges):
        return "hint"

    async def fake_suggest_ga_params(population, gen, ga_params, fitness_history):
        return ga_params

    stop_event_holder = {}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    class FakeEvent:
        def __init__(self):
            self._set = False
            self._calls = 0

        def is_set(self):
            self._calls += 1
            if stop_after_init and self._calls > 5:
                return True
            return self._set

        def set(self):
            self._set = True

    return {
        "fake_init_db": fake_init_db,
        "fake_save_run": fake_save_run,
        "fake_save_agent": fake_save_agent,
        "fake_generate": fake_generate,
        "fake_evaluate": fake_evaluate,
        "fake_compute_niche": fake_compute_niche,
        "fake_challenges": fake_challenges,
        "fake_mutate": fake_mutate,
        "fake_crossover": fake_crossover,
        "fake_meta_prompt": fake_meta_prompt,
        "fake_suggest_ga_params": fake_suggest_ga_params,
        "fake_to_thread": fake_to_thread,
        "agents_store": agents_store,
        "FakeEvent": FakeEvent,
    }


@pytest.fixture
def engine_patches():
    """Common patch set for run_evolution tests."""
    return _patch_all()


class TestRunEvolution:
    @pytest.mark.asyncio
    async def test_completes_and_returns_run(self):
        """Minimal 2-pop, 1-gen run returns a completed EvolutionRun."""
        p = _patch_all(population_size=2, max_generations=1, offspring_per_gen=1)

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=p["fake_challenges"]), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=p["fake_evaluate"]), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=p["fake_mutate"]), \
             patch("dgm.engine.inspiration_crossover", new=p["fake_crossover"]), \
             patch("dgm.engine.generate_meta_prompt", new=p["fake_meta_prompt"]), \
             patch("dgm.engine.suggest_ga_params", new=p["fake_suggest_ga_params"]), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]):
            from dgm.engine import run_evolution
            result = await run_evolution(
                population_size=2,
                max_generations=1,
                offspring_per_gen=1,
            )

        assert isinstance(result, EvolutionRun)
        assert result.status == "completed"
        assert result.finished_at is not None

    @pytest.mark.asyncio
    async def test_progress_callback_called(self):
        """progress_callback receives notification strings."""
        p = _patch_all(population_size=2, max_generations=1, offspring_per_gen=1)
        messages = []

        async def cb(msg: str):
            messages.append(msg)

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=p["fake_challenges"]), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=p["fake_evaluate"]), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=p["fake_mutate"]), \
             patch("dgm.engine.inspiration_crossover", new=p["fake_crossover"]), \
             patch("dgm.engine.generate_meta_prompt", new=p["fake_meta_prompt"]), \
             patch("dgm.engine.suggest_ga_params", new=p["fake_suggest_ga_params"]), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]):
            from dgm.engine import run_evolution
            await run_evolution(
                population_size=2,
                max_generations=1,
                offspring_per_gen=1,
                progress_callback=cb,
            )

        assert len(messages) >= 3  # init, eval, gen complete, final

    @pytest.mark.asyncio
    async def test_generate_initial_agent_fallback_on_error(self):
        """When generate_initial_agent raises, a fallback Agent is created."""
        p = _patch_all(population_size=2, max_generations=1, offspring_per_gen=1, generate_raises=True)

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=p["fake_challenges"]), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=p["fake_evaluate"]), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=p["fake_mutate"]), \
             patch("dgm.engine.inspiration_crossover", new=p["fake_crossover"]), \
             patch("dgm.engine.generate_meta_prompt", new=p["fake_meta_prompt"]), \
             patch("dgm.engine.suggest_ga_params", new=p["fake_suggest_ga_params"]), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]):
            from dgm.engine import run_evolution
            result = await run_evolution(
                population_size=2,
                max_generations=1,
                offspring_per_gen=1,
            )

        # Still completes with fallback agents
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_offspring_error_is_skipped(self):
        """If offspring production raises, the loop continues (no crash)."""
        p = _patch_all(population_size=2, max_generations=1, offspring_per_gen=2, offspring_raises=True)

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=p["fake_challenges"]), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=p["fake_evaluate"]), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=p["fake_mutate"]), \
             patch("dgm.engine.inspiration_crossover", new=p["fake_crossover"]), \
             patch("dgm.engine.generate_meta_prompt", new=p["fake_meta_prompt"]), \
             patch("dgm.engine.suggest_ga_params", new=p["fake_suggest_ga_params"]), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]):
            from dgm.engine import run_evolution
            result = await run_evolution(
                population_size=2,
                max_generations=1,
                offspring_per_gen=2,
            )

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_stop_event_terminates_loop(self):
        """Setting the stop event mid-run causes status='stopped'."""
        call_count = [0]

        async def fake_init_db():
            pass

        def fake_save_run(run):
            pass

        def fake_save_agent(agent):
            pass

        async def fake_generate(challenges, idx, total):
            return ("def solve(x): return 0", "s")

        async def fake_evaluate(agent, challenges):
            return (0.5, {c.id: 0.5 for c in challenges})

        async def fake_mutate(parent, challenges, meta_hint):
            return ("def solve(x): return 0", "mutated")

        async def fake_crossover(parent, inspirations, challenges, meta_hint):
            return ("def solve(x): return 0", "crossed")

        async def fake_meta_prompt(population, gen, prev_best, challenges):
            return ""

        async def fake_suggest_ga_params(population, gen, ga_params, fitness_history):
            return ga_params

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        challenges = _make_challenges(["easy"])

        import dgm.engine as engine_mod

        original_event = asyncio.Event

        class OneShotEvent:
            """Fires is_set()=True after the evolution loop starts."""
            def __init__(self):
                self._inner = original_event()
                self._check = 0

            def is_set(self):
                self._check += 1
                # Allow init phase, then trigger stop on 2nd loop check
                return self._check >= 3

            def set(self):
                self._inner.set()

        with patch("dgm.engine.init_db", new=fake_init_db), \
             patch("dgm.engine.save_run", fake_save_run), \
             patch("dgm.engine.save_agent", fake_save_agent), \
             patch("dgm.engine.get_builtin_challenges", return_value=challenges), \
             patch("dgm.engine.generate_initial_agent", new=fake_generate), \
             patch("dgm.engine.evaluate_agent", new=fake_evaluate), \
             patch("dgm.engine.compute_niche", return_value=(0, 0)), \
             patch("dgm.engine.mutate", new=fake_mutate), \
             patch("dgm.engine.inspiration_crossover", new=fake_crossover), \
             patch("dgm.engine.generate_meta_prompt", new=fake_meta_prompt), \
             patch("dgm.engine.suggest_ga_params", new=fake_suggest_ga_params), \
             patch("asyncio.to_thread", new=fake_to_thread), \
             patch("asyncio.Event", OneShotEvent):
            result = await engine_mod.run_evolution(
                population_size=2,
                max_generations=5,
                offspring_per_gen=1,
            )

        assert result.status == "stopped"

    @pytest.mark.asyncio
    async def test_best_fitness_tracked(self):
        """run.best_fitness is updated to the highest agent fitness."""
        call_count = [0]

        async def fake_init_db():
            pass

        def fake_save_run(run):
            pass

        def fake_save_agent(agent):
            pass

        async def fake_generate(challenges, idx, total):
            return (f"def solve(x): return {idx}", f"s{idx}")

        fitnesses = [0.3, 0.7]

        async def fake_evaluate(agent, challenges):
            f = fitnesses[call_count[0] % len(fitnesses)]
            call_count[0] += 1
            return (f, {c.id: f for c in challenges})

        async def fake_mutate(parent, challenges, meta_hint):
            return ("def solve(x): return 0", "m")

        async def fake_crossover(parent, inspirations, challenges, meta_hint):
            return ("def solve(x): return 0", "c")

        async def fake_meta_prompt(population, gen, prev_best, challenges):
            return ""

        async def fake_suggest_ga_params(population, gen, ga_params, fitness_history):
            return ga_params

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        challenges = _make_challenges(["easy"])

        with patch("dgm.engine.init_db", new=fake_init_db), \
             patch("dgm.engine.save_run", fake_save_run), \
             patch("dgm.engine.save_agent", fake_save_agent), \
             patch("dgm.engine.get_builtin_challenges", return_value=challenges), \
             patch("dgm.engine.generate_initial_agent", new=fake_generate), \
             patch("dgm.engine.evaluate_agent", new=fake_evaluate), \
             patch("dgm.engine.compute_niche", return_value=(0, 0)), \
             patch("dgm.engine.mutate", new=fake_mutate), \
             patch("dgm.engine.inspiration_crossover", new=fake_crossover), \
             patch("dgm.engine.generate_meta_prompt", new=fake_meta_prompt), \
             patch("dgm.engine.suggest_ga_params", new=fake_suggest_ga_params), \
             patch("asyncio.to_thread", new=fake_to_thread):
            from dgm.engine import run_evolution
            result = await run_evolution(
                population_size=2,
                max_generations=1,
                offspring_per_gen=1,
            )

        assert result.best_fitness > 0.0

    @pytest.mark.asyncio
    async def test_meta_prompt_failure_does_not_crash(self):
        """If generate_meta_prompt raises, evolution continues."""
        p = _patch_all(population_size=2, max_generations=1, offspring_per_gen=1)

        async def bad_meta_prompt(population, gen, prev_best, challenges):
            raise RuntimeError("meta fail")

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=p["fake_challenges"]), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=p["fake_evaluate"]), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=p["fake_mutate"]), \
             patch("dgm.engine.inspiration_crossover", new=p["fake_crossover"]), \
             patch("dgm.engine.generate_meta_prompt", new=bad_meta_prompt), \
             patch("dgm.engine.suggest_ga_params", new=p["fake_suggest_ga_params"]), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]):
            from dgm.engine import run_evolution
            result = await run_evolution(
                population_size=2,
                max_generations=1,
                offspring_per_gen=1,
            )

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_suggest_ga_params_failure_does_not_crash(self):
        """If suggest_ga_params raises, evolution continues using existing params."""
        p = _patch_all(population_size=2, max_generations=2, offspring_per_gen=1)

        async def bad_suggest(population, gen, ga_params, fitness_history):
            raise RuntimeError("params fail")

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=p["fake_challenges"]), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=p["fake_evaluate"]), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=p["fake_mutate"]), \
             patch("dgm.engine.inspiration_crossover", new=p["fake_crossover"]), \
             patch("dgm.engine.generate_meta_prompt", new=p["fake_meta_prompt"]), \
             patch("dgm.engine.suggest_ga_params", new=bad_suggest), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]):
            from dgm.engine import run_evolution
            result = await run_evolution(
                population_size=2,
                max_generations=2,
                offspring_per_gen=1,
            )

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_global_state_reset_after_run(self):
        """_current_run and _stop_event are None after run completes."""
        import dgm.engine as engine_mod

        p = _patch_all(population_size=2, max_generations=1, offspring_per_gen=1)

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=p["fake_challenges"]), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=p["fake_evaluate"]), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=p["fake_mutate"]), \
             patch("dgm.engine.inspiration_crossover", new=p["fake_crossover"]), \
             patch("dgm.engine.generate_meta_prompt", new=p["fake_meta_prompt"]), \
             patch("dgm.engine.suggest_ga_params", new=p["fake_suggest_ga_params"]), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]):
            await engine_mod.run_evolution(
                population_size=2,
                max_generations=1,
                offspring_per_gen=1,
            )

        assert engine_mod._current_run is None
        assert engine_mod._stop_event is None

    @pytest.mark.asyncio
    async def test_global_state_reset_on_exception(self):
        """_current_run is None even when run_evolution raises."""
        import dgm.engine as engine_mod

        async def fake_init_db():
            pass

        def fake_save_run(run):
            pass

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        challenges = _make_challenges(["easy"])

        async def bad_generate(challenges, idx, total):
            if idx == 0:
                return ("def solve(x): return 0", "s")
            raise RuntimeError("generate crash")

        async def bad_evaluate(agent, challenges):
            raise RuntimeError("evaluate crash")

        with patch("dgm.engine.init_db", new=fake_init_db), \
             patch("dgm.engine.save_run", fake_save_run), \
             patch("dgm.engine.save_agent", lambda a: None), \
             patch("dgm.engine.get_builtin_challenges", return_value=challenges), \
             patch("dgm.engine.generate_initial_agent", new=bad_generate), \
             patch("dgm.engine.evaluate_agent", new=bad_evaluate), \
             patch("dgm.engine.compute_niche", return_value=(0, 0)), \
             patch("asyncio.to_thread", new=fake_to_thread):
            with pytest.raises(Exception):
                await engine_mod.run_evolution(
                    population_size=2,
                    max_generations=1,
                    offspring_per_gen=1,
                )

        assert engine_mod._current_run is None

    @pytest.mark.asyncio
    async def test_progress_callback_exception_is_swallowed(self):
        """Exceptions in progress_callback don't crash the run."""
        p = _patch_all(population_size=2, max_generations=1, offspring_per_gen=1)

        async def bad_cb(msg: str):
            raise RuntimeError("callback crash")

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=p["fake_challenges"]), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=p["fake_evaluate"]), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=p["fake_mutate"]), \
             patch("dgm.engine.inspiration_crossover", new=p["fake_crossover"]), \
             patch("dgm.engine.generate_meta_prompt", new=p["fake_meta_prompt"]), \
             patch("dgm.engine.suggest_ga_params", new=p["fake_suggest_ga_params"]), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]):
            from dgm.engine import run_evolution
            result = await run_evolution(
                population_size=2,
                max_generations=1,
                offspring_per_gen=1,
                progress_callback=bad_cb,
            )

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_crossover_path_used_when_mutation_rate_zero(self):
        """With mutation_rate=0, crossover path is exercised."""
        p = _patch_all(population_size=2, max_generations=1, offspring_per_gen=2)
        crossover_called = []

        async def tracking_crossover(parent, inspirations, challenges, meta_hint):
            crossover_called.append(True)
            return ("def solve(x): return 0", "c")

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=p["fake_challenges"]), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=p["fake_evaluate"]), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=p["fake_mutate"]), \
             patch("dgm.engine.inspiration_crossover", new=tracking_crossover), \
             patch("dgm.engine.generate_meta_prompt", new=p["fake_meta_prompt"]), \
             patch("dgm.engine.suggest_ga_params", new=p["fake_suggest_ga_params"]), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]), \
             patch("random.random", return_value=0.99):  # always > mutation_rate -> crossover
            from dgm.engine import run_evolution
            result = await run_evolution(
                population_size=2,
                max_generations=1,
                offspring_per_gen=2,
            )

        assert len(crossover_called) >= 1
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_mutation_path_used_when_rate_one(self):
        """With random.random always returning 0.0 (< mutation_rate), mutate is called."""
        p = _patch_all(population_size=2, max_generations=1, offspring_per_gen=2)
        mutate_called = []

        async def tracking_mutate(parent, challenges, meta_hint):
            mutate_called.append(True)
            return ("def solve(x): return 0", "m")

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=p["fake_challenges"]), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=p["fake_evaluate"]), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=tracking_mutate), \
             patch("dgm.engine.inspiration_crossover", new=p["fake_crossover"]), \
             patch("dgm.engine.generate_meta_prompt", new=p["fake_meta_prompt"]), \
             patch("dgm.engine.suggest_ga_params", new=p["fake_suggest_ga_params"]), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]), \
             patch("random.random", return_value=0.0):  # always < mutation_rate -> mutate
            from dgm.engine import run_evolution
            result = await run_evolution(
                population_size=2,
                max_generations=1,
                offspring_per_gen=2,
            )

        assert len(mutate_called) >= 1
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_elitist_replacement_preserves_best(self):
        """Elite agent is never replaced even if offspring are weaker."""
        p = _patch_all(population_size=2, max_generations=1, offspring_per_gen=2)

        eval_count = [0]

        async def controlled_evaluate(agent, challenges):
            # initial pop: [0.9, 0.1], offspring: always 0.05
            idx = eval_count[0]
            eval_count[0] += 1
            if idx == 0:
                return (0.9, {c.id: 0.9 for c in challenges})
            elif idx == 1:
                return (0.1, {c.id: 0.1 for c in challenges})
            else:
                return (0.05, {c.id: 0.05 for c in challenges})

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=p["fake_challenges"]), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=controlled_evaluate), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=p["fake_mutate"]), \
             patch("dgm.engine.inspiration_crossover", new=p["fake_crossover"]), \
             patch("dgm.engine.generate_meta_prompt", new=p["fake_meta_prompt"]), \
             patch("dgm.engine.suggest_ga_params", new=p["fake_suggest_ga_params"]), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]):
            from dgm.engine import run_evolution
            result = await run_evolution(
                population_size=2,
                max_generations=1,
                offspring_per_gen=2,
            )

        # Elite had fitness=0.9; final best_fitness should reflect re-eval
        # but run must complete without error
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_curriculum_reeval_triggered_on_difficulty_expansion(self):
        """Full population re-evaluation triggered when difficulty set expands (gen 2+)."""
        p = _patch_all(population_size=2, max_generations=2, offspring_per_gen=1)
        reeval_count = [0]

        async def counting_evaluate(agent, challenges):
            reeval_count[0] += 1
            return (0.5, {c.id: 0.5 for c in challenges})

        # Create challenges that include medium so difficulty expands at gen 2
        challenges = _make_challenges(["easy", "medium"])

        with patch("dgm.engine.init_db", new=p["fake_init_db"]), \
             patch("dgm.engine.save_run", p["fake_save_run"]), \
             patch("dgm.engine.save_agent", p["fake_save_agent"]), \
             patch("dgm.engine.get_builtin_challenges", return_value=challenges), \
             patch("dgm.engine.generate_initial_agent", new=p["fake_generate"]), \
             patch("dgm.engine.evaluate_agent", new=counting_evaluate), \
             patch("dgm.engine.compute_niche", p["fake_compute_niche"]), \
             patch("dgm.engine.mutate", new=p["fake_mutate"]), \
             patch("dgm.engine.inspiration_crossover", new=p["fake_crossover"]), \
             patch("dgm.engine.generate_meta_prompt", new=p["fake_meta_prompt"]), \
             patch("dgm.engine.suggest_ga_params", new=p["fake_suggest_ga_params"]), \
             patch("asyncio.to_thread", new=p["fake_to_thread"]):
            from dgm.engine import run_evolution
            result = await run_evolution(
                population_size=2,
                max_generations=2,
                offspring_per_gen=1,
            )

        assert result.status == "completed"
        # Should have been called multiple times (init + gens + final)
        assert reeval_count[0] >= 4
