---
name: briefing
description: Morning briefing. Pulls today's events from watched calendars and unread Gmail summary, speaks one or two sentences. Use when the user asks "morning briefing", "what's my day", "brief me", "what's going on".
---

# briefing

Surface today's calendar + inbox state in one or two TTS-friendly
sentences.

## Procedure

1. **Calendar agenda:** for each name in `google.watched_calendars`
   from tend.toml, run:

   ```bash
   bash ~/.tend/workspace/bin/gws-agenda.sh --calendar "<name>" --today --json
   ```

   The script outputs JSON. Parse and combine across calendars. Sort by
   start time.

2. **Inbox:** run:

   ```bash
   bash ~/.tend/workspace/bin/gws-recent-mail.sh --triage --json
   ```

   This wraps `gws gmail +triage --json`.

3. **Speak:** one or two sentences, conversational. Lead with the next
   meeting (if any in the next 6h). Mention unread count + top sender
   if material. If both are empty, say "Nothing on your calendar
   today, inbox is quiet."

## Output style

- One or two sentences. No lists, no markdown.
- Dates as "this morning / this afternoon / tomorrow"; not ISO times.
- If a meeting is in <30min, lead with that.
