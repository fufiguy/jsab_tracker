# JSAB Tracker

Watches the [Just Shapes & Beats Twitch category](https://www.twitch.tv/directory/category/just-shapes-and-beats)
and posts a Discord notification whenever someone new goes live.

## How it works

- Authenticates to the Twitch Helix API using an **app access token**
  (client-credentials flow) — no user login required.
- Looks up the category's game ID once, then polls `GET /helix/streams`
  for that game every `POLL_INTERVAL_SECONDS`.
- Keeps a small JSON state file of who was seen live and when. Comparing
  each poll against that state is how it detects "new" go-lives instead of
  re-notifying every cycle for someone who's still streaming.
- A short grace period (`OFFLINE_GRACE_SECONDS`) means a single missed/late
  API response won't cause a false "went offline then back online" ping.
- Posts a Discord embed via your webhook with the streamer's name, title,
  viewer count, live thumbnail, and avatar.

## Setup

1. **Twitch app** — register one at https://dev.twitch.tv/console/apps.
   Any OAuth Redirect URL works (e.g. `http://localhost`) since this script
   never does a user login, only the app-only client-credentials flow.
   Copy the **Client ID** and generate a **Client Secret**.

2. **Discord webhook** — in the target channel:
   `Channel Settings -> Integrations -> Webhooks -> New Webhook`, then
   copy the webhook URL.

3. **Configure**:
   ```bash
   cp .env.example .env
   # edit .env and fill in TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, DISCORD_WEBHOOK_URL
   ```

4. **Install & run**:
   ```bash
   pip install -r requirements.txt
   python jsab_tracker.py
   ```

Leave it running (e.g. in a `screen`/`tmux` session, a systemd service, or a
Docker container) — it polls forever until stopped with Ctrl+C.

## Configuration reference

| Variable | Default | Notes |
|---|---|---|
| `TWITCH_CLIENT_ID` | — | required |
| `TWITCH_CLIENT_SECRET` | — | required |
| `DISCORD_WEBHOOK_URL` | — | required |
| `TWITCH_GAME_NAME` | `Just Shapes & Beats` | must match Twitch's category name exactly |
| `POLL_INTERVAL_SECONDS` | `60` | how often to check Twitch |
| `OFFLINE_GRACE_SECONDS` | `2x poll interval + 30` | debounce before marking someone offline |
| `NOTIFY_ON_FIRST_RUN` | `false` | announce streamers already live on startup |
| `DISCORD_MENTION` | *(empty)* | e.g. `@everyone` or `<@&ROLE_ID>` |
| `STATE_FILE` | `jsab_tracker_state.json` | where "who's live" is persisted |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

## Notes on rate limits

- Twitch: app tokens get a generous rate limit bucket; polling once a
  minute uses a tiny fraction of it. The script also handles `401`
  (re-authenticates) and `429` (backs off) automatically.
- Discord webhooks: rate limits are handled with a retry using Discord's
  `retry_after` value.

## Extending

- **Multiple categories**: call `get_game_id`/`get_live_streams` per game
  and merge the results before diffing against state.
- **EventSub instead of polling**: Twitch's EventSub `stream.online` topic
  can push events instead of polling, but requires a public HTTPS endpoint
  (or WebSocket client) to receive them — a bigger lift than this
  polling approach, which needs no inbound networking at all.

## Running it for free, no card, no local machine

The Twitch tracker can run entirely on **GitHub Actions**, which never asks
for a credit card at any point (unlike Oracle Cloud, DigitalOcean, or
Fly.io, which all require one for identity verification even on their free
tiers). The trade-off: instead of a continuous 60-second loop, it runs as
a scheduled job that wakes up, checks once, and exits -- so notification
latency goes from "within a minute" to "within several minutes."

Since your repo is private, config is just a committed `.env` file --
no GitHub Secrets step needed. (On a public repo, do this via repo
Secrets instead, since a public `.env` would be visible to anyone.)

1. **Fill in your real values**: rename `.env.example` to `.env` and put
   your actual `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, and
   `DISCORD_WEBHOOK_URL` in it.

2. **Push everything to your private repo**, `.env` included --
   `.gitignore` no longer excludes it, so a normal `git add .` picks it up.

3. **That's it.** The included workflow at
   `.github/workflows/jsab-tracker.yml` runs every 10 minutes, does one
   poll cycle (reading config straight from the committed `.env`), and
   commits the updated `jsab_tracker_state.json` back to the repo so the
   next run knows who was already live. Trigger a run immediately from
   the repo's Actions tab ("Run workflow") to test it without waiting for
   the schedule.

4. Private repos get 2,000 free Actions minutes/month, which the default
   10-minute schedule comfortably fits under. To poll faster, edit the
   `cron` line in the workflow (e.g. `*/5 * * * *` for every 5 minutes,
   the practical minimum) -- just keep an eye on Settings -> Billing ->
   Actions usage if you do.

You can still run `python jsab_tracker.py` locally or on a VPS exactly as
before; `RUN_ONCE` only changes behavior when explicitly set to `true`.

