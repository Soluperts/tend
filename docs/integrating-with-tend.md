# Integrating with tend

This page is the contract for any external process that wants to make
tend speak (`POST /say`) or hand it a structured event (`POST /event`).
You can wire up an integration from this page alone — no need to read
tend's source.

> tend is a single-user voice assistant running on a Raspberry Pi. The
> webhook surface exists so co-located processes (a vision daemon, a
> Gmail watcher, etc.) can produce announcements without becoming part
> of tend's main process.

## Setup

- Server: aiohttp on `127.0.0.1:7331` by default. Configurable via
  `tend.toml`:

  ```toml
  [webhook]
  host = "127.0.0.1"
  port = 7331
  ```

- Auth token: `TEND_WEBHOOK_TOKEN` in `.env` (gitignored). Share it
  with your producer process out-of-band (env var, secrets file, etc.).
- Smoke test: `tend webhook test` POSTs to `/say` using the token in
  your environment. A `200` with `{"delivered": true}` confirms the
  server is up and reachable.

## Authentication

Every request must include:

```
Authorization: Bearer <TEND_WEBHOOK_TOKEN>
```

Missing or wrong token → `401 {"error": "unauthorized"}`.

Token rotation is manual: edit `.env`, restart the unit
(`systemctl --user restart tend`), update your producer config.

## `POST /say`

Direct TTS — tend speaks the text you provide. No LLM round-trip;
lowest latency.

**Request body**

| Field      | Type    | Required | Description                                   |
|------------|---------|----------|-----------------------------------------------|
| `text`     | string  | yes      | What to say. Goes straight to TTS.            |
| `category` | string  | no       | Cooldown bucket (default `"general"`).        |
| `urgent`   | boolean | no       | Bypass cooldown + active-Brain deferral.      |

**Response**

```json
{ "delivered": true }
```

`delivered: true` means the announcement was either spoken immediately
or queued for delivery once Brain stops conversing with the user.
`delivered: false` means the call was dropped on cooldown (the same
category fired too recently; see "Cooldown semantics" below).

**Status codes**

- `200` — handled (delivered or dropped on cooldown).
- `400` — body missing `text` or invalid JSON.
- `401` — missing or wrong token.

**Examples**

```bash
curl -sX POST http://127.0.0.1:7331/say \
  -H "Authorization: Bearer $TEND_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Sit up straight.","category":"posture"}'
```

```python
import os, requests
TOKEN = os.environ["TEND_WEBHOOK_TOKEN"]

def say(text, category="general", urgent=False):
    r = requests.post(
        "http://127.0.0.1:7331/say",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"text": text, "category": category, "urgent": urgent},
        timeout=2,
    )
    r.raise_for_status()
    return r.json()["delivered"]
```

## `POST /event`

Structured event — tend matches the event kind against installed
skills' `events:` frontmatter and dispatches to each subscribing skill
via the GeneralWorker. Each skill sees the full body in its dispatch
payload.

**Request body**

| Field  | Type   | Required | Description                                   |
|--------|--------|----------|-----------------------------------------------|
| `kind` | string | yes      | Event identifier — `dotted.snake_case`.       |
| ...    | any    | no       | Free-form fields, passed through verbatim.    |

**Response**

```json
{ "dispatched": ["skill-a", "skill-b"] }
```

`dispatched` lists the skills that were handed the event. An empty
list is **not** an error — it means no installed skill subscribes to
that kind. Delivery is fire-and-forget: the response does not wait
for the worker to finish.

**Status codes**

- `200` — handled (zero or more skills dispatched).
- `400` — body missing `kind` or invalid JSON.
- `401` — missing or wrong token.

**Examples**

```bash
curl -sX POST http://127.0.0.1:7331/event \
  -H "Authorization: Bearer $TEND_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"kind":"posture.slumped","duration_s":1200}'
```

## Cooldown semantics

`/say` shares one cooldown registry with all other proactive sources
(scheduler fires, internal worker announcements). The default cooldown
is 5 minutes per category; specific categories can have their own
cooldown set in `tend.toml [announcer.category]`:

```toml
[announcer.category]
posture = 600
hydration = 900
```

If you POST a `category="posture"` message and the same category was
spoken less than the cooldown window ago, your message is **dropped**
and the response is `{"delivered": false}`. Don't retry — that defeats
the purpose. Either pick a different category if the messages are
genuinely unrelated, or accept the drop.

`urgent: true` bypasses cooldown. Reserve for safety-class events
(smoke alarm, doorbell, glass-break detection). Routine nags should
always be `urgent: false`.

## Best practices

- **Pick a stable, meaningful category.** `posture`, `hydration`,
  `meal-reminder` — not `general` for everything. Distinct categories
  throttle independently.
- **Keep `text` short.** It goes straight to TTS. One sentence is
  ideal; two is the upper bound. Long announcements interrupt the user
  for too long.
- **Reserve `urgent: true`.** Default to `false`. The user is the
  arbiter of what's actually urgent; if everything is urgent, nothing
  is.
- **Retry on `ConnectionRefusedError`.** tend may be restarting. Wait
  a few seconds, try again. Don't loop forever — fall back to logging
  if tend is down for an extended period.
- **Don't pre-rate-limit on the producer side based on TTS timing.**
  Let the cooldown drop happen naturally. The response tells you
  whether the message landed.

## Limitations

- **Loopback only.** The HTTP server binds to `127.0.0.1`; it is not
  reachable from outside the host. Use SSH tunnelling or a reverse
  proxy if you need cross-host access.
- **No streaming.** Each request is one announcement. There is no
  long-lived connection for progressive output.
- **No delivery acknowledgement beyond the synchronous response.** The
  producer cannot tell whether a queued (`delivered: true`) message
  actually got spoken later — it might be dropped on a subsequent
  cooldown when Brain finally goes inactive.
- **No inbound channel.** tend cannot push messages to your producer
  in v1. If your producer needs to know what tend is saying, read the
  daemon logs (`journalctl --user -u tend` or `/tmp/tend.log`).

## Versioning

This contract follows tend's normal release cadence. Breaking changes
will be called out in `CHANGELOG.md` (when one exists) or on the
release notes for the affected commit. Until then, pin to a known-good
commit if your producer needs strict stability.

## Worked example: a posture-nag daemon

A minimal vision daemon that:

- nags posture via `/say` (rate-limited by tend's cooldown);
- emits a structured `/event` for long sitting periods that a skill
  might react to.

```python
"""posture_daemon.py — example tend integration."""

from __future__ import annotations

import os
import time
from typing import Optional

import requests


TEND_BASE = "http://127.0.0.1:7331"
TOKEN = os.environ["TEND_WEBHOOK_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _post(path: str, body: dict) -> Optional[dict]:
    try:
        r = requests.post(TEND_BASE + path, headers=HEADERS,
                          json=body, timeout=2)
        if r.ok:
            return r.json()
        print(f"tend {path} responded {r.status_code}: {r.text}")
    except requests.ConnectionError:
        print("tend not reachable; will retry next tick")
    return None


def say(text: str, category: str = "general", urgent: bool = False) -> None:
    _post("/say", {"text": text, "category": category, "urgent": urgent})


def event(kind: str, **payload) -> None:
    _post("/event", {"kind": kind, **payload})


def main_loop() -> None:
    last_seated_at = time.monotonic()
    while True:
        bad_posture = detect_bad_posture()    # your CV pipeline
        if bad_posture:
            say("Sit up straight.", category="posture")
        seated_seconds = time.monotonic() - last_seated_at
        if seated_seconds > 90 * 60:
            event("user_idle.long_sit",
                  duration_s=int(seated_seconds))
            last_seated_at = time.monotonic()
        time.sleep(2.0)


if __name__ == "__main__":
    main_loop()
```

The vision daemon owns wording and timing of `/say` calls; the
cooldown in tend prevents posture nags from machine-gunning. The
`/event` call gives skills a chance to react with richer behaviour
(a meal-plan adjustment, a calendar block, a one-line nudge with
context); if no skill subscribes, the event is harmlessly logged.
