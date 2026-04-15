"""DGM v2 — Genetic algorithm engine with MAP-Elites, curriculum, adaptive rates, self-referential improvement."""

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Callable, Awaitable

from config import (
    DGM_POPULATION_SIZE,
    DGM_MAX_GENERATIONS,
    DGM_OFFSPRING_PER_GEN,
    DGM_TOURNAMENT_K,
)
from dgm.models import Agent, Challenge, EvolutionRun
from dgm.challenges import get_builtin_challenges
from dgm.evaluator import evaluate_agent
from dgm.llm import (
    generate_initial_agent,
    inspiration_crossover,
    mutate,
    generate_meta_prompt,
    suggest_ga_params,
)
from dgm.archive import MAPElitesArchive, compute_niche
from dgm.persistence import save_agent, save_run, init_db

log = logging.getLogger(__name__)

# Module-level evolution state
_stop_event: asyncio.Event | None = None
_current_run: EvolutionRun | None = None
_current_archive: MAPElitesArchive | None = None


def is_running() -> bool:
    return _current_run is not None and _current_run.status == "running"


def get_current_run() -> EvolutionRun | None:
    return _current_run


def get_archive() -> MAPElitesArchive | None:
    return _current_archive


def request_stop():
    if _stop_event:
        _stop_event.set()


def tournament_select(population: list[Agent], k: int) -> Agent:
    contestants = random.sample(population, min(k, len(population)))
    return max(contestants, key=lambda a: a.fitness)


# ---------------------------------------------------------------------------
# Curriculum learning — gradual difficulty increase
# ---------------------------------------------------------------------------

def _curriculum_filter(challenges: list[Challenge], generation: int, max_generations: int) -> list[Challenge]:
    """Filter challenges based on generation progress.

    Early: easy only → Mid: easy+medium → Late: all including hard.
    """
    if max_generations <= 1:
        return challenges

    progress = generation / max_generations  # 0.0 to 1.0

    if progress <= 0.3:
        allowed = {"easy"}
    elif progress <= 0.6:
        allowed = {"easy", "medium"}
    else:
        allowed = {"easy", "medium", "hard"}

    filtered = [c for c in challenges if c.difficulty in allowed]
    return filtered if filtered else challenges


# ---------------------------------------------------------------------------
# Adaptive mutation rate — explore when stagnating, exploit when improving
# ---------------------------------------------------------------------------

def _compute_mutation_rate(fitness_history: list[float], base_rate: float = 0.5) -> float:
    """Adjust mutation vs crossover ratio based on fitness trajectory.

    Returns mutation probability (0.0 = all crossover, 1.0 = all mutation).
    """
    if len(fitness_history) < 2:
        return base_rate

    recent = fitness_history[-3:]
    if len(recent) >= 2:
        improvement = recent[-1] - recent[0]
        if improvement < 0.01:
            # Stagnating: increase mutation (exploration)
            return min(0.9, base_rate + 0.2)
        elif improvement > 0.05:
            # Improving well: more crossover (exploitation)
            return max(0.2, base_rate - 0.2)

    return base_rate


# ---------------------------------------------------------------------------
# Core evaluation helper
# ---------------------------------------------------------------------------

async def _evaluate_and_update(agent: Agent, challenges: list[Challenge]) -> None:
    fitness, scores = await evaluate_agent(agent, challenges)
    agent.fitness = fitness
    agent.scores = scores
    agent.niche = compute_niche(agent)


# ---------------------------------------------------------------------------
# Main evolution loop
# ---------------------------------------------------------------------------

async def run_evolution(
    population_size: int = DGM_POPULATION_SIZE,
    max_generations: int = DGM_MAX_GENERATIONS,
    offspring_per_gen: int = DGM_OFFSPRING_PER_GEN,
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
) -> EvolutionRun:
    global _stop_event, _current_run, _current_archive

    # init_db is async (it dispatches its own to_thread internally) — wrapping
    # it again with asyncio.to_thread leaks an unawaited coroutine.
    await init_db()

    _stop_event = asyncio.Event()
    archive = MAPElitesArchive()
    _current_archive = archive

    run = EvolutionRun(
        population_size=population_size,
        max_generations=max_generations,
    )
    _current_run = run
    await asyncio.to_thread(save_run, run)

    all_challenges = get_builtin_challenges()
    fitness_history: list[float] = []
    meta_hint = ""

    # Adaptive GA params (can be modified by self-referential improvement)
    ga_params = {
        "mutation_rate": 0.5,
        "offspring_count": offspring_per_gen,
        "tournament_k": DGM_TOURNAMENT_K,
    }

    async def notify(msg: str):
        log.info(msg)
        if progress_callback:
            try:
                await progress_callback(msg)
            except Exception:
                pass

    try:
        # === Phase 1: Generate initial population ===
        await notify(f"🧬 初期個体群を生成中 ({population_size}体)")
        population: list[Agent] = []

        for i in range(population_size):
            if _stop_event.is_set():
                break
            try:
                code, strategy = await generate_initial_agent(all_challenges, i, population_size)
                agent = Agent(generation=0, code=code, strategy=strategy, run_id=run.id)
                population.append(agent)
            except Exception as e:
                log.error("Failed to generate initial agent %d: %s", i, e)
                agent = Agent(
                    generation=0,
                    code="def solve(problem): return ''",
                    strategy=f"Fallback agent ({e})",
                    run_id=run.id,
                )
                population.append(agent)

        # === Phase 2: Evaluate initial population (curriculum: easy only) ===
        await notify("📊 初期個体群を評価中 (カリキュラム: easy)")
        init_challenges = _curriculum_filter(all_challenges, 0, max_generations)
        for agent in population:
            await _evaluate_and_update(agent, init_challenges)
            archive.try_add(agent)
            await asyncio.to_thread(save_agent, agent)

        best = max(population, key=lambda a: a.fitness)
        run.best_fitness = best.fitness
        fitness_history.append(best.fitness)
        await asyncio.to_thread(save_run, run)
        await notify(
            f"📊 初期評価完了 — 最高: {best.fitness:.1%} | "
            f"アーカイブ: {archive.size}セル ({archive.coverage:.0%})"
        )

        # === Phase 3: Evolution loop ===
        for gen in range(1, max_generations + 1):
            if _stop_event.is_set():
                await notify("⛔ 進化が停止されました")
                break

            run.current_generation = gen
            await asyncio.to_thread(save_run, run)

            # --- Curriculum: select challenges for this generation ---
            gen_challenges = _curriculum_filter(all_challenges, gen, max_generations)
            difficulty_set = sorted({c.difficulty for c in gen_challenges})
            await notify(f"🔄 世代 {gen}/{max_generations} 開始 (難易度: {'+'.join(difficulty_set)})")

            # --- Meta-prompting: analyze and generate strategic hints ---
            prev_best = fitness_history[-1] if fitness_history else 0.0
            try:
                meta_hint = await generate_meta_prompt(
                    population, gen, prev_best, gen_challenges
                )
            except Exception as e:
                log.warning("Meta-prompt failed: %s", e)
                meta_hint = ""

            # --- Self-referential improvement: adjust GA params ---
            if gen >= 2:
                try:
                    ga_params = await suggest_ga_params(
                        population, gen, ga_params, fitness_history
                    )
                    log.info("GA params adjusted: %s", ga_params)
                except Exception as e:
                    log.warning("GA param suggestion failed: %s", e)

            # --- Adaptive mutation rate ---
            mutation_rate = _compute_mutation_rate(fitness_history, ga_params["mutation_rate"])
            effective_offspring = ga_params["offspring_count"]
            effective_k = ga_params["tournament_k"]

            children: list[Agent] = []

            for child_idx in range(effective_offspring):
                if _stop_event.is_set():
                    break

                try:
                    use_mutation = random.random() < mutation_rate

                    if use_mutation:
                        # --- Mutation (Haiku, diverse) ---
                        parent = tournament_select(population, effective_k)
                        code, strategy = await mutate(parent, gen_challenges, meta_hint)
                        child = Agent(
                            generation=gen,
                            parent_ids=[parent.id],
                            code=code,
                            strategy=strategy,
                            run_id=run.id,
                        )
                    else:
                        # --- Inspiration-based Crossover (Opus, quality) ---
                        parent = tournament_select(population, effective_k)
                        inspirations = archive.sample_for_inspiration(3)
                        # Don't include parent in inspirations
                        inspirations = [i for i in inspirations if i.id != parent.id]
                        if not inspirations:
                            inspirations = [a for a in population if a.id != parent.id][:2]
                        code, strategy = await inspiration_crossover(
                            parent, inspirations, gen_challenges, meta_hint
                        )
                        child = Agent(
                            generation=gen,
                            parent_ids=[parent.id] + [i.id for i in inspirations],
                            code=code,
                            strategy=strategy,
                            run_id=run.id,
                        )

                    # Evaluate child
                    await _evaluate_and_update(child, gen_challenges)
                    archive.try_add(child)
                    await asyncio.to_thread(save_agent, child)
                    children.append(child)
                    log.info(
                        "Gen %d child %d: fitness=%.3f niche=%s (%s)",
                        gen, child_idx, child.fitness, child.niche, child.id[:8],
                    )

                except Exception as e:
                    log.error("Failed to produce offspring %d in gen %d: %s", child_idx, gen, e)

            # --- Replacement: elitist + archive-aware ---
            if children:
                population.sort(key=lambda a: a.fitness)
                elite = population[-1]

                for child in children:
                    if child.fitness > population[0].fitness:
                        population[0] = child
                        population.sort(key=lambda a: a.fitness)

                # Ensure elite survives
                if elite.id not in {a.id for a in population}:
                    population[0] = elite
                    population.sort(key=lambda a: a.fitness)

                # Re-evaluate full population on current curriculum if difficulty changed
                if gen > 1 and len(gen_challenges) > len(_curriculum_filter(all_challenges, gen - 1, max_generations)):
                    await notify("📊 カリキュラム拡張 — 全個体を再評価中")
                    for agent in population:
                        await _evaluate_and_update(agent, gen_challenges)
                        archive.try_add(agent)
                        await asyncio.to_thread(save_agent, agent)

            best = max(population, key=lambda a: a.fitness)
            run.best_fitness = best.fitness
            fitness_history.append(best.fitness)
            await asyncio.to_thread(save_run, run)

            scores_summary = " | ".join(
                f"{a.id[:6]}:{a.fitness:.0%}" for a in sorted(population, key=lambda a: -a.fitness)
            )
            await notify(
                f"✅ 世代 {gen} 完了 — 最高: {best.fitness:.1%} | "
                f"アーカイブ: {archive.size}セル | "
                f"変異率: {mutation_rate:.0%} [{scores_summary}]"
            )

        # === Finalize: re-evaluate all on FULL challenge set ===
        await notify("📊 最終評価 — 全課題で再評価中")
        for agent in population:
            await _evaluate_and_update(agent, all_challenges)
            archive.try_add(agent)
            await asyncio.to_thread(save_agent, agent)

        run.status = "stopped" if _stop_event.is_set() else "completed"
        run.finished_at = datetime.now(timezone.utc).isoformat()
        run.best_fitness = max(a.fitness for a in population)
        await asyncio.to_thread(save_run, run)

        best = max(population, key=lambda a: a.fitness)
        await notify(
            f"🏁 進化完了 — 最終フィットネス: {best.fitness:.1%}\n"
            f"最優秀: {best.id[:8]} (戦略: {best.strategy[:80]})\n"
            f"アーカイブ: {archive.size}セル ({archive.coverage:.0%})\n"
            f"```\n{archive.grid_display()}\n```"
        )
        return run

    except Exception as e:
        log.exception("Evolution failed: %s", e)
        run.status = "stopped"
        run.finished_at = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(save_run, run)
        raise
    finally:
        _current_run = None
        _stop_event = None
