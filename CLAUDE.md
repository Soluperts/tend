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
└── Hub (BaseAgent, always running) — owns LLMContext
      pipeline:
        transport.in → VAD → OpenWakeWordGate → STT
          → InputLatencyLogger → SleepPhraseGate
          → user_aggregator → BusBridgeProcessor (unnamed bridge)
          → TTS → OutputLatencyLogger → transport.out
          → assistant_aggregator
      └── Brain (LLMAgent, bridged=(), starts inactive)
            pipeline: [LLM]   (default LLMAgent.build_pipeline)
            └── Workers (lazy, persistent across wake/sleep)
                  e.g. ReminderWorker (v1 stub)
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

## Module layout

```
src/tend/
  audio/
    hub.py         Hub agent — audio + STT/TTS + LLMContext + aggregators
    gates.py       OpenWakeWordGate (pre-STT, with cooldown), SleepPhraseGate (post-STT)
    logging.py     Input/OutputLatencyLogger (pass-through, debug only)
  workers/
    reminder.py    ReminderWorker (v1 stub)
  brain.py         Brain (thin LLMAgent — build_llm + tools + on_task_update)
  session.py       SessionManager (soul.md, daily reset → Hub.reset_session)
  services.py      STT / TTS / brain LLM factories with preflight + fallback
  config.py        Settings (pydantic-settings, TOML + env)
  main.py          AgentRunner setup; entry point
scripts/
  audio_check.py   Standalone PyAudio mic/speaker sanity probe
deploy/
  tend.service     systemd user unit
soul.md            committed persona / context (loaded into system prompt)
tend.toml          committed default settings
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

## When in doubt

Read the spec: `docs/superpowers/specs/2026-05-05-tend-smart-speaker-design.md`. Read the implementation plan: `docs/superpowers/plans/2026-05-05-tend-v1.md`. If a request conflicts with the principles above, raise it before coding.
