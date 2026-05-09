---
name: lunch-prep
description: Suggest a meal an hour before a calendar event tagged [lunch]. If a meal-plan was generated for today, mention it; otherwise offer to plan one. Subscribed to lunch.upcoming via the schedule-watcher.
events:
  - lunch.upcoming
---

# lunch-prep

You were dispatched because a `lunch.upcoming` event fired. The user
has lunch coming up.

## Procedure

1. **Look for an existing meal plan** for today in
   `~/.tend/workspace/plans/`:

   ```bash
   ls -1t ~/.tend/workspace/plans/*.md 2>/dev/null | head -5
   ```

   If a plan exists with today's date in the filename or front of the
   file, read its top section.

2. **Speak one short sentence:**
   - With existing plan: "Lunch in an hour — your plan is the chicken
     stir-fry with broccoli."
   - Without: "Lunch in an hour — want me to suggest something?"

## Output style

- One sentence. Conversational.
- Don't read out a recipe — this is just a heads-up.
