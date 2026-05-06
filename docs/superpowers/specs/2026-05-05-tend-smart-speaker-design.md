# Tend — Personal AI assistant (v1)

**Date:** 2026-05-05
**Scope:** Voice-first personal assistant running continuously on a Raspberry Pi in the user's workroom. v1 covers the audio loop, multi-turn brain with day-session memory, worker scaffolding, and one stub worker (timer/reminder). Real workers (calendar, research, code edits, planning), camera/vision, persistent cross-day memory, and compaction are explicit non-goals for this spec; each gets its own brainstorm → spec → build later.

## 1. Goals

What v1 delivers, end-to-end:

1. The Pi runs `tend` as a systemd user service. Reboot the Pi and it comes up listening.
2. User says "hey jarvis"; the system responds "Yes?". User has a casual conversation with the brain (Anthropic Claude). Brain's context persists across wake/sleep cycles within a day.
3. User can say things like "remind me in 5 minutes about water" — the brain dispatches a `ReminderWorker`, replies "Got it", the worker runs in the background.
4. User says "goodbye jarvis" or stays silent for 30 seconds — the brain deactivates. While deactivated, **no audio data leaves the device** (no Deepgram traffic, no Anthropic calls, no ElevenLabs synthesis).
5. While the brain is deactivated, in-flight workers continue running. When a worker completes, it speaks a brief autonomous announcement through the speaker (e.g., "Reminder: drink water") and updates the brain's context so the user can ask follow-up questions on next wake.
6. At a configurable wall-clock time (default 04:00 local), the day-session resets — the brain's rolling context is flushed and `soul.md` is reloaded.
7. The system can be tested component-by-component (gates, brain, worker, session manager) without spinning up the full audio pipeline.

## 2. Principles

These constrain implementation decisions throughout. They override convenience.

- **Compose Pipecat / pipecat-subagents.** If a processor, service, transport, filter, agent base class, bus primitive, or activation hook exists upstream, use it. Custom code is a last resort. If we think we need a custom component, prove the upstream one doesn't fit first.
- **Privacy by default at the audio boundary.** While the brain is deactivated, the wake gate drops audio frames before STT. No conversation audio reaches Deepgram. Workers running in the background still touch the cloud (the user dispatched them); ambient room conversation does not.
- **Keep things simple.** No premature abstraction. No config knobs for hypothetical needs. Three similar lines is better than a premature abstraction.
- **Each unit testable in isolation.** Constructor injection, no module-level singletons, no hidden global state. If you can't test a component without spinning up the whole pipeline, the boundary is wrong.

## 3. Architecture

### Agent topology

```
AgentRunner  (one process, in-process AsyncQueueBus)
│
├── Hub (BaseAgent, always running, owns mic + speakers)
│     pipeline:
│       transport.in → VAD → OpenWakeWordGate → STT
│         → SleepPhraseGate → BusBridge("voice") → TTS → transport.out
│
└── Brain (LLMContextAgent, child of Hub, bridged=("voice",))
      wraps AnthropicLLMService with an LLMContext that persists across
      wake/sleep within a day-session. Starts with active=False.
      │
      └── Workers (children of Brain, lazy-created on first @tool dispatch)
            e.g. ReminderWorker (the v1 stub)
            persist across Brain wake/sleep cycles
            publish TTSSpeakFrames directly to the "voice" bridge
            deliver structured context updates to Brain via send_task_update
```

Hub is the audio hub and the runner's only top-level agent. Brain is added as Hub's child at startup. Workers are added as Brain's children lazily when the brain's `@tool` calls them for the first time.

### Two state machines

The system has two independent state machines:

**Brain.active** — the wake/sleep flag. Toggled by the gates inside Hub's pipeline.

```
                   ┌── 30s silence timeout ──┐
                   ▼                          │
   ┌─────────┐   oww fires "hey jarvis"   ┌──────────┐
   │  ASLEEP │ ──────────────────────────▶│  AWAKE   │
   └─────────┘                            └──────────┘
        ▲                                       │
        └── sleep phrase fuzzy match ◀──────────┘
```

`@tool start_fresh` is **not** a transition on this state machine — it triggers a day-session reset (Section 5.6) but Brain stays active, ready for the next user turn against a freshly-built `LLMContext`.

**Day-session lifecycle** — owned by `SessionManager`. Independent of Brain.active. The day-session lives across many awake/asleep transitions.

```
boot ─▶ load soul.md ─▶ build LLMContext with system prompt ─▶ run forever
          │
          └── on configured wall-clock time (default 04:00 local)
              OR @tool start_fresh:
                 if Brain.active: defer until next deactivation
                 else: brain.reset_session(soul=read_soul_md())
```

Wake/sleep is *attention*. Day-session is *memory*. They don't interact except that an active day-session reset is deferred until Brain is asleep (so we don't yank context out from under a live conversation).

### What "asleep" means concretely

- `OpenWakeWordGate` drops `InputAudioRawFrame`s after running them through the oww model. STT receives no audio → Deepgram websocket sees zero traffic → zero billing.
- `Brain.active = False`. `_BusEdgeProcessor` (the framework-injected gate at Brain's pipeline boundary) drops incoming bus frames. AnthropicLLMService is idle.
- `TTS` (ElevenLabs) has nothing to synthesize → zero billing.
- Workers continue running. They publish `TTSSpeakFrame`s directly to Hub's `"voice"` bridge — Hub is always active and Hub's `BusBridgeProcessor` does not gate on active state.
- `Brain.on_task_update` (a bus-message hook, not a frame processor) fires regardless of `Brain.active`. Workers use this channel to deliver structured context updates that mutate `Brain._context` directly. On next activation, Brain has those updates in context.

### What the user experiences

- Asleep: complete silence from the device, with the option of brief autonomous announcements from workers ("Reminder: water").
- Awake: standard conversational turn-taking. Brain hears, brain replies. Brain's `LLMContext` accumulates the conversation. Brain can dispatch workers via `@tool` calls.

## 4. Privacy boundary

The wake/sleep boundary is the privacy boundary. Concretely:

| What's running while asleep | Cloud reach |
|---|---|
| `LocalAudioTransport` (mic open) | None |
| `VADProcessor` (silero, local model) | None |
| `OpenWakeWordGate` (local ONNX model) | None |
| Deepgram websocket | Connection may be open but **receives no audio** → zero billing, zero conversation transmitted |
| ElevenLabs websocket | Open but receives no synthesis requests → zero billing |
| Anthropic SDK client | Instantiated but no `messages.create` calls → zero traffic |
| Workers in flight (e.g., a code-edit worker the user dispatched earlier) | **Yes** — they hit the APIs they need to. The user dispatched them; they're working on the user's behalf. |
| Worker keepalives, telemetry, etc. | Negligible protocol overhead, no conversation content |

Auditable by `lsof -p $(pgrep -f tend)` and `journalctl --user -u tend`.

The privacy claim: **conversation audio captured by the mic does not leave the device while the brain is deactivated**. It does not claim "no packets at all" — websocket pings to cloud services don't carry conversation, and user-dispatched workers can absolutely be talking to the cloud while user is in another room.

## 5. Components

Each component lists purpose, interface, and dependencies. Module paths are under `src/tend/`.

### 5.1 `Hub` — `tend.audio.hub.Hub(BaseAgent)`

**Purpose:** the audio hub. Always running. Owns the single Pipecat pipeline. Adds Brain as a child at startup.

**Pipeline:**
```python
[
    transport.input(),
    VADProcessor(SileroVADAnalyzer()),
    OpenWakeWordGate(model_name=settings.openwakeword_model,
                     threshold=settings.wake_threshold,
                     hub=self),
    stt,                        # Deepgram or Whisper
    SleepPhraseGate(sleep_phrase=settings.sleep_phrase,
                    fuzz_ratio=settings.sleep_fuzz_ratio,
                    timeout_s=settings.awake_timeout_s,
                    hub=self),
    BusBridgeProcessor(bus=self.bus, agent_name=self.name, bridge="voice"),
    tts,                        # ElevenLabs or Piper
    transport.output(),
]
```

**Interface:** standard subagents lifecycle. The gates call `await self.activate_agent("brain")` and `await self.deactivate_agent("brain")`.

**Depends on:** `LocalAudioTransport`, `_make_stt()` and `_make_tts()` factories (Section 5.7), `OpenWakeWordGate`, `SleepPhraseGate`, `BusBridgeProcessor`.

### 5.2 `OpenWakeWordGate` — `tend.audio.gates.OpenWakeWordGate(FrameProcessor)`

**Purpose:** pre-STT audio gate. Listens to every audio frame with openWakeWord; flips Brain to active on a confident wake-word match.

**Behaviour:**
- On `InputAudioRawFrame` while Brain is inactive: pass through `oww.predict()`. If max-score model ≥ threshold, push `TTSSpeakFrame("Yes?")` (rendered by whichever TTS service is active in the pipeline — ElevenLabs if available, Piper otherwise), call `await self._hub.activate_agent("brain")`. Drop the audio frame.
- On `InputAudioRawFrame` while Brain is active: forward downstream (STT will consume).
- All other frames: forward.

**Interface:** `__init__(model_name: str, threshold: float, hub: Hub)`. Constructor only.

**Depends on:** `openwakeword.Model`, hub reference.

### 5.3 `SleepPhraseGate` — `tend.audio.gates.SleepPhraseGate(FrameProcessor)`

**Purpose:** post-STT text gate. Detects sleep phrase via fuzzy match. Tracks silence for the awake timeout.

**Behaviour:**
- On `TranscriptionFrame` while Brain is active: rapidfuzz ratio against `sleep_phrase`. If ≥ `fuzz_ratio`, call `await self._hub.deactivate_agent("brain")`, swallow the frame.
- On `UserStartedSpeakingFrame` while Brain is active: cancel pending silence timer, schedule a new one for `timeout_s` seconds.
- On silence timer expiry: call `await self._hub.deactivate_agent("brain")`.
- All other frames: forward.

**Interface:** `__init__(sleep_phrase: str, fuzz_ratio: float, timeout_s: int, hub: Hub)`.

**Depends on:** `rapidfuzz`, hub reference.

### 5.4 `Brain` — `tend.brain.Brain(LLMContextAgent)`

**Purpose:** conversational LLM. Wraps `AnthropicLLMService`. Owns the rolling per-day `LLMContext`.

**Bridged:** `("voice",)`. The framework auto-injects `_BusEdgeProcessor` at Brain's pipeline boundaries; these route incoming voice-bridge frames into Brain's pipeline (and outgoing back) — but only while Brain is active.

**Custom methods:**
- `reset_session(soul_text: str)` — replaces `LLMContext` with a fresh one whose system prompt is `soul_text + voice_rules`. Called by SessionManager at boot and at the wall-clock boundary.
- `on_task_update(message)` — overrides BaseAgent's hook. If `message.update["kind"] == "announcement"`, append a structured system message to `LLMContext` (the spoken summary plus the structured context dict). If `kind == "error"`, append an error system message. This hook fires regardless of `self.active`, which is how worker context updates reach the brain while it's deactivated.

**Tools (`@tool` decorators):**
- `remind_in(seconds: int, what: str)` — fire-and-forget dispatch to `ReminderWorker`. Returns "Got it, I'll remind you in N seconds." Does NOT await the task response.
- `start_fresh()` — calls `SessionManager.reset_now()`. Brain replies "Starting fresh."

**Tool dispatch pattern (canonical for v1):**
```python
@tool
async def remind_in(self, params: FunctionCallParams, seconds: int, what: str):
    await self._ensure_reminder_worker()       # idempotent; adds child on first call only
    task_id = await self.request_task("reminder",
                                       payload={"seconds": seconds, "what": what})
    return f"Got it, I'll remind you in {seconds} seconds."
```

`request_task` is the fire-and-forget variant. Brain does NOT use `async with self.task(...)` because that context manager would block until the task completes — wrong for tasks that may fire after deactivation. The exact mechanism for "ensure reminder worker exists" is an implementation detail (a flag on Brain, a check against the agent registry, etc.); the spec just requires it be idempotent.

**Depends on:** `AnthropicLLMService`, `LLMContext`, `LLMContextAggregatorPair`.

### 5.5 `ReminderWorker` — `tend.workers.reminder.ReminderWorker(BaseAgent)`

**Purpose:** the v1 stub worker. Demonstrates the persistent + autonomous-TTS + context-update pattern. Real workers in later specs follow the same shape.

**Behaviour:**
- On task request: `asyncio.sleep(seconds)`.
- On completion: publish `TTSSpeakFrame(f"Reminder: {what}")` to bridge `"voice"` (Hub plays it). Send `task_update` with `{"kind": "announcement", "spoken": ..., "context": {...}}` (Brain's `on_task_update` appends to LLMContext). Send `task_response`.
- On exception: publish a `TTSSpeakFrame` saying the task failed; send `task_update` with `{"kind": "error", ...}`; send `task_response` with error status.
- Cancellable via subagents standard cancellation; on cancel, no announcement, just a clean exit.

**Interface:** standard subagents `BaseAgent` + `@task`-decorated handler.

**Depends on:** `asyncio`, the bus.

### 5.6 `SessionManager` — `tend.session.SessionManager`

**Purpose:** owns the day-session lifecycle. Loads `soul.md` at boot, schedules wall-clock daily reset, exposes manual reset.

**Interface:**
- `start()` — at boot: read `soul.md`, call `brain.reset_session(soul_text)`, schedule the next reset task.
- `reset_now()` — immediate reset. Re-reads `soul.md` (so edits to the file take effect on the next manual or scheduled reset), calls `brain.reset_session`. If `Brain.active`, defers until next deactivation by registering a one-shot hook on the next `on_deactivated` callback.

**Scheduling:** asyncio task that sleeps until the next configured wall-clock time (using `zoneinfo` for local-time math), then fires the reset and reschedules.

**Depends on:** filesystem (`soul.md`), `asyncio`, `zoneinfo`, brain reference.

### 5.7 STT / TTS factories — `tend.services`

**Purpose:** preserve the existing cloud-preferred / local-fallback pattern.

**Functions:**
- `_make_stt() -> STTService` — if `DEEPGRAM_API_KEY` is set, run preflight (`GET /v1/projects`); on success return `DeepgramSTTService`. On failure log and return `WhisperSTTService` (faster-whisper, `tiny.en`, int8 CPU).
- `_make_tts() -> tuple[TTSService, int]` — if `ELEVENLABS_API_KEY` is set, run preflight (`GET /v1/user/subscription`); on success return `ElevenLabsTTSService` and `24000`. On failure or near-exhausted quota log and return `PiperTTSService` and `22050`.
- `_make_brain() -> Brain` — if `ANTHROPIC_API_KEY` is set, run preflight (1-token `/v1/messages`); on success construct Brain wrapping `AnthropicLLMService`. On failure log and construct Brain wrapping a stub `EchoLLMService` (a built-in Pipecat-compatible service that echoes input as output, used purely so the rest of the pipeline still works degraded).

These functions are pure and unit-testable with `httpx` mocked.

### 5.8 Latency loggers — `tend.audio.logging`

**Purpose:** debug-time observability. Lightweight processors that timestamp key events.

`InputLatencyLogger` (placed before STT): logs `[t]` markers for `UserStartedSpeakingFrame`, `UserStoppedSpeakingFrame`, `TranscriptionFrame` arrival.
`OutputLatencyLogger` (placed after TTS): logs `[t]` markers for `TTSStartedFrame`, first `TTSAudioRawFrame`, `BotStartedSpeakingFrame`, `BotStoppedSpeakingFrame`.

Inherited from existing code with light renames. Useful when iterating on perceived response time.

## 6. Data flow

Five concrete walkthroughs.

### 6.1 Cold boot

1. systemd starts the process: `python -m tend`.
2. `Settings()` (pydantic-settings) loads from class defaults → `tend.toml` → `.env` → environment.
3. `SessionManager` reads `soul.md`, holds the text.
4. Cloud preflights run sequentially: Anthropic → Deepgram → ElevenLabs. Each either passes (cloud service used) or logs failure (local fallback).
5. `AgentRunner()` is constructed with the in-process `AsyncQueueBus`.
6. `Hub("hub", bus=runner.bus, transport=transport, stt=stt, tts=tts)` is added to the runner. Hub's `build_pipeline()` returns the full pipeline.
7. `Hub.add_agent(Brain("brain", bus=runner.bus, llm_service=anthropic_or_echo))` registers Brain as a child. Brain starts with `active=False`.
8. `SessionManager.start()` calls `brain.reset_session(soul_text)` and schedules the next 04:00 reset.
9. `runner.run()` — pipeline live; oww listening; STT/Brain idle.

### 6.2 Wake → conversation → sleep

```
user: "hey jarvis"
  InputAudioRawFrame → OpenWakeWordGate → oww.predict() ≥ threshold
    gate: push TTSSpeakFrame("Yes?")  (Piper renders locally)
          await self._hub.activate_agent("brain")
  speaker plays "Yes?"

user: "what's the weather like?"
  audio frames flow through the gate (Brain active)
  → STT (Deepgram) emits TranscriptionFrame
  → SleepPhraseGate: no fuzzy match, forwards
  → BusBridgeProcessor tags with "voice", publishes BusFrameMessage
  → Brain's _BusEdgeProcessor receives, forwards into Brain pipeline
  → LLMContextAggregator appends user message to LLMContext
  → AnthropicLLMService streams reply text
  → reply frames flow back through bridge to Hub's pipeline
  → TTS (ElevenLabs) synthesises
  → transport.out plays

user: "goodbye jarvis"
  → STT TranscriptionFrame("goodbye jarvis")
  → SleepPhraseGate: rapidfuzz ratio ≥ 0.85
       await self._hub.deactivate_agent("brain")
       swallow the frame
  → Brain.active = False
  → next audio frame is dropped by OpenWakeWordGate
```

### 6.3 Worker dispatch (Brain awake)

```
user: "remind me in five minutes to drink water"
  → STT, gates, bridge, Brain receives
  → Anthropic LLM decides to call @tool remind_in(seconds=300, what="drink water")
  → Brain.remind_in:
       ensure ReminderWorker child exists (idempotent)
       task_id = await self.request_task("reminder", payload={...})  # fire-and-forget
       return "Got it, I'll remind you in 300 seconds."
  → tool result becomes LLM tool-response → LLM speaks acknowledgment
  → user hears "Got it..."
  → ReminderWorker's task_handler is now running its asyncio.sleep
```

### 6.4 Worker autonomous notification (Brain deactivated)

```
3 minutes later, user said sleep, Brain.active = False
ReminderWorker.task_handler:
  await asyncio.sleep(300) completes
  await self.bus.publish(BusFrameMessage(
      frame=TTSSpeakFrame("Reminder: drink water"),
      bridge="voice"))
  await self.send_task_update(task_id, {
      "kind": "announcement",
      "spoken": "Reminder: drink water",
      "context": {"what": "drink water", "completed_at": "..."}})
  await self.send_task_response(task_id, {"delivered": True})

Hub side:
  Hub's BusBridgeProcessor receives the BusFrameMessage (not gated on active state)
  → pushes TTSSpeakFrame downstream → ElevenLabs → speaker plays
  user hears "Reminder: drink water"

Brain side:
  Brain's on_task_update fires (bus-message hook, not gated on active state)
  → kind == "announcement"
  → self._context.add_message({
        "role": "system",
        "content": "You announced to the user: 'Reminder: drink water'\n"
                   "Details:\n  what: drink water\n  completed_at: ..."})

  Brain's _BusEdgeProcessor sees the BusFrameMessage(TTSSpeakFrame) too,
  but drops it because Brain is deactivated. Fine — the audible announcement
  was already played by Hub.
```

### 6.5 Daily reset

```
04:00 local time:
  SessionManager's scheduled task wakes
  if brain.active is True:
      register one-shot deferred reset on next on_deactivated
  else:
      soul = read_text(settings.soul_path)
      await brain.reset_session(soul)
        → brain swaps LLMContext for a fresh one
  reschedule next 04:00 reset
```

For the manual `start_fresh` voice command: same `reset_session` call, but the `@tool start_fresh` handler returns "Starting fresh." which is spoken via the existing tool-result flow before the LLMContext swap (the swap takes effect before the next user turn).

## 7. Error handling

v1 policy: log, fall back where feasible, crash and let systemd restart for unrecoverable cases. No retry/circuit-breaker logic until we see what actually breaks.

### 7.1 Cloud service degradation

| When | What happens |
|---|---|
| Boot preflight fails | Log reason, fall back to local equivalent (Whisper / Piper / `EchoLLMService`). Process continues. Anthropic preflight failure leaves the brain in a degraded echo state — known limitation. |
| Deepgram websocket drops mid-session | Pipecat auto-reconnects. The current utterance may be lost — user retries. Logged; no user-facing TTS. |
| ElevenLabs fails mid-stream | Catch in the TTS factory, mark ElevenLabs unavailable for the rest of the day-session, fall back to Piper for subsequent turns. The current turn's audio is lost — accepted. Mid-stream switchover is out of scope. |
| Anthropic times out / errors mid-turn | Brain logs, pushes a `TTSSpeakFrame("I had trouble there, can you try again?")` (rendered by the active TTS) as a system-injected announcement. Brain doesn't crash. |

### 7.2 Worker errors

Workers catch their own exceptions and report via three channels: `TTSSpeakFrame` ("Sorry, I couldn't complete the reminder task"), `send_task_update({"kind": "error", ...})` (so Brain can answer follow-up questions about the failure), and `send_task_response(..., status=TaskStatus.ERROR)` (for the framework's bookkeeping).

Cancellation (`asyncio.CancelledError`) is silent — no announcement, no error update. Subagents handles it cleanly.

### 7.3 Agent-level crashes

A processor exception in Hub or Brain crashes the process. Logs go to `/tmp/tend.log` (loguru) and `/tmp/tend.faults.log` (faulthandler for native crashes). systemd restarts. We don't try to keep half-functioning state alive — fail loud, restart clean.

### 7.4 Day-session and configuration errors

| When | What happens |
|---|---|
| `soul.md` missing | Log warning, fall back to a built-in default system prompt ("You are a helpful voice assistant in the user's workroom. Keep replies brief, no markdown, plain prose."). Process continues. |
| `soul.md` malformed (e.g., enormous) | Not validated. User responsibility. |
| Daily reset fires while user is mid-conversation | Defer to next deactivation via a flag in SessionManager. |
| Voice `@tool start_fresh` mid-conversation | The tool returns "Starting fresh." which the LLM speaks as its assistant message; the current turn completes; THEN SessionManager swaps `LLMContext` for a fresh one before the next user turn. The user's next utterance lands in a clean context. |

### 7.5 Process-level recovery

systemd user unit has `Restart=on-failure` + `RestartSec=2s` + `StartLimitBurst=5` + `StartLimitIntervalSec=300`. Five failed starts within 5 minutes → systemd marks the service as `failed` and stops trying. Prevents thrashing-restart loops on a persistently broken state. User investigates via `journalctl --user -u tend`.

State that survives restart: nothing in v1. `LLMContext` and in-flight workers are in-memory; the next boot starts with a fresh LLMContext built from `soul.md`. Acceptable for v1; worker durability is v1.5 territory.

## 8. Configuration

Two-file split: secrets in `.env` (gitignored), settings in `tend.toml` (committed).

### 8.1 Settings (committed in `tend.toml`)

| Key | Purpose | Default |
|---|---|---|
| `llm_model` | Anthropic model id | `claude-haiku-4-5` |
| `deepgram_model` | Deepgram model | `nova-3-general` |
| `elevenlabs_voice_id` | ElevenLabs voice | `EXAVITQu4vr4xnSDxMaL` |
| `elevenlabs_model` | ElevenLabs model | `eleven_turbo_v2_5` |
| `whisper_model` | faster-whisper model | `tiny.en` |
| `piper_voice` | Piper voice | `en_US-ryan-high` |
| `sample_rate` | Mic sample rate | `16000` |
| `openwakeword_model` | oww model name | `hey_jarvis` |
| `wake_threshold` | oww confidence cutoff (0–1) | `0.5` |
| `sleep_phrase` | Sleep target phrase | `goodbye jarvis` |
| `sleep_fuzz_ratio` | rapidfuzz threshold (0–1) | `0.85` |
| `awake_timeout_s` | Silence-to-sleep timeout | `30` |
| `daily_reset_time` | Wall-clock daily reset (`HH:MM`) | `04:00` |
| `timezone` | IANA timezone for daily reset | system default |
| `soul_path` | Path to soul.md | `<project>/soul.md` |
| `log_path` | loguru log file | `/tmp/tend.log` |

Override via environment with `TEND_<KEY>` (e.g., `TEND_AWAKE_TIMEOUT_S=60`).

### 8.2 Secrets (gitignored in `.env`)

```
ANTHROPIC_API_KEY=
DEEPGRAM_API_KEY=
ELEVENLABS_API_KEY=
```

`.env.example` ships in git with the same shape and blank values.

### 8.3 Loading order (Pydantic Settings)

Highest precedence first:
1. Environment variables
2. `.env`
3. `tend.toml`
4. Class defaults

`pydantic-settings` natively loads TOML via `SettingsConfigDict(toml_file="tend.toml", env_file=".env")` and a `settings_customise_sources` override that adds `TomlConfigSettingsSource`.

### 8.4 Config schema

Single `Settings(BaseSettings)` class in `tend/config.py`. All keys above declared as fields with their defaults. No nesting in v1.

### 8.5 Soul (committed in `soul.md`)

A markdown file at the project root. Loaded as plain text and prepended (or templated, with voice-rules appended) into the brain's system prompt at session start. User edits `soul.md` to tune persona, behaviour rules, project facts.

## 9. Testing

### 9.1 Unit tests

| Component | What you test |
|---|---|
| `OpenWakeWordGate` | feed `InputAudioRawFrame`s, assert audio is dropped while inactive, assert `hub.activate_agent` + Piper "Yes?" `TTSSpeakFrame` are emitted on a real wake-word match. Real oww model + a small recorded "hey jarvis" audio fixture. |
| `SleepPhraseGate` | feed `TranscriptionFrame("goodbye jarvis")` → assert deactivate. Mock asyncio time, fire silence timeout → assert deactivate. |
| `SessionManager` | load soul.md → assert reset_session called with text. Mock wall-clock → assert scheduled fire. Call `reset_now()` → assert reset propagation. Defer-while-active path. |
| `ReminderWorker` | dispatch task with `seconds=0.01` → assert `TTSSpeakFrame` published, `task_update(kind="announcement")` sent, `task_response` sent. Cancellation → no announcement. Exception path → `task_update(kind="error")`. |
| `Brain` | `@tool remind_in` dispatches via `request_task` and returns acknowledgment. `on_task_update(kind="announcement")` appends a system message to `LLMContext`. `reset_session` swaps the context. |
| `_make_stt()` / `_make_tts()` / `_make_brain()` | mock `httpx`. With key + healthy preflight → cloud service. With bad key or failed preflight → local fallback. |

### 9.2 Integration tests

- `test_wake_conversation_sleep_cycle.py` — full pipeline with `pipecat`'s frame-injection helpers. Mocked transport, mocked STT/TTS/LLM. Inject "hey jarvis" audio fixture → assert Brain activates → inject transcript → assert mock LLM was invoked → inject "goodbye jarvis" → assert deactivate.
- `test_worker_autonomous_completion.py` — dispatch a `ReminderWorker` task, deactivate Brain, advance asyncio time, assert (a) a `TTSSpeakFrame` reached Hub's TTS path, (b) Brain's `LLMContext` got the system message via `on_task_update`. This is the regression test for the framework-active-flag-gating issue called out in §11.

### 9.3 Manual smoke test (acceptance for v1)

Run `python -m tend` on the actual Pi:
- Wake/sleep cycle end-to-end with real audio.
- "Remind me in 30 seconds about water" → "Got it" → wait → "Reminder: water".
- Sleep phrase → silence. 30s timeout from awake → silence.
- Wake again, ask "what was that earlier?" → confirm Brain has the reminder in its context.

### 9.4 Approach

- TDD via the `superpowers:test-driven-development` skill for new code.
- pytest + pytest-asyncio (matches subagents' setup).
- Pipecat ships frame-injection test helpers in `pipecat.tests` — used for integration tests.
- `tests/` mirrors `src/tend/` layout.

### 9.5 What's NOT tested in v1

- Real cloud services with real network. We mock at the SDK boundary.
- Real audio I/O. The Pi smoke test is the audio-correctness gate.
- Daily reset at literal 4am wall-clock. Scheduler tested with mocked time.

## 10. Build and deploy

### 10.1 `pyproject.toml`

- Package name: `tend`
- Dependencies:
  - `pipecat-ai[whisper,piper,silero,local,deepgram,elevenlabs] >= 1.1`
  - `pipecat-ai-subagents == 0.4.0` (pinned; track CHANGELOG)
  - `openwakeword >= 0.4`
  - `rapidfuzz >= 3.0`
  - `pydantic-settings >= 2.2`
  - `anthropic >= 0.40` (transitive via `AnthropicLLMService`)
- Dev dependencies: `pytest`, `pytest-asyncio`, `freezegun`.
- Entry point: `tend = "tend.__main__:main"`.

### 10.2 systemd unit (`deploy/tend.service`)

```ini
[Unit]
Description=Tend personal voice assistant
After=pipewire.service
Wants=pipewire.service
StartLimitBurst=5
StartLimitIntervalSec=300

[Service]
Type=simple
WorkingDirectory=%h/tend
ExecStart=%h/tend/.venv/bin/python -m tend
Restart=on-failure
RestartSec=2s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

### 10.3 Install steps (one-time, in README)

```bash
sudo apt install -y python3-venv python3-dev portaudio19-dev libportaudio2 \
                    pipewire pipewire-pulse ffmpeg

git clone <repo> ~/tend && cd ~/tend
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env && $EDITOR .env

mkdir -p ~/.config/systemd/user
ln -s ~/tend/deploy/tend.service ~/.config/systemd/user/tend.service
systemctl --user enable --now tend

sudo loginctl enable-linger $USER
```

### 10.4 Working against a local pipecat-subagents clone

By default `pyproject.toml` pins `pipecat-ai-subagents == 0.4.0` from PyPI, and a normal `pip install -e .` pulls that. For development — reading internal APIs while building, debugging framework behaviour, prototyping additive changes for upstream PRs — we keep a local clone of `https://github.com/pipecat-ai/pipecat-subagents` at `./pipecat-subagents/` (gitignored, not part of the `tend` repo's git history). The clone retains its own `.git` pointing at upstream; contributors who want to PR back add their fork as a remote inside the clone.

To override the PyPI install with the local clone:

```bash
pip install -e ./pipecat-subagents
```

To switch back to the pinned PyPI version:

```bash
pip install --force-reinstall "pipecat-ai-subagents==0.4.0"
```

PR workflow (typical):

```bash
cd pipecat-subagents
git checkout -b my-fix
# edit
git push myfork my-fix
gh pr create --repo pipecat-ai/pipecat-subagents
```

Constraints:

- Any local modifications to the clone must remain **additive and non-breaking** to the framework's existing API. If a change requires breaking framework users, raise it as an upstream issue and discuss the design before coding.
- The `tend` codebase always works against the pinned PyPI version. Don't introduce code paths that rely on un-merged changes in the local clone — that would silently break for anyone running `pip install -e .` cleanly.
- If we discover a real need for an additive framework change, the workflow is: prototype in the clone → confirm `tend` benefits → open upstream PR → wait for merge or hold the change behind a thin shim until the next PyPI release.

## 11. Framework dependencies and assumptions

This design relies on specific behaviours of pipecat-subagents 0.4.0. Calling them out so future upgrades can be evaluated against them.

1. **Bus-message hooks fire regardless of `BaseAgent.active`.** `on_task_update`, `on_task_response`, `on_task_error`, etc. are bus-message handlers, not frame processors. They run on bus message receipt regardless of activation state. **Verified** in `pipecat-subagents/src/pipecat_subagents/agents/base_agent.py`. Our worker → Brain context-update path depends on this.
2. **`_BusEdgeProcessor` (the framework-injected gate at a `bridged=` agent's pipeline boundary) DROPS incoming frames when the agent is deactivated.** Verified at `base_agent.py:160` (`if not self._agent.active: return`). This is why we route context updates via `on_task_update` rather than via a `LLMMessagesAppendFrame` published to the bridge.
3. **`BusBridgeProcessor` (the public, in-pipeline processor placed in Hub's pipeline) does NOT gate on active state.** Verified at `bus/bridge_processor.py`. This is why workers' `TTSSpeakFrame`s reach Hub's TTS even while Brain is deactivated.
4. **`asyncio.create_task` for worker handlers is always-on.** Subagents documents that "Task handlers always run in their own asyncio task so the bus message loop is never blocked." Workers continue running across activation transitions.

If a future subagents version changes (1) or (3), this design needs to adapt. A failing `test_worker_autonomous_completion.py` integration test would be the early warning.

## 12. Documentation cleanup (work this v1 PR does)

| Action | Path |
|---|---|
| Delete | `docs/superpowers/specs/2026-04-29-hasat-satellite-design.md` |
| Add | `docs/superpowers/specs/2026-05-05-tend-smart-speaker-design.md` (this) |
| Rewrite | `README.md` — tend identity, smart-speaker framing, slow/fast-loop overview, install steps, troubleshooting |
| Rewrite | `CLAUDE.md` — new architecture (subagents, Hub/Brain/Workers, deactivation gating, day-session model). Drop "no Flows / no bus / single-turn / stateless" non-choices |
| New | `soul.md` — committed starter persona |
| New | `tend.toml` — committed defaults |
| Rewrite | `.env.example` — secrets only |
| Update | `.gitignore` — add `pipecat-subagents/` (the dev-mode clone, see §10.4), `.superpowers/` (brainstorm scratch dir), and any TOML / soul files that shouldn't be checked in (none currently, but listed for completeness) |
| Delete | `src/hasat/` |
| New | `src/tend/` with the new module layout |
| New | `deploy/tend.service` |
| Update | `pyproject.toml` — rename, deps, entry point |

## 13. Out of scope (explicit non-goals for v1)

These will not be in v1. Each gets its own brainstorm → spec → build cycle.

- **Compaction.** No mid-day or end-of-day summarization of `LLMContext`. Day-session context grows naturally and gets dropped at the daily reset. Will revisit when context size becomes a real problem.
- **Persistent memory across days.** No `memory.md`, no fact storage, no end-of-day distillation. Each day starts from `soul.md` alone.
- **Real workers.** No calendar, research, code-edit, planner, water-tracking. Only `ReminderWorker` (the stub).
- **Camera / vision.** Audio-only. Camera comes in a later spec; nothing in v1's design forecloses it.
- **Multi-user / identity.** Single user, single Pi. No auth, no profiles.
- **Dev "always-awake" bypass.** No `TEND_DEV_FORCE_BRAIN_ACTIVE`. The architecture is honest in v1.
- **Distributed deployment.** No Redis bus, no remote agents, no multi-machine. In-process `AsyncQueueBus` only.
- **Worker durability across process restart.** In-flight workers die with the process.
- **Mid-stream cloud-service failover.** TTS or STT failing mid-utterance loses that turn. Falling back for the *next* turn is in scope.
- **Wake-word retraining or custom phrases.** Stock `hey_jarvis` openWakeWord model only.
- **Brain tools for managing its own memory.** No `save_memory`, no `recall`. Brain has no self-introspection of its session state.

## 14. Future (v1.5 and beyond, in rough priority order)

- Compaction (LLM-driven mid-day summarization).
- Persistent memory layer across days.
- Calendar worker.
- Research worker (web search + synthesis).
- Code-edit worker (Claude Agent SDK pattern, like the subagents code-assistant example).
- Planner worker.
- Camera / vision agent (multi-modal posture, water-bottle detection).
- Worker durability across process restart.
- Distributed deployment (slow-loop workers on a more powerful machine, satellite stays on the Pi).
- Custom openWakeWord model training for personalised wake/sleep phrases.
