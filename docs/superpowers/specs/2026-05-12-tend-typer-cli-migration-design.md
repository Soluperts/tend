# Typer CLI Migration — Design

Status: accepted
Date: 2026-05-12
Sub-project: ROADMAP #3 (prerequisite for #4 First-run UX)

## Goal

Replace tend's hand-rolled argparse CLI with a Typer-based one, without
changing the user-facing surface. The migration unblocks future
subcommand trees (`setup`, `doctor`, `service install`, `workspace`,
`skill {new,install,validate}`) by giving them a clean place to grow.

Estimated effort: ~½ day, matching the ROADMAP entry.

## Out of scope

Anything that is not a mechanical port. Explicitly excluded:

- New subcommands (`setup`, `doctor`, `service`, `workspace`, etc.) —
  those land in sub-project #4.
- Interactive prompts (`questionary`) and rich output (`rich`) —
  those land in #4 with `setup`/`doctor`.
- `audio-check` promotion from `scripts/audio_check.py` — also #4.
- Webhook hardening (HMAC, replay) — that's #6.

If a change is not strictly required to swap argparse for Typer or to
fix consistency issues the swap exposes, defer it.

## Constraints

1. **Surface preservation.** Every existing invocation continues to
   work verbatim:
   - `tend sessions {list,show,tail,cat}` with all current flags
     (`--limit`, `--status`, `--json`, `--follow`, `--raw`).
   - `tend skills {list,show,cat,rm,quarantined,enable-triggers,disable-triggers}`.
   - `tend scan-skill <name>` (top-level, not under `skills`).
   - `tend schedule {list,show,add,rm}` with all `add` flags
     (`--when`, `--name`, `--tz`, `--request`, `--event`, `--payload`,
     `--source`).
   - `tend webhook test`.
   - `tend snapshot`.
2. **Exit-code preservation.** `scan-skill` keeps its 0/1/2/3 contract.
   Other commands keep 0 for success and non-zero for failure. The
   specific non-zero codes documented today (1 for "not found", 2 for
   "user error") are preserved.
3. **Test entry preserved.** `tend.cli.main(argv: list[str] | None) -> int`
   stays callable from tests. Existing tests in `tests/test_cli.py` and
   `tests/test_cli_schedule.py` keep working after a small mechanical
   update for `pytest.raises(SystemExit)` cases (see below).
4. **No new runtime deps beyond Typer.** Specifically: do not pull in
   `rich` or `questionary` yet.

## Architecture

### Module layout

The current single `src/tend/cli.py` (845 lines) becomes a package:

```
src/tend/cli/
  __init__.py        Typer root app; exports main(argv) → int
  _shared.py         Small helpers (_ago, _validate_skill_name, _resolve_session,
                     _print_event). Used by multiple submodules.
  sessions.py        `tend sessions ...` sub-app
  skills.py          `tend skills ...` sub-app  +  top-level scan_skill_command
  schedule.py        `tend schedule ...` sub-app
  webhook.py         `tend webhook ...` sub-app
  snapshot.py        `tend snapshot` command
```

Each submodule constructs its own `typer.Typer()` instance, registers
its commands as functions with type-annotated parameters, and exports
the sub-app. `__init__.py` builds the root app and calls
`app.add_typer(sessions_app, name="sessions")` etc.

Why split: today's `cli.py` is the largest file in the project and is
about to grow further in sub-project #4 (4–6 new subcommand trees).
Splitting now gives each command group a focused home; each unit fits
in context for editing and review. This satisfies CLAUDE.md's "each
unit testable in isolation" principle.

### Entry point

`pyproject.toml`'s `[project.scripts]` keeps `tend = "tend.cli:main"`.
The new `tend/cli/__init__.py` exports a `main` function with the same
signature as today:

```python
def main(argv: list[str] | None = None) -> int:
    try:
        app(args=argv, standalone_mode=False, prog_name="tend")
        return 0
    except click.exceptions.Exit as e:
        return e.exit_code
    except click.exceptions.UsageError as e:
        # Typer prints its own usage error to stderr and exits with 2.
        e.show()
        return e.exit_code or 2
```

`standalone_mode=False` is what lets `main()` return an integer instead
of calling `sys.exit()`. Tests can call `main(["sessions", "list"])`
and inspect the rc the same way they do today.

### Command implementation pattern

Each command becomes a typed function. Example:

```python
# tend/cli/sessions.py
import typer
from typing import Optional

app = typer.Typer(no_args_is_help=True, help="Inspect worker sessions.")

@app.command("list")
def list_(
    limit: int = typer.Option(10, "--limit", help="Max sessions to show."),
    status: Optional[str] = typer.Option(
        None, "--status",
        click_type=typer.Choice(["running", "done", "failed", "killed"]),
        help="Filter by status.",
    ),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    rows = _store().list_recent(limit=limit)
    if status:
        rows = [r for r in rows if r.status == status]
    if json_:
        typer.echo(json.dumps([r.__dict__ for r in rows], indent=2, default=str))
        return
    # ... render table
```

Error paths use `raise typer.Exit(code=N)` instead of `sys.exit(N)`. This
unifies the exit-code mechanism across the CLI and integrates cleanly
with `standalone_mode=False`.

The `scan-skill` top-level command is registered directly on the root
app via `@app.command("scan-skill")`, not inside the `skills` sub-app,
preserving today's invocation `tend scan-skill <name>`.

## Env-var cleanup

The existing CLI uses three legacy env vars that predate sub-project #2
(workspace migration):

- `TEND_ROOT` → workspace root.
- `TEND_SKILLS_ROOT` → user skills dir.
- `TEND_SKILLS_QUARANTINE_ROOT` → quarantine dir.

The daemon now uses `tend.paths` (which reads `TEND_HOME`). The CLI
should agree. As part of #3:

- Remove the three `_default_root`/`_skills_root`/`_skills_quarantine_root`
  helpers from the CLI.
- Replace every call site with `tend.paths.{tend_home, user_skills_dir,
  skills_quarantine_root}`.
- Add `paths.skills_quarantine_root()` if it doesn't exist yet (today
  it lives only inline in the CLI).
- Drop the `TEND_SKILLS_ROOT` / `TEND_SKILLS_QUARANTINE_ROOT` env vars
  entirely. `TEND_HOME` is the single override.
- Drop the `Environment variables:` epilog block since the doc is now
  out of date.

Tests get migrated from `monkeypatch.setenv("TEND_ROOT", ...)` and
`monkeypatch.setenv("TEND_SKILLS_ROOT", ...)` to `TEND_HOME`.

## Test strategy

### Continuity

`tests/test_cli.py` and `tests/test_cli_schedule.py` already exercise
the CLI by calling `main(argv)` and inspecting `capsys` output. They
keep working after the migration with two mechanical fixes:

1. **Env-var migration.** Three tests use `TEND_ROOT`; ~ten use
   `TEND_SKILLS_ROOT`; two use `TEND_SKILLS_QUARANTINE_ROOT`. All
   become `TEND_HOME` (with the skills/quarantine subdirs implied by
   `paths.user_skills_dir()` and `paths.skills_quarantine_root()`).

2. **SystemExit → rc.** Three tests use `pytest.raises(SystemExit)`:
   - `test_cli_sessions_show_unknown_returns_nonzero`
   - `test_skills_invalid_name_rejected`
   - `test_scan_skill_invalid_name`

   With `standalone_mode=False`, `main()` returns an int instead of
   exiting. These three tests update to:
   ```python
   rc = main(["sessions", "show", "zzzz"])
   assert rc == 1
   ```

   The argparse-era `_validate_skill_name` raised `SystemExit` directly
   on bad input; that becomes `typer.Exit(code=2)`.

3. **Monkeypatch path.** `test_cli_snapshot_writes_file` does
   `monkeypatch.setattr(cli, "_default_root", lambda: root)`. After
   restructure, `_default_root` no longer exists. The test changes to
   `monkeypatch.setattr(cli.snapshot, "_tend_home", lambda: root)`
   (or whatever the equivalent helper is named in the new module),
   or — better — to `monkeypatch.setenv("TEND_HOME", str(root))` which
   takes effect at every `paths.tend_home()` call.

### New tests

No new test surface. Existing tests already cover every command path;
the migration is mechanical.

## Migration plan summary (details belong in the implementation plan)

1. Add `typer>=0.12` to `pyproject.toml` dependencies.
2. Create `src/tend/cli/` package with the six submodules from the
   layout above. Each submodule's functions copy the body of the
   corresponding `cmd_*` function from today's `cli.py`, with
   `args.foo` lookups replaced by named parameters and `sys.exit(N)`
   replaced by `raise typer.Exit(code=N)`.
3. Add `paths.skills_quarantine_root()` if missing.
4. Replace `_default_root`/`_skills_root`/`_skills_quarantine_root`
   call sites with `tend.paths` equivalents.
5. Delete the old `src/tend/cli.py`. (The `cli/` package takes its
   import path.)
6. Update `tests/test_cli.py` and `tests/test_cli_schedule.py` for the
   env-var / `SystemExit` changes above.
7. Run the full test suite — should be green.
8. Smoke-test on the user's Pi: `tend sessions list`, `tend skills
   list`, `tend schedule list`, `tend snapshot`, `tend webhook test`
   (with `TEND_WEBHOOK_TOKEN` set), `tend --help`, `tend sessions
   --help`.

## Risks and how they're handled

| Risk | Mitigation |
|---|---|
| Typer's auto-generated `--help` differs cosmetically from argparse's. | Acceptable. Help text is for humans; cosmetic differences don't break users. We do not assert on help text in tests. |
| `pytest.raises(SystemExit)` tests break. | Three named tests updated to assert on the returned rc instead. |
| Sub-projects #4 wants to add `tend setup` etc. while #3 is in flight. | #3 is small (~½ day). #4 starts after #3 merges. No overlap risk. |
| `typer.Choice` for `--status` rejects values the old argparse accepted. | The accepted set is identical (`running`, `done`, `failed`, `killed`). Hold the line — no regressions expected. |
| Console-script entry point breaks during transition. | The single edit to delete `cli.py` and add `cli/__init__.py` is atomic; we keep `tend.cli:main` resolvable throughout. |

## Acceptance

- `tend --help` shows all six command groups + `scan-skill` + `snapshot`.
- Every invocation listed in the README and in `tests/test_cli*.py`
  produces the same exit code and the same stdout content as before.
- `tend snapshot` writes a `claude-env.md` under `$TEND_HOME`.
- `pytest -q` is green on the Pi.
- `pip install -e .` followed by `tend sessions list` works in a fresh
  venv (smoke test of the entry-point + Typer integration).
