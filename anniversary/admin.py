from __future__ import annotations

from asgiref.sync import async_to_sync
from django import forms
from django.contrib import admin, messages
from django.db.models import F
from django.utils import timezone

from .models import (
    CATCH_TASK_TYPES,
    Event,
    EventAction,
    Reward,
    RewardGrant,
    Task,
    TaskContributor,
    TaskSpecies,
    TaskType,
)
from .services import grant_rewards, recalculate_task


class TaskAdminForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        task_type = cleaned.get("task_type")
        if task_type == TaskType.CATCH_BALLS and not cleaned.get("balls"):
            self.add_error("balls", "Select at least one collectible for this task.")
        if task_type == TaskType.CATCH_SPECIALS and not cleaned.get("specials"):
            self.add_error("specials", "Select at least one special card for this task.")
        max_rarity = cleaned.get("max_rarity")
        if task_type == TaskType.CATCH_RARE and max_rarity is None:
            self.add_error("max_rarity", "Set the maximum rarity for a rare-catch task.")
        if max_rarity is not None and max_rarity <= 0:
            self.add_error("max_rarity", "The maximum rarity must be greater than zero.")
        if cleaned.get("guilds") and task_type not in CATCH_TASK_TYPES:
            self.add_error("guilds", "Server filters are only supported by catch tasks.")
        return cleaned


class TaskInline(admin.StackedInline):
    model = Task
    form = TaskAdminForm
    fk_name = "event"
    extra = 0
    show_change_link = True
    fields = (
        "name",
        "description",
        "emoji",
        "order",
        "enabled",
        "task_type",
        "target",
        "max_rarity",
        "balls",
        "specials",
        "guilds",
        "rewards",
        "progress",
        "completed_at",
    )
    readonly_fields = ("progress", "completed_at")
    filter_horizontal = ("balls", "specials", "guilds", "rewards")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "enabled", "starts_at", "ends_at", "task_summary")
    list_filter = ("enabled",)
    search_fields = ("name", "description")
    readonly_fields = ("status", "task_summary", "created_at")
    inlines = (TaskInline,)
    fieldsets = (
        (
            "Event",
            {
                "description": (
                    "All times are UTC. Tick Enabled to activate the event, including scheduled "
                    "events. Set the dates to start and stop automatically, or leave them empty "
                    "to control the event manually with Enabled. Only one enabled event may cover "
                    "any moment in time."
                ),
                "fields": ("name", "description", "emoji", "accent_color", "enabled", "starts_at", "ends_at"),
            },
        ),
        (
            "Progress display",
            {
                "fields": (
                    "announcement_channel_id",
                    "progress_refresh_seconds",
                    "spawn_floor_seconds",
                    "status",
                    "task_summary",
                )
            },
        ),
        ("Audit", {"classes": ("collapse",), "fields": ("created_at",)}),
    )

    @admin.display(description="Status")
    def status(self, obj: Event) -> str:
        return obj.status


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    form = TaskAdminForm
    list_display = ("name", "event", "task_type", "progress", "target", "completed_at", "enabled")
    list_filter = ("event", "task_type", "enabled")
    search_fields = ("name", "description", "event__name")
    filter_horizontal = ("balls", "specials", "guilds", "rewards")
    readonly_fields = ("progress", "completed_at", "rewards_processed_at", "created_at")
    actions = ("reset_progress", "recalculate", "mark_complete", "grant_missing")
    fieldsets = (
        ("Task", {"fields": ("event", "name", "description", "emoji", "order", "enabled")}),
        (
            "Goal",
            {
                "description": (
                    "Specific collectible/special lists and server filters apply to catch tasks. "
                    "Server filters are ignored by trade and donation tasks."
                ),
                "fields": ("task_type", "target", "max_rarity", "balls", "specials", "guilds"),
            },
        ),
        ("Rewards", {"fields": ("rewards",)}),
        (
            "Progress",
            {
                "fields": ("progress", "completed_at", "rewards_processed_at", "created_at"),
                "description": (
                    "Progress is maintained automatically. Use the admin actions below to reset, "
                    "recalculate from the action log, or grant missing collectible rewards."
                ),
            },
        ),
    )

    @admin.action(description="Reset progress and contributors")
    def reset_progress(self, request, queryset):
        TaskContributor.objects.filter(task__in=queryset).delete()
        TaskSpecies.objects.filter(task__in=queryset).delete()
        updated = queryset.update(progress=0, completed_at=None, rewards_processed_at=None)
        self.message_user(request, f"Reset {updated} task(s).", messages.SUCCESS)

    @admin.action(description="Recalculate progress from the action log")
    def recalculate(self, request, queryset):
        for task in queryset:
            progress, contributors = recalculate_task(task)
            self.message_user(
                request,
                f"{task.name}: {progress:,}/{task.target:,} progress, {contributors} contributor(s).",
                messages.SUCCESS,
            )

    @admin.action(description="Mark complete")
    def mark_complete(self, request, queryset):
        updated = queryset.update(progress=F("target"), completed_at=timezone.now(), rewards_processed_at=None)
        self.message_user(request, f"Marked {updated} task(s) complete.", messages.SUCCESS)

    @admin.action(description="Grant missing collectible rewards")
    def grant_missing(self, request, queryset):
        granted = 0
        for task in queryset.filter(completed_at__isnull=False):
            try:
                async_to_sync(grant_rewards)(task.pk)
            except Exception as error:
                self.message_user(request, f"{task.name}: {error}", messages.ERROR)
            else:
                granted += 1
        self.message_user(request, f"Checked rewards for {granted} completed task(s).", messages.SUCCESS)


@admin.register(Reward)
class RewardAdmin(admin.ModelAdmin):
    list_display = ("name", "reward_type", "summary", "multiplier", "rarity_multiplier", "ball")
    list_filter = ("reward_type",)
    search_fields = ("name", "description", "text")
    fieldsets = (
        ("Reward", {"fields": ("name", "description", "emoji", "reward_type")}),
        (
            "Global boosters",
            {
                "description": (
                    "Global rewards apply to every server as soon as the task is complete. "
                    "Same-type boosters stack multiplicatively."
                ),
                "fields": ("multiplier", "rarity_multiplier", "special_chance_multiplier"),
            },
        ),
        (
            "Collectible grant",
            {
                "description": (
                    "Only contributors receive collectible rewards. Each contributor is granted once. "
                    "Random grants prefer the task's collectible list and fall back to enabled collectibles."
                ),
                "fields": ("ball", "special", "quantity", "attack_bonus", "health_bonus"),
            },
        ),
        ("Custom text", {"fields": ("text",)}),
    )

    @admin.display(description="Summary")
    def summary(self, obj: Reward) -> str:
        return obj.summary


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(EventAction)
class EventActionAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "event", "action_type", "player", "ball", "special", "guild_id", "quantity")
    list_filter = ("event", "action_type")
    search_fields = ("dedupe_key", "player__discord_id")
    date_hierarchy = "created_at"
    readonly_fields = tuple(field.name for field in EventAction._meta.fields)


@admin.register(TaskContributor)
class TaskContributorAdmin(ReadOnlyAdmin):
    list_display = ("task", "player", "amount", "last_contribution_at")
    list_filter = ("task__event", "task")
    search_fields = ("player__discord_id", "task__name")
    readonly_fields = ("task", "player", "amount", "last_contribution_at")


@admin.register(TaskSpecies)
class TaskSpeciesAdmin(ReadOnlyAdmin):
    list_display = ("task", "ball", "created_at")
    list_filter = ("task__event", "task")
    search_fields = ("task__name", "ball__country")
    readonly_fields = ("task", "ball", "created_at")


@admin.register(RewardGrant)
class RewardGrantAdmin(ReadOnlyAdmin):
    list_display = ("task", "reward", "player", "created_at")
    list_filter = ("task__event", "reward")
    search_fields = ("task__name", "reward__name", "player__discord_id")
    readonly_fields = ("task", "reward", "player", "created_at")
