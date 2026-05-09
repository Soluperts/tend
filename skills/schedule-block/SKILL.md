---
name: schedule-block
description: Schedule a single block of time into the tend calendar. Handles approximate times by finding free slots, exact times go directly. Use when the user says "schedule deep work tomorrow morning", "block 2-4pm Wednesday for X", "put a [tag] on my calendar".
---

# schedule-block

Add a single event to the tend calendar.

## Procedure

1. **Read the tend calendar id** from `~/.tend/google/tend-calendar-id`.
   If missing, error out with "tend calendar isn't set up — run the
   install step."

2. **Parse the user's request** for: tag (e.g. `deep-work`, `exercise`),
   day, and time window. The day might be relative ("tomorrow",
   "Friday"). The time might be exact ("2-4pm") or approximate
   ("morning", "evening").

3. **If approximate time, find a free slot:**

   ```bash
   bash ~/.tend/workspace/bin/gws-find-slot.sh \
     --day <YYYY-MM-DD> --window morning|afternoon|evening \
     --duration <minutes> --json
   ```

   Pick the largest free slot returned. Propose to the user:
   "Tomorrow 9-11am is open, sound good?"

4. **Conflict-check exact times** before writing — query the user's
   primary calendar within that window:

   ```bash
   bash ~/.tend/workspace/bin/gws-events-window.sh \
     --calendar primary \
     --time-min <iso-start> --time-max <iso-end> --json
   ```

   The output is `{"items": [...]}`. If any item overlaps the
   proposed block, surface it and ask whether to proceed anyway.

5. **On user confirm**, insert the event:

   ```bash
   gws calendar +insert \
     --calendar "$(cat ~/.tend/google/tend-calendar-id)" \
     --summary "[<tag>]" \
     --start "<iso-start>" \
     --end "<iso-end>" \
     --format json
   ```

6. **Spoken summary**: "Added [tag] from <start> to <end>."

## Output style

- One question at a time.
- Always confirm before writing.
- If the request is fully exact and unambiguous (specific tag, specific
  day, specific time), you can confirm in one turn.
