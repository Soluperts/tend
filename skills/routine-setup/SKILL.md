---
name: routine-setup
description: Conversational walk-through to set up the user's daily routine as recurring events in the tend calendar. Use when the user says "set up my daily routine", "redo my schedule", "configure my day".
---

# routine-setup

Walk the user through their daily routine and create recurring events
in the tend calendar.

## Procedure

1. **Read the tend calendar id** from `~/.tend/google/tend-calendar-id`.
   If the file is missing, tell the user "tend calendar isn't set up
   yet — run the install step first" and stop.

2. **Walk through blocks** one at a time, asking for time + days
   + duration. Defaults:
   - Exercise: weekday mornings, 30 min
   - Lunch: every day, noon, 60 min
   - Deep work: weekdays, 9am-noon
   - Break: every day, 3pm, 15 min

   For each, ask: "What time do you want exercise? (or skip)" — accept
   ranges like "7-7:30" or "7:00 for 30 minutes".

3. **For each block the user accepts:**
   - Build the event title with the appropriate bracket tag prefix
     (e.g. `[exercise]`, `[lunch]`, `[deep-work]`, `[break]`).
   - Build an RRULE based on the requested days.
   - **Check conflicts:** for each day in the next two weeks where this
     block would land, query the user's primary calendar in that
     time window:

     ```bash
     bash ~/.tend/workspace/bin/gws-events-window.sh \
       --calendar primary \
       --time-min <day-start-iso> --time-max <day-end-iso> --json
     ```

     The output is `{"items": [...]}` from the Calendar events.list
     API. Filter to entries that overlap the proposed block. If any,
     surface them: "Tuesday morning has a 9am meeting; skip that day
     or pick another time?"

   - On confirm, insert the recurring event. Note: `gws calendar
     +insert` does not accept `--recurrence`; for recurring events use
     the resource-style call with a body that includes a `recurrence`
     array:

     ```bash
     CAL_ID="$(cat ~/.tend/google/tend-calendar-id)"
     gws calendar events insert \
       --params "$(python3 -c 'import json; print(json.dumps({"calendarId": "'"$CAL_ID"'"}))')" \
       --json '{
         "summary": "[exercise]",
         "start": {"dateTime": "<first-occurrence-iso>"},
         "end":   {"dateTime": "<first-occurrence-end-iso>"},
         "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"]
       }' --format json
     ```

     For a one-off (non-recurring) event the simpler helper works:

     ```bash
     gws calendar +insert \
       --calendar "$CAL_ID" \
       --summary "[exercise]" \
       --start "<first-occurrence-iso>" \
       --end "<first-occurrence-end-iso>" \
       --format json
     ```

4. **Spoken summary** at the end (one sentence):
   "Set up your daily routine — exercise, lunch, deep work, and a
   break. You can change anything in Google Calendar directly."

## Output style

- Conversational, ask one question at a time.
- Always confirm before writing: "Should I add this?" — wait for "yes".
- TTS-friendly: spell out times in plain English.
