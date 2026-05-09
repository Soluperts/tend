---
name: mail-triage
description: Summarises unread Gmail. Calls `gws gmail +triage` and groups by sender priority. Use when the user asks "what's in my inbox", "any new mail", "any urgent emails".
---

# mail-triage

Surface the unread inbox in a TTS-friendly summary.

## Procedure

1. Run:

   ```bash
   bash ~/.tend/workspace/bin/gws-recent-mail.sh --triage --json
   ```

2. Pick the 2-3 most material threads (recency + sender importance).
   Rough heuristic: a known correspondent (replied to within last 7
   days) is more important than newsletter-style senders.

3. Speak in 1-3 sentences:
   - Total unread count first.
   - Top 1-2 senders by name.
   - If nothing actionable, say "Inbox is mostly newsletters."

## Output style

- One to three sentences. No reading subject lines verbatim.
