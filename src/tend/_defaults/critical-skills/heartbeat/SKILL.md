---
name: heartbeat
description: Periodic silent check-in. Review pending work and announce only when something genuinely needs the user's attention.
silent_default: true
---

# Heartbeat

You are running on the heartbeat tick. By default, exit silently.

Only announce if there is something the user genuinely wants to know
right now and would not have heard otherwise. Examples that justify an
announcement: a follow-up deadline arrived, a long-running task you
started earlier finished while the user was away.

If you have nothing worth surfacing, end your run with the literal
phrase "(nothing to surface)" as your last assistant message.
