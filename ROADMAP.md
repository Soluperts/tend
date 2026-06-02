# tend roadmap

Where tend is headed. Horizons rather than dates. Updated each minor release.

## Where we are now (v0.1.0)

tend is a working single-user voice assistant for desk workers, running on Raspberry Pi 5 or macOS 14+ on Apple Silicon. The core conversational loop (wake → STT → brain → workers → TTS), day-session memory, proactive triggers (scheduler + announcer + webhook), the skills layer with autonomous skill authoring, and full Google Workspace integration via `gws` are all in place. As of 2026-06-02, the public PyPI distribution is `tend-assistant==0.1.0` (`pipx install tend-assistant`); the bare PyPI package name `tend` belongs to a different project. On macOS the audio transport uses AVAudioEngine with VoiceProcessingIO for OS-grade AEC + NS + AGC; software-AEC paths remain available as explicit opt-outs.

## Current release follow-ups (v0.1)

Goal: keep the public `tend-assistant` package healthy while the remaining release-follow-up PRs land. Queue truth refreshed with `gh pr list --repo Soluperts/tend --state open --limit 100` on 2026-06-02:

- #5 `codex/ci-portaudio-dev-package` — CI installs PortAudio headers.
- #3 `dependabot/github_actions/actions/checkout-6` — actions/checkout bump.
- #2 `dependabot/github_actions/astral-sh/setup-uv-7` — setup-uv bump.
- #1 `dependabot/pip/python-minor-and-patch-4eb6eddbdb` — pipecat-ai-subagents dependency bump.

Release follow-up items:

1. **OSS clerical** — ~~`LICENSE`~~ (MIT — Apache-2.0 was the original pick; rationale lives in `docs/conventions.md`), GH templates + `SECURITY.md` + `CONTRIBUTING.md` + dependabot, `pyproject.toml` migrated from setuptools to hatchling, distribution name set to `tend-assistant` (the bare `tend` was taken on PyPI). *Done.* Remaining follow-ups: (a) GitHub repo rename `Soluperts/DeskClaw` → `Soluperts/tend` (manual, via `gh repo rename`); (b) SPDX header sweep across `.py` files. Both are mechanical and tracked separately.
2. ~~**macOS port**~~ — *Done.* `AVAudioTransport` (`src/tend/audio/av_audio.py`) wraps `AVAudioEngine` with VoiceProcessingIO for OS-grade AEC + NS + AGC; `select_audio_path` picks it on Darwin; `tend service install` writes the launchd plist; the README and `tend setup` cover the install path. Software-AEC paths (Speex/WebRTC-AEC3) remain available as explicit opt-outs.
3. **Webhook hardening** — HMAC-SHA256 signing, `X-Tend-Timestamp` replay protection, `X-Tend-Delivery-Id` idempotency. *~½ day.*
4. **SKILL.md frontmatter v2** — add `version`, `min_tend_version`, `requires:`. Cheap now, breaking later. *~½ day.*
5. ~~**Decouple STT/TTS provider from API-key presence**~~ — *Done.* `tts_provider` (in `Settings`, `config.py:99`) accepts `auto | elevenlabs | avspeech | piper`; `services._make_tts` dispatches on it. The `auto` value preserves the historical key-presence inference for backwards-compatibility.
6. **Release machinery + docs follow-ups** — release-please workflow, PyPI Trusted Publishing + Sigstore, mkdocs-Material site, asciinema demo, Homebrew tap. *~2 days.*

Total: ~8.5–10.5 working days. Each item gets a spec under `docs/superpowers/specs/` and a plan under `docs/superpowers/plans/` before implementation, per the existing project convention. Standards every item must follow live in [`docs/conventions.md`](docs/conventions.md).

## After public release

Three candidate product directions, each independently coherent. Order will be picked based on user demand and tend's own dogfooding signal — none of these are committed.

- **Vision producer for posture and hydration nudges.** The persona already advertises vision; `[announcer.category]` already reserves `posture` and `hydration` cooldown buckets; the webhook contract was explicitly designed for "a vision daemon." A small separate process — Pi camera or USB webcam — POSTs to `/say` with the relevant category. Keeps vision out of tend's main process so the privacy boundary stays clean. **Highest leverage on existing surface.**
- **Outbound channel routing — Telegram and SMS.** Explicitly deferred in `CLAUDE.md` as "a separate workstream." Unblocks scheduled reminders for when the user has stepped away from the desk. Requires a delivery abstraction in the announcer and secrets management for the outbound provider.
- **Cross-day memory.** Currently every day starts from `soul.md` alone. A small per-user fact store — interests, ongoing projects, callbacks like "remind me about X tomorrow" — would change the texture of conversations. Pairs naturally with the existing day-session reset boundary.
- **Cross-platform local TTS via sherpa-onnx.** Optional path that gives Pi and Mac the same VITS voice for persona consistency across devices. Spec'd in the macOS-port design doc. ~1 day. Ship only if there's demand.
- **Secrets handling: keyring as a real pydantic-settings source.** Today, `tend.secrets.load_into_env()` copies keychain-stored secrets into `os.environ` at boot so pydantic-settings can read them. That works but puts secrets in the daemon's env vars for its entire run — visible via `launchctl print gui/$UID/com.tend.daemon` and inherited by subprocess children (`claude` CLI, `gws`). The canonical pydantic-settings pattern (per [pydantic-settings#139](https://github.com/pydantic/pydantic-settings/issues/139)) is a custom `PydanticBaseSettingsSource` subclass that queries the keyring directly in `get_field_value`, registered via `settings_customise_sources`. Secrets flow keyring → Settings without touching env. For single-user personal-infra the current approach is defensible; for any deployment that needs secret discipline (multi-user, exposed daemon, anything in a CI runner), this needs to land before 1.0's schema lock. *~½ day.*

## Toward 1.0

1.0 promises API stability for:

- The `tend.toml` schema
- The `SKILL.md` frontmatter schema
- The webhook contract (`/say` and `/event` request/response shapes)
- The `tend` CLI surface

Target: 2–3 external users running deployments and ~3 months without breaking renames. Realistically ~6 months after the v0.1 public release.

## Not on the roadmap

Quoting `CLAUDE.md`'s deliberate non-choices, with the rationale that motivates each. PRs that add these will be declined with a pointer here.

- **Multi-user / multi-tenant.** Single user, single device, no auth. tend is personal infrastructure.
- **New Python worker classes per capability.** Behavior lives in skills, not subclassing. A new class per capability is the design tend explicitly rejects.
- **Context compaction.** Day-session resets at the configured wall-clock boundary. Revisit when context size becomes a real problem.
- **Worker durability across process restart.** systemd / launchd brings the process back up clean.
- **Mid-stream cloud-service failover.** Lose the current turn if STT/TTS fails mid-frame; next turn falls back.
- **Retry/backoff on failed scheduled jobs.** Recurring jobs wait for the next fire; one-shot jobs delete after a single attempt.
- **Distributed deployment.** In-process `AsyncQueueBus` only.
- **App bundle and code signing on macOS.** Not for v1. Revisitable later if Mac users hit the friction.
- **Pip-installable Python plugins via entry points.** Reserved entry-point group name `tend.skills` for future, but no loader until a real third party asks. Filesystem-discovered skills remain the primary surface.

## Updating this document

Roadmap drift is fine; silent drift is not. Update this file when:

- A planned item ships → move from "Next" into the CHANGELOG and delete from here.
- A candidate item gets accepted or rejected → move it out of "After public" into "Next" or "Not on the roadmap."
- A deliberate non-choice gets revisited → move it out of "Not on the roadmap" with a brief rationale.
