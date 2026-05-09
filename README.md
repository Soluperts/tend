# tend

Personal AI assistant for desk workers. Lives on a Raspberry Pi in your workroom; conversational fast loop on the front, slow-loop worker dispatch behind it. Audio in → wake gate → STT → brain → TTS → audio out, built on [Pipecat](https://github.com/pipecat-ai/pipecat) and [pipecat-subagents](https://github.com/pipecat-ai/pipecat-subagents).

The brain is multi-turn within a day-session; conversation context persists across wake/sleep cycles and resets at a configurable wall-clock time (default 04:00 local). Workers (v1 ships one stub: timer/reminder) run independently of the brain — they keep going while the brain is asleep and announce themselves through the speaker when they finish.

## What you need

- Raspberry Pi 4 or 5 (64-bit Raspberry Pi OS).
- One USB microphone.
- One speaker (USB or Bluetooth).
- Python 3.11+.
- Internet for cloud STT/TTS/LLM (optional — local fallbacks work offline once models are downloaded).

The satellite assumes **exactly one input and one output device** are connected. Audio I/O uses the system default through PipeWire.

## System packages

```bash
sudo apt update
sudo apt install -y \
  python3-venv python3-dev \
  portaudio19-dev libportaudio2 \
  pipewire pipewire-pulse \
  ffmpeg
```

Plug in the USB mic and pair/connect the speaker before running.

## Install

```bash
git clone <this-repo> ~/tend
cd ~/tend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

The first run downloads:
- `faster-whisper` `tiny.en` model (~75 MB, cached under `~/.cache/huggingface`)
- Silero VAD model (~2 MB)
- openWakeWord `hey_jarvis` model (~few MB, cached under `~/.cache/openwakeword`)

The Piper voice (`en_US-ryan-high.onnx`) is downloaded by Pipecat on first use.

## Configure

```bash
cp .env.example .env
$EDITOR .env       # add your API keys
$EDITOR soul.md    # optionally tune the assistant's persona
```

`tend.toml` (committed in this repo) holds the defaults — model names, timeouts, paths, the wake/sleep phrases, etc. Edit it to override defaults; environment variables (`TEND_*`) override `tend.toml`; `.env` holds vendor API keys only.

| Variable | Where | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `.env` | Brain LLM. Without it, brain is degraded. |
| `DEEPGRAM_API_KEY` | `.env` | Streaming STT. Without it, local Whisper is used. |
| `ELEVENLABS_API_KEY` | `.env` | Cloud TTS. Without it, local Piper is used. |
| `llm_model` | `tend.toml` | Anthropic model id. Default `claude-haiku-4-5`. |
| `sleep_phrase` | `tend.toml` | Phrase that ends the conversation. Fuzzy-matched. |
| `awake_timeout_s` | `tend.toml` | Silence (seconds) before auto-sleep. Default 30. |
| `daily_reset_time` | `tend.toml` | Wall-clock daily session reset (`HH:MM`). Default 04:00. |
| `soul_path` | `tend.toml` | Path to the persona/context file. Default `soul.md`. |
| `TEND_SKILLS_ROOT` | env var | Override the directory the CLI and `list_skills` tool read from. Defaults to `~/.tend/skills`. The GeneralWorker uses `[workers.general].skills_dir` instead. |
| `TEND_SKILLS_QUARANTINE_ROOT` | env var | Override the directory `tend skills quarantined` reads from. Defaults to `~/.tend/skills-quarantined`. |

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

## Run (development)

```bash
python -m tend
```

Logs stream to stderr and to `/tmp/tend.log`. Native crash traces (PortAudio etc.) go to `/tmp/tend.faults.log`.

## Run (production — systemd user service)

```bash
mkdir -p ~/.config/systemd/user
ln -s ~/tend/deploy/tend.service ~/.config/systemd/user/tend.service
systemctl --user enable --now tend
sudo loginctl enable-linger $USER       # one-time, lets the user service start at boot
```

Logs: `journalctl --user -u tend`. The service auto-restarts on failure; after 5 failures within 5 minutes systemd marks it as `failed` and stops trying — investigate with `journalctl --user -u tend`.

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

- **No audio in/out:** check `pactl list short sources` and `pactl list short sinks`. PipeWire must see your mic and speaker as the defaults.
- **Bluetooth speaker latency:** expect 150–250 ms. Not fixable without switching to wired audio.
- **Cloud preflight failures:** the startup log prints the exact HTTP status and reason; fix the key or top up credits and restart.
- **First run is slow:** model downloads. Subsequent runs start in seconds.
- **systemd marks the service `failed`:** check `journalctl --user -u tend` for the reason. The 5-failures-in-5-minutes guard prevents thrashing-restart loops.

## Development

To work against a local clone of pipecat-subagents (for reading internals or prepping upstream PRs):

```bash
git clone https://github.com/pipecat-ai/pipecat-subagents ./pipecat-subagents
pip install -e ./pipecat-subagents
```

The clone is gitignored. Switch back with `pip install --force-reinstall pipecat-ai-subagents==0.4.0`. See spec §10.4 for the full workflow.
