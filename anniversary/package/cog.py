from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands
from django.db import DatabaseError
from django.utils import timezone

from bd_models.models import BallInstance, Player

from .. import config, hooks
from ..formatting import parse_colour
from ..models import ActionType, Event, Task
from ..services import Action, reconcile_event, record_action
from ..state import EventState

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot
    from ballsdex.core.events import BallCaughtEvent, BallGivenEvent, TradeCompletedEvent

log = logging.getLogger(config.LOGGER_NAME)


def _ballsdex_dispatches_events() -> bool:
    """
    Whether the running BallsDex version dispatches ballsdex events.

    Task progression relies on the ``ballsdex_ball_caught``, ``ballsdex_trade_completed``
    and ``ballsdex_ball_given`` events, added after the 3.2.0 release. On older bots
    those events are never dispatched, so the listeners below stay silent and no
    catch, trade or donation is ever counted.
    """
    return importlib.util.find_spec("ballsdex.core.events") is not None


def _when(moment: datetime) -> str:
    stamp = f"{moment:%Y-%m-%d %H:%M} UTC"
    if not config.SCHEDULE_TIMEZONE:
        return stamp
    try:
        local = moment.astimezone(ZoneInfo(config.SCHEDULE_TIMEZONE))
    except (ZoneInfoNotFoundError, ValueError):
        return stamp
    return f"{stamp} ({local:%Y-%m-%d %H:%M %Z})"


class Anniversary(commands.Cog):
    """Anniversary event: shared tasks, global boosters and collectible rewards."""

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        self.state = EventState(bot)
        self._uninstall: Callable[[], None] | None = None
        self._task: asyncio.Task | None = None
        self._db_error_logged = False
        self._dispatch_support_logged = False
        self._mode: str | None = None

    async def cog_load(self) -> None:
        self._task = asyncio.create_task(self._run(), name="anniversary-event")
        log.info("Anniversary event package loaded, waiting for the admin panel settings.")

    async def cog_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self._deactivate()
        hooks.strip(self.bot)

    async def _run(self) -> None:
        while True:
            try:
                delay = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error in the anniversary event task.")
                delay = float(config.SETTINGS_POLL_INTERVAL)
            await asyncio.sleep(delay)

    async def _tick(self) -> float:
        try:
            events = [event async for event in Event.objects.filter(enabled=True).order_by("created_at")]
        except DatabaseError:
            if not self._db_error_logged:
                log.error(
                    "Cannot read the anniversary event settings, did you run 'python3 -m django migrate anniversary'?",
                    exc_info=True,
                )
                self._db_error_logged = True
            return float(config.SETTINGS_POLL_INTERVAL)
        self._db_error_logged = False

        now = timezone.now()
        event = next((candidate for candidate in events if candidate.is_active(now)), None)
        if event is None and events:
            upcoming = [candidate for candidate in events if candidate.starts_at and candidate.starts_at > now]
            if upcoming:
                event = min(upcoming, key=lambda candidate: candidate.starts_at or now)
            else:
                event = events[-1]

        if event is None:
            self._deactivate()
            disabled = (
                await Event.objects.filter(enabled=False)
                .exclude(starts_at__isnull=True, ends_at__isnull=True)
                .order_by("created_at")
                .afirst()
            )
            if disabled is not None:
                self._set_mode("disabled", disabled)
            else:
                self._set_mode("no event")
            return float(config.SETTINGS_POLL_INTERVAL)

        if not self._dispatch_support_logged:
            self._dispatch_support_logged = True
            if not _ballsdex_dispatches_events():
                log.error(
                    "This BallsDex installation does not dispatch ballsdex events "
                    "(ballsdex.core.events is missing, event dispatches were added after the 3.2.0 release): "
                    "no catch, trade or donation will ever be counted, tasks cannot progress. "
                    "Update the bot to a version with event dispatch support and restart it."
                )

        await self.state.load(event)
        if event.is_active(now):
            try:
                uninstall = hooks.ensure(self.bot, self.state)
            except Exception:
                log.exception("Could not hook the anniversary event into the bot, it stays inactive.")
                return float(config.SETTINGS_POLL_INTERVAL)
            if uninstall is not None:
                self._uninstall = uninstall
                log.info(
                    "Anniversary event %r is now ACTIVE. It stops at %s.",
                    event.name,
                    _when(event.ends_at) if event.ends_at else "manually (untick enabled)",
                )
            self._set_mode("active")
            try:
                await reconcile_event(event.pk)
            except Exception:
                log.exception("Could not reconcile anniversary rewards.")
        else:
            if self.state.hooks_installed:
                self._uninstall_hooks()
            self._set_mode("scheduled" if event.starts_at and event.starts_at > now else "inactive", event)

        delays = [float(config.SETTINGS_POLL_INTERVAL), float(config.MAX_POLL_INTERVAL)]
        if boundary := event.next_boundary(now):
            delays.append(max(0.5, (boundary - now).total_seconds()))
        return max(0.5, min(delays))

    def _set_mode(self, mode: str, event: Event | None = None) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "no event":
            log.info("No enabled anniversary event was found; the event is idle.")
        elif mode == "disabled" and event is not None:
            log.warning(
                "Anniversary event %r is disabled but has a schedule; tick 'enabled' in the admin panel to run it.",
                event.name,
            )
        elif mode == "scheduled" and event is not None:
            log.info(
                "Anniversary event %r is scheduled to start at %s.",
                event.name,
                _when(event.starts_at) if event.starts_at else "an unknown time",
            )
        elif mode == "inactive" and event is not None:
            log.info("Anniversary event %r is enabled but not inside its scheduled window.", event.name)

    def _uninstall_hooks(self) -> None:
        if self.state.hooks_installed:
            hooks.uninstall(self.bot, self.state)
        self._uninstall = None

    def _deactivate(self) -> None:
        self._uninstall_hooks()
        self.state.clear()

    @commands.Cog.listener()
    async def on_ballsdex_ball_caught(self, event: "BallCaughtEvent") -> None:
        if event.dropped or not self.state.active:
            return
        instance = event.ball_instance
        action = Action(
            action_type=ActionType.CATCH,
            player_id=event.player.pk,
            ball_id=instance.ball_id,
            special_id=instance.special_id,
            guild_id=event.guild.id if event.guild else None,
            ball_instance_id=instance.pk,
            dedupe_key=f"catch:{instance.pk}",
        )
        completed = await record_action(self.state, action)
        await self._announce(completed)

    @commands.Cog.listener()
    async def on_ballsdex_trade_completed(self, event: "TradeCompletedEvent") -> None:
        if not self.state.active:
            return
        action = Action(
            action_type=ActionType.TRADE,
            player_id=event.trader1.player.pk,
            other_player_id=event.trader2.player.pk,
            trade_id=event.trade.pk,
            dedupe_key=f"trade:{event.trade.pk}",
        )
        completed = await record_action(self.state, action)
        await self._announce(completed)

    @commands.Cog.listener()
    async def on_ballsdex_ball_given(self, event: "BallGivenEvent") -> None:
        if not self.state.active:
            return
        instances = event.ball_instances
        if not instances:
            return
        first = instances[0]
        action = Action(
            action_type=ActionType.DONATION,
            player_id=event.sender.pk,
            other_player_id=event.recipient.pk,
            ball_id=first.ball_id,
            special_id=first.special_id,
            trade_id=event.trade.pk,
            quantity=max(1, len(instances)),
            dedupe_key=f"given:{event.trade.pk}",
        )
        completed = await record_action(self.state, action)
        await self._announce(completed)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        if not self.state.active:
            return
        if ctx.command is None or ctx.command.name != "give":
            return
        if not ctx.command.full_parent_name.startswith("admin "):
            return
        user = ctx.kwargs.get("user")
        flags = ctx.kwargs.get("flags")
        if user is None or flags is None:
            return
        countryball = getattr(flags, "countryball", None)
        if countryball is None:
            return
        sender, _ = await Player.objects.aget_or_create(discord_id=ctx.author.id)
        recipient, _ = await Player.objects.aget_or_create(discord_id=user.id)
        if sender.pk == recipient.pk:
            return
        special = getattr(flags, "special", None)
        reference_time = ctx.interaction.created_at if ctx.interaction else ctx.message.created_at
        instance = (
            await BallInstance.objects.filter(
                player=recipient,
                ball=countryball,
                special=special,
                spawned_time__isnull=True,
                server_id__isnull=True,
                catch_date__gte=reference_time - timedelta(seconds=10),
            )
            .order_by("-pk")
            .afirst()
        )
        action = Action(
            action_type=ActionType.DONATION,
            player_id=sender.pk,
            other_player_id=recipient.pk,
            ball_id=countryball.pk,
            special_id=special.pk if special else None,
            ball_instance_id=instance.pk if instance else None,
            quantity=1,
            dedupe_key=f"admin-give:{ctx.interaction.id if ctx.interaction else ctx.message.id}",
        )
        completed = await record_action(self.state, action)
        await self._announce(completed)

    async def _announce(self, task_ids: list[int]) -> None:
        if not task_ids:
            return
        event = self.state.event
        if event is None or not event.announcement_channel_id:
            return
        channel = self.bot.get_channel(event.announcement_channel_id)
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            return

        tasks = [task async for task in Task.objects.filter(pk__in=task_ids).prefetch_related("rewards__ball")]
        if not tasks:
            return
        lines = [f"{event.emoji} **Community goals completed!**"]
        for task in tasks:
            lines.append(f"{task.emoji} **{task.name}** \N{MIDDLE DOT} goal {task.target:,}")
            rewards = [reward async for reward in task.rewards.all()]
            if rewards:
                lines.append("Rewards: " + ", ".join(f"{r.emoji} {r.summary}" for r in rewards))
        embed = discord.Embed(
            title=f"{event.emoji} {event.name}", description="\n".join(lines), colour=parse_colour(event.accent_color)
        )
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Could not announce a completed anniversary task in channel %s.", channel.id, exc_info=True)
