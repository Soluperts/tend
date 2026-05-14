# tend

Personal AI assistant for desk workers. Lives on a Raspberry Pi in your workroom; conversational fast loop on the front, slow-loop worker dispatch behind it. Audio in → wake gate → STT → brain → TTS → audio out, built on [Pipecat](https://github.com/pipecat-ai/pipecat) and [pipecat-subagents](https://github.com/pipecat-ai/pipecat-subagents).

The brain is multi-turn within a day-session; conversation context persists across wake/sleep cycles and resets at a configurable wall-clock time (default 04:00 local). Workers (v1 ships one stub: timer/reminder) run independently of the brain — they keep going while the brain is asleep and announce themselves through the speaker when they finish.

## What you need

- Raspberry Pi 4 or 5 (64-bit Raspberry Pi OS), **or** a Mac running macOS 14+ on Apple Silicon.
- One USB microphone (Linux/Pi) or built-in / USB mic (macOS).
- One speaker (USB or Bluetooth).
- Python 3.11+.
- Internet for cloud STT/TTS/LLM (optional — local fallbacks work offline once models are downloaded).

The assistant assumes **exactly one input and one output device** are connected. Audio I/O uses the system default through PipeWire (Linux) or CoreAudio (macOS).

## Install

### Linux (Raspberry Pi / Ubuntu / Debian)

Prereqs:

    sudo apt install portaudio19-dev python3-pip pipx
    pipx ensurepath

Install:

    pipx install tend

Bootstrap and verify:

    tend setup
    tend doctor

Install as a systemd user service and start at login:

    tend service install
    tend service start
    tend service status

### macOS (Apple Silicon, macOS 14+)

Prereqs:

    brew install python portaudio

Install:

    pipx install tend

Optional state-of-the-art AEC (otherwise tend uses pyaec/Speex by default):

    xcode-select --install
    brew install webrtc-audio-processing
    pipx inject tend webrtc-audio-processing

Bootstrap and verify:

    tend setup           # grants microphone access when prompted
    tend doctor

Install as a LaunchAgent and start:

    tend service install
    tend service start
    tend service status

#### Optional: install a Premium voice

The default Apple TTS voice is dated. For markedly better quality
(~300–600 MB, free), download a Premium voice via VoiceOver Utility,
which exposes the full voice catalog on macOS 14+:

  1.  Open VoiceOver Utility:  ⌃ ⌥  Fn  F8
        (or `open -a "VoiceOver Utility"` from the terminal)
  2.  Select "Speech" in the sidebar → Voices → +
  3.  Pick a language → pick a voice marked Premium or Enhanced
  4.  Click Download

After the download finishes:

    tend voices list                   # see what tend can use
    tend voices set <identifier>       # pick it
    tend voices test                   # confirm

Note: Apple's Siri-tier voices are not exposed to third-party apps
via AVSpeechSynthesizer. Premium voices are the highest quality tier
tend can reach.

## Configure

tend's user state lives at `$TEND_HOME` (default `~/.tend/`). Run `tend setup` to bootstrap it interactively (prompts for API keys, generates the webhook token, and seeds the default persona and skills). For manual bootstrapping:

```bash
mkdir -p ~/.tend
echo "ANTHROPIC_API_KEY=sk-ant-..." > ~/.tend/.env
echo "0.1.0" > ~/.tend/.tend-version
```

Optional overrides go in `~/.tend/tend.toml`:

```toml
llm_model = "claude-opus-4-7"
awake_timeout_s = 120
elevenlabs_speed = 1.15

[announcer]
default_cooldown_s = 600
```

| Variable | Where | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `$TEND_HOME/.env` or env | Brain LLM. Without it, brain is degraded. |
| `DEEPGRAM_API_KEY` | `$TEND_HOME/.env` or env | Streaming STT. Without it, local Whisper is used. |
| `ELEVENLABS_API_KEY` | `$TEND_HOME/.env` or env | Cloud TTS. Without it, local Piper is used. |
| `llm_model` | `$TEND_HOME/tend.toml` | Anthropic model id. Default `claude-haiku-4-5`. |
| `sleep_phrase` | `$TEND_HOME/tend.toml` | Phrase that ends the conversation. Fuzzy-matched. |
| `awake_timeout_s` | `$TEND_HOME/tend.toml` | Silence (seconds) before auto-sleep. Default 30. |
| `daily_reset_time` | `$TEND_HOME/tend.toml` | Wall-clock daily session reset (`HH:MM`). Default 04:00. |

### Running from a clone (development)

Set `TEND_HOME` to a project-local workspace so dev work doesn't touch your real one:

```bash
export TEND_HOME=$PWD/.tend-dev
mkdir -p $TEND_HOME && echo "0.1.0" > $TEND_HOME/.tend-version
python -m tend        # Linux/Pi
python -m tend        # macOS — same command; CoreAudio + AVSpeechSynthesizer picked automatically
```

`.tend-dev/` is gitignored.

The GeneralWorker is configured under `[workers.general]` in `tend.toml`:

```toml
[workers.general]
model = "claude-opus-4-7"
setting_sources = "user,project,local"
allowed_tools = ["Read", "Edit", "Write", "Bash", "Grep", "Glob"]
workspace_dir = "~/.tend/workspace"   # persistent build dir, shared across tasks
skills_dir = "~/.tend/skills"         # markdown procedures the worker can run/author
```

If a configured cloud service preflight fails (bad key, no credit, network), tend logs the reason and falls back to its local equivalent.

## Running

Development:

```bash
python -m tend
```

Logs stream to stderr and to the platform log dir. Native crash traces (PortAudio etc.) go to the platform log dir as `tend.faults.log`.

Production (after `tend service install`):

```bash
# Linux
systemctl --user start tend
# macOS
launchctl kickstart gui/$UID/com.tend.daemon
```

Linux: logs via `journalctl --user -u tend`. The service auto-restarts on failure; after 5 failures within 5 minutes systemd marks it as `failed` — investigate with `journalctl --user -u tend`.

macOS: logs at `~/Library/Logs/tend/tend.log`.

## Use

Speak the wake phrase ("hey jarvis" by default) — tend acknowledges with "Yes?". Have a casual conversation. Say "remind me in five minutes about water" to dispatch a reminder. Say the sleep phrase ("goodbye jarvis") or stay silent for 30 seconds to send the brain back to sleep. The reminder still fires whether you're awake or asleep — when it does, the speaker announces it and the brain's context records it so you can ask follow-up questions on next wake.

Ask for something the assistant can't answer directly ("plan my meals for the week", "summarize last week's watch data") and the brain dispatches it via `do_task` to the GeneralWorker, which runs `claude` in the background workspace and announces a result when done.

Say "start fresh" while awake to flush the day-session context (e.g. when topics have shifted dramatically).

### Skills

The worker's behavior comes from markdown procedures under
`~/.tend/skills/<name>/SKILL.md`. List them with `tend skills list`,
inspect with `tend skills show <name>`. New skills are authored mid-task
by the worker when an incoming request doesn't match an existing one;
`tend scan-skill <name>` runs the safety scanner before/after.

## Layout

```
src/tend/
  audio/        Hub (audio agent), gates, latency loggers
  workers/      Worker agents
    general.py        GeneralWorker (skill-driven; replaces former CodingWorker)
    reminder         (none — Brain.remind_in now wraps the scheduler)
    claude_cli.py     ClaudeCliWorker (base — runs `claude` CLI subprocess)
  skills.py     Skill catalog + safety scanner (~/.tend/skills/)
  brain.py      Brain (LLMContextAgent + tools)
  session.py    SessionManager (soul.md + daily reset)
  services.py   STT / TTS / brain LLM factories with preflight
  scheduler.py  Wall-clock dispatch loop (legacy + event-mode jobs)
  webhook.py    aiohttp /say + /event receiver (loopback-only)
  dispatch.py   Shared event fan-out helper used by webhook + scheduler
  google_watcher.py  Schedule-watcher pure logic + run_tick orchestrator
  config.py     pydantic-settings (TOML + env)
  main.py       AgentRunner setup, runs forever
deploy/
  tend.service  systemd user unit
docs/superpowers/specs/2026-05-05-tend-smart-speaker-design.md
docs/superpowers/plans/2026-05-05-tend-v1.md
```

## Troubleshooting

- **No audio in/out (Linux):** check `pactl list short sources` and `pactl list short sinks`. PipeWire must see your mic and speaker as the defaults.
- **No audio in/out (macOS):** run `tend doctor`. If the mic permission check fails, open Settings → Privacy & Security → Microphone and enable Terminal (or iTerm, or whatever launched `tend`). Permission persists once granted.
- **Bluetooth speaker latency:** expect 150–250 ms. Not fixable without switching to wired audio.
- **Echo on macOS:** if you hear tend's own speech fed back, the AEC filter is not active. Run `tend doctor` — the AEC check will report which engine is in use. Install the `webrtc-audio-processing` extra for improved echo suppression.
- **Cloud preflight failures:** the startup log prints the exact HTTP status and reason; fix the key or top up credits and restart.
- **First run is slow:** model downloads. Subsequent runs start in seconds.
- **systemd marks the service `failed` (Linux):** check `journalctl --user -u tend` for the reason. The 5-failures-in-5-minutes guard prevents thrashing-restart loops.
- **LaunchAgent not starting (macOS):** check `launchctl print gui/$UID/com.tend.daemon` for the exit code. Logs at `~/Library/Logs/tend/tend.log`.

## Development

To work against a local clone of pipecat-subagents (for reading internals or prepping upstream PRs):

```bash
git clone https://github.com/pipecat-ai/pipecat-subagents ./pipecat-subagents
pip install -e ./pipecat-subagents
```

The clone is gitignored. Switch back with `pip install --force-reinstall pipecat-ai-subagents==0.4.0`. See spec §10.4 for the full workflow.
