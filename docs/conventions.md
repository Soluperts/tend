# tend conventions and coding guidelines

The cross-cutting standards tend follows: workspace layout, packaging, CLI patterns, extension model, releases, OSS hygiene, and coding style. This is the canonical reference. When you change something covered here, update this doc in the same change.

These were derived from a review of comparable tools (uv, ruff, gh, llm, ollama, pipecat, simonw/llm) in May 2026; references to specific real-world tools that motivate each choice are kept inline so the reasoning stays auditable.

## Workspace and paths

- **`$TEND_HOME`** is the single user-state root. Default: `~/.tend/` on both Linux and macOS. Single-folder dotdir convention — same shape as `~/.ollama/`, `~/.docker/`, `~/.terraform.d/`. We deliberately don't use `~/.config/tend/`: XDG reserves `~/.config/` for config files, but tend has a real workspace (skills, scripts, persona, state) that doesn't fit there cleanly.
- **Cache** via `platformdirs.user_cache_dir("tend")` → `~/.cache/tend` / `~/Library/Caches/tend`.
- **State** (job state, sessions) via `platformdirs.user_state_dir("tend")`.
- **Logs** via `platformdirs.user_log_dir("tend")`. Replaces `/tmp/tend.log`.
- Use `platformdirs.PlatformDirs("tend", appauthor=False, ensure_exists=False)`.
- Setting `$TEND_HOME` collapses everything user-editable under one dir for power users.

Inside `$TEND_HOME`:

```
$TEND_HOME/
  tend.toml          # user overrides (optional, written by `tend setup`)
  soul.md            # persona; seeded once from defaults, user-owned thereafter
  .env               # secrets fallback when keyring is unavailable (mode 600)
  skills/<name>/     # user skills + edited copies of seed skills
  workspace/bin/     # user scripts callable from skills
  cron/jobs.json     # scheduled jobs (already user-owned)
```

## Packaging and shipped defaults

- Built-in defaults ship inside the wheel at `src/tend/_defaults/` (persona, seed skills, default `tend.toml` baseline).
- Read at runtime via `importlib.resources.files("tend._defaults")` and `as_file()` for directory traversal. Never assume the wheel is unzipped on disk.
- `pyproject.toml`:
  ```toml
  [tool.setuptools.package-data]
  "tend._defaults" = ["**/*"]
  ```
- Build backend: **hatchling**. Verify packaging with `uv build && unzip -l dist/*.whl`.

**Seed-once-then-hands-off.** On first launch, if `$TEND_HOME` is empty, copy `_defaults/` across. Thereafter, never auto-overwrite anything in `$TEND_HOME`. New shipped defaults in a later version become reachable because the worker enumerates **both** `_defaults/skills/` and `$TEND_HOME/skills/`, with the user dir winning on name collision.

`tend skill install <name>` forks a default skill into `$TEND_HOME/skills/` so the user can edit it. `tend skill list` shows both shipped and user skills with their source.

## Configuration loading

- **pydantic-settings v2** with explicit `settings_customise_sources` chain.
- Precedence (high → low): init args → env vars (`TEND_*`) → dotenv (`$TEND_HOME/.env`) → user TOML (`$TEND_HOME/tend.toml`) → shipped TOML (`tend._defaults/tend.toml`) → class defaults.
- `TomlConfigSettingsSource` accepts `toml_file=[...]` and silently skips missing files.
- The TOML source is **not** in the default chain — you must add it via `settings_customise_sources`.

Reference pattern:

```python
toml = TomlConfigSettingsSource(
    settings_cls,
    toml_file=[
        files("tend._defaults") / "tend.toml",
        Path(os.environ.get("TEND_HOME") or Path.home() / ".tend") / "tend.toml",
    ],
)
return (init, env, dotenv, toml, secrets)
```

## CLI structure

- **Framework: Typer.** Each command group lives in its own module under `src/tend/cli/` (`sessions.py`, `skills.py`, `schedule.py`, `webhook.py`, `snapshot.py`, `setup.py`, `doctor.py`, `service.py`). Sub-apps register via `app.add_typer(name=...)`; top-level commands (`snapshot`, `scan-skill`, `setup`, `doctor`) register directly on the root app via `app.command(name)(fn)`. `main(argv)` runs the root app with `standalone_mode=False` and returns the rc, so tests can call it directly.
- Every command exposes `--help`. Long-running commands respect `--quiet` and `--verbose`.
- Output: human-readable by default. Commands that compose with scripts expose `--json` and use stdout for JSON, stderr for status.
- Exit codes: 0 success, 1 user error, 2 system error. `tend doctor` exits non-zero if any check is `✗`.
- Subcommand list (shipped):
  - `tend setup` — interactive bootstrap (writes `$TEND_HOME/{tend.toml,.env}`, prompts for secrets, auto-generates webhook token, installs optional skills).
  - `tend doctor [--json]` — read-only diagnostic; runs the same `tend.checks` library as setup. Exit 0 all-ok, 1 warn-only, 2 any-fail.
  - `tend service install [--force]` / `tend service uninstall` — systemd user unit (Linux). macOS launchd is sub-project #3 (macOS port).
  - `tend sessions {list,show,tail,cat}` — worker session history.
  - `tend skills {list,show,cat,rm,quarantined,new,install,validate,enable-triggers,disable-triggers}` — skill management (plural, matching `$TEND_HOME/skills/`).
  - `tend schedule {list,show,add,rm}` — cron store wrapper.
  - `tend snapshot` — snapshot Claude Code env to `$TEND_HOME/claude-env.md`.
  - `tend webhook test` — POST a smoke message to `/say`.
  - `tend scan-skill <name>` — top-level safety scanner (use `tend skills validate` for the combined frontmatter + scan check).

## Bootstrap UX (`tend setup` / `tend doctor`)

- **Prompts:** `questionary` (arrow-key device pickers, password prompts, validators).
- **Status output:** `rich` (tables, spinners, `✓`/`⚠`/`✗` icons). Use `rich.console.Console.status()` for in-progress checks.
- **Flow:** auto-detect → ask only the gaps → validate each answer before storing → write → smoke-test.
- **Idempotent re-run:** when re-running, show current values as `[bracketed]` defaults — press Enter to keep (aws-configure style).
- **Explicit only.** When `$TEND_HOME/tend.toml` is missing, `tend` (no args) prints "No config found. Run `tend setup`." and exits. Do not auto-launch the wizard.
- **Visual model:** `flutter doctor` — per-check status icon + concrete remediation under each `✗`.
- `tend doctor` and `tend setup` share a common check library; setup runs writes after each check, doctor runs read-only.

## Secret handling

- **OS keyring first:** `keyring.set_password("tend", "ANTHROPIC_API_KEY", key)`.
- **Fallback to `$TEND_HOME/.env`** with mode 600 on `keyring.errors.NoKeyringError` (the common path on a headless Pi, since GNOME Keyring/KWallet needs a desktop session).
- **Surface the fallback explicitly** in setup output ("No system keyring; saved to $TEND_HOME/.env with mode 600.").
- **Never** store secrets in `tend.toml` or any committed file. Validate this in CI with a grep on the wheel before publish.
- Token rotation: `tend setup` with no args re-runs the prompts, replacing values.

## Extension model

Three surfaces. They're orthogonal — pick the one whose trust model matches the integration.

| Surface | Audience | Distribution | Trust |
|---|---|---|---|
| **Skills** | Users + the LLM itself | `$TEND_HOME/skills/<name>/` (filesystem) | In-process; sandboxed by the skill safety scanner |
| **External producers** | Other-language daemons | POST to loopback webhook with HMAC | Out-of-process; auth boundary |
| **Persona** | Single user | `$TEND_HOME/soul.md` (one file) | User-owned config, not really an extension |

**Not an extension surface:** new Python worker classes. Behavior lives in skills.

**Pip-installable Python plugins via entry points** are deferred — reserve the group name `tend.skills` in docs so the future migration is non-breaking, but don't build the loader until a real third party asks.

### Skills

`$TEND_HOME/skills/<name>/SKILL.md` is markdown with YAML frontmatter. Scripts live in `$TEND_HOME/workspace/bin/` (or alongside the skill).

Required frontmatter fields:

- `name` — kebab-case, matches dir name
- `version` — semver
- `min_tend_version` — semver constraint
- `description` — one line

Optional:

- `triggers` — cron expressions, `every 30m`, ISO timestamps, `in 30m`-relatives
- `events` — event kinds the skill subscribes to
- `requires` — `{ clis: [...], env: [...], scopes: [...] }`. Setup/install can pre-check these.
- `silent_default` — bool. Default false.

These fields are stable from v0.1.0 onward; renames are breaking changes and bump minor (pre-1.0) or major (post-1.0).

### External producers

The webhook contract (`POST /say`, `POST /event` on `127.0.0.1:7331`) is documented at `docs/integrating-with-tend.md`. Producers run as separate processes — a vision daemon, a Gmail watcher, etc.

### Persona

`$TEND_HOME/soul.md` is seeded once from `_defaults/soul.md`. The user edits it directly; no overlay layer. To preview the shipped default after editing, read it from the package: `python -c "from importlib.resources import files; print((files('tend._defaults') / 'soul.md').read_text())"`.

## Webhook security

The bar rises before going public — bearer-only is fine for loopback today but should be reinforced before the contract is announced.

- **Auth:** `Authorization: Bearer <TEND_WEBHOOK_TOKEN>`.
- **Signature:** `X-Tend-Signature: sha256=<hex>` — HMAC-SHA256 over the raw body with a per-producer secret. Compare with `hmac.compare_digest`.
- **Replay protection:** `X-Tend-Timestamp` (Unix seconds). Reject anything ±5 minutes.
- **Idempotency:** `X-Tend-Delivery-Id` (GUID). Bounded LRU cache (~1000 entries); return 200 on duplicates without re-processing.
- **Semantics:** always 200 + async. Document at-least-once delivery — retry logic is the producer's job.
- **Secret rotation:** producers can have a list of valid secrets; document the rotation path up front.

## Versioning and releases

- **Conventional Commits:** `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`. Breaking changes: `feat!:` or `BREAKING CHANGE:` footer.
- **SemVer.** Start at **0.1.0**. Stay pre-1.0 until SKILL.md frontmatter and the webhook contract are frozen. Target 1.0 once we have 2-3 external users and no recent breaking renames.
- **release-please** (Google) opens a Release PR with version bump + CHANGELOG diff. Merging tags + triggers publish. We pick this over `python-semantic-release` because release-on-merge gives us control of timing.
- **PyPI Trusted Publishing** (OIDC). No API tokens in CI. `permissions: { id-token: write }` on the publish job.
- **Sigstore attestations** via `pypa/gh-action-pypi-publish` with `attestations: true`.

## CI and quality

- **Matrix:** Python 3.11 / 3.12 / 3.13 × `ubuntu-latest` on every PR. Add `macos-latest` only on pushes to `main` and on release tags (PR runs skip macOS — 10× cost).
- **Jobs (per matrix cell):**
  - `ruff check`
  - `ruff format --check`
  - `pyright` (faster + better than mypy in 2026)
  - `pytest`
  - `uv build` (packaging smoke)
  - `tend --help` (CLI smoke)
- **Setup:** `astral-sh/setup-uv@v5` with `enable-cache: true`, cache key on `uv.lock`.
- **Concurrency:** `group: ci-${{ github.ref }}, cancel-in-progress: true`.
- **Dependabot:** weekly, `pip` + `github-actions` ecosystems.
- **Coverage:** aim for high coverage on pure logic (`cron_time`, `google_watcher`, `skills` parser); don't chase coverage on glue code.

## Repo furniture

**Load-bearing (required):**

- `LICENSE` — Apache-2.0 verbatim. SPDX header (`SPDX-License-Identifier: Apache-2.0`) at the top of every source file.
- `README.md` — see structure below.
- `pyproject.toml` — hatchling backend, project metadata, classifiers, `[project.scripts]`.
- `CHANGELOG.md` — auto-generated by release-please from Conventional Commits.
- `SECURITY.md` — short. Email contact + 7-day response SLA + "don't open public issues for vulns."
- `.github/workflows/{ci,release}.yml`
- `.github/dependabot.yml`

**Worth adding before launch:**

- `CONTRIBUTING.md` — dev setup (`uv sync`), test commands (`uv run pytest`), commit-style note, and an explicit **"No" list** quoting CLAUDE.md's deliberate non-choices. Saves rejecting PRs.
- `.github/ISSUE_TEMPLATE/{bug_report,feature_request}.yml` — YAML form schema, not legacy markdown.
- `.github/PULL_REQUEST_TEMPLATE.md` — three-line checklist: tests, changelog entry, no new deps without discussion.

**Skip until needed:**

- `CODE_OF_CONDUCT.md` — add on first conduct incident. Contributor Covenant 2.1 when needed.
- `CODEOWNERS` — pointless with one maintainer.
- `FUNDING.yml` — only if accepting money.

### License choice rationale

**Apache-2.0**, not MIT. The patent grant matters for a project that touches audio processing, wake-word detection, and prompt engineering — all areas with active patent activity. MIT dominates the AI-OSS herd (Ollama, llama.cpp, LangChain), but tend's downstream commercial-API surface is bigger than MIT's brevity warrants, and Apache-2.0 produces cleaner SBOMs for any future enterprise users.

## Distribution

- **Primary:** PyPI. Recommended user install: `pipx install tend` (isolated venv, on `$PATH`). Document this.
- **Secondary:** Homebrew tap at `ridhwanluthra/homebrew-tend`. The formula wraps the PyPI release (~20 lines of Ruby). `brew services start tend` writes both launchd plists on macOS and systemd user units on Linux — solving cross-platform service install for free.
- **`tend service install`** for non-Homebrew users: writes `~/.config/systemd/user/tend.service` (Linux) or `~/Library/LaunchAgents/com.tend.daemon.plist` (macOS). `--uninstall` reverses.
- **Skip:** AUR, Snap, Debian, Flatpak, `.app` bundle, code signing, notarization. All are revisitable later if users ask.

## macOS specifics

- **No `.app` bundle, no code signing, no notarization for v1.** Skipping saves $99/yr Apple Developer cost and a few days of work; users who hit the friction can speak up.
- **TCC mic permission** triggers on first PyAudio use. Document: Settings → Privacy & Security → Microphone → enable Terminal (or iTerm, or whatever launched `tend`). Once granted, persists.
- **`audio/channels.py`** selects mono-mic on macOS (no XVF3800 left-channel downmix); selects the XVF3800 path on Linux when the device-name match hits.
- **Acoustic echo cancellation:** built-in MacBook mic and speaker are ~10 cm apart with no AEC. The v1 strategy is **mute-during-TTS** — gate audio while a TTS output is in flight. Cheaper than full AEC, and tend has no barge-in anyway. Upgrade paths if needed: macOS Voice Processing IO unit (Apple's built-in AEC), or WebRTC AEC3 (cross-platform).
- **Service unit on macOS:** launchd plist at `~/Library/LaunchAgents/com.tend.daemon.plist`, loaded via `launchctl load ~/Library/LaunchAgents/com.tend.daemon.plist`. `tend service install` handles this.

## Coding style

- **Python 3.11+.** Use modern syntax: `match` where it fits, `X | Y` union types, `from __future__ import annotations` not needed but acceptable.
- **Type hints throughout.** pyright in strict-adjacent mode (`strictParameterNoneValue`, `reportMissingTypeStubs="information"`). Public functions and class fields must be annotated; internal lambdas and trivial locals can skip.
- **`ruff` is the only formatter and linter.** Config in `pyproject.toml`. No `# noqa` without an inline reason. No per-file ignores in `[tool.ruff.lint.per-file-ignores]` without an explaining comment.
- **Import order:** stdlib → third-party → first-party (`tend.*`) → relative. ruff's `I` rules enforce this.
- **Async:** every IO and pipeline boundary is async. Don't mix sync IO inside async coroutines — use `anyio.to_thread.run_sync` or `asyncio.to_thread` for unavoidable sync calls.
- **Constructor injection.** No module-level singletons. Each component should be testable with a fake or stub passed in; if you can't, the boundary is wrong. The one exception is `tend.config.settings`, which is a process-wide read-only snapshot.
- **No premature abstraction.** Three similar lines is better than a premature helper. No config knobs for hypothetical needs. No extension points without a real third party asking.
- **Comments:** default to none. Only add when the WHY is non-obvious — a hidden constraint, a workaround, a non-local invariant. Don't explain WHAT; let names do that.
- **Docstrings:** module-level docstring on every module; brief one-liner on public functions if the name doesn't carry it. Skip on `@tool` methods where the docstring becomes the LLM-visible tool description and care belongs there.
- **Error handling:** validate at system boundaries (user input, external APIs, frame deserialization). Inside the trust boundary, propagate exceptions; don't wrap-and-rethrow for the sake of it.
- **Testing:**
  - `pytest`, `pytest-asyncio` (`asyncio_mode = "auto"`), `freezegun` for time.
  - One test file per source file. Tests live in `tests/` mirroring `src/tend/`.
  - Fakes over mocks. Constructor-inject a fake bus, fake LLM, fake transport — never `unittest.mock.patch` on framework internals.
  - Integration tests that hit a real Pipecat pipeline live in `tests/integration/`; mark them `@pytest.mark.integration` and skip on PR (run on tag).
- **Pipecat-specific gotchas to remember:**
  - `@tool` methods MUST call `await params.result_callback(value)`. Returning a value silently does nothing in pipecat-subagents.
  - `BusBridgeProcessor` should be **unnamed** (no `bridge=` kwarg) — the framework's `_BusEdgeProcessor` publishes without a bridge name. Use `exclude_frames=` to keep frame types local.
  - The `LLMContext` aggregators are terminal sinks for their frame types; the assistant aggregator must sit at the very end of the pipeline.

## Documentation

- **Long-form:** mkdocs-Material site sourced from `docs/`. Build target lives in `docs/_site/` (gitignored), published via GitHub Pages on release.
- **Navigation:**
  - **Getting Started** — install, first run, `tend setup`.
  - **Usage** — talking to tend, conversational patterns, sleep/wake.
  - **Skills** — how skills work; cookbook recipes.
  - **Privacy** — the wake-gate boundary, what hits which cloud, audit instructions.
  - **Extending tend** — three subsections (skills, producers, persona), each with hello-world ≤30 LOC + 2-3 cookbook recipes.
  - **Reference** — `tend.toml` schema, CLI subcommand reference, webhook contract.
  - **Internals** — architecture, the bus, the LLMContext lifecycle, the scheduler.
- **Specs and plans** stay under `docs/superpowers/` and are **not** part of the public docs site — they're project memory for design decisions, not user-facing.
- **CHANGELOG.md** at the repo root, auto-generated by release-please.

### README structure

The repo's `README.md` is the front door. Length sweet spot: 600–900 lines source. Order:

1. **One-sentence hook** — what tend is, in 12 words.
2. **Badges** — PyPI version, CI, license, Python versions.
3. **Demo** — 60-second asciinema cast (not GIF — gifs are heavy and unsearchable). Linked from `/assets`.
4. **What it is** — three paragraphs answering WHY tend exists, not what it does.
5. **Install** — `pipx install tend` (Linux/Mac) or `brew install ridhwanluthra/tend/tend` (Mac).
6. **Quickstart** — three commands from install to wake word.
7. **How it works** — one architecture diagram, link to internals docs.
8. **Skills** — the killer feature; show a SKILL.md frontmatter example.
9. **Privacy model** — the wake-gate boundary; **this is the moat** — make it prominent.
10. **Configuration** — link to the `tend.toml` reference.
11. **Contributing** — link to `CONTRIBUTING.md`.
12. **License** — one line, link to `LICENSE`.

## Updating this document

When you change something covered here — a path convention, a shipped default, the webhook contract, a CLI command, a release tool — update this document in the same PR. Treat it as canonical. If implementation diverges from this doc, one of them is wrong; fix it before merging.
