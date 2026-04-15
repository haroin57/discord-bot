"""Fitness evaluation for DGM agents."""

import logging

from dgm.models import Agent, Challenge, SandboxResult
from dgm.sandbox import run_sandboxed

log = logging.getLogger(__name__)

DIFFICULTY_WEIGHT = {"easy": 1.0, "medium": 2.0, "hard": 3.0}


def generate_test_code(challenge: Challenge) -> str:
    """Generate test script that prints PASSED X/Y for a challenge."""
    lines = ["_passed = 0", f"_total = {len(challenge.test_cases)}"]
    for i, (args, expected) in enumerate(challenge.test_cases):
        args_repr = ", ".join(repr(a) for a in args)
        lines.append("try:")
        lines.append(f"    _result_{i} = {challenge.function_name}({args_repr})")
        lines.append(f"    if _result_{i} == {expected!r}:")
        lines.append(f"        _passed += 1")
        lines.append(f"    else:")
        lines.append(f'        print(f"FAIL test {i}: got {{_result_{i}!r}}, expected {expected!r}")')
        lines.append(f"except Exception as _e_{i}:")
        lines.append(f'    print(f"ERROR test {i}: {{_e_{i}}}")')
    lines.append('print(f"PASSED {_passed}/{_total}")')
    return "\n".join(lines)


async def evaluate_agent(agent: Agent, challenges: list[Challenge]) -> tuple[float, dict[str, float]]:
    """Evaluate agent on all challenges. Returns (fitness, per_challenge_scores)."""
    total_score = 0.0
    total_weight = 0.0
    scores: dict[str, float] = {}

    for challenge in challenges:
        test_code = generate_test_code(challenge)
        result = await run_sandboxed(agent.code, test_code)

        weight = DIFFICULTY_WEIGHT.get(challenge.difficulty, 1.0)
        total_weight += weight

        if result.error:
            log.debug("Agent %s failed %s: %s", agent.id[:8], challenge.id, result.error)
            scores[challenge.id] = 0.0
        else:
            scores[challenge.id] = result.pass_rate
            total_score += result.pass_rate * weight

    fitness = total_score / total_weight if total_weight > 0 else 0.0
    return fitness, scores
