# tend

Personal AI assistant for desk workers, running on a Raspberry Pi in the user's workroom. Audio I/O, VAD, openWakeWord-driven wake gate, STT, multi-turn brain (Anthropic Claude), worker scaffolding, optional ElevenLabs TTS. The brain dispatches long-running work to subagent workers that announce results autonomously through the speaker.

## Guiding principles

- **Compose Pipecat and pipecat-subagents.** If a processor, service, transport, agent base class, bus primitive, or activation hook exists upstream, use it. If you think you need a custom one, prove it doesn't exist first.
- **Privacy by default at the audio boundary.** While the brain is deactivated, the wake gate drops audio frames before STT — no conversation audio reaches Deepgram. Workers running in the background still hit the cloud (the user dispatched them); ambient room conversation does not.
- **Keep things simple.** No premature abstraction, no speculative extension points, no config knobs for hypothetical needs.
- **Each unit testable in isolation.** Constructor injection. No module-level singletons. If you can't test a component without spinning up the whole pipeline, the boundary is wrong.
- **Ask before adding deps.** New runtime dependencies need a real reason.

## Architecture (current)

Single Pipecat pipeline, owned by the `Hub` agent (always running). `Brain` is a `bridged=("voice",)` `LLMAgent` (with manually wired `LLMContextAggregatorPair`, since `LLMContextAgent` is post-0.4.0) child of Hub, deactivated by default; the gates inside Hub's pipeline activate/deactivate it. Workers are children of Brain, persistent across wake/sleep, can publish frames directly to the voice bridge for autonomous notifications.

```
AgentRunner (in-process AsyncQueueBus)
└── Hub (BaseAgent, always running)
      pipeline:
        transport.in → VAD → OpenWakeWordGate → STT
          → InputLatencyLogger → SleepPhraseGate
          → BusBridge("voice") → TTS → OutputLatencyLogger
          → transport.out
      └── Brain (LLMAgent + LLMContextAggregatorPair, bridged=("voice",), starts inactive)
            └── Workers (lazy, persistent across wake/sleep)
                  e.g. ReminderWorker (v1 stub)
```

Two state machines:
- **Brain.active** — toggled by the gates. ASLEEP ⇄ AWAKE on wake-word match / sleep-phrase / 30s silence.
- **Day-session** — owned by `SessionManager`. `LLMContext` flushed at boot and at the configured wall-clock time (default 04:00 local), or on `@tool start_fresh`.

Wake/sleep is *attention*. Day-session is *memory*. They don't interact except that an active day-session reset is deferred until Brain is asleep.

## Module layout

```
src/tend/
  audio/
    hub.py         Hub agent — audio pipeline owner
    gates.py       OpenWakeWordGate (pre-STT), SleepPhraseGate (post-STT)
    logging.py     Input/OutputLatencyLogger (pass-through, debug only)
  workers/
    reminder.py    ReminderWorker (v1 stub)
  brain.py         Brain (LLMAgent + tools + on_task_update)
  session.py       SessionManager (soul.md, daily reset)
  services.py      STT / TTS / brain LLM factories with preflight + fallback
  config.py        Settings (pydantic-settings, TOML + env)
  main.py          AgentRunner setup; entry point
deploy/
  tend.service     systemd user unit
soul.md            committed persona / context (loaded into system prompt)
tend.toml          committed default settings
```

## Privacy model

The wake gate is the privacy boundary. While Brain is deactivated:
- `OpenWakeWordGate` runs the local oww model, then **drops** `InputAudioRawFrame`s. STT receives zero audio → Deepgram billed zero, no conversation transmitted.
- Brain's `_BusEdgeProcessor` drops incoming bridge frames (framework-level — see spec §11). No Anthropic calls.
- TTS has nothing to synthesize → ElevenLabs billed zero.
- **Workers continue running** — they hit cloud APIs the user dispatched them against. Their output (TTS announcements, context updates to Brain) flows through anyway.

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
            bridge="voice",
            direction=FrameDirection.DOWNSTREAM,
        ))
        await self.send_task_update(message.task_id, {
            "kind": "announcement",
            "spoken": "Brief summary.",
            "context": {...rich detail for follow-ups...},
        })
        await self.send_task_response(message.task_id, {"delivered": True})
```

Brain dispatches via `request_task` (fire-and-forget) — never `async with self.task(...)` — so the brain isn't blocked on a tool call when it gets deactivated. Worker handles its own output; brain's `on_task_update` (which fires regardless of `Brain.active`) records the announcement in the `LLMContext` for follow-up questions on next wake.

## Deliberate non-choices for v1 (do not "fix" without asking)

- **No compaction** of the day-session context. Resets at the wall-clock boundary instead. Will revisit when context size becomes a real problem.
- **No persistent memory** across days. Each day starts from `soul.md` alone.
- **No real workers** beyond the `ReminderWorker` stub. Calendar / research / code / planning are each their own brainstorm → spec → build cycle.
- **No camera / vision.** Audio only.
- **No multi-user.** Single user, single device, no auth.
- **No dev "always-awake" bypass.** Architecture is honest; iterate by speaking the wake word.
- **No distributed deployment.** In-process AsyncQueueBus only.
- **No worker durability across process restart.** systemd brings the process back up clean.
- **No mid-stream cloud-service failover.** Lose the current turn if STT/TTS fails mid-frame; next turn falls back.

## Hardware assumptions

- Raspberry Pi 4 or 5.
- One USB microphone.
- One speaker (USB or Bluetooth — BT adds ~150-250 ms output latency, not fixable).
- PortAudio via PipeWire. Exactly one input and one output device.

## Running

```bash
# development
python -m tend

# production
systemctl --user enable --now tend
```

Config via `tend.toml` (committed) and `.env` (gitignored, secrets only). See README.

Logs: `/tmp/tend.log` (loguru, 5 MB rotation). Faults: `/tmp/tend.faults.log` (faulthandler). systemd journal: `journalctl --user -u tend`.

## When in doubt

Read the spec: `docs/superpowers/specs/2026-05-05-tend-smart-speaker-design.md`. Read the implementation plan: `docs/superpowers/plans/2026-05-05-tend-v1.md`. If a request conflicts with the principles above, raise it before coding.
