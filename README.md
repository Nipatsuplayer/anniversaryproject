# BallsDex Anniversary

A self-contained community event package for BallsDex v3. The whole server
community works on shared goals, and completed goals unlock global boosters and
rewards for the players who contributed.

The package never edits BallsDex core files. It is discovered by the admin panel
through `config/extra.toml` and hooks into the running bot only while the event is
active.

## Features

- Multiple scheduled events, with history and start/end dates. Only one enabled
  event may be active at a time.
- Community tasks that count:
  - any wild catch;
  - specific collectible catches;
  - specific special-card catches;
  - unique species caught;
  - completed trades (both players are credited);
  - collectibles given or donated (the sender is credited).
- Optional per-server filter on catch tasks.
- Global boosters unlocked by completed tasks:
  - spawn rate;
  - rarity weighting;
  - special-card chance;
  - catch window length.
  Same-type boosters stack multiplicatively.
- Contributor-only collectible rewards, with optional special card, quantity,
  attack bonus and health bonus. Grants are idempotent: a player can never
  receive the same reward from the same task twice.
- Append-only action log, cached progress, contributor totals and an admin action
  to recalculate a task from the log.
- Live `/anniversary progress` display, refreshed automatically every few
  seconds. The slash command replies ephemerally; the `b.anniversary progress`
  prefix command posts in the channel.
- Optional completion announcement in a channel.

## Installation

1. Install the package in the same environment as BallsDex:

   ```sh
   uv pip install -e extra/ballsdex-anniversary
   ```

   If you use the package manager plugin, add the package through its panel
   instead. A manual entry looks like this:

   ```toml
   [[ballsdex.packages]]
   location = "/absolute/path/to/extra/ballsdex-anniversary"
   path = "anniversary"
   enabled = true
   editable = true
   ```

2. Apply the migration:

   ```sh
   python3 -m django migrate anniversary
   ```

3. Restart the bot and the admin panel.

The bot checks the database regularly (usually every 30 seconds, and at most
every five minutes), so changes made in the admin panel are applied without a
restart. The event hooks are installed and removed automatically when the
schedule opens and closes.

## Admin panel

Create an **Anniversary event**, then add **tasks** inline. Each task points to
one or more **rewards**.

Times are always entered and displayed in UTC. Tick `enabled` to activate the
event, including scheduled events. Set `starts_at` and `ends_at` to start and
stop it automatically, or leave them empty to control the event entirely with
the `enabled` checkbox.

Useful admin actions on a task:

- **Reset progress and contributors** clears progress and contributor totals.
  Existing reward grants are kept, so a player cannot be rewarded twice.
- **Recalculate progress from the action log** rebuilds progress and contributor
  totals from `EventAction`. This is useful after changing a task's target or
  filters. It stops at the target and never revokes collectibles.
- **Mark complete** sets progress to the target without waiting for players.
- **Grant missing collectible rewards** gives rewards to contributors who do not
  have a `RewardGrant` yet.

The action log, contributor totals, species totals and reward grants are
read-only in the admin panel.

## Task and reward types

| Task type | Counted action |
| --- | --- |
| Catch collectibles | Every fresh wild catch. Dropped or traded collectibles do not count. |
| Catch specific collectible(s) | A wild catch whose collectible is in the task's list. |
| Catch specific special(s) | A wild catch whose special card is in the task's list. |
| Catch unique species | Each distinct collectible species caught once. |
| Catch rare balls | A wild catch whose collectible rarity weight is at most the task's maximum rarity (lower is rarer). |
| Complete trades | Each completed trade, credited to both players. |
| Give/donate collectibles | Each collectible given or donated, credited to the sender. Admin grants also count, credited to the admin. |

| Reward type | Effect |
| --- | --- |
| Spawn rate booster | Divides the spawn threshold and shortens the 10 minute dead time, never below the event's floor. |
| Rarity / special chance booster | Re-weights collectible rarity and special-card chance. |
| Longer catch window | Increases the spawn view timeout, within the package limits. |
| Grant collectible(s) | Creates collectible instances for every contributor. |
| Grant random collectible | Creates random collectible instances for every contributor, preferring the task's collectible list. |
| Custom text reward | Displays arbitrary text in the progress view and announcements. |

## Commands

- `/anniversary progress` — private live progress view.
- `b.anniversary progress` — public live progress view.

The display paginates tasks and can be refreshed manually. It stops refreshing
when the event ends, the message is deleted, or the view times out.

## Notes and limitations

- Progress is global across every server the bot is in.
- Only fresh wild catches count. A collectible dropped by one player and caught
  by another is not a wild catch.
- Trades credit both participants once per trade.
- Donations credit the sender, not the recipient.
- The action log uses a deduplication key, so replaying the same event or
  restarting the bot cannot count the same catch, trade or donation twice.
- If a task is completed and its reward list is edited later, run **Grant
  missing collectible rewards** to give new rewards to the existing
  contributors. Already granted rewards are never duplicated.
- Hooks are restored on extension unload. If the `countryballs` package is
  reloaded while the event is active, the next poll repairs the spawn wrapper;
  the class-level patches remain in place.
- The package does not add money rewards or any economy changes.
