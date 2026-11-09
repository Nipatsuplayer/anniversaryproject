from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Iterable

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from bd_models.models import Ball, BallInstance

from . import config
from .models import ActionType, EventAction, RewardGrant, RewardType, Task, TaskContributor, TaskSpecies, TaskType
from .state import EventState, TaskSnapshot

log = logging.getLogger(config.LOGGER_NAME)

_grant_locks: dict[int, asyncio.Lock] = {}

CATCH_TYPES = (
    TaskType.CATCH_ANY,
    TaskType.CATCH_BALLS,
    TaskType.CATCH_SPECIALS,
    TaskType.UNIQUE_SPECIES,
    TaskType.CATCH_RARE,
)


@dataclass(frozen=True)
class Action:
    action_type: str
    player_id: int
    dedupe_key: str
    other_player_id: int | None = None
    ball_id: int | None = None
    special_id: int | None = None
    guild_id: int | None = None
    trade_id: int | None = None
    ball_instance_id: int | None = None
    quantity: int = 1


def matches(
    task_type: str,
    ball_ids: Iterable[int],
    special_ids: Iterable[int],
    guild_ids: Iterable[int],
    action: Action,
    *,
    max_rarity: float | None = None,
    rarity_ball_ids: Iterable[int] | None = None,
) -> bool:
    """Whether an action counts toward a task with the given filters."""
    if action.action_type == ActionType.CATCH:
        if task_type not in CATCH_TYPES:
            return False
        guild_filter = set(guild_ids)
        if guild_filter and (action.guild_id is None or action.guild_id not in guild_filter):
            return False
        rarity_filter = set(rarity_ball_ids) if rarity_ball_ids is not None else None
        if task_type == TaskType.CATCH_RARE:
            if action.ball_id is None or max_rarity is None:
                return False
            return rarity_filter is None or action.ball_id in rarity_filter
        if max_rarity is not None:
            if action.ball_id is None:
                return False
            if rarity_filter is not None and action.ball_id not in rarity_filter:
                return False
        if task_type == TaskType.CATCH_BALLS:
            return action.ball_id is not None and action.ball_id in set(ball_ids)
        if task_type == TaskType.CATCH_SPECIALS:
            return action.special_id is not None and action.special_id in set(special_ids)
        if task_type == TaskType.UNIQUE_SPECIES:
            return action.ball_id is not None
        return True

    if action.action_type == ActionType.TRADE:
        return task_type == TaskType.TRADES

    if action.action_type == ActionType.DONATION:
        return task_type == TaskType.DONATIONS

    return False


def matches_snapshot(task: TaskSnapshot, action: Action) -> bool:
    return matches(
        task.task_type,
        task.ball_ids,
        task.special_ids,
        task.guild_ids,
        action,
        max_rarity=task.max_rarity,
        rarity_ball_ids=task.rarity_ball_ids,
    )


def _contributors(action: Action) -> dict[int, int]:
    """Player id -> contribution amount for one action."""
    if action.action_type == ActionType.TRADE:
        contributors = {action.player_id: 1}
        if action.other_player_id is not None:
            contributors[action.other_player_id] = 1
        return contributors
    return {action.player_id: max(1, action.quantity)}


async def _apply_to_snapshot(task: TaskSnapshot, action: Action) -> int:
    """Update contributors and return how much the task progress increased."""
    if task.task_type == TaskType.UNIQUE_SPECIES:
        if action.ball_id is None:
            return 0
        _, created = await TaskSpecies.objects.aget_or_create(task_id=task.task_id, ball_id=action.ball_id)
        if not created:
            return 0
        increment = 1
    else:
        increment = max(1, action.quantity) if action.action_type != ActionType.TRADE else 1

    now = timezone.now()
    for player_id, amount in _contributors(action).items():
        contributor, _ = await TaskContributor.objects.aget_or_create(
            task_id=task.task_id, player_id=player_id, defaults={"amount": 0}
        )
        await TaskContributor.objects.filter(pk=contributor.pk).aupdate(
            amount=F("amount") + amount, last_contribution_at=now
        )
    return increment


async def record_action(state: EventState, action: Action) -> list[int]:
    """
    Store an action, update every matching task and return the ids of tasks that
    were completed by it.
    """
    event = state.event
    if event is None or not state.active:
        return []

    defaults = {
        "action_type": action.action_type,
        "player_id": action.player_id,
        "other_player_id": action.other_player_id,
        "ball_id": action.ball_id,
        "special_id": action.special_id,
        "guild_id": action.guild_id,
        "trade_id": action.trade_id,
        "ball_instance_id": action.ball_instance_id,
        "quantity": action.quantity,
    }
    try:
        _, created = await EventAction.objects.aget_or_create(
            event=event, dedupe_key=action.dedupe_key, defaults=defaults
        )
    except Exception:
        log.exception("Could not store an anniversary action, progress may be incomplete.")
        return []
    if not created:
        return []

    completed: list[int] = []
    async with state.lock:
        for task in list(state.tasks):
            if task.completed or not matches_snapshot(task, action):
                continue
            increment = await _apply_to_snapshot(task, action)
            if increment <= 0:
                continue

            await Task.objects.filter(pk=task.task_id, completed_at__isnull=True).aupdate(
                progress=F("progress") + increment
            )
            task.progress += increment
            if task.progress < task.target:
                continue

            updated = await Task.objects.filter(pk=task.task_id, completed_at__isnull=True).aupdate(
                completed_at=timezone.now()
            )
            if updated:
                task.completed = True
                completed.append(task.task_id)
        state.recompute_boosters()

    for task_id in completed:
        try:
            await grant_rewards(task_id)
        except Exception:
            log.exception("Could not grant the rewards of completed task %s.", task_id)
    if completed:
        state.recompute_boosters()
    return completed


async def grant_rewards(task_id: int) -> None:
    """Grant all missing collectible rewards of a completed task, idempotently."""
    lock = _grant_locks.setdefault(task_id, asyncio.Lock())
    async with lock:
        try:
            task = await Task.objects.select_related("event").aget(pk=task_id)
        except Task.DoesNotExist:
            return
        if task.completed_at is None:
            return

        rewards = [reward async for reward in task.rewards.all()]
        collectible_rewards = [
            reward
            for reward in rewards
            if reward.reward_type in (RewardType.GRANT_COLLECTIBLE, RewardType.GRANT_RANDOM_BALL)
        ]
        processed = True
        if collectible_rewards:
            contributor_ids = [
                player_id
                async for player_id in TaskContributor.objects.filter(task_id=task_id).values_list(
                    "player_id", flat=True
                )
            ]
            for reward in collectible_rewards:
                granted = await sync_to_async(_grant_collectible_reward, thread_sensitive=True)(
                    task.pk,
                    reward.pk,
                    reward.ball_id,
                    reward.special_id,
                    reward.quantity,
                    reward.attack_bonus,
                    reward.health_bonus,
                    contributor_ids,
                    reward.reward_type,
                )
                processed = granted and processed

        if processed:
            await Task.objects.filter(pk=task_id).aupdate(rewards_processed_at=timezone.now())


def _grant_collectible_reward(
    task_id: int,
    reward_id: int,
    ball_id: int | None,
    special_id: int | None,
    quantity: int,
    attack_bonus: int,
    health_bonus: int,
    contributor_ids: list[int],
    reward_type: str | None = None,
) -> bool:
    if not contributor_ids:
        return True

    quantity = max(1, quantity)
    if reward_type == RewardType.GRANT_RANDOM_BALL:
        task_balls = list(Task.objects.get(pk=task_id).balls.all())
        candidates = task_balls or list(Ball.objects.filter(enabled=True))
        if not candidates:
            log.error("Reward %s of task %s has no collectible to choose from.", reward_id, task_id)
            return False
        ball_id = random.choice(candidates).pk
    elif ball_id is None:
        log.error("Reward %s of task %s has no collectible to grant.", reward_id, task_id)
        return True

    try:
        with transaction.atomic():
            instances: list[BallInstance] = []
            for player_id in contributor_ids:
                _, created = RewardGrant.objects.get_or_create(
                    task_id=task_id, reward_id=reward_id, player_id=player_id
                )
                if not created:
                    continue
                for _ in range(quantity):
                    instances.append(
                        BallInstance(
                            ball_id=ball_id,
                            special_id=special_id,
                            player_id=player_id,
                            attack_bonus=attack_bonus,
                            health_bonus=health_bonus,
                            spawned_time=None,
                            server_id=None,
                        )
                    )
            if instances:
                BallInstance.objects.bulk_create(instances, batch_size=config.REWARD_BATCH_SIZE)
    except Exception:
        log.exception("Could not grant collectible reward %s for task %s.", reward_id, task_id)
        return False
    return True


async def reconcile_event(event_id: int) -> None:
    """Grant missing collectible rewards for every completed task of an event."""
    task_ids = [
        task_id
        async for task_id in Task.objects.filter(
            event_id=event_id,
            completed_at__isnull=False,
            rewards_processed_at__isnull=True,
            rewards__reward_type__in=(RewardType.GRANT_COLLECTIBLE, RewardType.GRANT_RANDOM_BALL),
        )
        .distinct()
        .values_list("pk", flat=True)
    ]
    for task_id in task_ids:
        try:
            await grant_rewards(task_id)
        except Exception:
            log.exception("Could not reconcile rewards of task %s.", task_id)


def _sync_task_filters(task: Task) -> tuple[set[int], set[int], set[int], float | None, set[int] | None]:
    max_rarity = task.max_rarity
    rarity_ball_ids = None
    if max_rarity is not None:
        rarity_ball_ids = set(Ball.objects.filter(rarity__lte=max_rarity).values_list("pk", flat=True))
    return (
        set(task.balls.values_list("pk", flat=True)),
        set(task.specials.values_list("pk", flat=True)),
        set(task.guilds.values_list("guild_id", flat=True)),
        max_rarity,
        rarity_ball_ids,
    )


def recalculate_task(task: Task) -> tuple[int, int]:
    """
    Rebuild a task from the stored action log (synchronous, for the admin panel).

    Existing reward grants are deliberately kept: already granted collectibles can
    never be taken back, and the unique constraint prevents duplicates.
    """
    ball_ids, special_ids, guild_ids, max_rarity, rarity_ball_ids = _sync_task_filters(task)
    contributors: dict[int, int] = {}
    species: set[int] = set()
    progress = 0
    now = timezone.now()

    for action in EventAction.objects.filter(event_id=task.event_id).iterator():
        match = Action(
            action_type=action.action_type,
            player_id=action.player_id,
            other_player_id=action.other_player_id,
            dedupe_key=action.dedupe_key,
            ball_id=action.ball_id,
            special_id=action.special_id,
            guild_id=action.guild_id,
            trade_id=action.trade_id,
            ball_instance_id=action.ball_instance_id,
            quantity=action.quantity,
        )
        if not matches(
            task.task_type,
            ball_ids,
            special_ids,
            guild_ids,
            match,
            max_rarity=max_rarity,
            rarity_ball_ids=rarity_ball_ids,
        ):
            continue
        if task.task_type == TaskType.UNIQUE_SPECIES:
            if match.ball_id is None or match.ball_id in species:
                continue
            species.add(match.ball_id)
            increment = 1
        else:
            increment = 1 if match.action_type == ActionType.TRADE else max(1, match.quantity)
        progress += increment
        for player_id, amount in _contributors(match).items():
            contributors[player_id] = contributors.get(player_id, 0) + amount
        if progress >= task.target:
            break

    progress = min(progress, task.target)
    TaskContributor.objects.filter(task_id=task.pk).delete()
    TaskSpecies.objects.filter(task_id=task.pk).delete()
    if contributors:
        TaskContributor.objects.bulk_create(
            [
                TaskContributor(task_id=task.pk, player_id=player_id, amount=amount, last_contribution_at=now)
                for player_id, amount in contributors.items()
            ],
            batch_size=config.REWARD_BATCH_SIZE,
        )
    if species:
        TaskSpecies.objects.bulk_create(
            [TaskSpecies(task_id=task.pk, ball_id=ball_id) for ball_id in species], batch_size=config.REWARD_BATCH_SIZE
        )

    task.progress = progress
    task.completed_at = now if progress >= task.target else None
    task.rewards_processed_at = None
    task.save(update_fields=("progress", "completed_at", "rewards_processed_at"))
    return progress, len(contributors)
