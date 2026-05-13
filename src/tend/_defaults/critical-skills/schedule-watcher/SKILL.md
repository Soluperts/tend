---
name: schedule-watcher
description: Periodic check of watched calendars. Reads bracket-tagged events ([deep-work] X, [lunch] Y) on the tend and primary calendars and schedules one-shot phase events (tag.upcoming, tag.starting, tag.ended) so reactive skills can run on time. Silent unless something noteworthy comes up.
silent_default: true
triggers:
  - when: "every 15m"
---

# schedule-watcher

You are the schedule-watcher tick. Run the tick script and act on what
it returns.

## Procedure

1. Run the tick:

   ```bash
   python3 ~/.tend/skills/schedule-watcher/bin/tick.py
   ```

   The output is a single JSON line like
   `{"scheduled": 3, "seen_tagged": 2, "missed_fired": 0}` or
   `{"skipped": "no_watched_calendars"}`.

2. Exit silently. End your run with the literal phrase
   `(nothing to surface)` as your last assistant message.

## Notes

- This skill must NEVER speak unprompted. The tick has its own log
  channel; if errors happen, the user can read them in `/tmp/tend.log`.
- If the tick prints `skipped: no_watched_calendars`, the user hasn't
  set up the Google integration yet. Stay silent.
