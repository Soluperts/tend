---
name: meeting-prep
description: Surfaces context for the user's next meeting. Reads the next event with attendees and grabs recent Gmail threads with each attendee. Use when the user says "prep my next meeting", "who am I meeting next", "what's my next meeting about".
---

# meeting-prep

Find the next meeting on watched calendars and surface the most recent
email context with the attendees.

## Procedure

1. **Find next meeting with attendees:** run

   ```bash
   bash ~/.tend/workspace/bin/gws-agenda.sh --next 1 --json
   ```

   Inspect the result. If the event has no `attendees` field, fall back
   to "no meeting prep available — your next event has no attendees."

2. **For each attendee** (up to 3), pull their recent threads:

   ```bash
   bash ~/.tend/workspace/bin/gws-recent-mail.sh --from <attendee-email> --limit 3 --json
   ```

3. **Speak the prep:**
   - Lead with the meeting time + topic.
   - One short summary per attendee: "Recent thread with Sarah was
     about the Q2 plan."
   - Total: 2-3 sentences.

## Output style

- Conversational TTS. No quoting subject lines verbatim if they're long.
- If recent threads have no clear connection, just name the attendees
  ("with Sarah and Tom").
