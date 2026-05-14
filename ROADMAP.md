# tend roadmap

Where tend is headed. Horizons rather than dates. Updated each minor release.

## Where we are now (v0.1.0-dev)

tend is a working single-user voice assistant for desk workers, running on Raspberry Pi 5. The core conversational loop (wake → STT → brain → workers → TTS), day-session memory, proactive triggers (scheduler + announcer + webhook), the skills layer with autonomous skill authoring, and full Google Workspace integration via `gws` are all in place. tend currently runs from a git clone via `python -m tend`; it is not yet pip-installable, public, or macOS-supported.

## Next: public release (v0.1)

Goal: a stranger can install tend tonight and have it working in 10 minutes. Critical path, in dependency order:

1. **OSS clerical** — `LICENSE` (Apache-2.0), repo rename to `tend`, secrets audit (move `luthraridhwan@gmail.com` out of `tend.toml`), GH templates + `SECURITY.md` + dependabot. *~½ day.*
2. **macOS port** — `audio/channels.py` mono branch, software AEC (preserves barge-in), launchd plist, README install path. *~1–2 days. AEC is the design risk.*
3. **Webhook hardening** — HMAC-SHA256 signing, `X-Tend-Timestamp` replay protection, `X-Tend-Delivery-Id` idempotency. *~½ day.*
4. **SKILL.md frontmatter v2** — add `version`, `min_tend_version`, `requires:`. Cheap now, breaking later. *~½ day.*
5. **Decouple STT/TTS provider from API-key presence** — `services._make_stt`/`_make_tts` currently route on "is the key set?", which conflates *I have credentials* with *I want to use them now* and makes a third provider structurally ambiguous. Add explicit `stt_provider` / `tts_provider` fields to `tend.toml`, default to local (Whisper/Piper) so fresh installs work without keys, write the wizard's pick into the file, and rewrite the runtime + doctor dispatch to read the field. Migration: users with no provider field fall back to the current key-presence inference for one version with a deprecation warning. Schema change — must land before the 1.0 schema lock. *~½ day.*
6. **Release machinery + docs + README rewrite** — release-please workflow, PyPI Trusted Publishing + Sigstore, mkdocs-Material site, README rewrite with asciinema demo, Homebrew tap. *~2 days.*

Total: ~8.5–10.5 working days. Each item gets a spec under `docs/superpowers/specs/` and a plan under `docs/superpowers/plans/` before implementation, per the existing project convention. Standards every item must follow live in [`docs/conventions.md`](docs/conventions.md).

## After public release

Three candidate product directions, each independently coherent. Order will be picked based on user demand and tend's own dogfooding signal — none of these are committed.

- **Vision producer for posture and hydration nudges.** The persona already advertises vision; `[announcer.category]` already reserves `posture` and `hydration` cooldown buckets; the webhook contract was explicitly designed for "a vision daemon." A small separate process — Pi camera or USB webcam — POSTs to `/say` with the relevant category. Keeps vision out of tend's main process so the privacy boundary stays clean. **Highest leverage on existing surface.**
- **Outbound channel routing — Telegram and SMS.** Explicitly deferred in `CLAUDE.md` as "a separate workstream." Unblocks scheduled reminders for when the user has stepped away from the desk. Requires a delivery abstraction in the announcer and secrets management for the outbound provider.
- **Cross-day memory.** Currently every day starts from `soul.md` alone. A small per-user fact store — interests, ongoing projects, callbacks like "remind me about X tomorrow" — would change the texture of conversations. Pairs naturally with the existing day-session reset boundary.

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
