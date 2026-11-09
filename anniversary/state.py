from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils import timezone

from bd_models.models import Ball

from . import config
from .models import Event, RewardType, Task

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger(config.LOGGER_NAME)


@dataclass(frozen=True)
class RewardSnapshot:
    reward_id: int
    name: str
    emoji: str
    reward_type: str
    multiplier: float
    rarity_multiplier: float
    special_chance_multiplier: float
    summary: str
    is_global: bool


@dataclass
class TaskSnapshot:
    task_id: int
    name: str
    task_type: str
    target: int
    progress: int
    completed: bool
    emoji: str = ""
    ball_ids: frozenset[int] = field(default_factory=frozenset)
    special_ids: frozenset[int] = field(default_factory=frozenset)
    guild_ids: frozenset[int] = field(default_factory=frozenset)
    max_rarity: float | None = None
    rarity_ball_ids: frozenset[int] = field(default_factory=frozenset)
    rewards: tuple[RewardSnapshot, ...] = ()

    @property
    def percent(self) -> float:
        if self.target <= 0:
            return 100.0
        return min(100.0, self.progress / self.target * 100)


class EventState:
    """In-memory snapshot of the active event, used by hooks and listeners."""

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        self.event: Event | None = None
        self.tasks: list[TaskSnapshot] = []
        self.spawn_multiplier = 1.0
        self.rarity_multiplier = 1.0
        self.special_chance_multiplier = 1.0
        self.catch_window_multiplier = 1.0
        self.lock = asyncio.Lock()
        self.spawner: object | None = None
        self.hooks_installed = False
        self.hook_originals: dict[str, object] = {}
        self.patch_token: object | None = None

    @property
    def active(self) -> bool:
        return self.event is not None and self.event.is_active()

    @property
    def event_id(self) -> int | None:
        return self.event.pk if self.event else None

    def clear(self) -> None:
        self.event = None
        self.tasks = []
        self.recompute_boosters()

    async def load(self, event: Event) -> None:
        """Reload the event snapshot and all of its enabled tasks."""
        async with self.lock:
            tasks: list[TaskSnapshot] = []
            query = (
                Task.objects.filter(event=event, enabled=True)
                .prefetch_related("balls", "specials", "guilds", "rewards__ball")
                .order_by("order", "pk")
            )
            async for task in query:
                rewards = ()
                reward_rows = [reward async for reward in task.rewards.all()]
                if reward_rows:
                    rewards = tuple(
                        RewardSnapshot(
                            reward_id=reward.pk,
                            name=reward.name,
                            emoji=reward.emoji,
                            reward_type=reward.reward_type,
                            multiplier=reward.multiplier,
                            rarity_multiplier=reward.rarity_multiplier,
                            special_chance_multiplier=reward.special_chance_multiplier,
                            summary=reward.summary,
                            is_global=reward.is_global,
                        )
                        for reward in reward_rows
                    )
                rarity_ball_ids = frozenset()
                if task.max_rarity is not None:
                    rarity_ball_ids = frozenset(
                        [
                            ball_id
                            async for ball_id in Ball.objects.filter(rarity__lte=task.max_rarity).values_list(
                                "pk", flat=True
                            )
                        ]
                    )
                tasks.append(
                    TaskSnapshot(
                        task_id=task.pk,
                        name=task.name,
                        task_type=task.task_type,
                        target=task.target,
                        progress=task.progress,
                        completed=task.completed_at is not None,
                        emoji=task.emoji,
                        ball_ids=frozenset([ball.pk async for ball in task.balls.all()]),
                        special_ids=frozenset([special.pk async for special in task.specials.all()]),
                        guild_ids=frozenset([guild.guild_id async for guild in task.guilds.all()]),
                        max_rarity=task.max_rarity,
                        rarity_ball_ids=rarity_ball_ids,
                        rewards=rewards,
                    )
                )
            self.event = event
            self.tasks = tasks
            self.recompute_boosters()
            log.debug("Loaded event %r with %s task(s), now active=%s.", event.name, len(tasks), event.is_active())

    def recompute_boosters(self) -> None:
        spawn = 1.0
        rarity = 1.0
        special = 1.0
        catch_window = 1.0
        for task in self.tasks:
            if not task.completed:
                continue
            for reward in task.rewards:
                if reward.reward_type == RewardType.SPAWN_MULTIPLIER:
                    spawn *= max(1.0, reward.multiplier)
                elif reward.reward_type == RewardType.LUCK_BOOST:
                    rarity *= max(1.0, reward.rarity_multiplier)
                    special *= max(1.0, reward.special_chance_multiplier)
                elif reward.reward_type == RewardType.CATCH_WINDOW:
                    catch_window *= max(1.0, reward.multiplier)
        self.spawn_multiplier = spawn
        self.rarity_multiplier = rarity
        self.special_chance_multiplier = special
        self.catch_window_multiplier = catch_window

    def mark_completed(self, task_id: int) -> None:
        for task in self.tasks:
            if task.task_id == task_id:
                task.completed = True
                task.progress = max(task.progress, task.target)
                break
        self.recompute_boosters()

    def update_progress(self, task_id: int, progress: int) -> None:
        for task in self.tasks:
            if task.task_id == task_id:
                task.progress = progress
                break

    @property
    def completed_count(self) -> int:
        return sum(1 for task in self.tasks if task.completed)

    @property
    def global_rewards(self) -> list[RewardSnapshot]:
        rewards: list[RewardSnapshot] = []
        for task in self.tasks:
            if task.completed:
                rewards.extend(reward for reward in task.rewards if reward.is_global)
        return rewards

    @staticmethod
    def is_now_active(event: Event | None) -> bool:
        return event is not None and event.is_active(timezone.now())
