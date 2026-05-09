# tend × Google: Calendar awareness, schedule authoring, and event-driven proactivity

**Date:** 2026-05-09
**Status:** Design

## 1. Goal

Make tend an active partner in the user's day. Three layers, shipped as one
unified change:

- **A — Reads.** tend can talk about today's calendar and inbox state.
- **B — Writes.** tend can shape the day by writing scheduled blocks
  (deep work, exercise, lunch, breaks) into a tend-owned calendar.
- **C — Triggers.** Bracket-tagged calendar events fan out into the
  proactive-trigger bus, so reactive skills can run without being asked
  ("lunch in an hour, want me to suggest something?", "deep work block
  ended, time to stretch?").

The integration is delivered almost entirely as configuration + skills.
Source-tree churn is small (~50 lines + tests) and limited to one
extension of the scheduler shipped in the proactive-triggers feature.

## 2. Non-goals

- **No Brain `@tool` shortcuts** for calendar/gmail reads. Every read
  goes through GeneralWorker via `do_task`. Keeps Brain thin; matches
  the existing pattern; trades ~3-5s of voice latency for architectural
  coherence. Revisit only if latency becomes a real annoyance.
- **No Python wrapper around `gws`.** Skills shell out to `gws`
  directly; we wrap nothing. The cost of writing a thin Python helper
  module is rejected as premature abstraction for v1.
- **No `gws gmail +watch` → `/event` integration.** Real-time Gmail push
  is a future enhancement; v1 polls via the schedule-watcher cadence
  and a separate `mail-triage` skill.
- **No description-body machine tags.** Title prefix tags only.
  Description bodies stay user-prose-only in v1.
- **No mid-day conflict re-balancing.** If the user adds a meeting that
  overlaps a tend-scheduled deep-work block, tend flags it on the next
  watcher tick (via the briefing or a passing comment) but does not
  auto-move the block.
- **No Calendar/Gmail writes outside the tend-owned calendar.** The
  `calendar.app.created` scope ensures Google enforces this server-side
  even if a buggy skill tried.

## 3. Architecture

```
                                 ┌──────────────────┐
                                 │  Google APIs     │
                                 └────────▲─────────┘
                                          │
                                  gws (CLI on Pi)
                                          │
   ┌──────────────────────────────────────┼──────────────────────────────┐
   │                                      │                              │
   │         (Layer A — reads)            │   (Layer B — writes)         │
   │                                      │                              │
   │   skills:                            │   skills:                    │
   │   - briefing                         │   - routine-setup            │
   │   - meeting-prep                     │   - schedule-block           │
   │   - mail-triage                      │                              │
   │                                      │                              │
   │              shell out  ▲   ▲  shell out                            │
   │                         │   │                                       │
   │                         │   │                                       │
   │   (Layer C — triggers)  │   │                                       │
   │                                                                     │
   │   schedule-watcher (heartbeat-fired skill, every 15m):              │
   │   1. `gws calendar +agenda --json` for watched calendars            │
   │   2. parse bracket-tag prefixes from event titles                   │
   │   3. compute fire times per tag config                              │
   │   4. dedupe vs state file                                           │
   │   5. create one-shot scheduler jobs (event_kind = "<tag>.<phase>")  │
   │                                                                     │
   │   scheduler fires those jobs → /event-style dispatch →              │
   │   reactive skills run:                                              │
   │   - lunch-prep    (events: [lunch.upcoming])                        │
   │   - post-deep-work (events: [deep-work.ended])                      │
   │                                                                     │
   └─────────────────────────────────────────────────────────────────────┘
```

Three layers, all consumers of `gws`. Layer C uses the proactive-triggers
infrastructure (`Scheduler`, `find_event_subscribers`, `ProactiveAnnouncer`)
that shipped 2026-05-08, with one small extension to support
event-mode scheduled jobs.

## 4. gws setup & OAuth

`gws` (the [Google Workspace CLI](https://github.com/googleworkspace/cli))
is the API layer for everything below. It dynamically reads Google's
Discovery Service and exposes both helper commands (`+agenda`, `+triage`,
`+watch`) and the full discovery-generated surface. Authentication uses
its officially supported headless export flow.

### 4.1 One-time install (documented in `docs/google-setup.md`)

1. **Install gws on the Pi:** `npm install -g @googleworkspace/cli`.
2. **Create OAuth client** in Google Cloud Console (Desktop app type),
   download `client_secret.json`. One-time per Google project.
3. **Login on laptop:** `gws auth setup` followed by `gws auth login`,
   granting **all three scopes**:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/calendar.readonly`
   - `https://www.googleapis.com/auth/calendar.app.created`
4. **Export to Pi:**
   ```
   gws auth export --unmasked > google-creds.json
   scp google-creds.json pi:~/.tend/secrets/google-creds.json
   chmod 600 ~/.tend/secrets/google-creds.json   # on the Pi
   ```
5. **Wire into systemd** by adding the env var to `deploy/tend.service`:
   ```
   Environment=GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=%h/.tend/secrets/google-creds.json
   ```
6. **Verify on the Pi:** `gws auth status` should show all three scopes.

If a refresh ever fails (rare with refresh tokens; happens if the user
revokes consent or Google rotates), repeat steps 3–4.

### 4.2 Scope rationale

- **`gmail.readonly`** — full read access including bodies. Required by
  `gws gmail +triage` (which surfaces snippets) and any meeting-prep
  use-case that needs to summarize a thread. Read-only at the API
  level — no send/delete/modify possible. The constraint comes from
  Google's enforcement, not our code.
- **`calendar.readonly`** — read every calendar the user has access to,
  including shared calendars added to their account. Required for
  briefing and meeting-prep across calendars beyond `primary`.
- **`calendar.app.created`** — write access **only** to calendars the
  app itself created. Google enforces this server-side. The user's
  personal/work calendars stay readable but un-writable even if a
  buggy skill tries.

All three are granted at the single OAuth dance. We don't widen
incrementally — re-OAuthing later is friction we can avoid by asking
once.

## 5. Tend calendar bootstrap

Single manual command on the Pi after auth:

```bash
gws calendar +insert --summary "tend" \
                     --description "Schedule blocks managed by tend"
```

The command returns the new calendar's ID. Save it to a single-line
file at `~/.tend/google/tend-calendar-id`. Write skills (Layer B) read
this file to know where to send events.

The file lives outside `tend.toml` because:
- It's per-instance state, not user-edited config.
- It must survive `tend.toml` edits without manual re-application.
- A missing file is a recoverable error (write skills fail with a clear
  "tend calendar not bootstrapped" message); a stale value would silently
  send writes to the wrong calendar.

This step is documented but not automated in `tend` — automating it
would risk re-creating the calendar on each restart if the file went
missing, and the one-liner is acceptable as a one-time install step.

## 6. Tag vocabulary, lead times, and watched calendars

### 6.1 Title prefix tags

Calendar events drive triggers via a leading bracket tag in the title:

- `[deep-work] Plan Q2 docs`
- `[lunch] with kids`
- `[exercise]`

The schedule-watcher parses this with `re.match(r"^\[([\w-]+)\]", title)`.
Untagged events are skipped without logging (most events the user
creates are untagged; logging each would dwarf the signal). Tagged
events with a tag absent from `[google.events.<tag>]` are skipped
*with* a debug log line so the user can spot misspellings.

Why title prefix and not description tags or labels:
- Tags are visible to the user in calendar UI summaries.
- They survive moves between calendars without metadata loss.
- They're easy to type into Google Calendar directly without a
  config-block convention to remember.

### 6.2 `tend.toml` shape

```toml
[google]
watched_calendars = ["tend", "primary"]   # by name; resolved at startup

[google.events.deep-work]
upcoming_lead = "5m"
emit = ["upcoming", "starting", "ended"]

[google.events.lunch]
upcoming_lead = "60m"
emit = ["upcoming", "starting"]

[google.events.exercise]
upcoming_lead = "15m"
emit = ["upcoming", "starting", "ended"]

[google.events.break]
upcoming_lead = "0m"
emit = ["starting"]
```

**Mechanics:**
- `upcoming_lead`: offset from event start where `<tag>.upcoming` fires
  (`0m` is equivalent to `starting`).
- `emit`: phases the watcher actually emits. Anything not listed is
  computed but never dispatched.
- Unknown tags are ignored by the watcher, with a debug log line.
- Adding a new tag is a `tend.toml` edit + service restart.

**Watched calendars:**
- Default: `["tend", "primary"]`. The list-of-strings shape is the only
  configuration; users wanting more (e.g. a shared work calendar) add
  entries by name.
- The watcher resolves names → IDs once at startup via
  `gws calendar list --json`. Unresolvable names log a warning and
  are skipped.

## 7. Schedule authoring (Layer B)

### 7.1 Source of truth

The calendar itself. No separate routine config file. Routines are
recurring events stored directly in the tend calendar with RRULE.

If the user moves an event in Google Calendar UI, tend respects the
move on the next watcher tick (the event's `updated` timestamp changes,
the watcher cancels the old fired-state entry and reschedules
forward-looking phases). If the user deletes it, it's gone — tend
does not restore.

Rejected alternative: routine config file as truth, tend reconciling on
each restart. This produces "why did tend just put my deleted event
back?" surprises. The calendar is the canonical state.

### 7.2 `routine-setup` skill (conversational)

Triggered by phrases like "set up my daily routine," "redo my
schedule," "configure my day."

Conversation walk-through:
1. "What time and days do you want exercise?" — parses time + RRULE
   days (defaults: weekdays).
2. "Lunch?"
3. "Deep work blocks?"
4. "Anything else regular? (break, meal prep, walk?)"

For each block:
- Tag is inferred (exercise → `[exercise]`, etc.).
- Conflicts with watched calendars are checked at write time. Per-day
  conflicts are surfaced ("Tuesday morning has a 9am meeting; skip the
  deep-work block that day or pick a different time?"). User confirms
  inline.
- A single recurring event is written via `gws calendar +insert
  --calendar <tend-id> --recurrence "RRULE:FREQ=WEEKLY;BYDAY=..."`.

No silent calendar mutations — every write is preceded by a "shall I
go ahead?" turn.

### 7.3 `schedule-block` skill (ad-hoc)

Triggered by "schedule deep work tomorrow morning," "block 2-4pm
Wednesday for X."

For approximate times ("morning"):
1. Read watched calendars for the requested day.
2. Find the largest free slot in the requested window (rough heuristic:
   morning = 7am-12pm, afternoon = 12pm-5pm, evening = 5pm-9pm).
3. Propose back: "Tomorrow 9-11am is open, sound good?"
4. On confirm, write one event via `gws calendar +insert`.

Exact times skip the slot-finding step; conflict detection still runs
and warns the user.

## 8. Schedule-watcher (Layer C)

Heartbeat-fired skill at 15-minute cadence. Heartbeat config:

```toml
# already lives in tend.toml after the proactive-triggers feature
[scheduler]
heartbeat_every = "30m"   # the existing global heartbeat
```

The schedule-watcher itself uses the `triggers:` frontmatter convention
shipped in proactive-triggers:

```yaml
# ~/.tend/skills/schedule-watcher/SKILL.md
triggers:
  - when: "every 15m"
silent_default: true
```

When the user runs `enable_skill_triggers schedule-watcher` (or the
post-install script does it on first boot), the trigger is materialized
into `~/.tend/cron/jobs.json`.

### 8.1 Watcher tick

On each tick:

1. **Read watched calendars** for the next 2-hour window:
   ```
   gws calendar +agenda --calendar <each watched id> \
                        --time-min <now> --time-max <now+2h> --json
   ```
2. **Parse tags.** For each event, match `^\[([\w-]+)\]` against the
   title. Untagged → ignore.
3. **Compute phase fire times.** For each `(event, phase)` pair where
   `phase` is in `emit` for that tag:
   - `upcoming` → start_time − upcoming_lead
   - `starting` → start_time
   - `ended` → end_time
4. **Dedupe** against `~/.tend/cache/schedule-watcher-state.json`. The
   state file is keyed by `event_id`, with values
   `{"updated": "<gws timestamp>", "fired_phases": ["upcoming", ...]}`.
   - If the event's `updated` field has changed since last seen, drop
     the cached phases (event was edited; reschedule everything
     forward-looking).
   - Otherwise skip phases already in `fired_phases`.
5. **Schedule.** For each unfired phase whose fire time is in the
   future, run the CLI
   `tend schedule add --when <iso> --event "<tag>.<phase>" --payload <json> --name <unique> --source schedule-watcher`.
   The watcher is a skill subprocess; it talks to the in-process
   scheduler through the shared `~/.tend/cron/jobs.json` store. The
   running scheduler's nap cap (60s) ensures the new job is picked up
   well before any reasonable fire time. For phases that just passed
   while the watcher slept (`now > fire_time > last_tick`), fire
   immediately by HTTP POSTing to the local webhook
   (`POST http://127.0.0.1:7331/event` with the standard Bearer token
   from `TEND_WEBHOOK_TOKEN`).
6. **Persist** the updated state file.

### 8.2 Why one-shot scheduler jobs, not direct dispatch

Precision. A 15-min watcher cadence means a "lunch.upcoming"
fired directly would land somewhere in a 15-min window of T-60.
Creating a one-shot scheduler job at the precise fire time gives
second-level delivery via the scheduler we already trust.

### 8.3 Cadence rationale

- 1-min cadence: each tick is a Haiku turn (skill via GeneralWorker).
  ~$40/month just for polling. Rejected.
- 15-min cadence: ~$3/month. Sufficient because the scheduler delivers
  at second-precision once a one-shot job is in place. The watcher's
  only job is "have I already scheduled the firings for the next 2
  hours?" — that's tolerant of cadence.
- Cadence sets an upper bound on how late an event can be added (or
  edited forward) before its phases get missed. With a 15m cadence,
  if the user creates an event whose first phase fires within the
  next 15m, the watcher tick after that creation might run too late.
  Acceptable for v1 — the use-case is "user shaped their day in
  advance," not "user just added an event 5m before it fires." If
  this turns into a real problem, raise the cadence to 5m or add a
  manual "tend re-scan" voice command later.

### 8.4 State file lifecycle

`~/.tend/cache/schedule-watcher-state.json` is purely a dedupe cache.
Lost or corrupted: the watcher rebuilds it on the next tick. The
worst-case outcome of cache loss is one duplicate firing per affected
event — Announcer cooldowns mostly absorb this; users will rarely
notice.

Old entries are pruned on each tick: any `event_id` whose end time
is more than 24h in the past is removed.

## 9. Scheduler extension for event-mode jobs

The proactive-triggers scheduler dispatches every job to `general` (the
GeneralWorker). The watcher needs to schedule "fire `lunch.upcoming`
with this context at exactly 11:00am" — i.e. dispatch via
`/event` semantics, not via skill request.

### 9.1 Data model change

`CronJob` (in `src/tend/cron_store.py`) gains two optional fields:

```python
@dataclass
class CronJob:
    id: str
    name: str
    kind: Literal["at", "cron", "every"]
    schedule: str
    tz: str | None
    payload: dict
    source: str
    enabled: bool
    event_kind: str | None = None      # NEW
    event_payload: dict | None = None  # NEW
```

`_row_to_cronjob` already filters unknown keys (forward-compat shim
shipped in the proactive-triggers feature), so reading legacy rows is
unaffected.

### 9.2 Scheduler change

`Scheduler._fire_job`:

```python
async def _fire_job(self, job: CronJob) -> None:
    st = self._store.get_state(job.id)
    try:
        if job.event_kind:
            await self._dispatch_event(job.event_kind, job.event_payload or {})
        else:
            await self._dispatch("general", job.payload)
        # ... (existing succeeded-state recording)
    except Exception as e:
        # ... (existing failed-state recording)
```

`_dispatch_event` is a thin wrapper around the existing
`find_event_subscribers` + `request_task` loop the webhook uses for
`POST /event`. Factor that loop out of `webhook.py` into a small helper
in `webhook.py` (or `dispatch.py` if a third call site appears) so both
producers share it.

### 9.3 CLI change

`tend schedule add` gains:

```
--event KIND       # if set, creates an event-mode job
--payload JSON     # required when --event is set
```

When `--event` is set, `--request` is rejected (mutually exclusive).

## 10. Skills shipped in v1

Seven skills under `~/.tend/skills/`, plus shared helper scripts under
`~/.tend/workspace/bin/`.

### Reads (Layer A)

- **`briefing`** — morning summary. Fetches today's events from watched
  calendars and unread Gmail count + top urgent senders. Triggered by
  "morning briefing," "what's my day," "brief me." Speaks one or two
  sentences.
- **`meeting-prep`** — for the next event with attendees, calls
  `gws gmail +recent --from <attendee>` for each attendee and surfaces
  3-5 most recent threads. Triggered by "prep my next meeting," "who am
  I meeting next."
- **`mail-triage`** — runs `gws gmail +triage --json` and groups by
  sender priority. Triggered by "what's in my inbox," "any new mail."

### Writes (Layer B)

- **`routine-setup`** — section 7.2.
- **`schedule-block`** — section 7.3.

### Reactive (Layer C)

- **`lunch-prep`** — `events: [lunch.upcoming]`. If a meal-plan was
  generated for today (cached in `~/.tend/workspace/plans/`), announce
  it. Otherwise, ask "lunch in an hour, want me to suggest something?"
- **`post-deep-work`** — `events: [deep-work.ended]`, `silent_default:
  true`. Suggests a stretch/water break only if the deep-work block was
  ≥45min (sourced from `event_payload.duration_min`).

### Infrastructure

- **`schedule-watcher`** — heartbeat-fired, `silent_default: true`. The
  C-layer engine. Has `triggers: [{ when: "every 15m" }]`.

### Shared scripts in `~/.tend/workspace/bin/`

- `gws-agenda.sh` — wraps `gws calendar +agenda --json` with a
  `--watched` flag that reads the tend.toml watched_calendars.
- `gws-recent-mail.sh` — wraps `gws gmail +recent` with sane defaults.
- `gws-find-slot.sh` — given a day + window, returns the largest free
  slot in JSON.
- `tend-schedule-event.sh` — wraps `tend schedule add --event ...` so
  skills don't have to remember the flag set.

Each script is 5-15 lines. They exist so skill authors don't repeat
gws invocations across skills.

## 11. Failure modes

| Failure | User-visible behavior |
|---|---|
| OAuth token expired (refresh failed) | First skill that hits `gws` announces "I lost access to your Google account, please re-authorize." Cooldown: 24h on category `google-auth`. |
| `gws` not installed / wrong path | Same path; message "Google integration isn't installed." Once per boot. |
| Network down | Skill silently fails, logs; no announcement. Watcher skips this tick. Recovers on next tick when network returns. |
| Empty briefing (no events, no urgent mail) | Skill speaks "Nothing on your calendar today, inbox is quiet." |
| Routine conflict at write time | Inline in the conversation: "Tuesday morning has a 9am meeting; skip that day or pick another time?" |
| Event created or moved less than `cadence` before its first phase | That phase may be missed by up to one cadence (15m). Subsequent phases on the same event still fire. |
| `~/.tend/google/tend-calendar-id` missing during a write skill | Skill announces "tend calendar not bootstrapped — run the install step." |
| `schedule-watcher-state.json` corrupted | Watcher logs and rebuilds. Worst case: one duplicate firing per affected event; Announcer cooldown absorbs most. |

All `gws` invocations capture stdout/stderr. Non-zero exit codes
propagate as skill failures, which the GeneralWorker already routes to
logs (`/tmp/tend.log`) without crashing the daemon.

## 12. Configuration summary

`tend.toml` additions:

```toml
[google]
watched_calendars = ["tend", "primary"]

[google.events.deep-work]
upcoming_lead = "5m"
emit = ["upcoming", "starting", "ended"]

[google.events.lunch]
upcoming_lead = "60m"
emit = ["upcoming", "starting"]

[google.events.exercise]
upcoming_lead = "15m"
emit = ["upcoming", "starting", "ended"]

[google.events.break]
upcoming_lead = "0m"
emit = ["starting"]
```

`deploy/tend.service` addition:

```
Environment=GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=%h/.tend/secrets/google-creds.json
```

State files (gitignored, per-instance):

- `~/.tend/secrets/google-creds.json` — gws encrypted token. chmod 600.
- `~/.tend/google/tend-calendar-id` — single-line file with the tend
  calendar ID.
- `~/.tend/cache/schedule-watcher-state.json` — fired-phase dedupe +
  last-seen `updated` timestamps per event id.

## 13. Source-tree changes

Minimal, all to support the scheduler event-mode extension:

- `src/tend/config.py` — new `GoogleConfig` Pydantic model: top-level
  `watched_calendars: list[str]` + `events: dict[str, GoogleEventConfig]`
  where `GoogleEventConfig` has `upcoming_lead: str` and
  `emit: list[Literal["upcoming", "starting", "ended"]]`.
- `src/tend/cron_store.py` — `CronJob` gains `event_kind: str | None`
  and `event_payload: dict | None`, both default `None`. Backward-compat:
  `_row_to_cronjob` already filters unknown keys.
- `src/tend/scheduler.py` — `_fire_job` branches on `event_kind`. When
  set, dispatches via the same path used by webhook `/event`. Else
  legacy GeneralWorker dispatch.
- `src/tend/webhook.py` — extract a `dispatch_event(kind, payload, *,
  skills_root, brain_request_task)` helper from the `/event` handler.
  Both webhook and scheduler use it.
- `src/tend/cli.py` — `tend schedule add` gains `--event KIND`,
  `--payload JSON`. Mutually exclusive with `--request`.

Total churn: ~50 lines + tests. No new files in `src/tend/`.

## 14. Testing

- **Unit tests**, no live API calls in CI:
  - Mock `subprocess.run` for `gws` calls. Fixture JSON files under
    `tests/fixtures/gws/` for representative responses (one
    `agenda.json`, one `triage.json`, one `calendar-list.json`).
  - Tag parsing happy path + edge cases (`[]`, `[bad spaces]`,
    `[multi-word-tag]`, missing brackets, multibyte chars).
  - Phase computation respects `upcoming_lead` + `emit`.
  - Dedupe logic: rerun watcher → no duplicate jobs.
  - Event `updated` change → old jobs cancelled, new ones scheduled.
- **Scheduler event-dispatch tests:** new fakes for `event_kind` jobs.
  Assert dispatch routes to event-subscribers path, not
  `dispatch("general", ...)`.
- **CLI test:** `tend schedule add --event lunch.upcoming --payload
  '{"event_id":"x"}' --when 2026-12-31T11:00:00+00:00 --name lunch-prep
  --source test` persists with `event_kind` set, `payload` empty.
- **Manual smoke tests** documented in `docs/google-setup.md`:
  - `gws calendar +agenda --json` from the Pi shell.
  - `tend schedule list` — should show `schedule-watcher` job after
    enable.
  - "morning briefing" voice command.

## 15. Migration / rollout

One-time procedure (in `docs/google-setup.md`):

1. Install `gws`.
2. Create OAuth client in Google Cloud Console.
3. `gws auth login` on laptop with all three scopes.
4. `gws auth export --unmasked > google-creds.json`; scp to Pi at
   `~/.tend/secrets/google-creds.json`. chmod 600.
5. Add env var to `tend.service`. Restart unit.
6. `gws calendar +insert --summary tend ...` once on the Pi. Save the
   returned ID to `~/.tend/google/tend-calendar-id`.
7. Edit `tend.toml` to add the `[google]` blocks (paste from this spec).
8. Restart tend.
9. Speak "set up my daily routine" — `routine-setup` walks through.

After install, existing tend behavior is unchanged for users who skip
the Google steps:
- No `tend.toml [google]` → `GoogleConfig` defaults to empty `events`
  and `watched_calendars = []` → schedule-watcher skill does nothing.
- The scheduler event-mode extension is a no-op for jobs without
  `event_kind`.

## 16. Open questions

None blocking. Items for future work:

- **`gws gmail +watch` → `/event` real-time integration.** The seam is
  identified (it would post `mail.received` events with sender/subject
  to the existing `/event` endpoint, where skills like `urgent-mail`
  could subscribe). Out of scope for v1; tracking only.
- **Description-body machine tags** for per-event lead-time overrides
  ("this lunch needs 30m heads-up, not 60"). Speculative; revisit when
  a real use-case appears.
- **Mid-day conflict rebalancing.** If the user adds a meeting that
  overlaps a tend-scheduled deep-work block, tend currently flags but
  doesn't move. Auto-move on conflict is a UX call we should make after
  living with the current behavior for a few weeks.
- **Brain `@tool` shortcuts.** Whether casual voice queries like
  "what's on my calendar?" feel slow enough through GeneralWorker
  (~3-5s) to justify direct Brain tools (~300ms cached). Defer until
  measured.
