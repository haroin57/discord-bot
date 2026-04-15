"""Discord embed formatting for DGM status and results."""

import discord

from dgm.models import Agent, EvolutionRun


def agent_embed(agent: Agent, rank: int | None = None) -> discord.Embed:
    """Create an embed for a single agent."""
    title = f"Agent {agent.id[:8]}"
    if rank is not None:
        title = f"#{rank} {title}"

    embed = discord.Embed(
        title=title,
        color=discord.Color.green() if agent.fitness >= 0.7 else
              discord.Color.gold() if agent.fitness >= 0.4 else
              discord.Color.red(),
    )
    embed.add_field(name="Fitness", value=f"{agent.fitness:.1%}", inline=True)
    embed.add_field(name="Generation", value=str(agent.generation), inline=True)
    embed.add_field(
        name="Parents",
        value=", ".join(pid[:8] for pid in agent.parent_ids) or "None (initial)",
        inline=True,
    )
    embed.add_field(name="Strategy", value=agent.strategy[:200] or "N/A", inline=False)

    # Per-challenge scores
    if agent.scores:
        scores_text = "\n".join(
            f"{'✅' if s >= 1.0 else '⚠️' if s > 0 else '❌'} {cid}: {s:.0%}"
            for cid, s in sorted(agent.scores.items(), key=lambda x: -x[1])
        )
        embed.add_field(name="Scores", value=scores_text[:1024], inline=False)

    embed.set_footer(text=f"ID: {agent.id} | Created: {agent.created_at[:19]}")
    return embed


def code_embed(agent: Agent) -> discord.Embed:
    """Create an embed showing agent's code."""
    embed = discord.Embed(
        title=f"Code: Agent {agent.id[:8]} (fitness: {agent.fitness:.1%})",
        color=discord.Color.blue(),
    )
    code = agent.code
    if len(code) > 4000:
        code = code[:3990] + "\n# ... truncated"
    embed.description = f"```python\n{code}\n```"
    return embed


def population_embed(agents: list[Agent], run: EvolutionRun | None = None) -> discord.Embed:
    """Create an embed for the full population."""
    embed = discord.Embed(
        title="Population Status",
        color=discord.Color.purple(),
    )
    if run:
        embed.add_field(
            name="Evolution",
            value=f"Gen {run.current_generation}/{run.max_generations} | Status: {run.status}",
            inline=False,
        )

    sorted_agents = sorted(agents, key=lambda a: -a.fitness)
    lines = []
    for i, a in enumerate(sorted_agents):
        medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"#{i+1}"
        lines.append(f"{medal} `{a.id[:8]}` — {a.fitness:.1%} (gen {a.generation})")
    embed.add_field(
        name=f"Agents ({len(agents)})",
        value="\n".join(lines) if lines else "No agents",
        inline=False,
    )
    return embed


def run_embed(run: EvolutionRun) -> discord.Embed:
    """Create an embed for evolution run status."""
    status_emoji = {
        "running": "🔄",
        "completed": "✅",
        "stopped": "⛔",
    }
    embed = discord.Embed(
        title=f"{status_emoji.get(run.status, '❓')} Evolution Run",
        color=discord.Color.blue() if run.status == "running" else
              discord.Color.green() if run.status == "completed" else
              discord.Color.orange(),
    )
    embed.add_field(name="Status", value=run.status, inline=True)
    embed.add_field(name="Generation", value=f"{run.current_generation}/{run.max_generations}", inline=True)
    embed.add_field(name="Best Fitness", value=f"{run.best_fitness:.1%}", inline=True)
    embed.add_field(name="Population", value=str(run.population_size), inline=True)
    embed.add_field(name="Started", value=run.started_at[:19], inline=True)
    if run.finished_at:
        embed.add_field(name="Finished", value=run.finished_at[:19], inline=True)
    embed.set_footer(text=f"Run ID: {run.id}")
    return embed


def archive_embed(grid_display: str, size: int, coverage: float) -> discord.Embed:
    """Create an embed for the MAP-Elites archive grid."""
    embed = discord.Embed(
        title="MAP-Elites Archive",
        description=f"```\n{grid_display}\n```",
        color=discord.Color.dark_teal(),
    )
    embed.add_field(name="Cells Filled", value=str(size), inline=True)
    embed.add_field(name="Coverage", value=f"{coverage:.0%}", inline=True)
    embed.set_footer(text="X=Complexity(LOC) Y=Pattern diversity")
    return embed


def lineage_text(agents: list[Agent]) -> str:
    """Format agent lineage as a text tree (newest first)."""
    if not agents:
        return "No lineage found."

    lines = []
    for i, a in enumerate(agents):
        prefix = "└─" if i == len(agents) - 1 else "├─"
        indent = "  " * i
        parents = ", ".join(pid[:8] for pid in a.parent_ids) or "origin"
        lines.append(
            f"{indent}{prefix} `{a.id[:8]}` gen={a.generation} "
            f"fitness={a.fitness:.1%} ({parents})"
        )
    return "\n".join(lines)
