---
name: post-deep-work
description: Suggests a stretch, water, or short walk after a [deep-work] block ends. Silent unless the block was at least 45 minutes long.
silent_default: true
events:
  - deep-work.ended
---

# post-deep-work

You were dispatched because a `deep-work.ended` event fired. The user
just finished a deep-work block.

## Procedure

1. The dispatch payload includes `event` with `start` and `end`
   timestamps. Compute the duration in minutes.

2. **If the block was less than 45 minutes**, exit silently with the
   literal phrase `(nothing to surface)`.

3. **Otherwise**, pick one short suggestion at random:
   - "Deep work block done — stretch?"
   - "That was a long stretch — drink some water."
   - "Block ended — walk around?"

   Speak it as one sentence.

## Output style

- One sentence. Don't be preachy.
- If you have nothing to say (block too short), end silently.
