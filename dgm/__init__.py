"""Darwin Gödel Machine — self-improving coding agents via genetic algorithms."""

from discord.ext import commands


async def setup(bot: commands.Bot):
    """Called by bot.load_extension('dgm')."""
    from dgm.cog import DGMCog
    await bot.add_cog(DGMCog(bot))
