from __future__ import annotations

import asyncio
import logging
import math

import discord
from discord.ui import ActionRow, Button, TextDisplay
from django.utils import timezone

from ballsdex.core.discord import Container, LayoutView

from .. import config
from ..formatting import parse_colour, progress_bar
from ..state import EventState, TaskSnapshot

log = logging.getLogger(config.LOGGER_NAME)


class ProgressView(LayoutView):
    """The live, paginated progress display."""

    def __init__(self, state: EventState, page_size: int = config.PROGRESS_PAGE_SIZE):
        super().__init__(timeout=config.PROGRESS_VIEW_LIFETIME)
        self.state = state
        self.page_size = page_size
        self.page = 0
        self._refresh_task: asyncio.Task | None = None
        self._build()
        self._refresh_task = asyncio.create_task(self._auto_refresh(), name="anniversary-progress-refresh")

    @property
    def event(self):
        return self.state.event

    @property
    def page_count(self) -> int:
        if not self.state.tasks:
            return 1
        return max(1, math.ceil(len(self.state.tasks) / self.page_size))

    def _visible_tasks(self) -> list[TaskSnapshot]:
        start = self.page * self.page_size
        return self.state.tasks[start : start + self.page_size]

    def _header(self) -> str:
        event = self.event
        if event is None:
            return "No anniversary event is configured."
        lines = [f"# {event.emoji} {event.name}", f"-# {event.status}"]
        if event.description:
            lines.append(event.description)
        lines.append(
            f"**{self.state.completed_count}/{len(self.state.tasks)}** tasks complete "
            f"\N{MIDDLE DOT} page {self.page + 1}/{self.page_count}"
        )
        boosters = []
        if self.state.spawn_multiplier > 1:
            boosters.append(f"spawn x{self.state.spawn_multiplier:g}")
        if self.state.rarity_multiplier > 1:
            boosters.append(f"rarity x{self.state.rarity_multiplier:g}")
        if self.state.special_chance_multiplier > 1:
            boosters.append(f"special chance x{self.state.special_chance_multiplier:g}")
        if self.state.catch_window_multiplier > 1:
            boosters.append(f"catch window x{self.state.catch_window_multiplier:g}")
        if boosters:
            lines.append("**Global boosters:** " + ", ".join(boosters))
        else:
            lines.append("**Global boosters:** none unlocked yet")
        return "\n".join(lines)

    def _task_text(self, task: TaskSnapshot) -> str:
        progress = min(task.progress, task.target)
        lines = [
            f"{'DONE' if task.completed else 'OPEN'} {task.emoji} **{task.name}**",
            f"`{progress_bar(task.percent)}` {progress:,}/{task.target:,} ({task.percent:.0f}%)",
        ]
        if task.rewards:
            lines.append("Rewards: " + ", ".join(f"{reward.emoji} {reward.summary}" for reward in task.rewards))
        return "\n".join(lines)

    def _build(self) -> None:
        self.page = max(0, min(self.page, self.page_count - 1))
        self.clear_items()
        event = self.event
        colour = parse_colour(event.accent_color if event else "#5865F2")

        container = Container(accent_colour=colour)
        container.add_item(TextDisplay(self._header()))
        if not self.state.tasks:
            container.add_item(TextDisplay("No tasks have been added to this event yet."))
        else:
            for task in self._visible_tasks():
                container.add_item(TextDisplay(self._task_text(task)))
        refresh = event.progress_refresh_seconds if event else config.MIN_PROGRESS_REFRESH
        refresh = max(config.MIN_PROGRESS_REFRESH, refresh)
        container.add_item(TextDisplay(f"-# Refreshes every {refresh}s \N{MIDDLE DOT} preview only"))
        self.add_item(container)

        row = ActionRow()
        previous = Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page <= 0)
        previous.callback = self._previous  # type: ignore[method-assign]
        row.add_item(previous)

        refresh_button = Button(label="Refresh", style=discord.ButtonStyle.primary)
        refresh_button.callback = self._refresh_now  # type: ignore[method-assign]
        row.add_item(refresh_button)

        following = Button(label="Next", style=discord.ButtonStyle.secondary, disabled=self.page >= self.page_count - 1)
        following.callback = self._next  # type: ignore[method-assign]
        row.add_item(following)
        self.add_item(row)

    async def _previous(self, interaction: discord.Interaction) -> None:
        if self.page <= 0:
            await interaction.response.defer()
            return
        self.page -= 1
        self._build()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        if self.page >= self.page_count - 1:
            await interaction.response.defer()
            return
        self.page += 1
        self._build()
        await interaction.response.edit_message(view=self)

    async def _refresh_now(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(view=self)

    async def _auto_refresh(self) -> None:
        event = self.event
        interval = event.progress_refresh_seconds if event else config.MIN_PROGRESS_REFRESH
        interval = max(config.MIN_PROGRESS_REFRESH, min(config.MAX_PROGRESS_REFRESH, interval))
        try:
            while not self.is_finished():
                await asyncio.sleep(interval)
                if self.is_finished():
                    break
                event = self.event
                if event is None or not event.enabled or (event.ends_at and timezone.now() > event.ends_at):
                    break
                if self.original_message is None:
                    continue
                self._build()
                try:
                    await self.original_message.edit(view=self)
                except discord.NotFound:
                    break
                except discord.HTTPException:
                    log.debug("Could not refresh an anniversary progress view.", exc_info=True)
        except asyncio.CancelledError:
            raise

    async def on_timeout(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
        await super().on_timeout()
