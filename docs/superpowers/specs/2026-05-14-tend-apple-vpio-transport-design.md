# Apple VPIO Audio Transport — Design

## Problem

On macOS, the speex-based software AEC (`pyaec`) shipped in the macOS port (commit `f708942`) yields 12–26 dB suppression on the built-in MacBook mic+speaker. That's enough for the local Silero VAD to ignore the residual, but not enough for Deepgram's cloud STT to stop transcribing fragments of the bot's own voice ("nice to meet", "I'm Jan", …) into the LLM context. Those fragments confuse subsequent turns and turn the assistant into an unreliable partner.

There is no software-AEC ceiling above ~25 dB realistically achievable on consumer Mac hardware without OS-level integration with the audio HAL. The clean fix is to stop running AEC ourselves and let macOS do it.

## Goal

Replace pipecat's `LocalAudioTransport` on macOS with `AVAudioTransport` — an `AVAudioEngine`-based transport that runs Apple's VoiceProcessingIO audio unit (AEC + NS + AGC, the same path FaceTime / Voice Memos use). When VPIO is the active engine, no software AEC filter runs at all; the OS handles it upstream of pipecat seeing any audio.

Naming follows pipecat's convention (platform identifier + `AudioTransport`, mirroring `LocalAudioTransport` in `pipecat/transports/local/audio.py`). Voice processing is the default-on feature, not the transport's identity.

The speex path remains in the codebase as an explicit opt-out (`aec_engine="speex"`) for users who want to override. Linux/Pi is untouched.

## Approach (vs. alternatives)

**Chosen:** `AVAudioEngine` + `setVoiceProcessingEnabled_error_(True)` via PyObjC. High-level Foundation API; the input node and output node are both backed by the underlying VoiceProcessingIO audio unit. Mirrors what FaceTime, Voice Memos, and the WWDC23 "What's new in voice processing" session promote.

**Rejected — raw `AudioUnit` of subtype `kAudioUnitSubType_VoiceProcessingIO`.** Lower-level, more control over format negotiation, but PyObjC's AudioToolbox bindings are thinner and the manual format-negotiation surface is large. No real benefit over AVAudioEngine for our use case.

**Rejected — manual-rendering mode + direct AudioUnit (the 2018 Twilio approach).** Significantly more complex; necessary only for VoIP apps that mix multiple participants. We have one TTS stream out and one mic stream in.

**Rejected — accept the speex ceiling and move on.** Deepgram transcribing the bot's own voice into context is a real user-experience bug, not a polish issue. The assistant cannot be reliable while this is unfixed.

## Architecture

Single new module that mirrors pipecat's `local/audio.py` file shape, so it's pasteable upstream as `pipecat.transports.local.av_audio` when we file the PR later.

```
src/tend/audio/av_audio.py
  AVAudioTransportParams(TransportParams)
    - no extra fields for v1
    - reserved for input_device_uid / output_device_uid additions later
  AVAudioTransport(BaseTransport)
    - owns one AVAudioEngine
    - input() lazy-returns AVAudioInputTransport
    - output() lazy-returns AVAudioOutputTransport
  AVAudioInputTransport(BaseInputTransport)
    - installs tap on engine.inputNode at the bus's native format
    - per-buffer AVAudioConverter → target sample_rate/int16/mono
    - tap callback marshals to asyncio via run_coroutine_threadsafe
  AVAudioOutputTransport(BaseOutputTransport)
    - owns one AVAudioPlayerNode attached to engine.mainMixerNode
    - write_audio_frame: schedule a single AVAudioPCMBuffer with
      completionCallbackType=.dataPlayedBack; await its completion
      via asyncio.Event set in the callback
```

### Engine lifecycle

```
engine = AVAudioEngine()
input_node = engine.inputNode      # forces VPIO unit construction
output_node = engine.outputNode

input_node.setVoiceProcessingEnabled_error_(True, None)  # MUST be before start
                                                          # enables AEC+NS+AGC
                                                          # on BOTH I/O buses

# Output path: player → outputNode DIRECT (not via mainMixer).
# Reason: under VPIO, mainMixer's default 44.1 kHz format conflicts with
# outputNode's 48 kHz, and engine.start() fails with err=-10875
# (kAudioUnitErr_FailedInitialization). Validated empirically in the PoC.
player = AVAudioPlayerNode()
engine.attachNode_(player)
out_format = output_node.inputFormatForBus_(0)   # 2ch 48 kHz Float32 deinterleaved
engine.connect_to_format_(player, output_node, out_format)

# Input tap at native (9-channel) format; conversion happens in the callback.
input_node.installTapOnBus_bufferSize_format_block_(
    0, 1024, input_node.outputFormatForBus_(0), tap_callback,
)
engine.prepare()
engine.startAndReturnError_(None)
```

`setVoiceProcessingEnabled` cannot toggle while the engine is running — the engine must be stopped first. That's fine for our lifecycle (start once, stop once).

### Format handling

**Input side (mic):** When VPIO is enabled, the input node's `outputFormat(forBus: 0)` exposes a 9-channel Float32 deinterleaved aggregate device at 48 kHz: channel 0 is the post-VPIO mic, channels 1-8 are the speaker-reference channels VPIO uses internally. The tap is installed at this native format.

Conversion path in the tap callback:

1. Tap receives `AVAudioPCMBuffer` at 9ch Float32 48 kHz.
2. `AVAudioConverter` (constructed once at `start()`, reused per buffer) converts to 1ch int16 16 kHz. **The converter's default `channelMap` is `[-1]` (drop output, produces zero-valued buffers); we must explicitly set `channelMap = [0]` to take channel 0 from the input.** Validated empirically in the PoC — without this, captured signal is silent.
3. Apply post-VPIO gain. **VPIO aggressively noise-gates at desk distance** (the design point for tend), attenuating user speech ~12 dB compared to the raw mic. Multiply int16 samples by ~4× (linear, =+12 dB) with clipping. Residual echo (currently suppressed to ~-90 dB by VPIO per the PoC) stays at -78 dB after boost — still ~40 dB below Deepgram's STT floor.
4. Wrap the result in an `InputAudioRawFrame(audio=bytes, sample_rate=16000, num_channels=1)` and hand to asyncio via `asyncio.run_coroutine_threadsafe`.

**Output side (speaker):** Output node's input format under VPIO is 2ch 48 kHz Float32 deinterleaved (the OS-negotiated format for the speaker hardware). We cannot wrap our int16 16 kHz mono TTS output in that format directly. Two-stage conversion at `write_audio_frame` time:

1. Build an int16 source buffer at the TTS-produced rate (16 kHz mono).
2. Use an output-side `AVAudioConverter` (also built once at `start()`) to convert 16 kHz int16 mono → 48 kHz Float32 deinterleaved at the output node's required channel count (typically 2; copy mono to both channels).
3. Schedule the resulting Float32 buffer on the player with `.dataPlayedBack` completion type.

This adds one extra conversion compared to the original spec, but it's required by the VPIO output constraints.

### Threading

- Input tap callback runs on a real-time CoreAudio I/O thread. Cannot do `await`. Bridges to asyncio via `asyncio.run_coroutine_threadsafe(push_audio_frame(frame), loop)`. Same pattern PyAudio's `_audio_in_callback` uses today.
- Output `scheduleBuffer_completionCallbackType_completionHandler_` completion callback also fires on a CoreAudio thread. Bridges via `loop.call_soon_threadsafe(event.set)` where `event` is an `asyncio.Event` that `write_audio_frame` is awaiting.
- The engine itself manages its own threads; nothing for us to do beyond starting / stopping it.

### Output backpressure

`write_audio_frame` schedules one buffer at a time and awaits its `.dataPlayedBack` completion before returning. This makes writes paced to actual playback wall-clock, which is the semantics pipecat's `TTSStopFrame` interruption assumes. Concretely:

```python
async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
    buf = _pcm_buffer_from_bytes(frame.audio, format)
    done = asyncio.Event()
    def _completion():
        self._loop.call_soon_threadsafe(done.set)
    self._player.scheduleBuffer_completionCallbackType_completionHandler_(
        buf, AVAudioPlayerNodeCompletionDataPlayedBack, _completion,
    )
    if not self._player.isPlaying():
        self._player.play()
    await done.wait()
    return True
```

Buffer size is one TTS-produced frame at a time (~20 ms typical), so the in-flight queue stays at one buffer; barge-in interruption stops the player and the next `await done.wait()` resolves when the cancelled buffer's playback ends.

## Wiring into the existing tend pipeline

`AudioPath` gains a `transport_factory` field. `select_audio_path` picks `AVAudioTransport` on macOS when `aec_engine` resolves to `"vpio"`.

```python
@dataclass(frozen=True)
class AudioPath:
    in_channels: int
    pre_vad_processors: tuple[FrameProcessor, ...]
    aec: AECPair | None
    transport_factory: Callable[[TransportParams], BaseTransport]
```

Default factory is `LocalAudioTransport`. macOS + `aec_engine="vpio"` returns:

```python
AudioPath(
    in_channels=1,
    pre_vad_processors=(),
    aec=None,                       # VPIO does AEC upstream of pipecat
    transport_factory=AVAudioTransport,
)
```

The factory signature uses `TransportParams` (pipecat's base class) rather than `LocalAudioTransportParams` because the two transports take different params subclasses. `Hub.build_pipeline` chooses which params subclass to instantiate based on which transport the factory expects — concretely, the macOS branch constructs `AVAudioTransportParams`, the rest construct `LocalAudioTransportParams`. The existing `RefTappedLocalAudioTransport` (for speex) stays — it's used when the user explicitly picks `aec_engine="speex"`.

### `aec_engine` setting

Adds one value:

| Value | Behaviour |
|-------|-----------|
| `auto` | macOS → `vpio`. Linux → `off` (unchanged). |
| `vpio` | macOS only. Use `AVAudioTransport`. No filter. |
| `speex` | Use plain `LocalAudioTransport` + `SpeexAECFilter` + `RefTappedLocalAudioTransport`. (Unchanged.) |
| `webrtc-aec3` | Reserved. Raises `NotImplementedError` as today. |
| `off` | No AEC, plain `LocalAudioTransport`. (Unchanged.) |

Picking `vpio` on Linux raises `ValueError("aec_engine='vpio' is only supported on macOS")` at startup, with a clear message pointing at `aec_engine="speex"` or `"off"`.

## De-risking — Task 1: standalone PoC (DONE)

`scripts/audio_check_vpio.py` shipped at commit `223ac9d`. Validated on user's MacBook (built-in mic+speaker, macOS 26 Tahoe). Measured results:

- **1 kHz echo suppression: +77 dB** (after post-tap +12 dB gain; +91 dB raw). Far exceeds the ≥30 dB target.
- **Speech preserved at peak 262, RMS 51** — matches PyAudio raw baseline (peak 239), confirming user voice is recoverable to STT-usable levels.
- **No engine init failures**, no segfaults, all PyObjC bindings work as expected on macOS 26.

The PoC drove the four design corrections folded into the **Format handling** and **Engine lifecycle** sections above (9-channel aggregate input, explicit channelMap=[0], direct player→outputNode connection, post-tap gain). These were not visible in Apple's docs and only surfaced empirically.

Re-run with `python scripts/audio_check_vpio.py` if hardware changes — same `PASS`/`FAIL` summary.

## Configuration changes

`tend.toml`: no required changes for users. The default `aec_engine="auto"` now resolves to `vpio` on macOS instead of `speex`. Users who explicitly set `aec_engine="speex"` keep that behavior.

`README.md`: macOS install block gets one line noting VPIO is the default AEC on macOS. Users who want speex are pointed at the explicit override.

## Failure modes

| Failure | Behavior |
|---------|----------|
| `setVoiceProcessingEnabled` returns NSError on engine start | Log clearly with the error description. Raise `RuntimeError` from `start()`. Don't fall back to PyAudio silently — opt-in is explicit. |
| Engine fails to start (mic permission missing) | Standard pipecat error path. Setup wizard's mic-permission probe (already shipped) catches this before runtime. |
| `AVAudioEngineConfigurationChangeNotification` mid-conversation | Log. Don't auto-recover in v1. Document in spec as a known limitation. User can restart the daemon. |
| AVAudioConverter creation fails (formats incompatible) | Log + raise. Should never happen with mono int16/Float32 pair, but guard anyway. |
| Tap stops firing | No direct detection. Indirectly visible via no STT input after some time. Out of scope for v1. |

## Dependencies

- `pyobjc-framework-AVFoundation` — already installed for AVSpeechSynthesizer TTS. ✅
- `pyobjc-framework-CoreAudio` — maybe needed for low-level format types (`AudioStreamBasicDescription`). Verify in the PoC. If needed, add as a Darwin-conditional dependency.
- No new third-party packages.

## Testing

- **Unit tests:** `tests/test_audio_av_audio.py`. Mock `AVAudioEngine` / `AVAudioPlayerNode` / `AVAudioConverter`. Verify:
  - The asyncio bridge: a fake tap callback (called from a thread) results in `push_audio_frame` being awaited with the right frame.
  - Backpressure: `write_audio_frame` blocks until the completion handler is invoked.
  - Lifecycle: start ↔ stop is idempotent and ordered correctly.
- **Integration smoke:** `scripts/audio_check_vpio.py` (described above). Run by hand.
- **Manual end-to-end:** the v0.1 acceptance criterion. Conversation with the bot, bot speaks, user interrupts mid-sentence, conversation continues without echo-induced LLM-context pollution.

## Out of scope for this work

- **Upstreaming to pipecat.** File the PR after the code is shipping locally and has baked. Separate workstream.
- **Removing the speex code path.** Stays as an explicit opt-out.
- **Output device routing / device selection UI.** Engine uses system default audio device, same as today's PyAudio behavior.
- **Auto-recovery on hardware route changes.** Future iteration. v1 logs the event and continues with whatever the OS routes to next; if the engine breaks, the user restarts the daemon.
- **WWDC23 ducking + muted-talker APIs.** Available, not needed for v1.

## Files touched

| File | Change |
|------|--------|
| `src/tend/audio/av_audio.py` | NEW. AVAudioTransport + helpers. ~250 LOC. |
| `src/tend/audio/channels.py` | Add `transport_factory` to `AudioPath`. macOS VPIO branch in `select_audio_path`. |
| `src/tend/audio/aec.py` | Add `"vpio"` to `resolve_aec_engine`. Update auto-resolution on macOS. |
| `src/tend/audio/hub.py` | Use `path.transport_factory(params)` to construct transport. |
| `src/tend/config.py` | Document `vpio` as a valid `aec_engine` value in the field's comment. |
| `scripts/audio_check_vpio.py` | NEW. Standalone PoC + suppression measurement. |
| `tests/test_audio_av_audio.py` | NEW. Unit tests with mocked AVFoundation. |
| `tests/test_audio_channels_select_path.py` | Add macOS-VPIO branch coverage. |
| `README.md` | Note VPIO is the macOS default. |
| `ROADMAP.md` | Move the VPIO line from "After public release" into the changelog row when this lands. |
| `pyproject.toml` | Maybe add `pyobjc-framework-CoreAudio` if PoC needs it. |

## Estimated cost

~1–2 days. Bulk of the risk is in the PoC (Task 1). After that, the transport-class plumbing follows a well-known pattern (mirror pipecat's `LocalAudioTransport`).

## References

- [AVAudioIONode `setVoiceProcessingEnabled(_:)` — Apple Developer Documentation](https://developer.apple.com/documentation/avfaudio/avaudioionode/setvoiceprocessingenabled(_:)) (macOS 10.15+)
- [AVAudioIONode header — iOS-SDKs repo](https://github.com/xybp888/iOS-SDKs/blob/master/iPhoneOS13.0.sdk/System/Library/Frameworks/AVFoundation.framework/Frameworks/AVFAudio.framework/Headers/AVAudioIONode.h) — exact method signatures
- [WWDC23 Session 10235: What's new in voice processing](https://developer.apple.com/videos/play/wwdc2023/10235/) — current recommended setup order, ducking config, muted-talker API
- [WWDC19 Session 510: What's New in AVAudioEngine](https://developer.apple.com/videos/play/wwdc2019/510/) — original VPIO-in-AVAudioEngine introduction
- [Mastering AVAudioPlayerNode Interrupts and Completion Callbacks — Mehdi Samadi](https://medium.com/@mehsamadi/mastering-avaudioplayernode-interrupts-and-completion-callbacks-da39b36abbf7) — `.dataPlayedBack` vs `.dataRendered`
- [`AVAudioPlayerNodeCompletionCallbackType.dataPlayedBack` — Apple Developer Documentation](https://developer.apple.com/documentation/avfaudio/avaudioplayernodecompletioncallbacktype/dataplayedback)
- [Apple Developer Forums: AVAudioEngine VoIP usage thread](https://developer.apple.com/forums/thread/97679) — confirms VPIO is the right primitive
