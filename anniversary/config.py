"""
Code-only constants for the anniversary package.

Everything an administrator should be able to change lives in the database and is
edited through the Django admin panel (see ``models.py``). The values below are
either protocol constants or safety limits that cannot be changed from the panel.
"""

from __future__ import annotations

# How often the bot reloads the active event and its tasks from the database.
# The admin panel and the bot are separate processes, so this is the maximum
# delay between an admin edit and the bot applying it.
SETTINGS_POLL_INTERVAL = 30

# Never sleep longer than this, even when the next scheduled boundary is far away.
MAX_POLL_INTERVAL = 300

# BallsDex refuses to spawn twice in the same 600 seconds. Boosters shorten that
# dead time but never below the event's configured floor.
DEAD_TIME = 600

# Absolute bounds for the shortened/lengthened catch timeout.
MIN_CATCH_TIMEOUT = 30
MAX_CATCH_TIMEOUT = 900

# How often the live progress view refreshes itself, and how long it may live.
MIN_PROGRESS_REFRESH = 5
MAX_PROGRESS_REFRESH = 60
PROGRESS_VIEW_LIFETIME = 600
PROGRESS_PAGE_SIZE = 4

# Number of reward rows written per bulk insert.
REWARD_BATCH_SIZE = 500

# The collectibles granted by a reward may have a maximum attack/health bonus.
MAX_REWARD_BONUS = 100

# Timezone used when schedule dates are written to the log, next to UTC.
# Change this to your own timezone, or leave it empty for UTC only.
SCHEDULE_TIMEZONE = "Europe/Helsinki"

LOGGER_NAME = "anniversary"
