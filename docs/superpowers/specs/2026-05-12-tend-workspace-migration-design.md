# tend workspace migration

## Goal

Make `pip install tend && tend setup && tend` work. Today, tend reads `tend.toml`, `soul.md`, and the `skills/` directory from the current working directory (which only resolves correctly when running from a git clone), and seeds skills from `Path(__file__).parents[2] / "skills"` (which only resolves inside the repo, not inside a site-packages install). After this change, tend is a fully pip-installable package whose user state lives in `$TEND_HOME` (default `~/.config/tend/`), and whose default content (persona, seed skills, helper scripts) ships inside the wheel.

This is the prerequisite for sub-projects #3 (Typer CLI migration), #4 (first-run UX), and #5 (macOS port) in `ROADMAP.md`.

## Why now

`pip install tend` is currently broken in four places:

- `config.py:81` — `env_file=".env"` resolves from CWD.
- `config.py:83` — `toml_file="tend.toml"` resolves from CWD.
- `config.py:116` — `soul_path: str = "soul.md"` resolves from CWD.
- `main.py:85,111` — `_REPO_SKILLS_DIR = Path(__file__).parents[2] / "skills"` and `_REPO_WORKSPACE_BIN` resolve to the repo root in dev, but to `<venv>/lib/python3.13/` in a pip install (no `skills/` or `workspace/` directories there).

Plus eight hardcoded `Path.home() / ".tend"` callsites in `main.py` that pin the workspace to a non-standard, non-overridable location.

Without fixing these, publishing a public release means publishing something that immediately fails for a stranger trying it. Workspace migration is therefore the gating item for the v0.1 public-release critical path.

## Decisions made during brainstorming

1. **Single-folder workspace.** All user-touchable state lives under `$TEND_HOME` (default `~/.config/tend/` on Linux and macOS). No platformdirs split between config/data/state/cache/logs. One env var collapses or relocates everything. Logs go under `$TEND_HOME/logs/`. (Section 1.)
2. **Two-tier shipping.** A small set of "critical" skills (`heartbeat`, `schedule-watcher`) lives inside the wheel and is enumerated at runtime from there — never copied to the user workspace. Optional shipped skills (`briefing`, `mail-triage`, `meeting-prep`, etc.) also live in the wheel, and are copied into the workspace during `tend setup` based on user selection. (Sections 2, 4.)
3. **Workspace is fully user-owned.** Once installed in `$TEND_HOME/skills/<name>/`, a skill is the user's to edit, delete, or fork. No "fork before edit" friction. (Sections 2, 3.)
4. **Destructive updates with a single rolling backup.** `tend skill update` overwrites user-edited skills, but always backs them up first to `$TEND_HOME/skills-backup/`. Single rolling snapshot, not timestamped history — keeps the workspace tidy. (Section 5.)
5. **No shipped `tend.toml`.** Python class defaults on `Settings` are the authoritative baseline. The user's `$TEND_HOME/tend.toml` is the only TOML loaded at runtime, written by `tend setup` with their selections. (Section 6.)
6. **`.tend-version` marker** at the workspace root is the "is this workspace initialized?" signal. `tend` (no args) refuses to boot if it's missing. (Section 3.)
7. **`tend setup` handles all three workspace states.** Missing → fresh wizard. Populated-but-unclaimed → adopt mode (treats existing files as authoritative). Initialized → amend mode (bracketed-default prompts). No special `--claim` flag. (Section 3.)
8. **No separate migration doc.** The maintainer's one-time `~/.tend/` → `~/.config/tend/` move is part of the implementation work, executed during deployment. (Section 7.)

## Design

### 1. Path resolution

A new `src/tend/paths.py` module is the single source of truth for every filesystem path tend touches. Every callsite that currently does `Path.home() / ".tend" / ...` or `Path(__file__).parents[2] / ...` flows through these functions. Tests redirect via `monkeypatch.setenv("TEND_HOME", str(tmp_path))`.

```python
# src/tend/paths.py

def tend_home() -> Path:
    return Path(os.environ.get("TEND_HOME") or Path.home() / ".config" / "tend")

# User-side (anchored at tend_home())
def user_skills_dir() -> Path: ...      # $TEND_HOME/skills/
def workspace_bin_dir() -> Path: ...    # $TEND_HOME/workspace/bin/
def soul_path() -> Path: ...            # $TEND_HOME/soul.md
def env_path() -> Path: ...             # $TEND_HOME/.env
def toml_path() -> Path: ...            # $TEND_HOME/tend.toml
def log_path() -> Path: ...             # $TEND_HOME/logs/tend.log
def fault_log_path() -> Path: ...       # $TEND_HOME/logs/tend.faults.log
def cron_root() -> Path: ...            # $TEND_HOME/cron/
def sessions_dir() -> Path: ...         # $TEND_HOME/sessions/
def skills_backup_root() -> Path: ...   # $TEND_HOME/skills-backup/
def version_marker_path() -> Path: ...  # $TEND_HOME/.tend-version

# Wheel-side (importlib.resources)
def critical_skills_dir() -> Path: ...
def shipped_skills_dir() -> Path: ...
def shipped_soul_md() -> Path: ...
def shipped_workspace_bin() -> Path: ...

# Resolved helpers
def read_soul() -> str: ...             # user → shipped → embedded fallback
```

**Default location:** `~/.config/tend/` on both Linux and macOS. Override: set `$TEND_HOME` to any absolute path. No `$XDG_CONFIG_HOME` respect — one knob, one source of truth, no surprises.

**Dev mode:** running tend from a clone uses `TEND_HOME=$PWD/.tend-dev` in the dev shell. No auto-detection. `.tend-dev/` goes in `.gitignore`.

**Logs:** `$TEND_HOME/logs/tend.log` (loguru, 5 MB rotation) and `$TEND_HOME/logs/tend.faults.log` (faulthandler). `/tmp/tend.log` and `settings.log_path` both go away.

**Wheel-side functions return `Path`** even though `importlib.resources.files()` returns `Traversable`. `critical_skills_dir()` (and the other wheel-side functions) resolve to:

- The on-disk path if the wheel is unzipped (pip/pipx/uv common case).
- A tempdir materialization via `importlib.resources.as_file()` if the wheel is still zipped, cached for the process lifetime with `atexit` cleanup.

**Workspace CLI surface** (Typer wiring lands in sub-project #3; this spec defines the contract):

- `tend workspace path` — prints `$TEND_HOME` resolution, one line, machine-readable. Useful for `cd "$(tend workspace path)"`.
- `tend workspace info` — human-readable: location, how it was resolved (default vs env var), size on disk, skill counts, soul.md mtime, last log entry timestamp, backup presence + mtime.
- `tend workspace move <new-path>` — relocates:
  1. Validate `<new-path>` doesn't exist or is empty.
  2. `shutil.copytree($TEND_HOME, <new-path>)`, preserving perms.
  3. Detect how `$TEND_HOME` is currently set; offer to update shell rc (~/.bashrc or ~/.zshrc), systemd user unit's `Environment=` directive, or launchd plist's `EnvironmentVariables` key.
  4. Refuse to run if tend is up: hits the loopback webhook at `127.0.0.1:<port>/`; if it responds, prints "tend is running; stop the service first" and exits. (The lock signal that's already there.)
  5. Ask before removing the old location. Default: keep, so the user can verify the new location before nuking.

### 2. Shipped defaults layout (`_defaults/`)

Everything currently at the repo root that's "shipped content" moves into `src/tend/_defaults/` so it ships in the wheel.

```
src/tend/_defaults/
├── critical-skills/         ← runtime-enumerated from the wheel; never copied
│   ├── heartbeat/SKILL.md
│   └── schedule-watcher/    (full directory: SKILL.md + bin/run-tick.sh + …)
├── skills/                  ← offered during `tend setup`
│   ├── briefing/
│   ├── lunch-prep/
│   ├── mail-triage/
│   ├── meeting-prep/
│   ├── post-deep-work/
│   ├── routine-setup/
│   └── schedule-block/
├── workspace/
│   └── bin/                 ← bulk-copied to $TEND_HOME/workspace/bin/ during setup
└── soul.md                  ← offered during setup; runtime fallback if $TEND_HOME/soul.md is absent
```

**Repo-root files that go away:**

- `tend.toml` — deleted entirely. Python class defaults on `Settings` are the source of truth; `$TEND_HOME/tend.toml` is the only TOML at runtime.
- `soul.md` — moves to `src/tend/_defaults/soul.md`.
- `skills/` — splits: `heartbeat` and `schedule-watcher` → `_defaults/critical-skills/`; the rest → `_defaults/skills/`.
- `workspace/bin/` — moves to `src/tend/_defaults/workspace/bin/`.

**Code paths that go away:**

- `main.py:56-74` — inline `HEARTBEAT_SKILL_BODY` string. Heartbeat becomes a normal file in `_defaults/critical-skills/heartbeat/SKILL.md`.
- `main.py:87-96` — hardcoded `REPO_SKILL_NAMES` tuple. New shipped skills are discovered by iterating `_defaults/skills/`; no code change required to add one.
- `main.py:85,111` — `_REPO_SKILLS_DIR`, `_REPO_WORKSPACE_BIN`. Replaced by `paths.shipped_skills_dir()` and `paths.shipped_workspace_bin()`.
- `main.py:99-132` — the `_seed_*` helpers (`_seed_heartbeat_skill`, `_seed_skill_from_repo`, `_seed_workspace_bin`). Replaced by the setup-driven install flow (section 3) and the update flow (section 5).

**Packaging:**

```toml
# pyproject.toml — setuptools today; will become hatchling per conventions.md
[tool.setuptools.package-data]
"tend._defaults" = ["**/*"]
```

Hatchling equivalent (for the eventual migration):

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/tend/_defaults" = "tend/_defaults"
```

A new `tests/test_packaging.py` builds a wheel, unzips it, and asserts every expected `tend/_defaults/...` path is present. Runs on every PR. Catches "I added a default skill but forgot to update `package-data`."

**Critical-skill scripts at runtime.** Wheels installed by pip/pipx/uv are unzipped on disk, so `importlib.resources.files("tend._defaults") / "critical-skills" / "schedule-watcher" / "bin" / "run-tick.sh"` resolves to a real absolute path the brain can invoke directly. For the rare zipped-wheel case, `paths.critical_skills_dir()` lazily materializes the tree to a tempdir at first access and caches the result.

### 3. First-launch behavior and setup contract

`$TEND_HOME/.tend-version` is the workspace-initialized marker. Plain text containing the tend version (e.g. `0.1.0`) that initialized or last migrated the workspace. The runtime never inspects skills/, tend.toml, etc. to decide initialization state — just this marker.

**Three workspace states tend can detect at boot:**

| State | Detection | `tend` (no args) | `tend setup` |
|---|---|---|---|
| Missing | `$TEND_HOME` doesn't exist | "Run `tend setup`" + exit 1 | Fresh wizard, creates everything |
| Populated but unclaimed | exists, no `.tend-version` | "Workspace at … has files but isn't initialized. Run `tend setup` to adopt it." + exit 1 | **Adopt mode** — treats existing files as authoritative; asks only for things missing; writes `.tend-version` last |
| Initialized | `.tend-version` parseable and ≤ current | Boots normally | **Amend mode** — bracketed-default prompts (aws-configure style) |
| Future version | `.tend-version` > current | Refuses to boot; user upgrades tend or picks a different `$TEND_HOME` | Same |

No auto-creation, no auto-launching the wizard. Matches `gh auth login`, `aws configure`, `sentry-cli login` — explicit beats magical.

**`tend setup`'s contract with the workspace** (the wizard's UX lives in sub-project #4; from this spec's perspective, these are the outputs other consumers can rely on):

1. Create `$TEND_HOME/` if missing.
2. For each skill the user picks from `paths.shipped_skills_dir()`: `shutil.copytree(_defaults/skills/<name>, $TEND_HOME/skills/<name>)`.
3. Bulk-copy `_defaults/workspace/bin/*` → `$TEND_HOME/workspace/bin/` (plumbing, no granular pick).
4. Optionally copy `_defaults/soul.md` → `$TEND_HOME/soul.md` (wizard offers "use shipped / fork to edit / skip"). If skipped, runtime falls back to reading from the wheel via `paths.read_soul()`.
5. Write `$TEND_HOME/tend.toml` with any non-default values the user picked.
6. Write `$TEND_HOME/.env` with API keys (when OS keyring isn't available).
7. **Last** — write `$TEND_HOME/.tend-version` with current tend version. **Failure mid-setup leaves no marker**, so the next run treats the workspace as uninitialized (or as adopt-mode if files exist).

**Adopt mode** (populated but unclaimed) is what handles the maintainer's one-time migration. The wizard detects `$TEND_HOME/skills/<name>/` already exists and skips the "do you want briefing?" prompt for that skill. Same for `soul.md` and `workspace/bin/`. Asks only for missing things. Writes `.tend-version` at the end.

**Schema versioning.** When v0.2 ships a workspace layout change, a `paths.migrate_workspace(from_v, to_v) -> None` function runs at boot when `.tend-version < current`. For v0.1 this is a no-op stub.

### 4. Runtime skill enumeration

**Two roots, one merged catalog.**

- `paths.critical_skills_dir()` — wheel's `_defaults/critical-skills/` (always-on)
- `paths.user_skills_dir()` — `$TEND_HOME/skills/` (user-installed + user/LLM-authored)

A user skill with the same name as a critical skill shadows it. The runtime logs a warning at startup but doesn't refuse — escape hatch for overriding a misbehaving critical skill.

**Three skill catalogs the codebase needs, each with a different audience:**

| Catalog | What it contains | Reader |
|---|---|---|
| Critical | `_defaults/critical-skills/` | Internal only (merged into Runtime below) |
| Runtime | critical ∪ user, user wins on name collision | Brain's `available-skills` XML; scheduler event-subscriber lookup; webhook event fan-out; GeneralWorker's catalog |
| Available-to-install | `_defaults/skills/` minus what's already in `$TEND_HOME/skills/` | `tend setup` wizard; `tend skill list --available`; `tend skill install <name>` |

**New entry points in `skills.py`** (the existing `enumerate_skills(root)` and `find_event_subscribers(root, kind)` stay — useful as low-level primitives for any of the three catalogs):

```python
def enumerate_all_skills() -> list[SkillInfo]:
    """Runtime catalog: critical + user, user wins on name collision."""
    critical = enumerate_skills(paths.critical_skills_dir())
    user = enumerate_skills(paths.user_skills_dir())
    by_name = {s.name: s for s in critical}
    for s in user:
        if s.name in by_name:
            logger.warning(
                f"user skill {s.name!r} at {s.path} shadows critical skill "
                f"at {by_name[s.name].path}"
            )
        by_name[s.name] = s
    return sorted(by_name.values(), key=lambda s: s.name)

def find_event_subscribers_all(kind: str) -> list[SkillInfo]:
    return [s for s in enumerate_all_skills() if kind in s.events]

def enumerate_installable_skills() -> list[SkillInfo]:
    """Shipped optional skills not yet present in user dir."""
    shipped = enumerate_skills(paths.shipped_skills_dir())
    user_names = {s.name for s in enumerate_skills(paths.user_skills_dir())}
    return [s for s in shipped if s.name not in user_names]
```

**Callers that drop their `skills_root` argument** and use the new helpers internally:

- `Hub` (main.py:225) — uses `enumerate_all_skills()` for the brain's available-skills XML
- `Scheduler` (main.py:199) — uses `find_event_subscribers_all(kind)` at cron dispatch
- `webhook.build_app` (main.py:251) — uses `find_event_subscribers_all(kind)` for `/event` fan-out
- `GeneralWorker` — currently reads via `WorkerConfig.skills_dir` with fallback; switches to `enumerate_all_skills()`

**`WorkerConfig.skills_dir` field deleted.** No remaining use case after the dual-root model; the per-worker skill-directory override was an unused affordance.

**Safety-scanner scope is unchanged.** The scanner (`skills.scan_text` + the LLM-authoring flow) only runs on content arriving in `$TEND_HOME/skills/`. Critical skills are trusted by the release process.

### 5. Update flow

**Trigger:** `tend skill update`. Also offered as part of `tend setup` re-runs.

**The flow surfaces two cohorts:**

1. **Installed skills whose current bytes differ from shipped bytes** (covers both "user edited" and "shipped version changed" — the backup makes the distinction unnecessary).
2. **Newly-shipped skills** not yet in `$TEND_HOME/skills/`.

**Concrete UX:**

```
$ tend skill update
Checking for skill updates...

The following installed skills will be OVERWRITTEN:
  ~ briefing       (current differs from shipped)
  ~ mail-triage    (current differs from shipped)

The following new skills are available to install:
  + weather

A backup of overwritten skills will be saved to:
  /home/pi/.config/tend/skills-backup/

⚠ This replaces any previous backup (last one taken 2026-05-12T14:30:00).
  If you need the previous backup, move it elsewhere first.

Continue? [y/N]: y

  ✓ Backup written to skills-backup/
  ✓ Updated briefing
  ✓ Updated mail-triage
  ? Install weather? [y/N]: y
  ✓ Installed weather

Updated 2 skills, installed 1 new skill. Backup at skills-backup/.
To restore: rm -rf skills/<name> && mv skills-backup/<name>/ skills/<name>/
```

**Backup layout — single rolling snapshot:**

```
$TEND_HOME/skills-backup/
├── briefing/                          (verbatim copy of pre-update user dir)
├── mail-triage/
└── .restore-manifest.json             { "tend_version": "0.2.0", "backed_up_at": "2026-05-12T14:30:00", "skills": [...] }
```

The directory path is fixed — no timestamp subdir. Each update run that backs up anything wipes and replaces the directory atomically (write to `skills-backup.tmp/`, then `mv` to `skills-backup/`).

**Behavior nuances:**

- **Update finds nothing to back up?** Existing `skills-backup/` is left untouched. The "latest meaningful backup" stays preserved even if `tend skill update` is run repeatedly on a static system.
- **Update has things to back up?** Existing `skills-backup/` is wiped first; new one written atomically.
- **Previous-backup warning only shows if one exists** — first-time updates aren't cluttered with it.
- **`--yes` flag** skips all prompts (back up everything that differs; install no new skills unless `--install-new` is also passed). Useful for CI / automation.

**Deliberate simplifications for v0.1:**

- **No hash-based modification tracking.** Any installed skill that differs from shipped gets backed up before overwrite. Cost: occasional unnecessary backup of a skill the user didn't touch but where the shipped version simply changed. Fine — backups are cheap.
- **Restore is manual.** Two shell commands documented in the output. Promote to `tend skill restore <name>` later if recovery becomes a friction point.
- **All-or-nothing on the batch.** No per-skill picker in v0.1. Add later if requested.

**What's never touched by the update flow:**

- User-authored skills (any name not in the shipped catalog). `meal-plan/` and any LLM-authored skill survives, untouched.
- Critical skills — they version with tend, can't be updated independently.
- `$TEND_HOME/workspace/bin/` scripts — same logic could apply, but defer to a follow-up (`tend bin update`) if it becomes necessary. For v0.1, bin scripts are user-owned after the initial copy.

### 6. Configuration loading

`src/tend/config.py` changes:

```python
from tend.paths import env_path, toml_path

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TEND_",
        extra="ignore",
        # env_file / toml_file resolved dynamically — see settings_customise_sources
    )

    # all current fields stay …
    # REMOVED: soul_path  (moves to paths.soul_path() + paths.read_soul())
    # REMOVED: log_path   (moves to paths.log_path() + paths.fault_log_path())

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings,
    ):
        from pydantic_settings import DotEnvSettingsSource, TomlConfigSettingsSource
        dotenv = DotEnvSettingsSource(settings_cls, env_file=str(env_path()))
        toml = TomlConfigSettingsSource(settings_cls, toml_file=str(toml_path()))
        return (init_settings, env_settings, dotenv, toml, file_secret_settings)

settings = Settings()  # process-wide read-only snapshot — same as today
```

**Precedence (high → low):**

1. `init_settings` — kwargs passed to `Settings(...)` (used by tests and injection patterns).
2. `env_settings` — environment variables (`TEND_*`) + vendor SDK aliases (`ANTHROPIC_API_KEY`, etc., via existing `AliasChoices`).
3. `dotenv` — `$TEND_HOME/.env` (silently skipped if absent).
4. `toml` — `$TEND_HOME/tend.toml` (silently skipped if absent).
5. `file_secret_settings` — Docker-style secrets dir, kept for parity.
6. Python class defaults — the source of truth for "what tend ships with as defaults."

**No shipped TOML.** Class defaults ARE the shipped baseline. Saves one source in the chain and removes the "where are the defaults?" ambiguity.

**`soul.md` resolution moves to `paths.py`:**

```python
DEFAULT_SOUL_FALLBACK = "You are a helpful voice assistant. Keep replies brief, conversational, plain prose."

def read_soul() -> str:
    """User soul.md if present, else shipped default, else hard-coded fallback."""
    user = soul_path()
    if user.exists():
        return user.read_text(encoding="utf-8")
    try:
        return shipped_soul_md().read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return DEFAULT_SOUL_FALLBACK
```

`SessionManager._read_soul` becomes a one-liner calling `paths.read_soul()`. `SessionManager.__init__` drops the `soul_path` kwarg. `main.py:228` no longer passes it. `session.DEFAULT_SOUL` moves to `paths.DEFAULT_SOUL_FALLBACK`.

**Logging:** `main.py:_setup_logging` reads `paths.log_path()` and `paths.fault_log_path()` instead of `settings.log_path`. Replaces lines 45-49.

**Module-level `settings = Settings()` stays.** Read-only snapshot — the one allowed exception to "no module-level singletons" in CLAUDE.md. Tests construct `Settings(field=value, …)` directly via init kwargs (current pattern, preserved).

### 7. Migration, testing, and out-of-scope

**Maintainer-side migration** is part of the implementation work, not a permanent doc. The implementation PR includes:

- **Code and repo changes** (committed): `paths.py`, `$TEND_HOME` everywhere, dual-root enumeration, `_defaults/` layout, deletion of repo-root `tend.toml`/`soul.md`/`skills/`/`workspace/`, test updates.
- **One-time shell ops on the Pi** (executed during deployment, not committed):

  ```bash
  systemctl --user stop tend 2>/dev/null || true
  mv ~/.tend ~/.config/tend
  rm -rf ~/.config/tend/skills/heartbeat ~/.config/tend/skills/schedule-watcher
  mv tend.toml ~/.config/tend/tend.toml
  pipx install -e .   # or whichever install command we settle on
  tend setup          # adopt mode kicks in; writes .tend-version
  systemctl --user start tend
  ```

These steps live in the implementation plan (`docs/superpowers/plans/...`), not in a persistent doc.

**Testing strategy:**

- **`tests/test_paths.py`** — unit tests on `paths.py`:
  - `test_tend_home_default` — env unset → `~/.config/tend`
  - `test_tend_home_env_override` — `TEND_HOME=/tmp/x` honored
  - `test_user_skills_dir_anchored` — derived correctly from `tend_home()`
  - `test_critical_skills_dir_returns_real_path` — works even with wheel zipped
  - `test_critical_skills_dir_caches` — single materialization per process
  - `test_read_soul_user_wins` — user soul.md preferred over shipped
  - `test_read_soul_shipped_fallback` — missing user → reads shipped
  - `test_read_soul_hardcoded_fallback` — both missing → returns `DEFAULT_SOUL_FALLBACK`
- **`tests/test_workspace_lifecycle.py`** — boot-state integration (each uses `monkeypatch.setenv("TEND_HOME", str(tmp_path))`):
  - `test_tend_no_workspace_exits_with_message`
  - `test_tend_populated_no_marker_prompts_adopt`
  - `test_tend_initialized_boots` — `.tend-version` present → boots into pipeline construction (services mocked)
  - `test_version_marker_written_last` — injected failure mid-setup leaves no marker
- **`tests/test_skills_enumeration.py`**:
  - `test_critical_only` — empty user dir → runtime catalog == critical skills
  - `test_user_alongside_critical` — disjoint names → result is union
  - `test_user_shadows_critical` — same-name dir in both → user wins, warning logged
  - `test_installable_excludes_installed` — shipped catalog minus user skills
- **`tests/test_skill_update.py`**:
  - `test_unchanged_skill_no_backup` — current bytes == shipped bytes → flow skips
  - `test_modified_skill_backed_up_and_overwritten`
  - `test_new_shipped_skill_offered_as_install`
  - `test_user_skill_not_in_shipped_untouched` — meal-plan analogue survives
  - `test_critical_skill_excluded` — heartbeat/schedule-watcher never in diff
  - `test_yes_flag_skips_prompts`
  - `test_yes_with_install_new_includes_new_skills`
  - `test_backup_replaced_on_subsequent_update`
  - `test_no_changes_preserves_previous_backup`
- **`tests/test_packaging.py`** — build a wheel, unzip, assert every expected `tend/_defaults/...` path is present. Runs on every PR.
- **Existing tests** — `grep -rn 'Path.home() / ".tend"' tests/` finds ~15 files. All replaced with `monkeypatch.setenv("TEND_HOME", str(tmp_path))`. Mechanical change.

**Out of scope for this spec:**

- **Setup wizard UX** (Typer subcommands, questionary prompts, audio device picker, smoke-test flow) → sub-project #4 (First-run UX).
- **Service install** (systemd user unit, launchd plist generation) → sub-project #4.
- **`tend skill {list,install,update}` and `tend workspace …` Typer wiring** — this spec defines their *behavior contracts*; the actual CLI wiring lands with sub-projects #3 (Typer migration) and #4.
- **macOS-specific path handling** — the same `~/.config/tend/` default applies cross-platform; TCC + launchd are sub-project #5.
- **Webhook auth changes** — sub-project #6.

**What this spec does establish:** every primitive the other sub-projects depend on — `paths.tend_home()`, `paths.user_skills_dir()`, `paths.critical_skills_dir()`, `paths.read_soul()`, `enumerate_all_skills()`, `find_event_subscribers_all()`, `enumerate_installable_skills()`, the `.tend-version` marker semantics, and the backup directory layout. Once this lands, #3-#6 can proceed in any order without dependency conflicts.

## Risks / open questions

None outstanding. Implementation plan to follow.
