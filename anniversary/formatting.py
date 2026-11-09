from __future__ import annotations

import discord


def progress_bar(percent: float, length: int = 10) -> str:
    """A plain-text progress bar, safe to use in a Discord TextDisplay."""
    filled = max(0, min(length, round(length * percent / 100)))
    return "█" * filled + "░" * (length - filled)


def parse_colour(value: str) -> discord.Colour:
    try:
        return discord.Colour.from_str(value)
    except ValueError:
        return discord.Colour.blurple()
