# Apple VPIO Audio Transport — Design

## Problem

On macOS, the speex-based software AEC (`pyaec`) shipped in the macOS port (commit `f708942`) yields 12–26 dB suppression on the built-in MacBook mic+speaker. That's enough for the local Silero VAD to ignore the residual, but not enough for Deepgram's cloud STT to stop transcribing fragments of the bot's own voice ("nice to meet", "I'm Jan", …) into the LLM context. Those fragments confuse subsequent turns and turn the assistant into an unreliable partner.

There is no software-AEC ceiling above ~25 dB realistically achievable on consumer Mac hardware without OS-level integration with the audio HAL. The clean fix is to stop running AEC ourselves and let macOS do it.

## Goal

Replace pipecat's `LocalAudioTransport` on macOS with `AppleVoiceTransport` — an `AVAudioEngine`-based transport that runs Apple's VoiceProcessingIO audio unit (AEC + NS + AGC, the same path FaceTime / Voice Memos use). When VPIO is the active engine, no software AEC filter runs at all; the OS handles it upstream of pipecat seeing any audio.

The speex path remains in the codebase as an explicit opt-out (`aec_engine="speex"`) for users who want to override. Linux/Pi is untouched.

## Approach (vs. alternatives)

**Chosen:** `AVAudioEngine` + `setVoiceProcessingEnabled_error_(True)` via PyObjC. High-level Foundation API; the input node and output node are both backed by the underlying VoiceProcessingIO audio unit. Mirrors what FaceTime, Voice Memos, and the WWDC23 "What's new in voice processing" session promote.

**Rejected — raw `AudioUnit` of subtype `kAudioUnitSubType_VoiceProcessingIO`.** Lower-level, more control over format negotiation, but PyObjC's AudioToolbox bindings are thinner and the manual format-negotiation surface is large. No real benefit over AVAudioEngine for our use case.

**Rejected — manual-rendering mode + direct AudioUnit (the 2018 Twilio approach).** Significantly more complex; necessary only for VoIP apps that mix multiple participants. We have one TTS stream out and one mic stream in.

**Rejected — accept the speex ceiling and move on.** Deepgram transcribing the bot's own voice into context is a real user-experience bug, not a polish issue. The assistant cannot be reliable while this is unfixed.

## Architecture

Single new module that mirrors pipecat's `local/audio.py` file shape, so it's pasteable upstream as `pipecat.transports.local.apple_voice` when we file the PR later.

```
src/tend/audio/apple_voice.py
  AppleVoiceTransport(BaseTransport)
    - owns one AVAudioEngine
    - input() lazy-returns AppleVoiceInputTransport
    - output() lazy-returns AppleVoiceOutputTransport
  AppleVoiceInputTransport(BaseInputTransport)
    - installs tap on engine.inputNode at the bus's native format
    - per-buffer AVAudioConverter → target sample_rate/int16/mono
    - tap callback marshals to asyncio via run_coroutine_threadsafe
  AppleVoiceOutputTransport(BaseOutputTransport)
    - owns one AVAudioPlayerNode attached to engine.mainMixerNode
    - write_audio_frame: schedule a single AVAudioPCMBuffer with
      completionCallbackType=.dataPlayedBack; await its completion
      via asyncio.Event set in the callback
```

### Engine lifecycle

```
engine = AVAudioEngine()
input_node = engine.inputNode      # forces VPIO unit construction
input_node.setVoiceProcessingEnabled_error_(True, None)  # MUST be before start
                                                          # enables AEC+NS+AGC
                                                          # on BOTH I/O buses
player = AVAudioPlayerNode()
engine.attachNode_(player)
engine.connect_to_format_(player, engine.mainMixerNode, output_format)
input_node.installTapOnBus_bufferSize_format_block_(
    0, 1024, input_node.outputFormatForBus_(0), tap_callback,
)
engine.prepare()
engine.startAndReturnError_(None)
```

`setVoiceProcessingEnabled` cannot toggle while the engine is running — the engine must be stopped first. That's fine for our lifecycle (start once, stop once).

### Format handling

The input node's native output format after VPIO is enabled is the audio HAL's preferred format — typically 48 kHz Float32 mono on built-in MacBook mics. Two-stage conversion in the tap callback:

1. Tap receives `AVAudioPCMBuffer` at native format.
2. `AVAudioConverter` (constructed once at start, reused per buffer) converts to 16 kHz int16 mono — the format Silero VAD / Deepgram / wake word expect.
3. Wrap the converted bytes in an `InputAudioRawFrame(audio=bytes, sample_rate=16000, num_channels=1)`.
4. Hand to asyncio via `loop.call_soon_threadsafe(push_audio_frame, frame)` or `asyncio.run_coroutine_threadsafe`.

For output: TTS produces int16 PCM at `tts_sample_rate` (16 kHz with our resampled AVSpeechSynthesizer output). We wrap in `AVAudioPCMBuffer` at that format and schedule on the player node; the `mainMixerNode` handles upconversion to the hardware output rate. AVAudioEngine handles this conversion automatically along the mixer path (this is the path that *does* work — unlike the tap-with-different-format path which is unreliable).

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

`AudioPath` gains a `transport_factory` field. `select_audio_path` picks `AppleVoiceTransport` on macOS when `aec_engine` resolves to `"vpio"`.

```python
@dataclass(frozen=True)
class AudioPath:
    in_channels: int
    pre_vad_processors: tuple[FrameProcessor, ...]
    aec: AECPair | None
    transport_factory: Callable[[LocalAudioTransportParams], BaseTransport]
```

Default factory is `LocalAudioTransport`. macOS + `aec_engine="vpio"` returns:

```python
AudioPath(
    in_channels=1,
    pre_vad_processors=(),
    aec=None,                       # VPIO does AEC upstream of pipecat
    transport_factory=AppleVoiceTransport,
)
```

`Hub.build_pipeline` reads `path.transport_factory(params)` instead of constructing `LocalAudioTransport` directly. The existing `RefTappedLocalAudioTransport` (for speex) stays — it's used when the user explicitly picks `aec_engine="speex"`.

### `aec_engine` setting

Adds one value:

| Value | Behaviour |
|-------|-----------|
| `auto` | macOS → `vpio`. Linux → `off` (unchanged). |
| `vpio` | macOS only. Use `AppleVoiceTransport`. No filter. |
| `speex` | Use plain `LocalAudioTransport` + `SpeexAECFilter` + `RefTappedLocalAudioTransport`. (Unchanged.) |
| `webrtc-aec3` | Reserved. Raises `NotImplementedError` as today. |
| `off` | No AEC, plain `LocalAudioTransport`. (Unchanged.) |

Picking `vpio` on Linux raises `ValueError("aec_engine='vpio' is only supported on macOS")` at startup, with a clear message pointing at `aec_engine="speex"` or `"off"`.

## De-risking — Task 1: standalone PoC

Before any pipecat plumbing, ship `scripts/audio_check_vpio.py`. Mirrors the existing `scripts/audio_check.py` script shape. Pure PyObjC, no tend imports.

What it does, in order:

1. Construct `AVAudioEngine`. Enable VPIO on the input node. Verify `isVoiceProcessingEnabled()` returns true.
2. Construct `AVAudioPlayerNode`, attach to engine, connect to `mainMixerNode` at 16 kHz int16 mono format.
3. Install tap on inputNode at the bus's native format. Tap callback converts each buffer to 16 kHz int16 mono via `AVAudioConverter` and appends to an in-memory buffer.
4. Prepare and start the engine.
5. Schedule a 3-second 1 kHz tone PCM buffer on the player node.
6. Capture 3 seconds of mic input while the tone plays.
7. Stop the engine.
8. Compute the RMS amplitude of the captured signal in the 1 kHz frequency band (simple FFT) and compare to the tone's emit RMS. Report suppression in dB. With VPIO working, expect ≥40 dB.
9. Run a second capture without playing the tone, while the user speaks. Confirm the captured speech RMS is preserved (no false-positive suppression of user voice).

The script accepts no arguments and prints a one-line `PASS` / `FAIL` summary plus the measured numbers. Run with `python scripts/audio_check_vpio.py`.

If the PoC fails on user's macOS 26 / hardware, we discover it in ~50 lines of code before any pipecat code is written. If it passes, the architecture is proven for the rest of the implementation.

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

- **Unit tests:** `tests/test_audio_apple_voice.py`. Mock `AVAudioEngine` / `AVAudioPlayerNode` / `AVAudioConverter`. Verify:
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
| `src/tend/audio/apple_voice.py` | NEW. AppleVoiceTransport + helpers. ~250 LOC. |
| `src/tend/audio/channels.py` | Add `transport_factory` to `AudioPath`. macOS VPIO branch in `select_audio_path`. |
| `src/tend/audio/aec.py` | Add `"vpio"` to `resolve_aec_engine`. Update auto-resolution on macOS. |
| `src/tend/audio/hub.py` | Use `path.transport_factory(params)` to construct transport. |
| `src/tend/config.py` | Document `vpio` as a valid `aec_engine` value in the field's comment. |
| `scripts/audio_check_vpio.py` | NEW. Standalone PoC + suppression measurement. |
| `tests/test_audio_apple_voice.py` | NEW. Unit tests with mocked AVFoundation. |
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
