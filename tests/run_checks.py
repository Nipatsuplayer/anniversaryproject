"""
Small, dependency-free checks for the anniversary package.

This does not touch the real database and does not need a running bot. It only
checks the pure matching rules, formatting helpers and model properties that are
easy to break during a refactor.

Run from the repository root:

    BALLSDEXBOT_DB_URL=sqlite:////tmp/anniversary-checks.db \
        .venv/bin/python extra/ballsdex-anniversary/tests/run_checks.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

_toml = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
_toml.write(f'[[ballsdex.packages]]\nlocation = "{PACKAGE_ROOT}"\npath = "anniversary"\nenabled = true\n')
_toml.close()
os.environ["BALLSDEXBOT_EXTRA_TOML"] = _toml.name
os.environ.setdefault("BALLSDEXBOT_DB_URL", "sqlite:////tmp/anniversary-checks.db")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings")

import django  # noqa: E402

django.setup()

# ballsdex.packages.countryballs reads bot settings at import time (Modal titles,
# collectible names): provide an unsaved default instance so these checks can run
# against any database, including an empty one.
sys.path.insert(0, str(PACKAGE_ROOT.parents[1] / "admin_panel"))

from settings.models import Settings, settings  # noqa: E402

settings.instance = Settings()

from anniversary import hooks  # noqa: E402
from anniversary.formatting import progress_bar  # noqa: E402
from anniversary.models import ActionType, Event, Reward, RewardType, TaskType  # noqa: E402
from anniversary.rarity import rarity_exponent  # noqa: E402
from anniversary.services import Action, matches  # noqa: E402
from anniversary.spawn import (  # noqa: E402
    AnniversarySpawnManager,
    contains_anniversary,
    strip_anniversary,
    wrap_manager,
)
from anniversary.state import EventState  # noqa: E402
from django.utils import timezone  # noqa: E402

from ballsdex.packages.countryballs.countryball import BallSpawnView  # noqa: E402
from ballsdex.packages.countryballs.spawn import BaseSpawnManager  # noqa: E402


def action(kind: str, **kwargs) -> Action:
    return Action(kind, player_id=1, dedupe_key="test", **kwargs)


def check_matching() -> None:
    catch = action(ActionType.CATCH, ball_id=10, special_id=20, guild_id=30)

    assert matches(TaskType.CATCH_ANY, (), (), (), catch)
    assert matches(TaskType.CATCH_ANY, (), (), (30,), catch)
    assert not matches(TaskType.CATCH_ANY, (), (), (31,), catch)
    assert not matches(TaskType.CATCH_ANY, (), (), (31,), action(ActionType.CATCH, ball_id=10))

    assert matches(TaskType.CATCH_BALLS, (10,), (), (), catch)
    assert not matches(TaskType.CATCH_BALLS, (11,), (), (), catch)
    assert matches(TaskType.CATCH_SPECIALS, (), (20,), (), catch)
    assert not matches(TaskType.CATCH_SPECIALS, (), (21,), (), catch)
    assert matches(TaskType.UNIQUE_SPECIES, (), (), (), catch)
    assert not matches(TaskType.UNIQUE_SPECIES, (), (), (), action(ActionType.CATCH, special_id=20))

    assert matches(TaskType.CATCH_RARE, (), (), (), catch, max_rarity=1.0, rarity_ball_ids={10})
    assert not matches(TaskType.CATCH_RARE, (), (), (), catch, max_rarity=1.0, rarity_ball_ids={11})
    assert not matches(TaskType.CATCH_RARE, (), (), (), catch, max_rarity=None, rarity_ball_ids={10})
    assert matches(TaskType.CATCH_ANY, (), (), (), catch, max_rarity=1.0, rarity_ball_ids={10})
    assert not matches(TaskType.CATCH_ANY, (), (), (), catch, max_rarity=1.0, rarity_ball_ids={11})

    trade = action(ActionType.TRADE, other_player_id=2)
    donation = action(ActionType.DONATION, quantity=3)
    assert matches(TaskType.TRADES, (), (), (), trade)
    assert not matches(TaskType.CATCH_ANY, (), (), (), trade)
    assert matches(TaskType.DONATIONS, (), (), (), donation)
    assert not matches(TaskType.TRADES, (), (), (), donation)


def check_formatting() -> None:
    assert rarity_exponent(1) == 1
    assert rarity_exponent(2) == 0.5
    assert rarity_exponent(1.25) == 0.8
    assert rarity_exponent(100) == 0.25

    assert progress_bar(0) == "\N{LIGHT SHADE}" * 10
    assert progress_bar(100) == "█" * 10
    assert progress_bar(50) == "█" * 5 + "\N{LIGHT SHADE}" * 5
    assert len(progress_bar(33)) == 10


def check_models() -> None:
    event = Event(name="Test", enabled=True)
    assert event.is_active()
    assert event.next_boundary() is None

    future = timezone.now() + timedelta(days=1)
    scheduled = Event(name="Later", enabled=True, starts_at=future)
    assert not scheduled.is_active()
    assert scheduled.next_boundary() == future
    assert scheduled.status.startswith("Scheduled (starts")

    disabled_scheduled = Event(name="Disabled", enabled=False, starts_at=future)
    assert not disabled_scheduled.is_active()
    assert disabled_scheduled.next_boundary() is None
    assert disabled_scheduled.status.startswith("Disabled (scheduled to start")

    spawn = Reward(name="Spawn", reward_type=RewardType.SPAWN_MULTIPLIER, multiplier=2)
    assert spawn.is_global
    assert spawn.summary == "spawn rate x2"

    luck = Reward(name="Luck", reward_type=RewardType.LUCK_BOOST, rarity_multiplier=1.5, special_chance_multiplier=2)
    assert luck.is_global
    assert luck.summary == "rarity x1.5, special chance x2"

    custom = Reward(name="Text", reward_type=RewardType.CUSTOM_TEXT, text="A badge")
    assert not custom.is_global
    assert custom.summary == "A badge"

    grant = Reward(name="Prize", reward_type=RewardType.GRANT_COLLECTIBLE, quantity=2)
    assert not grant.is_global
    assert grant.summary == "2x collectible"

    random_grant = Reward(name="Random", reward_type=RewardType.GRANT_RANDOM_BALL, quantity=2)
    assert not random_grant.is_global
    assert random_grant.summary == "2x random collectible"


class FakeManager(BaseSpawnManager):
    def __init__(self, inner: BaseSpawnManager | None = None):
        super().__init__(cast(Any, None))
        self.inner = inner

    async def handle_message(self, message):
        return False


class CompensationSpawnManager(FakeManager):
    pass


def check_spawn_wrappers() -> None:
    state = EventState(cast(Any, None))
    real = FakeManager()

    compensation = CompensationSpawnManager(real)
    wrapped = wrap_manager(cast(Any, None), compensation, state)
    assert wrapped is compensation
    inner = compensation.inner
    assert isinstance(inner, AnniversarySpawnManager)
    assert inner.inner is real
    assert contains_anniversary(compensation)
    assert not contains_anniversary(real)

    assert strip_anniversary(compensation) is compensation
    assert compensation.inner is real
    assert not contains_anniversary(compensation)

    wrapped = wrap_manager(cast(Any, None), real, state)
    assert isinstance(wrapped, AnniversarySpawnManager)
    assert wrapped.inner is real
    assert contains_anniversary(wrapped)

    old_spawner = SimpleNamespace(spawn_manager=FakeManager())
    new_spawner = SimpleNamespace(spawn_manager=CompensationSpawnManager(FakeManager()))
    fake_bot = SimpleNamespace(get_cog=lambda name: new_spawner)
    state.spawner = old_spawner
    state.hooks_installed = True
    assert hooks.ensure(cast(Any, fake_bot), state) is None
    assert state.spawner is new_spawner
    assert contains_anniversary(new_spawner.spawn_manager)
    manager = new_spawner.spawn_manager
    assert hooks.ensure(cast(Any, fake_bot), state) is None
    assert new_spawner.spawn_manager is manager


def check_patch_restore_order() -> None:
    saved = {
        "get_random": BallSpawnView.__dict__["get_random"],
        "get_random_special": BallSpawnView.__dict__["get_random_special"],
        "spawn": BallSpawnView.__dict__["spawn"],
    }
    originals = {
        "get_random": saved["get_random"].__func__,
        "get_random_special": saved["get_random_special"].__func__,
        "spawn": saved["spawn"],
    }
    state = EventState(cast(Any, None))
    state.hook_originals = originals
    try:
        hooks._install_patches(state, originals["get_random"], originals["get_random_special"], originals["spawn"])
        ours = BallSpawnView.__dict__["get_random"].__func__

        async def other_get_random(cls, bot):
            return await ours(cls, bot)

        setattr(BallSpawnView, "get_random", classmethod(other_get_random))
        hooks._restore_patches(state)

        assert BallSpawnView.__dict__["get_random"].__func__ is other_get_random
        assert state.patch_token is None
    finally:
        setattr(BallSpawnView, "get_random", saved["get_random"])
        setattr(BallSpawnView, "get_random_special", saved["get_random_special"])
        setattr(BallSpawnView, "spawn", saved["spawn"])


def check_progress_view_timeout() -> None:
    import asyncio
    import contextlib
    from unittest.mock import AsyncMock

    import discord
    from anniversary.package.views import ProgressView

    class FakeResponse:
        status = 401
        reason = "Unauthorized"

    async def run() -> None:
        state = EventState(cast(Any, None))
        view = ProgressView(state)
        refresh_task = view._refresh_task

        edit = AsyncMock(
            side_effect=discord.HTTPException(
                cast(Any, FakeResponse()), {"code": 50027, "message": "Invalid Webhook Token"}
            )
        )
        view.original_message = cast(Any, SimpleNamespace(edit=edit))

        await view.on_timeout()
        assert view._refresh_task is None
        assert edit.await_count == 1
        assert all(button.view is view for button in (view.previous_button, view.refresh_button, view.next_button))

        if refresh_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task

    asyncio.run(run())


def main() -> None:
    check_matching()
    check_formatting()
    check_models()
    check_spawn_wrappers()
    check_patch_restore_order()
    check_progress_view_timeout()
    print("anniversary checks passed")


if __name__ == "__main__":
    main()
