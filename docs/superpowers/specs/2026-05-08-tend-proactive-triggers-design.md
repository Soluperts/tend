# tend proactive triggers — design

Date: 2026-05-08
Status: design

## Why

tend today is reactive: the user speaks, Brain replies, optionally a worker
runs. The deskclaw vision needs proactive behaviour — work that fires from
a schedule (a noon meal plan, a Monday morning fitness check-in) or from
an external signal (a co-located vision process saying "fix your posture")
and announces itself through the speaker without the user asking first.

Every primitive needed is already in place:

- Workers can publish `TTSSpeakFrame` directly to the bus; that frame
  reaches Hub's TTS and the speaker regardless of `Brain.active`.
- Workers can publish `LLMMessagesAppendFrame` so the announcement is
  recorded into Hub's `LLMContext` for follow-up. Brain's `on_task_update`
  uses this exact pattern today.
- `Brain.request_task("general", payload=...)` is fire-and-forget; it
  works whether Brain is active or not.

What is missing:

- A **scheduler** that fires at wall-clock times and dispatches work.
- Durable **schedule state** that survives systemd restart and Pi reboot.
- A **webhook receiver** so external processes (the vision daemon today,
  Gmail/Calendar webhooks tomorrow) can produce announcements.
- A **convergent announcer** so all proactive sources share one cooldown,
  one active-Brain deferral policy, and one path into Hub's context.

This spec adds those four pieces and wires the existing bus primitives to
them.

## Goals

- Cron-style schedules (`at`, `every`, `cron`) that survive restart and
  fire reliably.
- A worker-side heartbeat tick: a periodic, *silent-by-default* run of a
  dedicated `heartbeat` skill that announces only when there is something
  worth surfacing.
- A loopback HTTP receiver so external producers can push direct TTS or
  structured events into tend.
- One announcer with: per-category cooldown, active-Brain deferral, an
  urgency override, and automatic LLMContext logging.
- Two authoring paths for schedules: voice (Brain tools) and skill
  frontmatter (off until explicitly enabled).
- Brain context awareness: every announcement, regardless of source,
  appears in Hub's `LLMContext` so a later conversation turn can
  reference it.

## Non-goals (v1)

- **OpenClaw-style main-session heartbeat.** Brain monologuing into a
  silent room every 30 minutes is the wrong UX for a voice device. The
  heartbeat tick lives on the worker side instead.
- **Presence detection inside tend.** The vision process may surface
  presence as a `/event` payload, but tend does not gate announcements
  on presence in v1.
- **Retry and exponential backoff on failed jobs.** Recurring jobs simply
  wait until next scheduled fire. One-shot jobs are deleted regardless of
  outcome (with the failure recorded).
- **Outbound channel delivery.** Announcements only go to local TTS in
  v1. Telegram/SMS/etc. delivery is a separate workstream.
- **Built-in event sources beyond a generic webhook.** No first-class
  Gmail watcher, no calendar push subscription. Those become external
  producers that POST to `/event` once they exist.
- **Per-text similarity dedup.** Cooldown is per-category only; if a
  producer reuses the same category for unrelated nags it gets the
  same throttle.
- **Cross-process bus.** The pipecat-subagents bus stays in-process.
  External producers reach tend over HTTP, not over the bus.
- **Mining conversation history to suggest schedules.** Schedules are
  authored by the user (voice) or by the worker at skill-build time;
  nothing scrapes transcripts to invent them.

## Architecture

```
┌────────────────────────────── tend process ──────────────────────────────┐
│                                                                          │
│  Hub (audio + STT + TTS + LLMContext, unchanged)                         │
│   └── Brain (LLMAgent, voice-tight)                                      │
│         tools (new): schedule, list_schedules, cancel_schedule,          │
│                       enable_skill_triggers, disable_skill_triggers      │
│         tools (existing): do_task, list_skills, remind_in, …             │
│         └── Workers                                                      │
│               GeneralWorker (skill-driven; learns silent-default mode)   │
│               ReminderWorker (unchanged)                                 │
│                                                                          │
│  Scheduler (new BaseAgent, peer to Brain)                                │
│      reads ~/.tend/cron/jobs.json on init                                │
│      asyncio loop: sleep until next-fire, then dispatch                  │
│      dispatch path = Brain.request_task("general", payload=...)          │
│                                                                          │
│  Webhook receiver (new, aiohttp on 127.0.0.1:7331)                       │
│      POST /say   → ProactiveAnnouncer.announce(...)                      │
│      POST /event → router → request_task("general", ...)  if subscribed  │
│                                                                          │
│  ProactiveAnnouncer (new)                                                │
│      converging point for: scheduler fires, webhook /say, worker         │
│      announcements                                                       │
│      enforces cooldown, defers when Brain is active (unless urgent),     │
│      publishes TTSSpeakFrame + LLMMessagesAppendFrame to the bus         │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
                ▲                          ▲
                │ HTTP /say /event         │
                │                          │
        ┌───────┴────────┐         (future producers)
        │ vision daemon  │
        │ (separate proc │
        │  with camera)  │
        └────────────────┘
```

Frame flow on a scheduled fire:

1. Scheduler's loop wakes at the target time.
2. Scheduler calls `Brain.request_task("general", payload={...})` over the
   in-process bus.
3. GeneralWorker spawns `claude` in `~/.tend/workspace/`, runs the named
   skill end-to-end.
4. Worker produces a short spoken summary and calls
   `ProactiveAnnouncer.announce(text, source="scheduler:<job>",
   category="<skill>", urgent=False)`.
5. Announcer publishes `TTSSpeakFrame` and `LLMMessagesAppendFrame` to
   the bus.
6. Hub's TTS speaks the text; Hub's assistant aggregator captures the
   `LLMMessagesAppendFrame` into the shared `LLMContext`.

Frame flow on a vision-process nag:

1. Vision daemon detects bad posture, decides to nag.
2. `requests.post("http://127.0.0.1:7331/say", json={"text": "Sit up
   straight.", "category": "posture", "urgent": False})`.
3. Webhook handler authenticates, calls
   `ProactiveAnnouncer.announce(...)` with `source="webhook:vision"`.
4. Announcer applies cooldown (per category): if posture was nagged in
   the last 10 minutes, drop. Otherwise, if Brain is active, queue for
   deactivation. Otherwise publish to bus.
5. Same downstream as the scheduler path: TTS speaks, context records.

Frame flow on a structured `/event`:

1. Vision daemon: `POST /event` with `{"kind": "user_idle.45min"}`.
2. Webhook handler resolves which skills declare this event in their
   frontmatter (`events: [user_idle.45min]`).
3. For each match, `Brain.request_task("general", payload={"request":
   "Handle event", "skill": "<name>", "event": {...}})`.
4. GeneralWorker handles it like any other dispatch and may or may not
   call the announcer.

## Module additions

```
src/tend/
  scheduler.py         New — Scheduler BaseAgent + cron evaluation.
  announcer.py         New — ProactiveAnnouncer.
  webhook.py           New — aiohttp receiver and event router.
  cron_store.py        New — atomic JSON read/write for jobs.json
                              and jobs-state.json.
  skills.py            Extended — parse triggers + events frontmatter.
  brain.py             Add 5 tools, hold a Scheduler reference.
  workers/general.py   Accept silent_default + event payload; preamble
                              tweaks for heartbeat-mode behaviour.
  audio/hub.py         Hold a ProactiveAnnouncer reference; pass it
                              to scheduler and webhook.
  cli.py               Add `tend schedule …` and
                              `tend skills enable-triggers/disable-triggers`.
  config.py            Add SchedulerConfig, WebhookConfig, AnnouncerConfig.
  main.py              Construct + register the new components.
tests/
  test_scheduler.py    New
  test_announcer.py    New
  test_webhook.py      New
  test_cron_store.py   New
  workers/test_general.py  Add silent-default coverage.
~/.tend/skills/heartbeat/SKILL.md   Seeded on first boot.
tend.toml              New [scheduler], [webhook], [announcer] sections.
```

## Data model

Two files, mirroring OpenClaw's split between definitions and runtime.

`~/.tend/cron/jobs.json` (definitions, hand-editable, git-trackable):

```json
[
  {
    "id": "f7b2…",
    "name": "daily-meal-plan",
    "kind": "cron",
    "schedule": "0 12 * * *",
    "tz": "America/Los_Angeles",
    "payload": {
      "skill": "meal-plan",
      "request": "Generate today's lunch plan from the fridge."
    },
    "source": "voice",
    "enabled": true,
    "created_at": "2026-05-08T18:30:00-07:00"
  }
]
```

Allowed `kind`:

- `at` — one-shot at an ISO timestamp or relative offset (`"in 20m"`).
  Auto-deletes after run regardless of outcome.
- `cron` — 5-field cron expression with a required `tz`.
- `every` — fixed interval (`"30m"`, `"6h"`).

`source` is one of `voice`, `cli`, `skill:<name>`. It exists for
auditability and so `cancel_schedule` and `disable_skill_triggers` can
delete the right rows without ambiguity.

`payload.request` is always present and is the natural-language
instruction the worker receives. `payload.skill` is optional: when
present, the worker preamble is told "use the named skill"; when
absent, the worker picks from the catalog the same way it does for a
voice `do_task`. This lets ad-hoc voice schedules ("remind me in 20
minutes") work without a skill, while skill-authored triggers can pin
the skill explicitly.

`~/.tend/cron/jobs-state.json` (runtime, gitignored):

```json
{
  "f7b2…": {
    "last_run_at": "2026-05-08T12:00:03-07:00",
    "last_run_status": "succeeded",
    "last_error": null,
    "next_run_at": "2026-05-09T12:00:00-07:00",
    "consecutive_errors": 0
  }
}
```

Atomic writes use the same temp-file-rename pattern already in
`SessionStore`. A schema-version field at the top of each file lets us
evolve the layout without surprises.

## Authoring paths

### Voice — Brain tools

```python
@tool
async def schedule(self, params, when: str, request: str, name: str | None = None):
    """Schedule a recurring or one-shot job.

    Args:
        when: cron expression like '0 12 * * *', a relative offset like
              'in 3 hours', an ISO timestamp, or 'every 30m'.
        request: what the worker should do, in plain English.
        name: optional human-readable label.
    """

@tool
async def list_schedules(self, params): ...

@tool
async def cancel_schedule(self, params, name_or_id: str): ...

@tool
async def enable_skill_triggers(self, params, skill: str): ...

@tool
async def disable_skill_triggers(self, params, skill: str): ...
```

`schedule(...)` parses `when` with a small wrapper: cron-style strings
go to `croniter`; `"in 3h"` and `"every 30m"` are parsed by a custom
duration parser; ISO timestamps go to `datetime.fromisoformat`. Failure
to parse returns a clear error string for the LLM to relay to the user.

`enable_skill_triggers("meal-plan")` reads
`~/.tend/skills/meal-plan/SKILL.md`'s frontmatter and copies each
entry in `triggers:` into `jobs.json` with `source="skill:meal-plan"`,
`enabled=true`, and a `next_run_at` computed from the schedule. The
copy is one-time and inert: editing the SKILL.md `triggers:` later
does not propagate to active rows. To re-sync, the user calls
`enable_skill_triggers` again — it is idempotent (existing rows for
the same source are replaced rather than duplicated). Disabling
deletes those rows.

### Skill frontmatter

```yaml
---
name: meal-plan
description: Generate a meal plan from a list of ingredients on hand.
triggers:
  - cron: "0 12 * * *"
    tz: "America/Los_Angeles"
    request: "Generate today's lunch plan from the fridge inventory; lunch is in 30 minutes."
events:
  - user_idle.lunch_window
---
```

`triggers:` declares a default cadence the user might want; off until
`enable_skill_triggers` is called. `events:` declares webhook event
kinds the skill will handle when `/event` receives one.

### Default-off rationale

A skill the worker authors mid-conversation should never start nagging
the user without a second confirmation step. The scanner does not
reason about cron reasonableness — `* * * * *` (every minute) passes
regex checks just fine. Default-off is the simplest safety boundary.

The voice ergonomic cost is small: ad-hoc schedules go through
`Brain.schedule(...)` directly with `source="voice"` — they bypass
skill triggers entirely.

## ProactiveAnnouncer semantics

Single class, shared by scheduler, webhook `/say`, and worker
announcements. Pseudocode:

```python
class ProactiveAnnouncer:
    async def announce(self, text, *, source, category, urgent=False):
        if not urgent and self._on_cooldown(category):
            log.debug(f"announce dropped (cooldown): {category}")
            return False
        if not urgent and self._brain.active:
            self._pending.append((text, source, category))
            log.info(f"announce queued (brain active): {category}")
            return True
        await self._publish(text, source)
        self._mark_fired(category)
        return True

    async def on_brain_deactivated(self):
        for text, source, category in self._drain():
            if not self._on_cooldown(category):
                await self._publish(text, source)
                self._mark_fired(category)
```

`_publish` does both: `TTSSpeakFrame` for the speaker and
`LLMMessagesAppendFrame` for context. The append message is templated as
`"You announced (from {source}/{category}): {text!r}"` so Brain knows
who triggered it and can answer "what did the vision process tell me
earlier?".

Cooldown is per category. Defaults:

```toml
[announcer]
default_cooldown_s = 300

[announcer.category]
posture = 600
hydration = 900
```

Categories are free-form; producers pick whatever makes sense. The
configuration file is the only place to tune them; producers cannot
override their own cooldown.

Urgent announcements skip cooldown AND active-Brain deferral. Reserve
for safety-relevant cases (smoke alarm, doorbell-class events).
Everyday posture/hydration nags should always be `urgent=False`.

## External producer integration

### Webhook receiver

`tend.webhook` starts an `aiohttp` server during `runner.start()`.
Bound to `127.0.0.1` only. Token loaded from `TEND_WEBHOOK_TOKEN` in
`.env`. Token rotation is manual: edit `.env`, restart the unit.

```python
@routes.post("/say")
async def say(request):
    if not _auth_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    body = await request.json()
    text = body["text"]
    category = body.get("category", "general")
    urgent = bool(body.get("urgent", False))
    delivered = await announcer.announce(
        text, source=f"webhook:{_caller_label(body)}",
        category=category, urgent=urgent,
    )
    return web.json_response({"delivered": delivered})

@routes.post("/event")
async def event(request):
    if not _auth_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    body = await request.json()
    kind = body["kind"]
    matches = skills.find_event_subscribers(kind)
    for skill in matches:
        await brain.request_task("general", payload={
            "request": f"Handle event {kind}",
            "skill": skill.name,
            "event": body,
        })
    return web.json_response({"dispatched": [s.name for s in matches]})
```

### Vision-process integration

The vision process is a separate Python package on the same Pi. tend
ships no code for it. Documentation snippet:

```python
import os, requests
TOKEN = os.environ["TEND_WEBHOOK_TOKEN"]
def say(text, category="general", urgent=False):
    requests.post(
        "http://127.0.0.1:7331/say",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"text": text, "category": category, "urgent": urgent},
        timeout=2,
    )
def event(kind, **data):
    requests.post(
        "http://127.0.0.1:7331/event",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"kind": kind, **data},
        timeout=2,
    )
```

The vision process owns wording for `/say` and category choice. tend
trusts the payload — it does not sanitize, rate-limit beyond cooldown,
or content-filter.

## Heartbeat skill

Seeded on first boot at `~/.tend/skills/heartbeat/SKILL.md`:

```markdown
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

If you have nothing worth surfacing, send_task_response with
delivered=false and exit. Do not call ProactiveAnnouncer.
```

The corresponding `every: 30m` job is created in `jobs.json` on first
boot. Disabling: set `[scheduler] heartbeat_every = "off"` in
`tend.toml`; the job is removed on next start.

The heartbeat skill is the *only* place to register heartbeat
behaviour. Skills do not declare themselves as heartbeat-eligible
elsewhere; if the user wants a periodic silent check, they add to this
file (or the worker authors an addition during a build).

## GeneralWorker registration

GeneralWorker becomes **eagerly registered at boot** rather than added
lazily on the first `do_task` call. Reason: the scheduler or webhook
may dispatch a task before the user has spoken to Brain, and a
dispatch to an unregistered worker is silently dropped on the bus.

Construction cost is minimal: instantiating the BaseAgent does not
spawn a `claude` subprocess (that only happens per-task). The
`_ensure_general_worker` helper on Brain becomes a no-op — kept for
one release as a forwards-compatible shim, then removed.

ReminderWorker stays lazily registered for now since `remind_in` only
fires from the user-facing tool path and there is no proactive
caller.

## GeneralWorker payload changes

The worker dispatch payload gains two optional fields:

```python
payload = {
    "request": "...",
    "skill": "<name>",        # optional — pre-selects a skill
    "silent_default": False,  # heartbeat sets True
    "event": {...},           # /event payload, when present
}
```

Preamble additions:

- "If `silent_default` is true, you should publish a TTS announcement
  *only* if you have something the user genuinely needs to hear. If
  nothing is worth saying, return `{"delivered": false, "reason":
  "nothing-to-surface"}` from your task and do not call the announcer."
- "If `event` is present, the user did not ask for this directly — a
  trigger fired. Be brief; the user did not invite a long answer."

The worker still uses `ProactiveAnnouncer.announce` rather than
publishing `TTSSpeakFrame` directly, so cooldown and deferral apply
consistently.

## Privacy and security

- The webhook server binds to `127.0.0.1` only. Never exposed to the
  network. systemd unit picks up no extra ports.
- The token lives in `.env` (gitignored), shared with the vision
  process out-of-band. Rotation is manual.
- The vision process is *trusted*. tend does not sanitize the `text`
  field; it goes straight to TTS.
- The wake-gate privacy boundary is unaffected. Audio in is still
  gated by `OpenWakeWordGate`. The webhook only adds an *audio out*
  surface, which the user opted into by running the producer.
- Scheduler firing while the user is absent plays the announcement to
  an empty room. Acceptable — the user opted in by scheduling. v1
  does not gate on presence.
- Logs do not redact webhook payload `text` — assume anything you
  POST may appear in `/tmp/tend.log`. Same as Brain conversation
  content today.

## Configuration

```toml
[scheduler]
heartbeat_every = "30m"      # or "off"
missed_at_policy = "run-on-restart"   # or "skip"

[webhook]
host = "127.0.0.1"
port = 7331
# token from TEND_WEBHOOK_TOKEN in .env

[announcer]
default_cooldown_s = 300

[announcer.category]
posture = 600
hydration = 900
```

`missed_at_policy` controls one-shot `at` jobs whose target time fell
during a tend restart. `"run-on-restart"` (default) fires on resume;
`"skip"` deletes without firing. Recurring jobs always wait for next
scheduled fire — there is no concept of catching up.

## Migration

| File | Change |
|---|---|
| `src/tend/scheduler.py` | **New** — Scheduler BaseAgent. |
| `src/tend/announcer.py` | **New** — ProactiveAnnouncer. |
| `src/tend/webhook.py` | **New** — aiohttp receiver + event router. |
| `src/tend/cron_store.py` | **New** — atomic JSON I/O for jobs.json + jobs-state.json. |
| `src/tend/skills.py` | Extend frontmatter parser to accept `triggers:`, `events:`, `silent_default:`; add `find_event_subscribers(kind)`. |
| `src/tend/brain.py` | Add `schedule`, `list_schedules`, `cancel_schedule`, `enable_skill_triggers`, `disable_skill_triggers` tools; hold a Scheduler reference. |
| `src/tend/workers/general.py` | Accept `silent_default` + `event` + `skill` payload fields; replace direct `TTSSpeakFrame` publishes with announcer calls; preamble tweaks. Registered eagerly at boot. |
| `src/tend/audio/hub.py` | Hold a ProactiveAnnouncer reference, pass it to scheduler/webhook constructors; on `Brain.deactivated` notify the announcer to drain pending queue. |
| `src/tend/cli.py` | `tend schedule list/show/add/rm`, `tend skills enable-triggers/disable-triggers`, `tend webhook test`. |
| `src/tend/config.py` | Add `SchedulerConfig`, `WebhookConfig`, `AnnouncerConfig` to `Settings`. |
| `src/tend/main.py` | Construct scheduler, announcer, webhook; register with runner. |
| `tend.toml` | New `[scheduler]`, `[webhook]`, `[announcer]`, `[announcer.category]` sections with defaults shown above. |
| `~/.tend/skills/heartbeat/SKILL.md` | Seeded on first boot if missing. |
| `tests/test_scheduler.py` | **New** — cron eval, missed-fire behaviour, restart resume. |
| `tests/test_announcer.py` | **New** — cooldown, deferral, urgency override, drain on deactivate. |
| `tests/test_webhook.py` | **New** — auth, /say, /event routing, malformed payloads. |
| `tests/test_cron_store.py` | **New** — atomic writes, schema version, concurrent edits. |
| `tests/workers/test_general.py` | Add silent-default heartbeat coverage. |
| `CLAUDE.md` | Add "Proactive triggers" section: scheduler + webhook + announcer; document the privacy posture. |

What stays exactly as-is: Hub's audio pipeline, the wake gate, the
sleep-phrase gate, day-session reset, the in-process bus, GeneralWorker's
skill-catalog injection, the safety scanner, ReminderWorker.

## Open questions / risks

- **Same-tick contention.** Scheduler and webhook can both call
  `announce` near-simultaneously. The bus is FIFO, so they queue, but
  rapid back-to-back announcements may sound robotic. v1 ships with no
  inter-announcement gap; revisit if it feels wrong.
- **Cooldown granularity.** Per-category is coarse; if a producer
  reuses `category="general"` everything throttles together. Documented
  but not enforced. Text-similarity dedup is a v2 follow-up.
- **Unreasonable cron cadences.** A skill with `cron: "* * * * *"`
  passes the safety scanner's regex rules. v1 mitigations: voice/CLI
  recovery via `tend schedule rm`, plus default-off-until-enabled. A
  minimum-interval check on `enable_skill_triggers` is a v2 follow-up.
- **Webhook token trust.** Anyone with the token can speak through
  TTS. Token in `.env`, loopback-bound, manual rotation. Acceptable for
  single-user single-device. Reconsider if multi-user lands.
- **Missed `at` jobs across reboot.** `run-on-restart` is correct for
  reminders but wrong for time-sensitive operations (e.g. "send an
  email at 8 AM"). tend does not send email in v1, so the policy is
  fine; document the limitation.
- **Heartbeat skill becoming a junk drawer.** If everything ends up in
  one file, the prompt grows and the worker spends every tick on a long
  read. Mitigation: keep the seeded body firmly worded ("by default,
  exit silently"); user can split into multiple skills if needed.
- **Outbound channel delivery.** Spec describes only local TTS. Telegram
  / SMS / Slack delivery is not in v1. Producers that want
  channel-routing today must implement it themselves; the spec
  deliberately keeps the announcer single-target.
- **Vision process race on token.** If tend has not yet started its
  HTTP server when the vision daemon comes up, the daemon's POSTs fail.
  Vision daemon should retry on `ConnectionRefusedError`. tend does
  not need to know.

## What this enables once shipped

**Daily lunch plan.** User says "every day at noon, generate a meal
plan from the fridge and tell me lunch is in 30 minutes." Brain calls
`schedule("0 12 * * *", "Generate today's lunch plan from the fridge;
lunch is in 30 minutes")`. At noon every day, the scheduler dispatches
to GeneralWorker, which runs the meal-plan skill and announces the
result. If the user is mid-conversation with Brain when noon hits, the
announcement queues until Brain deactivates — typical UX.

**Posture nag.** Vision daemon detects bad posture for 60 seconds.
`POST /say` with `{"text": "Sit up straight.", "category":
"posture"}`. tend speaks it. Daemon detects the same posture again 30
seconds later, posts again — cooldown drops the second nag silently.
Ten minutes later the cooldown expires; if still bad, the next post
fires.

**Hydration timer.** Daemon watches the water bottle. After 90 minutes
without a sip: `POST /event` with `{"kind": "hydration.lapse",
"minutes": 90}`. A `hydration-coach` skill (declared `events:
[hydration.lapse]`) runs, may write a one-line nudge, may also update a
local hydration log under `~/.tend/workspace/data/hydration.json` for
later review. The skill decides the wording.

**One-shot reminder via voice.** "Remind me in 20 minutes that the oven
is on." Brain calls `schedule("in 20m", "Oven is on", name="oven")`.
At T+20 the scheduler fires, announcer speaks "Oven is on", job
deletes. (`remind_in` becomes a thin wrapper over `schedule` for
backwards compatibility.)

**Worker authoring a default cadence.** User says "make me a Friday
afternoon retro workflow." GeneralWorker authors
`~/.tend/skills/friday-retro/SKILL.md` with `triggers: [{cron: "0 16 *
* 5", tz: "...", request: "Run friday retro"}]`. The skill is created
disabled. Worker tells the user "I built it; say 'enable triggers for
friday-retro' if you want it to fire automatically." User can run it
on demand via `do_task` either way.
