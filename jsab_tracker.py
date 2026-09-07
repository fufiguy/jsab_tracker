#!/usr/bin/env python3
"""
JSAB Tracker
============
Polls the Twitch Helix API for live streams in the "Just Shapes & Beats"
category (https://www.twitch.tv/directory/category/just-shapes-and-beats)
and posts a notification to a Discord webhook whenever someone new goes live.

Setup:
  1. Create a Twitch application at https://dev.twitch.tv/console/apps
     to get a Client ID and Client Secret (any redirect URL works, e.g.
     http://localhost).
  2. Create a Discord webhook in the channel you want notifications in:
     Channel Settings -> Integrations -> Webhooks -> New Webhook -> Copy URL.
  3. Copy .env.example to .env and fill in your values.
  4. pip install -r requirements.txt
  5. python jsab_tracker.py
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; env vars can be set another way

# --------------------------------------------------------------------------
# Configuration (env vars / .env)
# --------------------------------------------------------------------------

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

GAME_NAME = os.environ.get("TWITCH_GAME_NAME", "Just Shapes & Beats")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
# How long a streamer can go "missing" from the live list before we treat
# them as offline. Guards against a single flaky poll causing a duplicate
# notification when they reappear.
OFFLINE_GRACE_SECONDS = int(
    os.environ.get("OFFLINE_GRACE_SECONDS", str(POLL_INTERVAL_SECONDS * 2 + 30))
)
# If false (default), streamers already live when the script starts are
# recorded but not announced -- only *new* go-lives after that are posted.
NOTIFY_ON_FIRST_RUN = os.environ.get("NOTIFY_ON_FIRST_RUN", "false").lower() == "true"
# Optional content to prefix the embed with, e.g. "@everyone" or a role
# mention like "<@&123456789012345678>". Leave blank for none.
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "")
STATE_FILE = Path(os.environ.get("STATE_FILE", "jsab_tracker_state.json"))
# If true, do exactly one poll cycle and exit instead of looping forever.
# Used when an external scheduler (e.g. a GitHub Actions cron workflow)
# invokes this script periodically rather than it running as a daemon.
RUN_ONCE = os.environ.get("RUN_ONCE", "false").lower() == "true"

# Optional: mirror this script's log output (the same lines you see in the
# terminal) to a second Discord channel, separate from the "someone's
# live!" announcements above. Leave blank to disable.
DISCORD_LOG_WEBHOOK_URL = os.environ.get("DISCORD_LOG_WEBHOOK_URL", "")
# Minimum level forwarded to that channel. Defaults to INFO -- the same
# routine lines you see in the terminal (poll results, auth, etc). Note
# this means one Discord message per poll cycle, which adds up fast on a
# short POLL_INTERVAL_SECONDS. Set to WARNING if you only want problems.
DISCORD_LOG_LEVEL = os.environ.get("DISCORD_LOG_LEVEL", "INFO").upper()

TWITCH_OAUTH_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_BASE = "https://api.twitch.tv/helix"
EMBED_COLOR = 0x9146FF  # Twitch purple

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("jsab_tracker")


class DiscordLogHandler(logging.Handler):
    """Sends log records to a Discord webhook -- the same messages you see
    in the terminal, posted to a separate channel from the "someone's
    live!" announcements so the two don't mix."""

    def __init__(self, webhook_url: str, level: int = logging.WARNING):
        super().__init__(level=level)
        self.webhook_url = webhook_url
        self._session = requests.Session()

    def _post(self, content: str) -> None:
        # Deliberately prints to stderr instead of using `log.*` here --
        # this handler is attached to `log`, so logging a failure through
        # it would try to re-send itself. A failed send should never be
        # invisible, so at minimum it shows up in the terminal.
        try:
            resp = self._session.post(
                self.webhook_url,
                json={"content": content[:1900], "username": "JSAB Tracker Logs"},
                timeout=10,
            )
            if resp.status_code >= 400:
                print(
                    f"[jsab_tracker] DISCORD_LOG_WEBHOOK_URL post failed: "
                    f"HTTP {resp.status_code} {resp.text[:200]}",
                    file=sys.stderr,
                )
        except requests.RequestException as exc:
            print(f"[jsab_tracker] DISCORD_LOG_WEBHOOK_URL post failed: {exc}", file=sys.stderr)

    def send_raw(self, content: str) -> None:
        """Send a message regardless of level filtering -- used for the
        one-off startup confirmation below."""
        self._post(content)

    def emit(self, record: logging.LogRecord) -> None:
        # Matches the terminal's "[LEVEL] message" format exactly, just
        # without the timestamp prefix.
        message = self.format(record)
        self._post(f"[{record.levelname}] {message}")


if DISCORD_LOG_WEBHOOK_URL:
    _log_level = getattr(logging, DISCORD_LOG_LEVEL, logging.WARNING)
    _discord_log_handler = DiscordLogHandler(DISCORD_LOG_WEBHOOK_URL, level=_log_level)
    _discord_log_handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_discord_log_handler)
    # Terminal-only (never mirrored to Discord itself) so it's unambiguous
    # from the run log whether the env var was actually picked up.
    print(
        f"[jsab_tracker] Log forwarding ENABLED -> Discord (level={DISCORD_LOG_LEVEL}).",
        file=sys.stderr,
    )
else:
    print(
        "[jsab_tracker] DISCORD_LOG_WEBHOOK_URL is not set -- log forwarding disabled.",
        file=sys.stderr,
    )


def require_config() -> None:
    missing = [
        name
        for name, val in [
            ("TWITCH_CLIENT_ID", TWITCH_CLIENT_ID),
            ("TWITCH_CLIENT_SECRET", TWITCH_CLIENT_SECRET),
            ("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL),
        ]
        if not val
    ]
    if missing:
        log.error("Missing required configuration: %s", ", ".join(missing))
        log.error("Set them as environment variables or in a .env file (see .env.example).")
        sys.exit(1)


# --------------------------------------------------------------------------
# Twitch API client
# --------------------------------------------------------------------------

class TwitchClient:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._session = requests.Session()

    def _headers(self) -> Dict[str, str]:
        return {
            "Client-Id": self.client_id,
            "Authorization": f"Bearer {self._token}",
        }

    def authenticate(self) -> None:
        resp = self._session.post(
            TWITCH_OAUTH_URL,
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        log.info("Authenticated with the Twitch API.")

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        if self._token is None:
            self.authenticate()
        url = f"{TWITCH_API_BASE}{path}"
        resp = self._session.get(url, headers=self._headers(), params=params, timeout=15)

        if resp.status_code == 401:
            log.info("Twitch token expired or invalid, re-authenticating.")
            self.authenticate()
            resp = self._session.get(url, headers=self._headers(), params=params, timeout=15)

        if resp.status_code == 429:
            reset = resp.headers.get("Ratelimit-Reset")
            wait = max(1, int(reset) - int(time.time())) if reset else 5
            log.warning("Rate limited by Twitch, waiting %ss.", wait)
            time.sleep(wait)
            resp = self._session.get(url, headers=self._headers(), params=params, timeout=15)

        resp.raise_for_status()
        return resp.json()

    def get_game_id(self, name: str) -> Optional[str]:
        data = self._get("/games", params={"name": name})
        results = data.get("data", [])
        return results[0]["id"] if results else None

    def get_live_streams(self, game_id: str) -> List[dict]:
        streams: List[dict] = []
        cursor = None
        while True:
            params = {"game_id": game_id, "first": 100}
            if cursor:
                params["after"] = cursor
            data = self._get("/streams", params=params)
            page = data.get("data", [])
            streams.extend(page)
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor or not page:
                break
        return streams

    def get_user_profile_images(self, user_ids: Iterable[str]) -> Dict[str, str]:
        """Batch-fetch avatar URLs for a set of user IDs (100 per request)."""
        ids = list(dict.fromkeys(user_ids))  # dedupe, keep order
        images: Dict[str, str] = {}
        for i in range(0, len(ids), 100):
            batch = ids[i : i + 100]
            data = self._get("/users", params={"id": batch})
            for user in data.get("data", []):
                images[user["id"]] = user.get("profile_image_url", "")
        return images


# --------------------------------------------------------------------------
# Discord notifier
# --------------------------------------------------------------------------

class DiscordNotifier:
    def __init__(self, webhook_url: str, mention_content: str = ""):
        self.webhook_url = webhook_url
        self.mention_content = mention_content
        self._session = requests.Session()

    def notify_live(self, stream: dict, avatar_url: str = "") -> None:
        thumbnail = (
            stream.get("thumbnail_url", "")
            .replace("{width}", "440")
            .replace("{height}", "248")
        )
        stream_url = f"https://twitch.tv/{stream['user_login']}"

        embed = {
            "title": f"{stream['user_name']} is now live!",
            "url": stream_url,
            "description": stream.get("title") or "(no title)",
            "color": EMBED_COLOR,
            "fields": [
                {
                    "name": "Playing",
                    "value": stream.get("game_name") or GAME_NAME,
                    "inline": True,
                },
                {
                    "name": "Viewers",
                    "value": str(stream.get("viewer_count", 0)),
                    "inline": True,
                },
            ],
            "footer": {"text": "Twitch \u2022 Just Shapes & Beats tracker"},
            "timestamp": stream.get("started_at"),
        }
        if thumbnail:
            embed["image"] = {"url": thumbnail}
        if avatar_url:
            embed["thumbnail"] = {"url": avatar_url}

        payload = {"username": "JSAB Tracker", "embeds": [embed]}
        if self.mention_content:
            payload["content"] = self.mention_content

        resp = self._session.post(self.webhook_url, json=payload, timeout=15)
        if resp.status_code == 429:
            try:
                retry_after = float(resp.json().get("retry_after", 1))
            except ValueError:
                retry_after = 1.0
            log.warning("Discord rate limit hit, waiting %ss.", retry_after)
            time.sleep(retry_after)
            resp = self._session.post(self.webhook_url, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("Notified Discord: %s is live.", stream["user_name"])


# --------------------------------------------------------------------------
# State tracking (persists across restarts, with an offline grace period)
# --------------------------------------------------------------------------

class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.last_seen: Dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.last_seen = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning("Could not read state file, starting fresh.")
                self.last_seen = {}

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.last_seen))
        except OSError as exc:
            log.warning("Could not write state file: %s", exc)

    def is_new(self, user_id: str) -> bool:
        return user_id not in self.last_seen

    def mark_seen(self, user_id: str, ts: float) -> None:
        self.last_seen[user_id] = ts

    def prune_offline(self, now: float, grace_seconds: int) -> None:
        stale = [uid for uid, seen in self.last_seen.items() if now - seen > grace_seconds]
        for uid in stale:
            del self.last_seen[uid]


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

_shutdown = False


def _handle_signal(signum, _frame) -> None:
    global _shutdown
    log.info("Received signal %s, shutting down after this cycle.", signum)
    _shutdown = True


def poll_once(
    twitch: TwitchClient,
    discord: DiscordNotifier,
    state: StateStore,
    game_id: str,
    first_cycle: bool,
) -> None:
    """Run exactly one check-and-notify cycle. Used both by the continuous
    loop below and by RUN_ONCE mode (a single invocation from a scheduler
    like a GitHub Actions cron job)."""
    cycle_start = time.time()
    try:
        streams = twitch.get_live_streams(game_id)
        live_user_ids = [s["user_id"] for s in streams]
        new_streams = [s for s in streams if state.is_new(s["user_id"])]

        if new_streams and (not first_cycle or NOTIFY_ON_FIRST_RUN):
            avatars = twitch.get_user_profile_images(s["user_id"] for s in new_streams)
            for s in new_streams:
                try:
                    discord.notify_live(s, avatars.get(s["user_id"], ""))
                except requests.RequestException as exc:
                    log.error("Failed to notify Discord for %s: %s", s.get("user_name"), exc)
        elif new_streams:
            log.info(
                "Skipping notifications for %d already-live streamer(s) on the first run.",
                len(new_streams),
            )

        for uid in live_user_ids:
            state.mark_seen(uid, cycle_start)
        state.prune_offline(cycle_start, OFFLINE_GRACE_SECONDS)
        state.save()

        log.info("Poll complete: %d live stream(s) in category.", len(streams))
    except requests.RequestException as exc:
        log.error("Twitch API request failed: %s", exc)
    except Exception:
        log.exception("Unexpected error during poll cycle.")


def run() -> None:
    require_config()
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    twitch = TwitchClient(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
    discord = DiscordNotifier(DISCORD_WEBHOOK_URL, DISCORD_MENTION)
    state = StateStore(STATE_FILE)

    twitch.authenticate()
    game_id = twitch.get_game_id(GAME_NAME)
    if not game_id:
        log.error('Could not find a Twitch category named "%s". Check TWITCH_GAME_NAME.', GAME_NAME)
        sys.exit(1)
    log.info('Tracking category "%s" (game_id=%s).', GAME_NAME, game_id)

    # "First cycle" means no state has ever been recorded yet -- not just
    # "this process just started" -- so a restart with existing state
    # (e.g. a fresh GitHub Actions run-once invocation) still notifies
    # normally instead of re-suppressing every time.
    first_cycle = len(state.last_seen) == 0

    if RUN_ONCE:
        poll_once(twitch, discord, state, game_id, first_cycle)
        log.info("RUN_ONCE is set, exiting after one poll cycle.")
        return

    while not _shutdown:
        loop_start = time.time()
        poll_once(twitch, discord, state, game_id, first_cycle)
        first_cycle = False
        elapsed = time.time() - loop_start
        time.sleep(max(1.0, POLL_INTERVAL_SECONDS - elapsed))

    log.info("Stopped.")


if __name__ == "__main__":
    run()
