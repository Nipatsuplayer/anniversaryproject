from __future__ import annotations

from datetime import datetime
from datetime import timezone as dt_timezone

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

from bd_models.models import Ball, BallInstance, GuildConfig, Player, Special, Trade

from . import config

HEX_COLOR_RE = RegexValidator(r"^#[0-9a-fA-F]{6}$", message="Use a colour like #5865F2.")


class Event(models.Model):
    """A scheduled community event, its tasks and its rewards."""

    name = models.CharField(max_length=128, default="Anniversary event")
    description = models.TextField(blank=True, default="", help_text="Shown at the top of the progress command.")
    emoji = models.CharField(max_length=32, blank=True, default="\N{PARTY POPPER}")
    accent_color = models.CharField(
        max_length=7,
        default="#5865F2",
        validators=(HEX_COLOR_RE,),
        help_text="Accent colour used by the progress display, in #RRGGBB format.",
    )

    enabled = models.BooleanField(
        default=False, help_text="Master switch. When off, no task counts progress and no booster is applied."
    )
    starts_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Optional start date in UTC. Leave empty to start as soon as the event is enabled.",
    )
    ends_at = models.DateTimeField(
        null=True, blank=True, help_text="Optional end date in UTC. Leave empty to run until you disable the event."
    )

    announcement_channel_id = models.BigIntegerField(
        null=True, blank=True, help_text="Optional Discord channel ID where task completions are announced."
    )
    spawn_floor_seconds = models.PositiveIntegerField(
        default=120, help_text="Hard minimum number of seconds between two spawns while a spawn booster is active."
    )
    progress_refresh_seconds = models.PositiveSmallIntegerField(
        default=10,
        help_text="How often the live progress command refreshes itself, in seconds.",
        validators=(MaxValueValidator(config.MAX_PROGRESS_REFRESH),),
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    tasks: models.QuerySet[Task]

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "event"
        verbose_name_plural = "events"

    def __str__(self) -> str:
        return self.name

    def is_active(self, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        now = now or timezone.now()
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True

    def next_boundary(self, now: datetime | None = None) -> datetime | None:
        """The next date at which `is_active` can change, if any."""
        if not self.enabled:
            return None
        now = now or timezone.now()
        candidates = [value for value in (self.starts_at, self.ends_at) if value and value > now]
        return min(candidates) if candidates else None

    @property
    def status(self) -> str:
        now = timezone.now()
        if self.is_active(now):
            if self.ends_at:
                return f"ACTIVE (ends {self.ends_at:%Y-%m-%d %H:%M} UTC)"
            return "ACTIVE (manual)"
        if not self.enabled:
            if self.starts_at and self.starts_at > now:
                return f"Disabled (scheduled to start {self.starts_at:%Y-%m-%d %H:%M} UTC)"
            if self.ends_at and self.ends_at > now:
                return f"Disabled (scheduled to end {self.ends_at:%Y-%m-%d %H:%M} UTC)"
            if self.starts_at or self.ends_at:
                return "Disabled (schedule is in the past)"
            return "Disabled"
        if self.starts_at and self.starts_at > now:
            return f"Scheduled (starts {self.starts_at:%Y-%m-%d %H:%M} UTC)"
        if self.ends_at and self.ends_at <= now:
            return f"Finished (ended {self.ends_at:%Y-%m-%d %H:%M} UTC)"
        return "Inactive"

    @property
    def task_summary(self) -> str:
        total = self.tasks.count()
        completed = self.tasks.filter(completed_at__isnull=False).count()
        return f"{completed}/{total} tasks complete"

    def _interval(self) -> tuple[datetime, datetime]:
        start = self.starts_at or datetime.min.replace(tzinfo=dt_timezone.utc)
        end = self.ends_at or datetime.max.replace(tzinfo=dt_timezone.utc)
        return start, end

    def clean(self) -> None:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "The end of the event must be after its start."})
        if not 1 <= self.progress_refresh_seconds <= config.MAX_PROGRESS_REFRESH:
            raise ValidationError(
                {
                    "progress_refresh_seconds": (
                        f"The refresh interval must be between 1 and {config.MAX_PROGRESS_REFRESH} seconds."
                    )
                }
            )

        if not self.enabled:
            return

        start, end = self._interval()
        other_events = Event.objects.filter(enabled=True).exclude(pk=self.pk)
        for other in other_events:
            other_start, other_end = other._interval()
            if start < other_end and other_start < end:
                raise ValidationError(
                    {"enabled": f"This schedule overlaps the enabled event {other.name!r}. Disable it first."}
                )


class RewardType(models.TextChoices):
    SPAWN_MULTIPLIER = "spawn_multiplier", "Spawn rate booster"
    LUCK_BOOST = "luck_boost", "Rarity / special chance booster"
    CATCH_WINDOW = "catch_window", "Longer catch window"
    GRANT_COLLECTIBLE = "grant_collectible", "Grant collectible(s)"
    CUSTOM_TEXT = "custom_text", "Custom text reward"
    GRANT_RANDOM_BALL = "grant_random_ball", "Grant random collectible"


class Reward(models.Model):
    """A prize unlocked when a task is completed."""

    name = models.CharField(max_length=128)
    description = models.TextField(
        blank=True, default="", help_text="Shown next to the reward and in the completion announcement."
    )
    emoji = models.CharField(max_length=32, blank=True, default="\N{WRAPPED PRESENT}")
    reward_type = models.CharField(max_length=32, choices=RewardType.choices, default=RewardType.SPAWN_MULTIPLIER)

    multiplier = models.FloatField(
        default=1.0, help_text="Spawn-rate or catch-window multiplier. Values above 1 increase the effect."
    )
    rarity_multiplier = models.FloatField(
        default=1.0, help_text="Rarity weighting multiplier for luck boosts. 1 leaves the rarity system untouched."
    )
    special_chance_multiplier = models.FloatField(
        default=1.0, help_text="Special-card chance multiplier for luck boosts. 1 leaves the special chance untouched."
    )

    ball = models.ForeignKey(
        Ball,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Collectible granted by a grant-collectible reward.",
    )
    special = models.ForeignKey(
        Special,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Optional special card applied to a granted collectible.",
    )
    quantity = models.PositiveIntegerField(default=1, help_text="How many collectibles each contributor receives.")
    attack_bonus = models.IntegerField(
        default=0,
        validators=(MaxValueValidator(config.MAX_REWARD_BONUS),),
        help_text="Attack bonus in percent for granted collectibles.",
    )
    health_bonus = models.IntegerField(
        default=0,
        validators=(MaxValueValidator(config.MAX_REWARD_BONUS),),
        help_text="Health bonus in percent for granted collectibles.",
    )
    text = models.TextField(
        blank=True, default="", help_text="Custom text shown as the reward for a custom-text reward."
    )

    ball_id: int | None
    special_id: int | None

    class Meta:
        ordering = ("name", "pk")
        verbose_name = "reward"
        verbose_name_plural = "rewards"

    def __str__(self) -> str:
        return self.name

    @property
    def is_global(self) -> bool:
        """Whether the reward applies to everyone rather than only to contributors."""
        return self.reward_type in (RewardType.SPAWN_MULTIPLIER, RewardType.LUCK_BOOST, RewardType.CATCH_WINDOW)

    @property
    def summary(self) -> str:
        if self.reward_type == RewardType.SPAWN_MULTIPLIER:
            return f"spawn rate x{self.multiplier:g}"
        if self.reward_type == RewardType.LUCK_BOOST:
            parts = []
            if self.rarity_multiplier != 1:
                parts.append(f"rarity x{self.rarity_multiplier:g}")
            if self.special_chance_multiplier != 1:
                parts.append(f"special chance x{self.special_chance_multiplier:g}")
            return ", ".join(parts) or "no change"
        if self.reward_type == RewardType.CATCH_WINDOW:
            return f"catch window x{self.multiplier:g}"
        if self.reward_type == RewardType.GRANT_COLLECTIBLE:
            ball = self.ball.country if self.ball else "collectible"
            if self.quantity > 1:
                return f"{self.quantity}x {ball}"
            return ball
        if self.reward_type == RewardType.GRANT_RANDOM_BALL:
            if self.quantity > 1:
                return f"{self.quantity}x random collectible"
            return "random collectible"
        return self.text or "custom reward"

    def clean(self) -> None:
        if self.reward_type in (RewardType.SPAWN_MULTIPLIER, RewardType.CATCH_WINDOW):
            if self.multiplier < 1:
                raise ValidationError({"multiplier": "This multiplier cannot be lower than 1."})
        if self.reward_type == RewardType.LUCK_BOOST:
            if self.rarity_multiplier < 1:
                raise ValidationError({"rarity_multiplier": "This multiplier cannot be lower than 1."})
            if self.special_chance_multiplier < 1:
                raise ValidationError({"special_chance_multiplier": "This multiplier cannot be lower than 1."})
        if self.reward_type == RewardType.GRANT_COLLECTIBLE:
            if self.ball_id is None:
                raise ValidationError({"ball": "Select the collectible to grant."})
            if self.quantity < 1:
                raise ValidationError({"quantity": "Grant at least one collectible."})
        if self.reward_type == RewardType.CUSTOM_TEXT and not self.text.strip():
            raise ValidationError({"text": "Write the custom reward text."})


class TaskType(models.TextChoices):
    CATCH_ANY = "catch_any", "Catch collectibles"
    CATCH_BALLS = "catch_balls", "Catch specific collectible(s)"
    CATCH_SPECIALS = "catch_specials", "Catch specific special(s)"
    UNIQUE_SPECIES = "unique_species", "Catch unique species"
    TRADES = "trades", "Complete trades"
    DONATIONS = "donations", "Give/donate collectibles"
    CATCH_RARE = "catch_rare", "Catch rare balls"


CATCH_TASK_TYPES = (
    TaskType.CATCH_ANY,
    TaskType.CATCH_BALLS,
    TaskType.CATCH_SPECIALS,
    TaskType.UNIQUE_SPECIES,
    TaskType.CATCH_RARE,
)


class Task(models.Model):
    """One shared community goal."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tasks")
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    emoji = models.CharField(max_length=32, blank=True, default="\N{DIRECT HIT}")
    order = models.PositiveIntegerField(default=0, help_text="Tasks are displayed in this order.")
    enabled = models.BooleanField(default=True, help_text="Disabled tasks do not count progress.")

    task_type = models.CharField(max_length=32, choices=TaskType.choices, default=TaskType.CATCH_ANY)
    target = models.PositiveBigIntegerField(default=1, help_text="Amount required to complete this task.")
    max_rarity = models.FloatField(
        null=True,
        blank=True,
        help_text="Only count catches where the collectible's rarity weight is at most this value (lower is rarer).",
    )
    balls = models.ManyToManyField(
        Ball,
        blank=True,
        related_name="+",
        help_text="For 'Catch specific collectible(s)' tasks: the collectibles that count.",
    )
    specials = models.ManyToManyField(
        Special,
        blank=True,
        related_name="+",
        help_text="For 'Catch specific special(s)' tasks: the special cards that count.",
    )
    guilds = models.ManyToManyField(
        GuildConfig,
        blank=True,
        related_name="+",
        help_text="Optional for catch tasks: only count catches made in these servers. Empty means all servers.",
    )
    rewards = models.ManyToManyField(
        Reward, blank=True, related_name="tasks", help_text="Rewards granted or unlocked when this task is completed."
    )

    progress = models.PositiveBigIntegerField(
        default=0, help_text="Cached progress. It is updated automatically; edit it only for corrections."
    )
    completed_at = models.DateTimeField(null=True, blank=True, editable=False)
    rewards_processed_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    event_id: int

    class Meta:
        ordering = ("order", "pk")
        verbose_name = "task"
        verbose_name_plural = "tasks"

    def __str__(self) -> str:
        return self.name

    @property
    def percent(self) -> float:
        if self.target <= 0:
            return 100.0
        return min(100.0, self.progress / self.target * 100)

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None

    @property
    def is_catch_task(self) -> bool:
        return self.task_type in CATCH_TASK_TYPES

    def clean(self) -> None:
        if self.target < 1:
            raise ValidationError({"target": "The target must be at least 1."})
        if self.task_type == TaskType.CATCH_BALLS and self.pk and not self.balls.exists():
            raise ValidationError({"balls": "Select at least one collectible for this task."})
        if self.task_type == TaskType.CATCH_SPECIALS and self.pk and not self.specials.exists():
            raise ValidationError({"specials": "Select at least one special card for this task."})
        if self.task_type == TaskType.CATCH_RARE and self.max_rarity is None:
            raise ValidationError({"max_rarity": "Set the maximum rarity for a rare-catch task."})
        if self.max_rarity is not None and self.max_rarity <= 0:
            raise ValidationError({"max_rarity": "The maximum rarity must be greater than zero."})
        if self.pk and self.guilds.exists() and not self.is_catch_task:
            raise ValidationError({"guilds": "Server filters are only supported by catch tasks."})


class ActionType(models.TextChoices):
    CATCH = "catch", "Catch"
    TRADE = "trade", "Trade"
    DONATION = "donation", "Donation"


class EventAction(models.Model):
    """Append-only record of every counted community action."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="actions")
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="+")
    other_player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="+", null=True, blank=True)
    action_type = models.CharField(max_length=16, choices=ActionType.choices)

    ball = models.ForeignKey(Ball, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    special = models.ForeignKey(Special, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    guild_id = models.BigIntegerField(null=True, blank=True)
    trade = models.ForeignKey(Trade, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    ball_instance = models.ForeignKey(BallInstance, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    quantity = models.PositiveIntegerField(default=1)

    dedupe_key = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    player_id: int
    other_player_id: int | None
    ball_id: int | None
    special_id: int | None
    trade_id: int | None
    ball_instance_id: int | None

    class Meta:
        ordering = ("created_at", "pk")
        constraints = (models.UniqueConstraint(fields=("event", "dedupe_key"), name="anniversary_action_unique"),)
        indexes = (
            models.Index(fields=("event", "action_type", "created_at")),
            models.Index(fields=("event", "ball")),
            models.Index(fields=("event", "special")),
            models.Index(fields=("event", "guild_id")),
            models.Index(fields=("event", "player")),
        )

    def __str__(self) -> str:
        return f"{self.get_action_type_display()} by player #{self.player_id}"  # type: ignore[attr-defined]


class TaskContributor(models.Model):
    """How much one player contributed to one task."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="contributors")
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="+")
    amount = models.PositiveBigIntegerField(default=0)
    last_contribution_at = models.DateTimeField(default=timezone.now)

    task_id: int
    player_id: int

    class Meta:
        ordering = ("-amount", "pk")
        constraints = (models.UniqueConstraint(fields=("task", "player"), name="anniversary_contributor_unique"),)
        indexes = (models.Index(fields=("task", "amount")),)

    def __str__(self) -> str:
        return f"player #{self.player_id}: {self.amount}"


class TaskSpecies(models.Model):
    """One distinct collectible species counted by a unique-species task."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="species")
    ball = models.ForeignKey(Ball, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    task_id: int
    ball_id: int

    class Meta:
        constraints = (models.UniqueConstraint(fields=("task", "ball"), name="anniversary_species_unique"),)

    def __str__(self) -> str:
        return f"{self.task_id}: {self.ball_id}"


class RewardGrant(models.Model):
    """Tracks collectible rewards already granted to a contributor."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="grants")
    reward = models.ForeignKey(Reward, on_delete=models.CASCADE, related_name="+")
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    task_id: int
    reward_id: int
    player_id: int

    class Meta:
        ordering = ("-created_at",)
        constraints = (models.UniqueConstraint(fields=("task", "reward", "player"), name="anniversary_grant_unique"),)

    def __str__(self) -> str:
        return f"{self.reward_id} -> player #{self.player_id}"
