"""
Rarity and special-chance helpers for the luck booster.

BallsDex picks a collectible with `random.choices(population, weights=[b.rarity])`,
where a higher rarity value means a more common collectible. The booster raises
each weight to the power `1 / rarity_multiplier`, which favours rarer
collectibles without changing the underlying `balls` cache.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import TYPE_CHECKING

from django.utils import timezone

from bd_models.models import balls, specials

from . import config

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot
    from ballsdex.packages.countryballs.countryball import BallSpawnView
    from bd_models.models import Ball, Special

log = logging.getLogger(config.LOGGER_NAME)


def rarity_exponent(multiplier: float) -> float:
    if multiplier <= 1:
        return 1.0
    return max(0.25, min(1.0, 1 / multiplier))


def pick_ball(multiplier: float) -> "Ball":
    population = [ball for ball in balls.values() if ball.enabled]
    if not population:
        raise RuntimeError("No collectible to spawn")

    exponent = rarity_exponent(multiplier)
    weights = [(ball.rarity if ball.rarity > 0 else 0.0) ** exponent for ball in population]
    if not any(weights):
        log.warning("All collectibles have a rarity of 0, ignoring the rarity multiplier.")
        return random.choice(population)
    return random.choices(population=population, weights=weights, k=1)[0]


def pick_special(multiplier: float) -> "Special | None":
    """Roll a special card with its weight multiplied by the luck booster."""
    active = [
        special
        for special in specials.values()
        if (special.start_date or datetime.min.replace(tzinfo=timezone.get_current_timezone()))
        <= timezone.now()
        <= (special.end_date or datetime.max.replace(tzinfo=timezone.get_current_timezone()))
    ]
    if not active:
        return None

    common_weight = max(0.0, 1 - sum(special.rarity for special in active))
    weights = [max(0.0, special.rarity) * max(1.0, multiplier) for special in active] + [common_weight]
    if not any(weights):
        return None
    return random.choices(population=active + [None], weights=weights, k=1)[0]


async def get_view(bot: "BallsDexBot", multiplier: float) -> "BallSpawnView":
    from ballsdex.packages.countryballs.countryball import BallSpawnView

    return BallSpawnView(bot, pick_ball(multiplier))
