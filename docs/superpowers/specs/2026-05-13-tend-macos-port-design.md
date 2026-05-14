# macOS port — Design

Status: draft
Date: 2026-05-13
Sub-project: ROADMAP #2 (v0.1 critical path, after first-run UX)

## Goal

A stranger with a recent MacBook (Apple Silicon, macOS 14+) can run
`brew install python portaudio && pipx install tend && tend setup`
and reach a working voice assistant in the same ~10 minutes the Pi
flow does. tend keeps barge-in working, runs as a LaunchAgent, and
ships a local TTS path so first-run doesn't depend on an ElevenLabs
API key.

## Scope

In scope:

- `audio/channels.py` mono branch + `audio_in_filter` slot integration.
- Software AEC at pipecat's input-filter slot with a pluggable engine
  (`webrtc-aec3` or `speex`/pyaec), platform-aware defaults.
- New `OutputAudioCapture` processor + `ReferenceBuffer` so the AEC
  filter sees the speaker reference signal.
- New `AVSpeechSynthesizerTTSService` for local TTS on Mac (~80 lines).
- `[tts] provider` field in `tend.toml` with `auto` resolution; wizard
  step on Mac picks AVSpeech (default) or ElevenLabs.
- New `tend voices {list,set,test}` subcommand for voice management.
- `launchd` plist template + mac branches in every `service.py` verb.
- TCC microphone-permission probe in `tend setup` and `tend doctor`.
- Premium-voice install instructions surfaced from `tend setup` and
  `tend doctor` (VoiceOver Utility path).
- `pyproject.toml` conditional dependencies + minimum-version pin.
- README install blocks for both Linux and macOS.
- Conventions and roadmap doc updates.

Out of scope:

- Replacing pipecat's `LocalAudioTransport`. PyAudio + PortAudio stays.
- Apple Voice Processing IO unit (AUHAL VPIO). Listed as a v0.2
  candidate; would require a custom Mac-only audio transport.
- Sherpa-onnx as a third TTS path. Documented as a post-v1 candidate
  for cross-platform persona consistency.
- App bundling, code signing, notarization (roadmap non-goal).
- Homebrew tap / brew formula (roadmap item #6).
- Lid-closed wake assertions (`IOPMAssertionCreateWithName`).
- Bluetooth-routing-change handling — pipecat doesn't and we won't.
- Full README rewrite (roadmap item #6); this spec only adds Linux +
  macOS install blocks.

## Reference target

- **Hardware:** Apple Silicon MacBook with built-in microphone and
  built-in speaker. ~10 cm separation, no hardware AEC. Software AEC
  earns its keep against this configuration.
- **OS:** macOS 14 (Sonoma) minimum. Tested on macOS 26 (Tahoe).
- **Architecture:** arm64 primary. Intel `x86_64` works in theory
  (all dependency wheels exist) but is not in the v1 test matrix.

## Architecture

### Audio path branching

Today `Hub.build_pipeline` hardcodes XVF3800 assumptions:
`audio_in_channels=2` plus an unconditional `StereoToMonoLeft`
processor. That breaks on a built-in mac mic (mono device).

New helper in `src/tend/audio/channels.py`:

```python
@dataclass(frozen=True)
class AudioPath:
    in_channels: int
    pre_vad_processors: tuple[FrameProcessor, ...]
    aec_filter: BaseAudioFilter | None

def select_audio_path(settings: Settings) -> AudioPath: ...
```

Selection rules:

| Platform | `[audio] mic_channels` override | Result |
|---|---|---|
| Linux  | unset             | `(2, (StereoToMonoLeft(),), None)` — existing XVF3800 path |
| macOS  | unset             | `(1, (), make_aec_filter(resolved_engine, settings))` |
| any    | `mic_channels = 1` | `(1, (), make_aec_filter_if_enabled())` |
| any    | `mic_channels = 2` | `(2, (StereoToMonoLeft(),), make_aec_filter_if_enabled())` |

`Hub.build_pipeline` becomes platform-agnostic — it consumes whatever
`select_audio_path` returns. The Mac vs. Linux knowledge lives in
`channels.py`.

New settings (in `tend/config.py`):

```python
[audio]
mic_channels: int | None = None       # platform default
aec_engine: str = "auto"              # auto | webrtc-aec3 | speex | off
```

### AEC: engine selection

`[audio] aec_engine` resolves at startup:

- `"off"` → no filter installed; transport runs as today.
- `"speex"` → pyaec-backed filter.
- `"webrtc-aec3"` → webrtc-audio-processing-backed filter. Errors at
  startup with a clear message if the optional dep isn't importable.
- `"auto"` → platform-aware:
  - Linux → `"off"` (XVF3800 hardware AEC already cleans the signal).
  - macOS → try `import webrtc_audio_processing` → `"webrtc-aec3"`
    if successful, else `"speex"`, else `"off"` with a warning.

Resolution happens once in `tend/audio/aec.py::resolve_aec_engine` and
the resolved name is logged + surfaced by `tend doctor`.

### AEC: plumbing

Pipecat ships `BaseAudioFilter` and a `audio_in_filter` slot on
`LocalAudioTransportParams`. The filter runs after `transport.input()`
and before VAD, on every captured chunk. Its signature is
`filter(audio: bytes) -> bytes` — same length in/out, no access to
any other signal.

To run AEC we also need the **speaker reference signal** — the audio
that was just played out. Pipecat's filter contract doesn't provide
it, so we install a tap.

Two new components in `src/tend/audio/`:

1. `aec.py::make_aec_filter(engine, sample_rate, reference) -> BaseAudioFilter | None`
   - `reference` is a `ReferenceBuffer` (small ring buffer, ~1 s at 16 kHz mono ≈ 32 KB).
   - On each `filter(audio)` call, reads `len(audio)` bytes from `reference` (zeros if buffer is empty) and runs `(mic, ref)` through the engine. Returns the cleaned `bytes`.
   - For pyaec/Speex: `Aec(frame_size=N, filter_length=8N, sample_rate=sr)`; filter length covers ~160 ms at 16 kHz, enough for desk-distance acoustic delay plus speaker/mic hardware latency margin.
   - For webrtc-audio-processing: equivalent configuration.

2. `output_tap.py::OutputAudioCapture(reference: ReferenceBuffer)`
   - A `FrameProcessor` that sits immediately before `transport.output()`.
   - On every `OutputAudioRawFrame`, appends `frame.audio` to `reference`.
   - Pure pass-through; introduces no latency.

The `Hub` pipeline (mac-flavoured) becomes:

```
transport.input()
  → *path.pre_vad_processors        # empty on Mac
  → VADProcessor
  → OpenWakeWordGate
  → STT
  → InputLatencyLogger
  → SleepPhraseGate
  → user_aggregator → bridge → bus
  → TTS
  → OutputLatencyLogger
  → OutputAudioCapture(ref)         # NEW (only when aec_filter != None)
  → transport.output()
  → assistant_aggregator
```

The AEC filter is passed to `LocalAudioTransportParams(audio_in_filter=...)`.

### Barge-in preservation

AEC is purely additive on the input path: it cleans the mic stream,
never gates it. VAD, OWW, STT, pipecat interruption frames, and
`SleepPhraseGate` all keep receiving audio while TTS is playing.
Barge-in (the user interrupting tend mid-speech) — which today works
on the Pi via the XVF3800's hardware AEC + pipecat's interruption
machinery — keeps working on Mac via the software AEC equivalent.

This is the spec's hard contract: **muting the mic during TTS is not
an acceptable AEC strategy** because it breaks barge-in. (See the
2026-05-13 conventions update.)

### Local TTS on macOS: AVSpeechSynthesizer

Piper's Python package (`piper-tts`) depends on `piper-phonemize`,
which ships no `macosx_*_arm64` wheels. On Apple Silicon `pip
install piper-tts` falls back to a source build that requires
eSpeak NG and a C++ toolchain, and frequently fails.

For v1 mac the local TTS path is **Apple's `AVSpeechSynthesizer`**
via the focused `pyobjc-framework-AVFoundation` package (~10 MB).

New service: `src/tend/services/avspeech_tts.py::AVSpeechSynthesizerTTSService(TTSService)` (~80 lines).

- Uses `AVSpeechSynthesizer.write(_:toBufferCallback:)` (macOS 13+) to
  receive `AVAudioPCMBuffer` chunks.
- Each chunk is converted to `int16` little-endian bytes at the
  pipecat-requested sample rate (resampling via the buffer's
  `audioFormat` when needed).
- Emits `TTSStartedFrame` → `TTSAudioRawFrame(...)`×N → `TTSStoppedFrame`.
- Voice selected via `AVSpeechSynthesisVoice(identifier:)`; the
  identifier comes from `[tts] avspeech_voice` (string) — empty means
  "system default."

#### TTS provider selection

New setting:

```toml
[tts]
provider = "auto"            # auto | elevenlabs | avspeech | piper
avspeech_voice = ""          # AVSpeechSynthesisVoice identifier, empty = system default
```

Resolution order in `services._make_tts(settings)`:

- `"avspeech"` → AVSpeechSynthesizerTTSService. macOS only; on Linux this raises a clear error.
- `"elevenlabs"` → existing pipecat service.
- `"piper"` → existing pipecat service (HTTP client). Linux only; on macOS this raises a clear error.
- `"auto"`:
  - macOS → `"avspeech"` if PyObjC importable; else `"elevenlabs"` if key present; else error with both remediations.
  - Linux → `"piper"` if a reachable HTTP server is detected (or the user explicitly configured one); else `"elevenlabs"` fallback.

Wizard step on Mac replaces the current `_ask_tts`:

```
How should tend speak?

  ▸ Apple's built-in voice
        Free, fast, runs offline. Default Apple voice is okay;
        you can install a free Premium voice afterwards (~300–
        600 MB) for markedly better quality.

  ▸ ElevenLabs (cloud)
        Best quality, but costs per minute and needs an API key.
```

If Apple: wizard runs the TCC mic-permission probe, then offers to
open VoiceOver Utility for Premium-voice install (see Premium-voice
flow below).

If ElevenLabs: existing key-prompt flow.

### Service install via launchd

New template at `src/tend/_defaults/launchd/com.tend.daemon.plist.tmpl`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>com.tend.daemon</string>
    <key>ProgramArguments</key> <array>
        <string>{{PYTHON}}</string>
        <string>-m</string>
        <string>tend</string>
    </array>
    <key>EnvironmentVariables</key> <dict>
        <key>TEND_HOME</key>    <string>{{TEND_HOME}}</string>
        <key>PATH</key>         <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>WorkingDirectory</key> <string>{{TEND_HOME}}</string>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>        <dict>
        <key>SuccessfulExit</key> <false/>
    </dict>
    <key>ThrottleInterval</key> <integer>10</integer>
    <key>StandardOutPath</key>  <string>/tmp/tend.launchd.out</string>
    <key>StandardErrorPath</key><string>/tmp/tend.launchd.err</string>
    <key>ProcessType</key>      <string>Interactive</string>
</dict>
</plist>
```

Notes:
- Two placeholders (`{{PYTHON}}`, `{{TEND_HOME}}`) — same substitution
  shape as the systemd template.
- `PATH` includes the Homebrew prefixes so `gws`, `claude`, and other
  subprocess targets resolve without the user's shell rc files.
- `KeepAlive.SuccessfulExit = false` → restart on crash, not on
  `tend service stop`.
- `ProcessType = Interactive` → tend runs at user interactive QoS so
  it can capture audio without being throttled into background QoS.
- `WorkingDirectory = {{TEND_HOME}}` defensive (tend uses absolute
  paths via `paths.tend_home()`, but cheap to set).

`service.py` adds a mac branch per verb. Drop `MacOSNotSupported`
entirely.

| Verb | Linux (existing) | macOS (new) |
|---|---|---|
| `install()`     | write unit + `systemctl daemon-reload` | write plist + `launchctl bootstrap gui/$UID <plist>` (fallback: `launchctl load <plist>`) |
| `uninstall()`   | delete unit + `systemctl daemon-reload` | `launchctl bootout gui/$UID/com.tend.daemon` + delete plist |
| `start()`       | `systemctl --user start tend` | `launchctl kickstart gui/$UID/com.tend.daemon` |
| `stop()`        | `systemctl --user stop tend`  | `launchctl kill SIGTERM gui/$UID/com.tend.daemon` |
| `status()`      | `systemctl --user is-active tend` + `status` | parse `launchctl print gui/$UID/com.tend.daemon` — extract `state =` for state token, first ~20 lines of output for `details` |

Implementation helpers (local to `service.py`):

```python
def _label() -> str: return "com.tend.daemon"
def _domain() -> str: return f"gui/{os.getuid()}"

def unit_path() -> Path:
    if _is_macos():
        return Path.home() / "Library" / "LaunchAgents" / f"{_label()}.plist"
    return Path.home() / ".config" / "systemd" / "user" / "tend.service"

def render_unit(*, python, tend_home) -> str:
    name = "launchd/com.tend.daemon.plist.tmpl" if _is_macos() else "systemd/tend.service.tmpl"
    body = files("tend._defaults").joinpath(name).read_text(encoding="utf-8")
    return body.replace("{{PYTHON}}", python).replace("{{TEND_HOME}}", str(tend_home))
```

All five public functions keep their existing signatures and return
shapes. Tests mock `subprocess.run` per branch.

### Microphone permission (TCC)

macOS gates microphone access via TCC (Transparency, Consent, and
Control). The OS prompt only fires when a process actually opens a
mic stream — there is no public API to pre-warm consent. For an
unsigned CLI installed via pipx, TCC grants the binary at
`~/.local/pipx/venvs/tend/bin/python`.

New helper in `tend/checks.py`:

```python
def probe_microphone_access() -> CheckResult:
    """Open a brief PyAudio input stream; return ok/denied/error.

    On first call macOS pops the TCC prompt; the result is cached
    by the OS keyed to the (responsible code, executable) pair.
    """
```

Wired into three places:

- **`tend setup` (macOS only)**: a new step after audio-related
  questions tells the user "macOS will ask for microphone permission
  — click Allow when the dialog appears," then calls
  `probe_microphone_access()` and reports the outcome.
- **`tend doctor`'s existing `check_audio()`**: on macOS, calls the
  same probe after device enumeration. If denied, status is `fail`
  with remediation: `"grant Microphone access for {python_path} in
  System Settings → Privacy & Security → Microphone."`
- **`tend service install` (macOS)**: prints a post-install warning
  that the first launchd-launched start may trigger one additional
  mic prompt because launchd has a different TCC responsibility
  chain from Terminal. Once granted, both grants persist.

Long-term fix for the double-prompt is code signing + notarization
(deferred per roadmap non-goal).

### Premium voice install flow

Apple keeps moving the canonical voice-install pane. The verified
state at spec-write time:

- macOS 14 (Sonoma): Settings → Accessibility → Read & Speak.
- macOS 15 (Sequoia): Premium/Enhanced voice catalog hidden from
  Read & Speak; full catalog only via VoiceOver Utility → Speech.
- macOS 26 (Tahoe): same as Sequoia; the "current voice" UI lives
  in Accessibility → Speech → Live Speech, but installs go through
  VoiceOver Utility.

The wizard text uses the VoiceOver Utility path because it is the
single path that works on Sequoia and Tahoe — Apple's most recent
two major versions:

```
For markedly better quality install a free Premium voice
(~300–600 MB).

  1. Open VoiceOver Utility:  ⌃ ⌥  Fn  F8
  2. Speech → Voices → +
  3. Pick a language → pick a voice marked Premium or Enhanced
  4. Click Download — wait until it finishes
  5. Close VoiceOver Utility — the voice is now system-wide

Open VoiceOver Utility now? [Y/n]
```

`Y` runs `subprocess.run(["open", "-a", "VoiceOver Utility"])`.

After the user confirms the install completed, the wizard calls
`AVSpeechSynthesisVoice.speechVoices()` via PyObjC and offers to
write the highest-quality voice ID into `[tts] avspeech_voice` in
`tend.toml`. If no Premium voice is found, the wizard continues
with the system default and prints the `tend voices set` recipe.

**Important caveat surfaced to the user**: Siri-tier voices are not
exposed to `AVSpeechSynthesizer` and are inaccessible to tend.
Premium quality is the ceiling we can reach.

New CLI subcommand `tend voices`:

- `tend voices list` — prints all `AVSpeechSynthesisVoice.speechVoices()` with identifier / display name / language / quality tier.
- `tend voices set <identifier>` — writes `[tts] avspeech_voice` to `tend.toml`.
- `tend voices test [<identifier>]` — speaks a fixed sample utterance with the given (or configured) voice via the AVSpeechSynthesizer service.

`tend doctor` on macOS reports the resolved voice and its quality
tier. If only `.default`-quality voices are installed, it emits a
`warn` with the same VoiceOver Utility instructions.

### Packaging

Updates to `pyproject.toml`:

```toml
[project]
requires-python = ">=3.11"
dependencies = [
    # existing entries kept...
    'pipecat-ai[whisper,silero,local,deepgram,elevenlabs]>=1.1',
    'pipecat-ai[piper]>=1.1; platform_system == "Linux"',
    'pyobjc-framework-AVFoundation>=10; platform_system == "Darwin"',
    'pyaec>=1.0.1',
]

[project.optional-dependencies]
aec-webrtc = ['webrtc-audio-processing>=0.1.3']
```

Why these choices:

- `pipecat-ai[piper]` becomes Linux-only. macOS users can't natively
  install `piper-phonemize` on Apple Silicon.
- `pyobjc-framework-AVFoundation` is Darwin-only. It pulls in
  `pyobjc-core` and `pyobjc-framework-Cocoa` transitively (~30 MB
  total on Mac arm64); much lighter than the full PyObjC meta-package.
- `pyaec` ships wheels for every relevant platform (Mac arm64/x86,
  Linux arm64/x86, Windows x86/arm64). Default-installed so `"auto"`
  always has a working AEC fallback on Mac.
- `webrtc-audio-processing` source-builds only. Optional extra so
  default `pipx install tend` works on a fresh MacBook without
  Xcode CLT. Users wanting AEC3 run `pipx inject tend
  webrtc-audio-processing` after `brew install
  webrtc-audio-processing`.

`pyproject.toml` package-data already covers `tend._defaults/**/*`,
so the new `launchd/com.tend.daemon.plist.tmpl` ships automatically.

### Versioning & architecture commitment

- **macOS 14 (Sonoma) minimum** — tightest constraint is the
  AVSpeechSynthesizer streaming API (macOS 13+) and onnxruntime's
  `macosx_14_0_arm64` wheel coverage. Pin classifier:
  `"Operating System :: MacOS :: MacOS X"` plus a runtime check
  emitting a clear error on `platform.mac_ver()[0] < "14"`.
- **Apple Silicon (arm64) primary**. Intel `x86_64` Macs install
  successfully (all deps have x86_64 wheels) but are not in v1 CI.
- **macOS 26 (Tahoe)** is the documented "tested on" version.

## Module changes summary

```
src/tend/
  audio/
    aec.py                            NEW  engine factory + ReferenceBuffer + resolve_aec_engine
    output_tap.py                     NEW  OutputAudioCapture FrameProcessor
    channels.py                       MOD  add select_audio_path + AudioPath dataclass
    hub.py                            MOD  consume AudioPath; insert OutputAudioCapture conditionally
  services/
    avspeech_tts.py                   NEW  AVSpeechSynthesizerTTSService
  cli/
    setup.py                          MOD  mac-aware TTS step; mic-permission probe step; voice install nudge
    doctor.py                         MOD  no logic change (uses checks.py)
    voices.py                         NEW  list / set / test subcommand
    __init__.py                       MOD  register voices sub-app on Typer
  _defaults/
    launchd/
      com.tend.daemon.plist.tmpl      NEW
  service.py                          MOD  mac branch in install/uninstall/start/stop/status; drop MacOSNotSupported
  services.py                         MOD  _make_tts dispatches on [tts] provider; auto resolution
  checks.py                           MOD  probe_microphone_access(); check_aec_engine(); check_tts_provider()
  config.py                           MOD  Settings adds [audio] mic_channels, aec_engine, [tts] provider, avspeech_voice
pyproject.toml                        MOD  conditional deps + [aec-webrtc] extra + macOS 14 marker
docs/
  conventions.md                      MOD  install command updates; voice-install pointer; AEC bullet already updated
README.md                             MOD  Linux + macOS install blocks
ROADMAP.md                            MOD  item #2 estimate bumped to ~2.5–3 days; sherpa-onnx added as post-v1 candidate
```

## Test plan

### Automated (pytest, on `ubuntu-latest` and `macos-latest`)

| Test | Pins down |
|---|---|
| `test_select_audio_path_linux_default` | XVF3800 path returned |
| `test_select_audio_path_macos_default` | mono + aec filter |
| `test_select_audio_path_explicit_override` | `mic_channels` honored on both platforms |
| `test_resolve_aec_engine_off` | `off` → `None` |
| `test_resolve_aec_engine_auto_linux` | linux auto → `off` |
| `test_resolve_aec_engine_auto_macos_webrtc_present` | mocked import → `webrtc-aec3` |
| `test_resolve_aec_engine_auto_macos_webrtc_missing` | mocked ImportError → `speex` |
| `test_resolve_aec_engine_explicit_unavailable` | explicit `webrtc-aec3` with no import → clear error |
| `test_aec_filter_speex_round_trip` | mic+ref bytes round-trip same length |
| `test_output_audio_capture_appends_to_reference` | OutputAudioRawFrame → buffer state |
| `test_reference_buffer_most_recent_semantics` | reads return the most-recently-written bytes |
| `test_service_unit_path_macos` | `~/Library/LaunchAgents/com.tend.daemon.plist` |
| `test_service_render_unit_macos` | template substitutes `{{PYTHON}}` + `{{TEND_HOME}}` |
| `test_service_install_macos_subprocess_calls` | mock `subprocess.run` → `launchctl bootstrap gui/$UID <plist>` |
| `test_service_status_macos_parses_launchctl_print` | fixture output → `("active", details)` |
| `test_probe_microphone_access_success` | mock PyAudio → `ok` |
| `test_probe_microphone_access_denied` | mock PyAudio raising `OSError` → `denied` with python path |
| `test_make_tts_avspeech_on_macos` | platform branch returns AVSpeechSynthesizerTTSService |
| `test_make_tts_piper_on_macos_errors` | clear error, not import surprise |
| `test_make_tts_auto_macos_pyobjc_present` | auto → avspeech |
| `test_make_tts_auto_macos_pyobjc_missing_with_key` | auto → elevenlabs |
| `test_make_tts_auto_macos_no_options` | clear error with both remediations |
| `test_avspeech_service_chunks_pcm` | mocked AVSpeechSynthesizer → emits TTSStartedFrame + TTSAudioRawFrame + TTSStoppedFrame in order |
| `test_voices_list_filters_by_quality` | mocked speechVoices → output sorted by quality desc |
| `test_voices_set_writes_toml` | rewrites `[tts] avspeech_voice` without clobbering other settings |
| `test_check_aec_engine_doctor_states` | each combo of (configured, importable) → expected (status, detail) |
| `test_check_tts_provider_doctor_states` | each combo → expected (status, detail) |

macOS-specific tests use `pytest.importorskip("AVFoundation")` or
`pytest.mark.skipif(sys.platform != "darwin")` so the suite still
runs on Linux.

### Manual smoke tests (Apple Silicon MacBook, documented as acceptance bar)

1. **Fresh install path**: blank Mac account → `brew install python
   portaudio` → `pipx install tend` → `tend setup` → wizard step
   triggers TCC prompt → grant → wizard verifies → wizard offers
   VoiceOver Utility for Premium voice → user downloads one → wizard
   sets it → `tend doctor` → all green.
2. **AEC quality, speex**: `aec_engine = "speex"`, awake tend, ask
   a question that returns a long reply, interrupt halfway with the
   sleep phrase. Confirm: tend stops speaking; sleep phrase fires.
   Repeat with a normal interruption ("hey wait, actually..."). STT
   should hear the user, not the tail of tend's reply.
3. **AEC quality, webrtc-aec3**: `pipx inject tend webrtc-audio-processing`
   after `brew install webrtc-audio-processing` + `xcode-select
   --install`. Restart tend. Repeat test 2. Compare residual echo /
   STT transcription accuracy.
4. **TTS quality with default voice vs Premium voice**: speak a few
   sentences via tend with `avspeech_voice = ""` (system default) and
   with a downloaded Premium voice. Document the perceptual delta.
5. **Launchd lifecycle**: `tend service install` → `tend service start`
   → `launchctl list | grep tend` shows running → speak wake word →
   get reply. Crash test: `kill <pid>` → KeepAlive restarts.
   `tend service stop` → process stays stopped.
6. **TCC second-prompt under launchd**: after wizard-granted Terminal
   access, run `tend service install && tend service start` and
   confirm the second prompt fires. Grant. Confirm subsequent starts
   do not re-prompt.
7. **Doctor outputs**: all combos of `[tts] provider`, `[audio]
   aec_engine`, `[audio] mic_channels` produce sensible doctor lines.

## Implementation order

Dependency order — each step is independently testable, the order
minimizes redo work.

1. `audio/channels.py`: add `AudioPath` dataclass + `select_audio_path`. Tests.
2. `audio/aec.py`: `ReferenceBuffer`, `resolve_aec_engine`, `make_aec_filter` (no-op + speex implementations). Tests.
3. `audio/output_tap.py`: `OutputAudioCapture`. Tests.
4. `Hub.build_pipeline`: switch to consume `AudioPath`, conditionally insert `OutputAudioCapture`. Manual smoke on Pi (regression check — same XVF3800 path) + Mac with `aec_engine="off"`.
5. `services/avspeech_tts.py`: `AVSpeechSynthesizerTTSService`. Tests with mocked AVFoundation.
6. `services._make_tts`: add provider dispatch + auto resolution. Tests.
7. `pyproject.toml`: conditional deps + `[aec-webrtc]` extra. Verify `pipx install tend` on a fresh Mac.
8. `service.py`: mac branches. Tests.
9. `_defaults/launchd/com.tend.daemon.plist.tmpl`: ship it.
10. `checks.py`: `probe_microphone_access`, `check_aec_engine`, `check_tts_provider`. Tests.
11. `cli/setup.py`: mac TTS step; mic-permission probe step; Premium-voice install nudge.
12. `cli/voices.py`: `list`, `set`, `test`.
13. WebRTC AEC3 implementation in `aec.py`. Gated on the speex path being shown adequate-or-inadequate by manual ear-test in step 4's smoke pass — i.e., we do this work only once we know whether it's necessary.
14. Documentation updates: README, ROADMAP, conventions.
15. Manual smoke tests (full list above).

Steps 1–4 keep tend working on Pi at all times. Step 4 introduces no
behavior change on Linux because `select_audio_path` returns the
existing tuple. Step 13 (WebRTC) is gated on the speex path being
proven adequate or inadequate by manual ear-test.

## Known limitations (documented, not fixed in v1)

- macOS shows the orange microphone indicator in the menu bar
  whenever tend runs. The wake-word gate keeps the stream open even
  when Brain is asleep (drops frames) so audio capture never stalls.
  Closing the stream during sleep would add wake-time latency.
- tend sleeps with the MacBook lid. No `IOPMAssertionCreateWithName`
  in v1.
- PortAudio uses the macOS system default audio device. Users with
  multiple inputs/outputs choose via System Settings → Sound.
- Bluetooth speakers add 150–250 ms TTS latency; AEC filter length
  is sized to handle this, but the user experience is degraded vs.
  the built-in speaker. Pipecat does not handle BT route changes
  gracefully — tend doesn't fix this.
- Premium voices are an opt-in download (~300–600 MB). The default
  Apple voice is dated; the install nudge happens in the wizard.
- Siri-quality voices are not accessible to AVSpeechSynthesizer.
- Apple's TCC may double-prompt on first launchd start because the
  responsibility chain differs from Terminal. Documented in the
  service install output.

## Roadmap & docs deltas

- `ROADMAP.md` item #2 estimate: bump from ~1–2 days to ~2.5–3 days
  (AVSpeechSynthesizer service + voices subcommand + Premium-voice
  flow add roughly half a day beyond the original estimate).
- `ROADMAP.md` post-v1 candidates: add **"Cross-platform local TTS
  via sherpa-onnx"** — same VITS voice on Pi and Mac for users who
  want persona consistency across devices. Approx 1 day. Optional.
- `docs/conventions.md` install bullet: add `[aec-webrtc]` extra and
  the `brew install portaudio` Mac prerequisite.
- `docs/conventions.md` macOS specifics: AEC bullet already updated
  to software AEC; verify the "channels.py selects mono-mic on
  macOS" bullet still accurately describes
  `select_audio_path`.
- `README.md`: new Linux and macOS install sections.
- `CLAUDE.md`: no architecture changes; the hardware-assumptions
  block already mentions BT and mic-arrays. Audio path branching
  description in the "Architecture (current)" section should note
  that `Hub.build_pipeline` consumes `select_audio_path`.

## When in doubt

- Read the brainstorm transcript that produced this spec (commit
  surrounding this file) for the alternatives that were rejected.
- The hardest constraint is **barge-in preservation**. If anything
  in the implementation creates pressure to mute the mic during TTS,
  stop — the design forbids it.
- For TTS provider questions on Mac that aren't covered here, the
  rule is: AVSpeechSynthesizer or ElevenLabs in v1. Sherpa-onnx is
  v0.2. Piper is Linux-only.
