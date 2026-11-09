from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord.ext import commands

from .. import config
from .views import ProgressView

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger(config.LOGGER_NAME)

EVENT_COG = "Anniversary"
NO_EVENT = (
    "No anniversary event is configured yet. An administrator can create one in the "
    "Django admin panel under **Anniversary event**."
)


class AnniversaryCommands(commands.Cog):
    """Community anniversary progress."""

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

    @property
    def state(self):
        return getattr(self.bot.get_cog(EVENT_COG), "state", None)

    @commands.hybrid_group(name="anniversary")
    async def anniversary(self, ctx: commands.Context["BallsDexBot"]) -> None:
        """Show the community anniversary event."""
        await ctx.send_help(ctx.command)

    @anniversary.command(name="progress")
    async def progress(self, ctx: commands.Context["BallsDexBot"]) -> None:
        """Show the live progress of the anniversary event."""
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        else:
            await ctx.defer()

        state = self.state
        if state is None or state.event is None:
            await ctx.send(NO_EVENT, ephemeral=ctx.interaction is not None)
            return

        view = ProgressView(state)
        view.restrict_author(ctx.author.id)
        message = await ctx.send(view=view, ephemeral=ctx.interaction is not None)
        view.original_message = message
