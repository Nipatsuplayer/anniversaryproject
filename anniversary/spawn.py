"""
A delegating spawn manager that applies the event's global spawn booster.

It never re-implements the spawn algorithm: every decision is delegated to the
manager configured in `settings.spawn_manager`. Two small adjustments are made
around that call, mirroring the compensation frenzy package:

* the per-guild cooldown counter receives a fraction of its threshold, which is
  equivalent to dividing the threshold by the multiplier;
* the 10 minute dead time is shortened to `600 / multiplier`, never below the
  event's configured floor.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from ballsdex.packages.countryballs.spawn import BaseSpawnManager

from . import config
from .state import EventState

if TYPE_CHECKING:
    import discord

    from ballsdex.core.bot import BallsDexBot
    from ballsdex.packages.countryballs.spawn import SpawnCooldown

log = logging.getLogger(config.LOGGER_NAME)


def unwrap(manager: BaseSpawnManager | None) -> BaseSpawnManager | None:
    """Collapse a chain of anniversary wrappers down to the real manager."""
    seen: set[int] = set()
    while isinstance(manager, AnniversarySpawnManager):
        if id(manager) in seen:
            log.error("An anniversary spawn manager points at itself, ignoring the chain.")
            return None
        seen.add(id(manager))
        manager = manager.inner
    return manager


def strip_anniversary(manager: BaseSpawnManager | None) -> BaseSpawnManager | None:
    """Remove anniversary wrappers from anywhere in a spawn-manager chain."""
    seen: set[int] = set()

    def _strip(current: BaseSpawnManager | None) -> BaseSpawnManager | None:
        if current is None or id(current) in seen:
            return current
        seen.add(id(current))
        if isinstance(current, AnniversarySpawnManager):
            return _strip(current.inner)
        inner = getattr(current, "inner", None)
        if inner is not None:
            setattr(current, "inner", _strip(inner))
        return current

    return _strip(manager)


def contains_anniversary(manager: BaseSpawnManager | None) -> bool:
    """Whether an anniversary wrapper exists anywhere in a spawn-manager chain."""
    seen: set[int] = set()
    while manager is not None and id(manager) not in seen:
        if isinstance(manager, AnniversarySpawnManager):
            return True
        seen.add(id(manager))
        manager = getattr(manager, "inner", None)
    return False


def wrap_manager(bot: "BallsDexBot", manager: BaseSpawnManager | None, state: EventState) -> BaseSpawnManager | None:
    """
    Install the anniversary wrapper without putting it outside a compensation wrapper.

    The compensation package only unwraps its own manager, so an anniversary
    wrapper placed on top of it can be delegated back to itself. Keeping the
    anniversary wrapper inside the compensation chain lets both events apply their
    effects in either install order.
    """
    manager = strip_anniversary(manager)
    if manager is None:
        return None

    current = manager
    seen: set[int] = set()
    while type(current).__name__ == "CompensationSpawnManager":
        if id(current) in seen:
            return None
        seen.add(id(current))
        inner = getattr(current, "inner", None)
        if inner is None:
            return None
        if type(inner).__name__ != "CompensationSpawnManager":
            setattr(current, "inner", AnniversarySpawnManager(bot, inner, state))
            return manager
        current = inner
    return AnniversarySpawnManager(bot, manager, state)


def _innermost(manager: BaseSpawnManager | None) -> BaseSpawnManager | None:
    """Follow known wrapper chains down to the manager that actually spawns."""
    seen: set[int] = set()
    while manager is not None and id(manager) not in seen:
        seen.add(id(manager))
        if type(manager).__name__ not in ("AnniversarySpawnManager", "CompensationSpawnManager"):
            break
        inner = getattr(manager, "inner", None)
        if inner is None:
            break
        manager = inner
    return manager


def _contains(manager: BaseSpawnManager | None, target: BaseSpawnManager) -> bool:
    seen: set[int] = set()
    while manager is not None and id(manager) not in seen:
        if manager is target:
            return True
        seen.add(id(manager))
        manager = getattr(manager, "inner", None)
    return False


class AnniversarySpawnManager(BaseSpawnManager):
    """Delegating spawn manager that applies the anniversary spawn booster."""

    def __init__(self, bot: "BallsDexBot", inner: BaseSpawnManager, state: EventState):
        super().__init__(bot)
        self.inner = inner
        self.real_inner = _innermost(inner) or inner
        self.state = state
        self._handling = False

    def _active_manager(self) -> BaseSpawnManager:
        spawner = getattr(self.state.bot.get_cog("CountryBallsSpawner"), "spawn_manager", None)
        if spawner is None or spawner is self:
            return self.inner
        if isinstance(spawner, AnniversarySpawnManager):
            return unwrap(spawner) or self.inner
        if _contains(spawner, self):
            return self.inner
        return spawner

    @staticmethod
    def _get_cooldown(manager: BaseSpawnManager, guild_id: int) -> "SpawnCooldown | None":
        cooldowns = getattr(manager, "cooldowns", None)
        if not isinstance(cooldowns, dict):
            return None
        return cooldowns.get(guild_id)

    @staticmethod
    def _add_progress(cooldown: "SpawnCooldown", multiplier: float) -> None:
        try:
            threshold = float(cooldown.threshold)
            count = float(cooldown.scaled_message_count)
        except (AttributeError, TypeError, ValueError):
            return
        if threshold <= 0:
            return
        cooldown.scaled_message_count = min(threshold, count + threshold * (1 - 1 / multiplier))

    async def handle_message(self, message: "discord.Message") -> bool | tuple[Literal[True], str]:
        if self._handling:
            return await self.real_inner.handle_message(message)
        self._handling = True
        try:
            return await self._handle_message(message)
        finally:
            self._handling = False

    async def _handle_message(self, message: "discord.Message") -> bool | tuple[Literal[True], str]:
        inner = self._active_manager()
        if inner is self:
            log.error("Broken anniversary spawn manager chain, skipping this message.")
            return False
        if not self.state.active or self.state.spawn_multiplier <= 1:
            return await inner.handle_message(message)

        guild = message.guild
        if guild is None:
            return await inner.handle_message(message)

        multiplier = self.state.spawn_multiplier
        cooldown = self._get_cooldown(inner, guild.id)

        saved_time = None
        shifted_time = None
        if cooldown is not None and hasattr(cooldown, "time"):
            floor = max(1, self.state.event.spawn_floor_seconds if self.state.event else 1)
            interval = max(float(floor), config.DEAD_TIME / multiplier)
            shift = config.DEAD_TIME - interval
            if shift > 0:
                saved_time = cooldown.time
                shifted_time = saved_time - timedelta(seconds=shift)
                cooldown.time = shifted_time

        try:
            result = await inner.handle_message(message)
        finally:
            if saved_time is not None and cooldown is not None and cooldown.time == shifted_time:
                cooldown.time = saved_time

        if result:
            return result

        if cooldown is not None:
            self._add_progress(cooldown, multiplier)
        return result

    async def admin_explain(self, ctx, guild: "discord.Guild"):
        await self.inner.admin_explain(ctx, guild)
        if self.state.active and self.state.spawn_multiplier > 1:
            await ctx.send(
                f"\N{PARTY POPPER} Anniversary event active: spawn rate x{self.state.spawn_multiplier:.2f}.",
                ephemeral=True,
            )
