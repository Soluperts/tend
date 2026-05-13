#!/usr/bin/env python3
"""Schedule-watcher tick — invoked by the SKILL.md.

Reads google config + tend.toml, runs run_tick() against the live
~/.tend/, prints a one-line JSON summary to stdout. Stderr is the loguru
log stream (handled by the parent process if redirected).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from tend.config import settings
from tend.google_watcher import parse_lead_seconds, run_tick


def main() -> int:
    google = settings.google
    if not google.watched_calendars:
        print(json.dumps({"skipped": "no_watched_calendars"}))
        return 0

    events_config: dict[str, dict] = {}
    for tag, cfg in google.events.items():
        try:
            lead_s = parse_lead_seconds(cfg.upcoming_lead)
        except ValueError as e:
            print(
                f"google.events.{tag}.upcoming_lead invalid: {e}",
                file=sys.stderr,
            )
            continue
        events_config[tag] = {
            "upcoming_lead_s": lead_s,
            "emit": list(cfg.emit),
        }

    state_path = (
        Path.home() / ".tend" / "cache" / "schedule-watcher-state.json"
    )
    webhook_token = os.environ.get("TEND_WEBHOOK_TOKEN", "")
    webhook_url = (
        f"http://{settings.webhook.host}:{settings.webhook.port}/event"
    )
    now = dt.datetime.now(tz=ZoneInfo("UTC"))

    summary = asyncio.run(run_tick(
        watched_names=list(google.watched_calendars),
        events_config=events_config,
        state_path=state_path,
        webhook_token=webhook_token,
        webhook_url=webhook_url,
        now=now,
    ))
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
