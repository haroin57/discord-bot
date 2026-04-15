"""LLM operations for DGM v2 — via Claude Code SDK (handles OAuth/DPoP internally)."""

import asyncio
import json
import logging
import re

from claude_code_sdk import ClaudeCodeOptions, ResultMessage, query

from config import (
    CWD,
    DGM_LLM_MODEL,
    DGM_LLM_MODEL_CROSSOVER,
    DGM_LLM_MODEL_MUTATION,
    DGM_LLM_MODEL_META,
)
from dgm.models import Agent, Challenge

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an expert Python programmer participating in a coding competition. "
    "You write clean, efficient, correct Python code. Always respond with valid Python. "
    "Do NOT use any external packages — only Python standard library."
)

DGM_INTER_CALL_DELAY = 3.0
_dgm_api_semaphore = asyncio.Semaphore(1)


async def _call_llm(prompt: str, system: str = "", model: str = DGM_LLM_MODEL) -> str:
    """Call Claude via Claude Code SDK (handles OAuth, DPoP, retries internally)."""
    async with _dgm_api_semaphore:
        options = ClaudeCodeOptions(
            system_prompt=system or SYSTEM_PROMPT,
            model=model,
            max_turns=1,
            permission_mode="bypassPermissions",
            cwd=CWD,
        )

        response_text = ""
        try:
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, ResultMessage):
                    response_text = msg.result if hasattr(msg, "result") else str(msg)
        except Exception as e:
            log.error("DGM LLM call failed [model=%s]: %s", model, e)
            raise RuntimeError(f"DGM LLM failed: {e}")

        log.info("DGM LLM response [model=%s]: %d chars", model, len(response_text))
        await asyncio.sleep(DGM_INTER_CALL_DELAY)
        return response_text


def _extract_code(text: str) -> str:
    m = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _extract_strategy(text: str, fallback: str = "") -> str:
    for line in text.splitlines():
        if line.strip().upper().startswith("STRATEGY:"):
            return line.split(":", 1)[1].strip()
    return fallback


def _challenges_summary(challenges: list[Challenge]) -> str:
    parts = []
    for c in challenges:
        example_in = repr(c.test_cases[0][0]) if c.test_cases else "N/A"
        example_out = repr(c.test_cases[0][1]) if c.test_cases else "N/A"
        parts.append(
            f"- {c.function_name}: {c.description}\n"
            f"  Example: {example_in} -> {example_out} | Difficulty: {c.difficulty}"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Initial generation (uses DGM_LLM_MODEL — Opus for quality)
# ---------------------------------------------------------------------------

INIT_STRATEGIES = [
    "Focus on correctness with simple, straightforward implementations using basic string/list operations.",
    "Use regex patterns and functional programming (map, filter, reduce) for concise solutions.",
    "Emphasize robust error handling and edge cases, with defensive programming style.",
    "Optimize for performance using efficient data structures and algorithms.",
]


async def generate_initial_agent(
    challenges: list[Challenge], agent_index: int, total: int
) -> tuple[str, str]:
    prompt = f"""You are agent {agent_index + 1} of {total} in an evolutionary coding competition.

Your coding style hint: {INIT_STRATEGIES[agent_index % len(INIT_STRATEGIES)]}

Implement ALL of the following functions in a single Python code block.
Each function must match the exact signature described.
Use ONLY Python standard library (no pip packages).

Challenges:
{_challenges_summary(challenges)}

Respond with:
STRATEGY: <your strategy in 1-2 sentences>
```python
<all implementations>
```"""

    response = await _call_llm(prompt, model=DGM_LLM_MODEL)
    code = _extract_code(response)
    strategy = _extract_strategy(response, INIT_STRATEGIES[agent_index % len(INIT_STRATEGIES)])
    log.info("Generated initial agent %d: %d chars", agent_index, len(code))
    return code, strategy


# ---------------------------------------------------------------------------
# Inspiration-based Crossover (CodeEvolve-style, uses Opus)
# ---------------------------------------------------------------------------

async def inspiration_crossover(
    parent: Agent,
    inspirations: list[Agent],
    challenges: list[Challenge],
    meta_hint: str = "",
) -> tuple[str, str]:
    """CodeEvolve-style crossover: use top diverse agents as inspiration, not direct merge.

    Instead of merging two parents, we show the parent + top performers from different
    niches as "inspiration" and ask the LLM to create a new improved version.
    """
    insp_blocks = []
    for i, insp in enumerate(inspirations):
        insp_blocks.append(
            f"Inspiration {i+1} (fitness: {insp.fitness:.2f}, niche: {insp.niche}):\n"
            f"Strategy: {insp.strategy}\n"
            f"Scores: {json.dumps(insp.scores)}\n"
            f"```python\n{insp.code}\n```"
        )

    meta_section = f"\n\nEvolution insight from meta-analysis:\n{meta_hint}\n" if meta_hint else ""

    prompt = f"""You are performing INSPIRATION-BASED CROSSOVER in a genetic algorithm.

You have a parent agent and several high-performing inspiration agents from DIFFERENT niches.
Study what makes each inspiration successful, then create a NEW child that combines the best ideas.

Parent agent (fitness: {parent.fitness:.2f}):
Strategy: {parent.strategy}
Scores: {json.dumps(parent.scores)}
```python
{parent.code}
```

{chr(10).join(insp_blocks)}
{meta_section}
Challenges:
{_challenges_summary(challenges)}

Instructions:
- Study each inspiration's approach to challenges they excel at
- Create a NEW implementation that combines the best patterns from all sources
- Focus especially on improving challenges where the parent scores < 100%
- The child must implement ALL functions
- Be creative — don't just copy the best one, synthesize new approaches

Respond with:
STRATEGY: <brief description of synthesized approach>
```python
<all implementations>
```"""

    response = await _call_llm(prompt, model=DGM_LLM_MODEL_CROSSOVER)
    code = _extract_code(response)
    strategy = _extract_strategy(response, f"Inspired by {len(inspirations)} diverse agents")
    log.info("Inspiration crossover produced %d chars", len(code))
    return code, strategy


# ---------------------------------------------------------------------------
# Mutation (uses Haiku for diversity + low cost)
# ---------------------------------------------------------------------------

async def mutate(
    agent: Agent,
    challenges: list[Challenge],
    meta_hint: str = "",
) -> tuple[str, str]:
    weak = [cid for cid, score in agent.scores.items() if score < 1.0]
    weak_details = []
    for c in challenges:
        if c.id in weak:
            weak_details.append(
                f"  - {c.function_name} (score: {agent.scores.get(c.id, 0):.1%}): {c.description}"
            )

    meta_section = f"\n\nEvolution insight:\n{meta_hint}\n" if meta_hint else ""

    prompt = f"""You are performing MUTATION in a genetic algorithm for coding agents.

Current agent (fitness: {agent.fitness:.2f}, niche: {agent.niche}):
Strategy: {agent.strategy}
```python
{agent.code}
```

Weak/failing challenges:
{chr(10).join(weak_details) if weak_details else "None — all passing, try to optimize or explore a DIFFERENT coding style"}

All challenges:
{_challenges_summary(challenges)}
{meta_section}
Instructions:
- Analyze why the agent fails on weak challenges
- Make targeted improvements to fix failures
- Do NOT break functions that already work well
- Keep all function signatures the same
- Try a DIFFERENT approach than the parent (explore new patterns, data structures, or algorithms)

Respond with:
STRATEGY: <brief description of what you changed and why>
```python
<all implementations>
```"""

    response = await _call_llm(prompt, model=DGM_LLM_MODEL_MUTATION)
    code = _extract_code(response)
    strategy = _extract_strategy(response, f"Mutation of [{agent.id[:8]}]")
    log.info("Mutation produced %d chars", len(code))
    return code, strategy


# ---------------------------------------------------------------------------
# Meta-prompting (CodeEvolve-style, uses Haiku — cheap)
# ---------------------------------------------------------------------------

async def generate_meta_prompt(
    population: list[Agent],
    generation: int,
    prev_best_fitness: float,
    challenges: list[Challenge],
) -> str:
    """Auxiliary LLM analyzes evolution progress and generates strategic hints.

    Returns a short insight string to inject into crossover/mutation prompts.
    """
    pop_summary = []
    for a in sorted(population, key=lambda x: -x.fitness)[:4]:
        weak = [cid for cid, s in a.scores.items() if s < 1.0]
        pop_summary.append(
            f"  Agent {a.id[:8]} (gen={a.generation}, fitness={a.fitness:.2f}, niche={a.niche}): "
            f"weak=[{', '.join(weak)}]"
        )

    improved = population[0].fitness > prev_best_fitness if population else False

    prompt = f"""You are a meta-strategist analyzing the evolution of coding agents.

Generation: {generation}
Best fitness improved since last gen: {"YES" if improved else "NO (stagnating)"}
Previous best: {prev_best_fitness:.2f}

Current population (top 4):
{chr(10).join(pop_summary)}

Common failure patterns across population:
{_identify_common_failures(population, challenges)}

Provide a SHORT (2-3 sentences) strategic insight for the next generation:
- If stagnating: suggest what new approach to try
- If improving: suggest what to double down on
- Identify which challenge types need the most attention
- Suggest specific coding patterns or algorithms that might help

Respond with ONLY the insight text, no formatting."""

    response = await _call_llm(prompt, model=DGM_LLM_MODEL_META)
    insight = response.strip()[:500]  # Cap length
    log.info("Meta-prompt generated: %s", insight[:100])
    return insight


def _identify_common_failures(population: list[Agent], challenges: list[Challenge]) -> str:
    """Find challenges that most agents fail on."""
    fail_counts: dict[str, int] = {}
    for a in population:
        for cid, score in a.scores.items():
            if score < 1.0:
                fail_counts[cid] = fail_counts.get(cid, 0) + 1

    if not fail_counts:
        return "None — all agents passing all challenges"

    sorted_fails = sorted(fail_counts.items(), key=lambda x: -x[1])
    lines = []
    for cid, count in sorted_fails[:5]:
        challenge = next((c for c in challenges if c.id == cid), None)
        name = challenge.function_name if challenge else cid
        lines.append(f"  - {name}: {count}/{len(population)} agents fail")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-referential improvement (Gödel Machine element)
# ---------------------------------------------------------------------------

async def suggest_ga_params(
    population: list[Agent],
    generation: int,
    current_params: dict,
    fitness_history: list[float],
) -> dict:
    """LLM suggests GA parameter adjustments based on evolution progress.

    Returns dict with suggested params: mutation_rate, offspring_count, tournament_k.
    """
    history_str = " -> ".join(f"{f:.2f}" for f in fitness_history[-5:])

    prompt = f"""You are optimizing a genetic algorithm's hyperparameters.

Current generation: {generation}
Fitness history (best per gen): {history_str}
Current params: {json.dumps(current_params)}
Population diversity: {len(set(a.niche for a in population))} unique niches out of {len(population)} agents

Based on the fitness trajectory:
- If fitness is stagnating: increase mutation_rate, decrease tournament_k (more exploration)
- If fitness is improving steadily: keep current or slightly increase offspring_count
- If fitness is oscillating: increase tournament_k (more exploitation)

Respond with ONLY a JSON object (no markdown, no explanation):
{{"mutation_rate": <float 0.3-0.9>, "offspring_count": <int 2-4>, "tournament_k": <int 2-3>}}"""

    try:
        response = await _call_llm(prompt, model=DGM_LLM_MODEL_META)
        text = response.strip()
        m = re.search(r'\{[^}]+\}', text)
        if m:
            params = json.loads(m.group(0))
            params["mutation_rate"] = max(0.3, min(0.9, float(params.get("mutation_rate", 0.5))))
            params["offspring_count"] = max(2, min(4, int(params.get("offspring_count", 2))))
            params["tournament_k"] = max(2, min(3, int(params.get("tournament_k", 2))))
            log.info("GA params suggested: %s", params)
            return params
    except Exception as e:
        log.warning("Failed to parse GA param suggestion: %s", e)

    return current_params  # fallback to current


# ---------------------------------------------------------------------------
# Legacy backward-compat alias
# ---------------------------------------------------------------------------

async def crossover(
    parent_a: Agent, parent_b: Agent, challenges: list[Challenge]
) -> tuple[str, str]:
    """Legacy crossover interface — delegates to inspiration_crossover."""
    return await inspiration_crossover(
        parent=parent_a,
        inspirations=[parent_b],
        challenges=challenges,
    )
