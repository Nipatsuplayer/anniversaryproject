from typing import TYPE_CHECKING

from .cog import Anniversary
from .commands import AnniversaryCommands

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

__all__ = ("Anniversary", "AnniversaryCommands", "setup")


async def setup(bot: "BallsDexBot") -> None:
    await bot.add_cog(Anniversary(bot))
    await bot.add_cog(AnniversaryCommands(bot))
