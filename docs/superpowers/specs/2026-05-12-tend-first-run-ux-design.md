# First-Run UX — Design

Status: accepted
Date: 2026-05-12
Sub-project: ROADMAP #2 (after the renumber following #2 workspace-migration + #3 typer-cli shipping)

## Goal

A stranger who runs `pip install tend && tend setup` on a Raspberry Pi
should reach a working voice assistant in ~10 minutes without reading
documentation. After setup, `tend doctor` is the one-command
"is everything OK?" diagnostic, and `tend service install` makes the
daemon start at boot.

The four target commands:

1. `tend setup` — interactive bootstrap wizard.
2. `tend doctor [--json]` — read-only diagnostic; same check library as setup.
3. `tend skills {new, install, validate}` — skill scaffolding/management
   (alongside the existing `list/show/cat/rm/quarantined/enable-triggers/disable-triggers`).
4. `tend service install [--uninstall]` — systemd user unit writer (Linux only;
   macOS via launchd is sub-project #3).

## Naming note

The conventions doc speaks of `tend skill {new, install, list, validate}`
(singular). The existing shipped surface is `tend skills` (plural,
matching `$TEND_HOME/skills/`). To avoid breaking muscle memory, we
**keep the plural** and extend the existing sub-app with `new`,
`install`, and `validate`. Conventions doc gets updated to match what
shipped.

## Scope

In scope:
- All four commands listed above.
- Linux systemd user-unit install only. macOS launchd integration is
  explicitly deferred to sub-project #3 (macOS port). On macOS,
  `tend service install` prints a one-line "not yet implemented" message
  and exits 1.
- A pluggable check library (`tend.checks`) shared between setup and doctor.
- A keyring-first secret store (`tend.secrets`) with `.env` fallback.
- New runtime deps: `questionary`, `rich`, `keyring`.

Out of scope (deferred):
- macOS launchd plist (sub-project #3).
- HMAC/replay-protected webhook hardening (sub-project #4).
- Pluggable skill validators beyond the existing regex safety scanner +
  frontmatter parse.
- Network-reachability checks (slow, flaky, low signal — surface
  problems naturally on first turn instead).

## Architecture

### New modules

```
src/tend/
  checks.py            Pure check functions + Result dataclass
  secrets.py           keyring-first set/get/delete with $TEND_HOME/.env fallback
  service.py           systemd unit writer (Linux only)
  cli/
    setup.py           `tend setup` wizard
    doctor.py          `tend doctor` runner
    service.py         `tend service ...` sub-app
    skills.py          (existing) — extended with new/install/validate
  _defaults/
    systemd/
      tend.service.tmpl  Moved from deploy/tend.service
    skills/
      _template/         New-skill scaffolding template
        SKILL.md          Frontmatter + body with placeholders
```

The existing `src/tend/preflight.py` keeps its narrow purpose (boot-time
warnings). It gets folded into `tend.checks.claude_cli_check()` as a
thin caller; the public function stays for backward compatibility.

### Check library (`tend.checks`)

Each check is a pure function returning a `CheckResult`:

```python
from dataclasses import dataclass
from typing import Literal

CheckStatus = Literal["ok", "warn", "fail"]

@dataclass(frozen=True)
class CheckResult:
    name: str            # short label, e.g. "workspace"
    status: CheckStatus
    detail: str          # one-line human description of current state
    remediation: str | None = None  # what to do if not ok; None if status == "ok"
```

The 10 checks shipped in v1:

| # | name | What it verifies | warn vs fail |
|---|------|------------------|--------------|
| 1 | workspace | `$TEND_HOME` exists; `.tend-version` parses | fail if MISSING/UNCLAIMED/FUTURE_VERSION |
| 2 | config | `tend.config.Settings()` constructs without error | fail on validation error |
| 3 | anthropic_key | `ANTHROPIC_API_KEY` set and non-empty (env / .env / keyring) | fail |
| 4 | stt | Deepgram key OR local whisper model resolvable | fail if neither |
| 5 | tts | ElevenLabs key OR piper voice file resolvable | fail if neither |
| 6 | wake_model | openwakeword model file is on the model search path | fail |
| 7 | claude_cli | `claude --version` and `claude auth status` both 0 | fail |
| 8 | audio | PyAudio reports ≥1 input device and ≥1 output device | fail |
| 9 | gws_cli | `gws` on PATH and `gws auth status` 0 | warn (google features only) |
| 10 | webhook_token | `TEND_WEBHOOK_TOKEN` present | warn (proactive features only) |

`tend.checks.run_all(settings)` returns `list[CheckResult]`. Exit code
logic (used by `tend doctor`):

- 0 if every check is `ok`.
- 1 if at least one `warn` and no `fail`.
- 2 if any `fail`.

### Secret store (`tend.secrets`)

```python
def set_secret(key: str, value: str) -> Literal["keyring", "dotenv"]:
    """Store value under (service='tend', username=key). Returns the
    backend that accepted it. Tries keyring first; falls back to
    appending/replacing in $TEND_HOME/.env (mode 600) on
    keyring.errors.NoKeyringError or PasswordSetError."""

def get_secret(key: str) -> str | None:
    """env vars first → keyring → .env. Returns None if not found."""

def list_known_secrets() -> list[str]:
    """Return the canonical list of secret keys tend knows about."""
```

Canonical keys: `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`,
`ELEVENLABS_API_KEY`, `TEND_WEBHOOK_TOKEN`. The list lives in
`tend.secrets` to keep `tend.config` free of UX concerns.

### `.env` writes

`$TEND_HOME/.env` follows dotenv format (`KEY=value`, one per line).
Writes use a tmp-file-then-rename to avoid torn writes. On first write,
chmod the file to `0o600`. Idempotent: rewriting the same key replaces
the line; new keys append.

### Setup flow (`tend setup`)

`flutter doctor`-style: detect → ask only the gaps → write → verify.

Sequence:

1. **Workspace**: if `$TEND_HOME` is MISSING, create it; copy `soul.md`
   and core skill scripts via the existing
   `skill_update.install_soul()` / `install_workspace_bin()` primitives;
   write `.tend-version` to the current `tend.__version__`.
2. **STT**: ask "Cloud Deepgram (better) or local whisper (free)?"
   (questionary single-select). If Deepgram, prompt for `DEEPGRAM_API_KEY`
   (masked) and validate via 1 API call. If whisper, no key needed.
3. **TTS**: similar pattern. ElevenLabs (prompt key, validate with
   `/v1/voices/<id>` against the configured voice id) or Piper (no key).
4. **Anthropic**: prompt for `ANTHROPIC_API_KEY`. Validate via 1 call to
   `claude.messages.create` with `max_tokens=1`.
5. **Webhook token**: auto-generate via `secrets.token_urlsafe(32)` and
   show it once. Do not prompt — too easy to fat-finger.
6. **Optional skills**: show the list of shipped-but-not-installed skills
   (`paths.shipped_skills_dir()` minus `paths.user_skills_dir()`) as a
   questionary checkbox multi-select. Install via the existing
   `skill_update.install_optional_skills()`.
7. **Done**: print "Run `tend doctor` to verify, then
   `tend service install` to make tend start at boot."

Idempotent re-run: on each prompt, if the value is already set
(env > keyring > .env), show it as `[bracketed]` placeholder; pressing
Enter keeps it. This is questionary's native `default=...` shape.

Skip lists for non-interactive use: `tend setup --noninteractive` runs
through the flow but fails fast on any prompt where the value is unset.
Useful in CI; not the primary UX.

### Doctor flow (`tend doctor`)

```
$ tend doctor
Workspace status — $TEND_HOME = /home/pi/.tend
  ✓ workspace          version 0.1.0
  ✓ config             tend.toml parses; 4 worker overrides
  ✓ anthropic_key      ANTHROPIC_API_KEY present (keyring)
  ✓ stt                Deepgram key present (keyring)
  ✓ tts                ElevenLabs key present (.env)
  ✓ wake_model         hey_jarvis.tflite resolvable
  ✓ claude_cli         claude 2.6.0
  ✓ audio              1 input device, 1 output device
  ⚠ gws_cli            not on PATH — google skills will fail
                       remediation: npm i -g @googleworkspace/cli && gws auth login
  ✓ webhook_token      TEND_WEBHOOK_TOKEN present (.env)

1 warning, 0 failures.
```

With `--json`: emit a single JSON array of `{name, status, detail,
remediation}` objects to stdout, no human output. Suitable for
homelab dashboards.

Rich is used for icons (`✓`/`⚠`/`✗`) and color but degrades to plain
text when stdout isn't a TTY.

### Skill subcommands

#### `tend skills new <name>`

Scaffold `$TEND_HOME/skills/<name>/SKILL.md` from
`tend._defaults/skills/_template/SKILL.md`. The template body has
placeholders (`{{NAME}}`, `{{DESCRIPTION}}`); the command substitutes
`{{NAME}}` from the arg, prompts the user for the description (or uses
`--description`), and prompts for a trigger phrase example. Aborts with
exit 2 if the skill already exists.

Template body:

```markdown
---
name: {{NAME}}
description: {{DESCRIPTION}}
---

# {{NAME}}

## When to use

(One or two sentences describing the user-spoken cues that should
invoke this skill.)

## How to do it

(Steps. Reference scripts in `~/.tend/workspace/bin/` by their relative
name. Keep this section short and concrete.)
```

#### `tend skills install`

Interactive picker over shipped-but-not-installed skills. Uses
`skill_update.enumerate_installable_skills()` to compute the candidate
list and `skill_update.install_optional_skills(names)` to copy. With
`--all`, installs everything without prompting.

#### `tend skills validate <name>`

Two-stage check:
1. Frontmatter parses cleanly (via `tend.skills.parse_frontmatter`,
   which raises `SkillFrontmatterError` on malformed input — this is
   stricter than `enumerate_skills`, which swallows the error to keep
   one bad skill from breaking enumeration).
2. Regex safety scan is clean (via `tend.skills.scan_text`).

Same exit-code contract as `scan-skill`: 0 clean, 1 warnings only,
2 critical, 3 not found. Reuses existing code paths entirely — this
command is essentially "scan + frontmatter sanity" packaged together.

### Service install (`tend service install [--uninstall]`)

On Linux: render `tend._defaults/systemd/tend.service.tmpl` with two
substitutions (`{{PYTHON}}` → the current `sys.executable`,
`{{TEND_HOME}}` → `paths.tend_home()`) and write to
`~/.config/systemd/user/tend.service`. Run
`systemctl --user daemon-reload`. Print next-step instructions
(`systemctl --user enable --now tend`).

`--uninstall` removes the unit file and runs daemon-reload. If the
service is running, it's left untouched — uninstall doesn't stop it.

On macOS: print
"macOS service install is not yet implemented (sub-project #3)." and
exit 1.

Path collision: if the target file exists and differs from what we'd
write, prompt the user (questionary confirm); `--force` overrides.

### Existing `deploy/tend.service` → `_defaults/systemd/tend.service.tmpl`

The repo currently has `deploy/tend.service` as a static file. We move
it into the wheel under `_defaults/systemd/` and add the two template
placeholders. `deploy/` is removed since the service install now ships
inside the package.

## Dependencies

New runtime deps to add to `pyproject.toml`:

- `questionary>=2.0` — arrow-key prompts, validators, password masking.
- `rich>=13.0` — tables, icons, colored status output.
- `keyring>=24.0` — OS keyring access.

All three are popular, MIT/Apache-licensed, and have no runtime cost
when not in use (CLI-only imports gated inside the command modules).

## Test strategy

### Unit tests

- `tests/test_checks.py` — every check function with monkeypatched
  env vars, mocked subprocesses, and tmp_path-rooted filesystems.
  Each test makes one assertion about (status, detail substring,
  remediation presence).
- `tests/test_secrets.py` — keyring success path, keyring failure
  → .env fallback, .env round-trip, mode 600 enforcement, key
  replacement (no duplicate lines).
- `tests/test_service.py` — template rendering, unit-file path,
  daemon-reload invocation (mocked), `--uninstall`, macOS path.

### CLI tests

- `tests/test_cli_doctor.py` — CliRunner-driven; seed states with
  monkeypatch; assert on (rc, plain-text substring of output for
  human mode, parsed JSON for `--json`).
- `tests/test_cli_setup.py` — driven via questionary's test mode
  (`questionary.{select,text,password}.unsafe_ask` accepts canned
  responses via a context manager from `prompt_toolkit.input.defaults`).
  Each test injects a stdin script and asserts on the resulting
  filesystem state (`.env` contents, `.tend-version`, installed
  skills).
- `tests/test_cli_skill_new.py` — scaffold creates the right file
  with the right substitutions; refuses to overwrite.
- `tests/test_cli_skill_install.py` — picker installs from shipped;
  `--all` skips the prompt.
- `tests/test_cli_skill_validate.py` — clean / warn / critical / missing
  exit codes; reuses fixtures from `test_cli.py::test_scan_skill_*`.

### Integration smoke

A single end-to-end test under `tests/test_first_run_integration.py`:
spin up an isolated `TEND_HOME=tmp_path`, run setup with canned stdin,
then run doctor and assert exit 0. Slow (~5s); marked
`@pytest.mark.slow` so it can be filtered.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| questionary's test interface is undocumented in places. | We use `prompt_toolkit.input.create_pipe_input` directly. Existing pattern; well-supported. Worst case: monkeypatch the questionary call sites. |
| keyring on a headless Pi commonly raises NoKeyringError. | We've planned for it. The .env fallback is the documented path; surfaced explicitly in setup output. |
| Adding rich/questionary as runtime deps bloats the wheel. | Both are small (< 1 MB combined). They're CLI-only and imported lazily inside the command modules; the daemon doesn't pay for them. |
| Service install writes outside `$TEND_HOME`. | Conscious choice — systemd expects units at `~/.config/systemd/user/`. The path is hard-coded and `--uninstall` is symmetric, so blast radius is bounded. |
| API-key validation calls cost a fraction of a cent and could fail offline. | Validators are best-effort: a non-2xx response shows a warning but lets the user proceed. Skip entirely with `--no-validate`. |

## Acceptance

- `pip install tend && tend setup` on a fresh Pi produces a working
  workspace and brings `tend doctor` to all-green.
- `tend doctor` exits 0 only when all 10 checks are ok; 1 if only
  warnings; 2 if any failure.
- `tend service install` creates a working systemd user unit;
  `systemctl --user start tend` brings the daemon up. `--uninstall`
  removes the unit file.
- `tend skills new my-skill` scaffolds a skill that immediately
  shows up in `tend skills list`.
- `tend skills validate my-skill` exits 0 on a clean template,
  2 on a critical finding.
- Every check function has at least one unit test.
- Setup is driven by canned questionary input in at least one
  end-to-end test.
