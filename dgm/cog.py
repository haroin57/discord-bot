"""Discord Cog for DGM slash commands."""

import asyncio
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import ADMIN_USER_IDS
from dgm import engine
from dgm.formatter import (
    agent_embed,
    archive_embed,
    code_embed,
    lineage_text,
    population_embed,
    run_embed,
)
from dgm.persistence import (
    init_db,
    load_agents_by_run,
    load_best_agent,
    load_agent_lineage,
    load_latest_run,
)

log = logging.getLogger(__name__)


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id in ADMIN_USER_IDS


class DGMCog(commands.Cog, name="dgm"):
    """Darwin Gödel Machine — self-improving agents via genetic algorithms."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # init_db is already an async function that internally dispatches
        # the sync work via asyncio.to_thread. Wrapping it again with
        # asyncio.to_thread(init_db) handed the COROUTINE FUNCTION to a
        # worker thread, which then invoked it and produced an unawaited
        # coroutine — observable as `RuntimeWarning: coroutine 'init_db'
        # was never awaited` in the systemd journal. Just await it.
        await init_db()
        log.info("DGM cog loaded, database initialized")

    dgm_group = app_commands.Group(name="dgm", description="Darwin Gödel Machine commands")

    @dgm_group.command(name="start", description="Start evolution")
    @app_commands.describe(
        generations="Number of generations (default: 3)",
        population="Population size (default: 4)",
    )
    async def dgm_start(
        self,
        interaction: discord.Interaction,
        generations: int = 3,
        population: int = 4,
    ):
        if not is_admin(interaction):
            await interaction.response.send_message("管理者のみ実行可能です。", ephemeral=True)
            return

        if engine.is_running():
            await interaction.response.send_message("既に進化が実行中です。", ephemeral=True)
            return

        # Clamp values
        generations = max(1, min(generations, 20))
        population = max(2, min(population, 12))

        await interaction.response.send_message(
            f"🧬 **Darwin Gödel Machine** 起動\n"
            f"個体群: {population} | 世代数: {generations}\n"
            f"進化を開始します..."
        )

        channel = interaction.channel

        async def progress_cb(msg: str):
            try:
                await channel.send(msg)
            except Exception:
                pass

        # Run evolution in background task
        async def _run():
            try:
                await engine.run_evolution(
                    population_size=population,
                    max_generations=generations,
                    progress_callback=progress_cb,
                )
            except Exception as e:
                log.exception("Evolution failed: %s", e)
                try:
                    await channel.send(f"❌ 進化が失敗しました: {e}")
                except Exception:
                    pass

        asyncio.create_task(_run())

    @dgm_group.command(name="stop", description="Stop running evolution")
    async def dgm_stop(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("管理者のみ実行可能です。", ephemeral=True)
            return

        if not engine.is_running():
            await interaction.response.send_message("現在進化は実行されていません。", ephemeral=True)
            return

        engine.request_stop()
        await interaction.response.send_message("⛔ 進化停止をリクエストしました。現在の世代完了後に停止します。")

    @dgm_group.command(name="status", description="Show evolution status")
    async def dgm_status(self, interaction: discord.Interaction):
        run = engine.get_current_run()
        if run:
            await interaction.response.send_message(embed=run_embed(run))
            return

        # Check latest completed run
        latest = await asyncio.to_thread(load_latest_run)
        if latest:
            await interaction.response.send_message(embed=run_embed(latest))
        else:
            await interaction.response.send_message("まだ進化は実行されていません。`/dgm start` で開始してください。")

    @dgm_group.command(name="best", description="Show the best agent")
    async def dgm_best(self, interaction: discord.Interaction):
        latest = await asyncio.to_thread(load_latest_run)
        if not latest:
            await interaction.response.send_message("まだ進化は実行されていません。")
            return

        best = await asyncio.to_thread(load_best_agent, latest.id)
        if not best:
            await interaction.response.send_message("エージェントが見つかりません。")
            return

        await interaction.response.send_message(embeds=[
            agent_embed(best, rank=1),
            code_embed(best),
        ])

    @dgm_group.command(name="population", description="Show all agents")
    async def dgm_population(self, interaction: discord.Interaction):
        latest = await asyncio.to_thread(load_latest_run)
        if not latest:
            await interaction.response.send_message("まだ進化は実行されていません。")
            return

        agents = await asyncio.to_thread(load_agents_by_run, latest.id)
        if not agents:
            await interaction.response.send_message("エージェントが見つかりません。")
            return

        await interaction.response.send_message(embed=population_embed(agents, latest))

    @dgm_group.command(name="lineage", description="Show agent lineage tree")
    @app_commands.describe(agent_id="Agent ID (first 8 chars OK)")
    async def dgm_lineage(self, interaction: discord.Interaction, agent_id: str = ""):
        if not agent_id:
            # Default to best agent
            latest = await asyncio.to_thread(load_latest_run)
            if not latest:
                await interaction.response.send_message("まだ進化は実行されていません。")
                return
            best = await asyncio.to_thread(load_best_agent, latest.id)
            if not best:
                await interaction.response.send_message("エージェントが見つかりません。")
                return
            agent_id = best.id

        lineage = await asyncio.to_thread(load_agent_lineage, agent_id)
        if not lineage:
            await interaction.response.send_message(f"Agent `{agent_id}` が見つかりません。")
            return

        text = lineage_text(lineage)
        embed = discord.Embed(
            title=f"Lineage: {agent_id[:8]}",
            description=text,
            color=discord.Color.teal(),
        )
        await interaction.response.send_message(embed=embed)

    @dgm_group.command(name="code", description="Show agent's code")
    @app_commands.describe(agent_id="Agent ID (first 8 chars OK)")
    async def dgm_code(self, interaction: discord.Interaction, agent_id: str = ""):
        if not agent_id:
            latest = await asyncio.to_thread(load_latest_run)
            if not latest:
                await interaction.response.send_message("まだ進化は実行されていません。")
                return
            best = await asyncio.to_thread(load_best_agent, latest.id)
            if not best:
                await interaction.response.send_message("エージェントが見つかりません。")
                return
            agent_id = best.id

        lineage = await asyncio.to_thread(load_agent_lineage, agent_id)
        agent = lineage[0] if lineage else None
        if not agent:
            await interaction.response.send_message(f"Agent `{agent_id}` が見つかりません。")
            return

        if len(agent.code) > 3900:
            # Send as file
            f = discord.File(io.StringIO(agent.code), filename=f"agent_{agent.id[:8]}.py")
            await interaction.response.send_message(
                f"Agent `{agent.id[:8]}` (fitness: {agent.fitness:.1%})",
                file=f,
            )
        else:
            await interaction.response.send_message(embed=code_embed(agent))

    @dgm_group.command(name="archive", description="Show MAP-Elites diversity archive")
    async def dgm_archive(self, interaction: discord.Interaction):
        archive = engine.get_archive()
        if not archive or archive.size == 0:
            await interaction.response.send_message(
                "アーカイブが空です。`/dgm start` で進化を実行してください。"
            )
            return

        await interaction.response.send_message(
            embed=archive_embed(archive.grid_display(), archive.size, archive.coverage)
        )
