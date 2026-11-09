"""
Temporary integration points into the running bot.

Nothing is written to BallsDex's own files. The following attributes are patched
while the event is active and restored when it ends or the extension unloads:

1. ``CountryBallsSpawner.spawn_manager`` gets a delegating wrapper for the spawn
   rate booster (see ``spawn.py``).
2. ``BallSpawnView.get_random`` re-weights collectible rarity for the luck boost.
3. ``BallSpawnView.get_random_special`` re-weights special card chance.
4. ``BallSpawnView.spawn`` lengthens the catch window for wild spawns.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, cast

from ballsdex.packages.countryballs.cog import CountryBallsSpawner
from ballsdex.packages.countryballs.countryball import BallSpawnView

from . import config, rarity
from .spawn import contains_anniversary, strip_anniversary, wrap_manager
from .state import EventState

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger(config.LOGGER_NAME)

SPAWNER_COG = "CountryBallsSpawner"


def _unwrap_patch(value):
    """Return the original behind a leftover anniversary patch, if there is one."""
    func = getattr(value, "__func__", value)
    return getattr(func, "_anniversary_original", func)


def install(bot: "BallsDexBot", state: EventState) -> Callable[[], None]:
    """Hook the active event into the bot and return a restoring callable."""
    if state.hooks_installed:
        return lambda: _uninstall(bot, state)

    spawner = cast("CountryBallsSpawner", bot.get_cog(SPAWNER_COG))
    if spawner is None:
        raise RuntimeError(f"The '{SPAWNER_COG}' package is not loaded, the anniversary event cannot hook into it.")

    original_spawn = _unwrap_patch(BallSpawnView.__dict__["spawn"])
    original_get_random = _unwrap_patch(BallSpawnView.__dict__["get_random"])
    original_get_random_special = _unwrap_patch(BallSpawnView.__dict__["get_random_special"])
    original_manager = strip_anniversary(spawner.spawn_manager)
    if original_manager is None:
        raise RuntimeError("The spawn manager chain is broken, restart the bot to clear it.")

    wrapped_manager = wrap_manager(bot, original_manager, state)
    if wrapped_manager is None:
        raise RuntimeError("The spawn manager chain is broken, restart the bot to clear it.")

    state.hook_originals = {
        "spawn": original_spawn,
        "get_random": original_get_random,
        "get_random_special": original_get_random_special,
    }
    spawner.spawn_manager = wrapped_manager

    try:
        _install_patches(state, original_get_random, original_get_random_special, original_spawn)
    except Exception:
        spawner.spawn_manager = strip_anniversary(spawner.spawn_manager) or original_manager
        _restore_patches(state)
        raise

    state.spawner = spawner
    state.hooks_installed = True
    log.info("Anniversary event hooked into '%s' (spawn manager: %s).", SPAWNER_COG, type(original_manager).__name__)
    return lambda: uninstall(bot, state)


def uninstall(bot: "BallsDexBot", state: EventState) -> None:
    """Restore every patched attribute. Safe to call more than once."""
    _uninstall(bot, state)


def is_installed(bot: "BallsDexBot", state: EventState) -> bool:
    """Whether the active event is still wrapped around the current spawner cog."""
    if not state.hooks_installed:
        return False
    spawner = cast("CountryBallsSpawner", bot.get_cog(SPAWNER_COG))
    if spawner is None or spawner is not state.spawner:
        return False
    return contains_anniversary(spawner.spawn_manager)


def ensure(bot: "BallsDexBot", state: EventState) -> Callable[[], None] | None:
    """
    Install the hooks, or repair the spawn wrapper after a ``countryballs`` reload.

    Returns the restoring callable only when the hooks were installed from scratch.
    """
    if not state.hooks_installed:
        return install(bot, state)

    spawner = cast("CountryBallsSpawner", bot.get_cog(SPAWNER_COG))
    if spawner is None:
        log.warning("Cannot repair the anniversary hooks, the '%s' package is not loaded.", SPAWNER_COG)
        return None

    repaired = False
    if spawner is not state.spawner or not contains_anniversary(spawner.spawn_manager):
        wrapped_manager = wrap_manager(bot, spawner.spawn_manager, state)
        if wrapped_manager is None:
            log.error("Could not repair the anniversary spawn manager, the chain is broken.")
            return None
        spawner.spawn_manager = wrapped_manager
        state.spawner = spawner
        repaired = True

    originals = state.hook_originals
    if _patches_need_repair(state) and originals:
        _install_patches(state, originals["get_random"], originals["get_random_special"], originals["spawn"])
        repaired = True

    if repaired:
        log.info("Re-hooked the anniversary event after the '%s' package was reloaded.", SPAWNER_COG)
    return None


def _install_patches(state: EventState, original_get_random, original_get_random_special, original_spawn) -> None:
    token = object()
    state.patch_token = token

    async def get_random(cls, bot):
        if state.patch_token is token and state.active and state.rarity_multiplier > 1:
            return await rarity.get_view(bot, state.rarity_multiplier)
        return await original_get_random(cls, bot)

    setattr(get_random, "_anniversary_token", token)
    setattr(get_random, "_anniversary_original", original_get_random)
    BallSpawnView.get_random = classmethod(get_random)  # type: ignore[assignment]

    def get_random_special():
        if state.patch_token is token and state.active and state.special_chance_multiplier > 1:
            return rarity.pick_special(state.special_chance_multiplier)
        return original_get_random_special()

    setattr(get_random_special, "_anniversary_token", token)
    setattr(get_random_special, "_anniversary_original", original_get_random_special)
    BallSpawnView.get_random_special = staticmethod(get_random_special)  # type: ignore[assignment]

    async def spawn(self, channel):
        if (
            state.patch_token is token
            and self.ballinstance is None
            and state.active
            and state.catch_window_multiplier != 1
            and self.timeout
        ):
            self.timeout = max(
                config.MIN_CATCH_TIMEOUT,
                min(config.MAX_CATCH_TIMEOUT, int(self.timeout * state.catch_window_multiplier)),
            )
        return await original_spawn(self, channel)

    setattr(spawn, "_anniversary_token", token)
    setattr(spawn, "_anniversary_original", original_spawn)
    BallSpawnView.spawn = spawn  # type: ignore[method-assign]


def _patches_need_repair(state: EventState) -> bool:
    token = state.patch_token
    if token is None:
        return True
    for attr in ("get_random", "get_random_special", "spawn"):
        func = getattr(BallSpawnView.__dict__[attr], "__func__", BallSpawnView.__dict__[attr])
        wrapper_token = getattr(func, "_anniversary_token", None)
        if wrapper_token is token:
            continue
        if wrapper_token is not None:
            return True
        if func is state.hook_originals.get(attr):
            return True
    return False


def _restore_patch(attr: str, replacement, token: object | None) -> None:
    current = BallSpawnView.__dict__[attr]
    func = getattr(current, "__func__", current)
    if getattr(func, "_anniversary_token", None) is not token:
        return
    setattr(BallSpawnView, attr, replacement)


def _restore_patches(state: EventState) -> None:
    token = state.patch_token
    state.patch_token = None
    originals = state.hook_originals
    if not originals:
        return
    _restore_patch("get_random", classmethod(originals["get_random"]), token)  # type: ignore[arg-type]
    _restore_patch("get_random_special", staticmethod(originals["get_random_special"]), token)  # type: ignore[arg-type]
    _restore_patch("spawn", originals["spawn"], token)


def _uninstall(bot: "BallsDexBot", state: EventState) -> None:
    if not state.hooks_installed:
        return
    spawner = cast("CountryBallsSpawner", bot.get_cog(SPAWNER_COG))
    if spawner is not None:
        manager = strip_anniversary(spawner.spawn_manager)
        if manager is not None:
            spawner.spawn_manager = manager
    _restore_patches(state)
    state.hooks_installed = False
    state.spawner = None
    state.hook_originals = {}
    log.info("Anniversary event hooks removed.")


def strip(bot: "BallsDexBot") -> None:
    """Safety net for extension unload: remove a leftover wrapper."""
    spawner = cast("CountryBallsSpawner", bot.get_cog(SPAWNER_COG))
    if spawner is None:
        return
    manager = strip_anniversary(spawner.spawn_manager)
    if manager is not None and manager is not spawner.spawn_manager:
        log.info("Removing a leftover anniversary spawn manager.")
        spawner.spawn_manager = manager
