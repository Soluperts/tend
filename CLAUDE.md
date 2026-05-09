# tend

Personal AI assistant for desk workers, running on a Raspberry Pi in the user's workroom. Audio I/O, VAD, openWakeWord-driven wake gate, STT, multi-turn brain (Anthropic Claude), worker scaffolding, optional ElevenLabs TTS. The brain dispatches long-running work to subagent workers that announce results autonomously through the speaker.

## Guiding principles

- **Compose Pipecat and pipecat-subagents.** If a processor, service, transport, agent base class, bus primitive, or activation hook exists upstream, use it. If you think you need a custom one, prove it doesn't exist first.
- **Privacy by default at the audio boundary.** While the brain is deactivated, the wake gate drops audio frames before STT — no conversation audio reaches Deepgram. Workers running in the background still hit the cloud (the user dispatched them); ambient room conversation does not.
- **Keep things simple.** No premature abstraction, no speculative extension points, no config knobs for hypothetical needs.
- **Each unit testable in isolation.** Constructor injection. No module-level singletons. If you can't test a component without spinning up the whole pipeline, the boundary is wrong.
- **Ask before adding deps.** New runtime dependencies need a real reason.

## Architecture (current)

Two pipelines bridged by the in-process bus, matching the canonical pipecat-subagents shape.

`Hub` is the always-running transport-owning parent. It owns audio I/O, STT, TTS, and **the conversation context** (`LLMContext` + `LLMContextAggregatorPair`). The user aggregator sits before the bridge; the assistant aggregator sits at the very end of the pipeline (after `transport.output`) — that placement is required, the aggregators are terminal sinks for their frame types and only "see" the LLM's output if TTS is between them.

`Brain` is a thin `LLMAgent` child of Hub with `bridged=()`. It contributes `build_llm()`, the `@tool` methods, and `on_task_update`. It does **not** own context — the LLM just processes whatever `LLMContextFrame` arrives via the bus. Brain is deactivated by default; the gates inside Hub's pipeline flip its `active` flag.

Workers are children of Brain, persistent across wake/sleep, and can publish frames directly to the bus for autonomous notifications.

```
AgentRunner (in-process AsyncQueueBus)
├── Hub (BaseAgent, always running) — owns LLMContext + ProactiveAnnouncer ref
│     pipeline:
│       transport.in → VAD → OpenWakeWordGate → STT
│         → InputLatencyLogger → SleepPhraseGate
│         → user_aggregator → BusBridgeProcessor (unnamed bridge)
│         → TTS → OutputLatencyLogger → transport.out
│         → assistant_aggregator
│     └── Brain (LLMAgent, bridged=(), starts inactive)
│           pipeline: [LLM]   (default LLMAgent.build_pipeline)
│           └── GeneralWorker (eager-registered; uses ProactiveAnnouncer)
└── Scheduler (BaseAgent, peer to Hub) — owns wall-clock + jobs.json/jobs-state.json

WebhookServer (aiohttp on 127.0.0.1:7331) — POST /say, /event
  publishes via the same ProactiveAnnouncer that Scheduler and GeneralWorker use
```

Frame flow on a turn:
1. STT → `TranscriptionFrame` → SleepPhraseGate forwards → user_aggregator captures into `LLMContext` → emits `LLMContextFrame` downstream.
2. `LLMContextFrame` → bridge → bus → Brain.LLM → emits `TextFrame` chunks.
3. Brain's edge sink → bus → Hub.bridge.on_bus_message → pushes downstream past the bridge.
4. TTS synthesizes the text chunks into audio → `transport.output` plays it.
5. assistant_aggregator (terminal) captures the same `TextFrame` chunks back into the shared `LLMContext`.

Two state machines:
- **Brain.active** — toggled by the gates. ASLEEP ⇄ AWAKE on wake-word match / sleep-phrase / 30 s silence. After deactivation, OpenWakeWordGate enforces a 1.5 s cooldown (drops audio without running OWW + calls `Model.reset()` to clear oww's prediction history) so the trailing audio of the sleep phrase can't re-fire wake.
- **Day-session** — owned by `SessionManager`. `Hub.reset_session(soul_text)` flushes `LLMContext` at boot and at the configured wall-clock time (default 04:00 local), or on `@tool start_fresh`.

Wake/sleep is *attention*. Day-session is *memory*. They don't interact except that an active day-session reset is deferred until Brain is asleep.

### Bus-bridge gotcha

The framework's `_BusEdgeProcessor` (which wraps every bridged agent) sends outgoing bus frames **without** a bridge name (`pipecat_subagents/agents/base_agent.py:149`). So a named-bridge filter on `BusBridgeProcessor` would orphan Brain's responses — keep the bridge unnamed (no `bridge=` kwarg) and use `bridged=()` on the child. All canonical examples in pipecat-subagents follow this pattern. Use `exclude_frames=` (not bridge names) to keep specific frame types local; we exclude `TTSSpeakFrame` so wake-word acks ("Yes?") don't broadcast to Brain.

### Skills layer

`~/.tend/skills/<name>/SKILL.md` files teach the GeneralWorker how to handle
recurring requests. At spawn time the worker enumerates the catalog and
injects compact `<available-skills>` XML into claude's system prompt;
claude reads the full SKILL.md body via Read on demand. Scripts live in
`~/.tend/workspace/bin/`. New skills are authored mid-task by claude when
no existing skill matches; a regex safety scanner gates them, with
critical findings moved to `~/.tend/skills-quarantined/<name>/` instead of
being executed. Worker config (model, allowed tools, workspace dir) lives
under `[workers.general]` in `tend.toml`.

## Module layout

```
src/tend/
  audio/
    hub.py         Hub agent — audio + STT/TTS + LLMContext + announcer drain
    gates.py       OpenWakeWordGate, SleepPhraseGate (calls hub.on_brain_deactivated)
    logging.py     Input/OutputLatencyLogger (pass-through, debug only)
  workers/
    claude_cli.py  ClaudeCliWorker (base — runs `claude` CLI subprocess)
    general.py     GeneralWorker (skill-driven; silent_default heartbeat mode; routes via announcer)
  announcer.py     ProactiveAnnouncer — single TTS funnel; cooldown/deferral/urgency
  brain.py         Brain (thin LLMAgent — build_llm + tools; remind_in wraps schedule)
  cron_store.py    JSON job store (jobs.json + jobs-state.json)
  cron_time.py     Pure helpers: parse_when, next_fire_at
  scheduler.py     Scheduler BaseAgent — wall-clock dispatch loop
  webhook.py       aiohttp /say + /event receiver, token-authed, loopback-only
  skills.py        Skills layer (frontmatter parser learns triggers/events/silent_default)
  session.py       SessionManager (soul.md, daily reset → Hub.reset_session)
  sessions.py      SessionStore (per-worker persistent session metadata)
  services.py      STT / TTS / brain LLM factories with preflight + fallback
  preflight.py     `claude` CLI preflight check
  config.py        Settings (pydantic-settings, TOML + env)
  cli.py           `tend` CLI — sessions / skills / scan-skill / schedule / webhook test
  main.py          AgentRunner setup; entry point
scripts/
  audio_check.py   Standalone PyAudio mic/speaker sanity probe
deploy/
  tend.service     systemd user unit
soul.md            committed persona / context (loaded into system prompt)
tend.toml          committed default settings (now includes [scheduler], [webhook], [announcer])
```

## Privacy model

The wake gate is the privacy boundary. While Brain is deactivated:
- `OpenWakeWordGate` runs the local oww model, then **drops** `InputAudioRawFrame`s. STT receives zero audio → Deepgram billed zero, no conversation transmitted.
- Brain's `_BusEdgeProcessor` drops incoming bridge frames when `Brain.active=False` (framework-level — see `base_agent.py:160`). No Anthropic calls.
- TTS has nothing to synthesize → ElevenLabs billed zero.
- **Workers continue running** — they hit cloud APIs the user dispatched them against. Their output (TTS announcements, context updates to Hub) flows through anyway.

Auditable: `lsof -p $(pgrep -f tend)` and `journalctl --user -u tend`.

## Worker pattern (canonical)

```python
class MyWorker(BaseAgent):
    @task
    async def do_thing(self, message):
        result = await ...                          # the actual work
        await self.bus.publish(BusFrameMessage(
            source=self.name,
            frame=TTSSpeakFrame("Brief summary."),
            direction=FrameDirection.DOWNSTREAM,
        ))
        await self.send_task_update(message.task_id, {
            "kind": "announcement",
            "spoken": "Brief summary.",
            "context": {...rich detail for follow-ups...},
        })
        await self.send_task_response(message.task_id, {"delivered": True})
```

Brain dispatches via `request_task` (fire-and-forget) — never `async with self.task(...)` — so the brain isn't blocked on a tool call when it gets deactivated. Worker handles its own output. Brain's `on_task_update` fires regardless of `Brain.active`; it forwards the announcement to Hub's context as an `LLMMessagesAppendFrame` published on the bus, so the next conversation turn includes what was said while asleep.

GeneralWorker is the v1 reference: one worker handles every dispatch (Brain's `do_task` tool) and gets capability from skills under `~/.tend/skills/`, rather than each capability requiring a new worker class.

## Deliberate non-choices for v1 (do not "fix" without asking)

- **No compaction** of the day-session context. Resets at the wall-clock boundary instead. Will revisit when context size becomes a real problem.
- **No persistent memory** across days. Each day starts from `soul.md` alone.
- **No new worker classes per capability.** Calendar / research / planning worker behavior comes from skills authored on demand under `~/.tend/skills/` and run by the single `GeneralWorker`, not from new worker classes.
- **No camera / vision.** Audio only.
- **No multi-user.** Single user, single device, no auth.
- **No dev "always-awake" bypass.** Architecture is honest; iterate by speaking the wake word.
- **No distributed deployment.** In-process AsyncQueueBus only.
- **No worker durability across process restart.** systemd brings the process back up clean.
- **No mid-stream cloud-service failover.** Lose the current turn if STT/TTS fails mid-frame; next turn falls back.
- **No retry/backoff on failed scheduled jobs.** Recurring jobs wait for next scheduled fire; one-shot jobs delete after a single attempt.
- **No outbound channel routing.** Scheduler/webhook announcements only go to local TTS in v1; Telegram/SMS delivery is a separate workstream.

## Hardware assumptions

- Raspberry Pi 5.
- One USB microphone.
- One speaker (USB or Bluetooth — BT adds ~150-250 ms output latency, not fixable).
- PortAudio via PipeWire. Exactly one input and one output device.

USB mic-arrays like the reSpeaker XVF3800 are typically rate-locked to 16 kHz and PortAudio bypasses PipeWire's resampler, so TTS output is pinned to `settings.sample_rate` (default 16 kHz). ElevenLabs serves PCM at the requested rate; Piper resamples internally from the voice's native rate.

## Running

```bash
# development
python -m tend

# production
systemctl --user enable --now tend

# isolate hardware from pipeline issues
python scripts/audio_check.py speaker   # 1 kHz tone
python scripts/audio_check.py mic       # 3 s capture
python scripts/audio_check.py loopback  # record + play back
```

Config via `tend.toml` (committed) and `.env` (gitignored, secrets only). See README.

Logs: `/tmp/tend.log` (loguru, 5 MB rotation). Faults: `/tmp/tend.faults.log` (faulthandler). systemd journal: `journalctl --user -u tend`.

## Proactive triggers

The scheduler + announcer + webhook stack lets tend act without being
asked. Read `docs/superpowers/specs/2026-05-08-tend-proactive-triggers-design.md`
before editing any of `src/tend/{scheduler,announcer,webhook,cron_store,cron_time}.py`.

- **Voice authoring:** `Brain.schedule(when, request, name)`. `when`
  accepts cron expressions, `in 30m`-style relatives, ISO timestamps,
  or `every 30m`.
- **Skill defaults:** `triggers:` entries in `SKILL.md` frontmatter are
  inert until the user runs `enable_skill_triggers <skill>` (voice) or
  edits `~/.tend/cron/jobs.json` while tend is stopped.
- **External producers:** see `docs/integrating-with-tend.md`. Vision
  daemon, Gmail webhooks, etc. POST to `127.0.0.1:7331/say` or
  `/event` with a Bearer token from `TEND_WEBHOOK_TOKEN`.
- **Cooldown / deferral:** all proactive announcements go through
  `ProactiveAnnouncer.announce` so they share one cooldown registry
  (per-category) and queue while Brain is mid-conversation.
  `urgent=True` bypasses both.
- **Heartbeat:** an `every 30m` job seeded at first boot dispatches the
  `heartbeat` skill silently. The skill announces only when there is
  something genuinely worth saying. Disable with
  `[scheduler] heartbeat_every = "off"`.

## When in doubt

Read the spec: `docs/superpowers/specs/2026-05-05-tend-smart-speaker-design.md`
and `docs/superpowers/specs/2026-05-07-deskclaw-skills-and-general-worker-design.md`.
Read the implementation plans: `docs/superpowers/plans/2026-05-05-tend-v1.md` and
`docs/superpowers/plans/2026-05-07-deskclaw-skills-and-general-worker.md`. If a request conflicts with the principles above, raise it before coding.
