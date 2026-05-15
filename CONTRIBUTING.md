# Contributing to tend

Thanks for the interest. This is a personal-infrastructure project that happens to be open source — the bar for new features is high (see the "No" list below), but bug fixes, doc improvements, and platform compatibility patches are very welcome.

## Dev setup

Requires Python 3.11+ and [`uv`](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/Soluperts/tend.git
cd tend
uv sync                       # creates .venv/ and installs dev deps
```

Project layout, conventions, and design rationale live in [`docs/conventions.md`](docs/conventions.md). Read it before changing anything in workspace paths, packaging, CLI structure, secrets handling, webhook contract, or release tooling. If you change something covered there, update the doc in the same PR.

## Running tend from a clone

```bash
export TEND_HOME=$PWD/.tend-dev
mkdir -p $TEND_HOME && echo "0.1.0" > $TEND_HOME/.tend-version
uv run python -m tend
```

`.tend-dev/` is gitignored.

## Tests

```bash
uv run pytest                 # unit tests
uv run pytest -m integration  # integration tests (hits a real pipecat pipeline)
```

Integration tests are skipped by default on PR runs; they execute on tags. If you change anything in `src/tend/audio/` or the pipeline wiring in `main.py`, please run them locally if your platform supports it.

## Code style

- `ruff check .` and `ruff format --check .` must pass.
- `pyright` must pass (we run it in CI).
- Type hints on public functions and class fields.
- Constructor injection, no module-level singletons. If your component can't be tested without spinning up the whole pipeline, the boundary is wrong.
- Default to **no comments**. Only add one when the *why* is non-obvious — a hidden constraint, a workaround, a subtle invariant.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/) — `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, `BREAKING CHANGE:`. The CHANGELOG is generated from these; messages that don't match this format will block release-please.

## Pull requests

- One logical change per PR. If you find yourself wanting to fold in "while I'm here" cleanups, please split them.
- Tests for new behavior. Skills and pure-logic modules (`cron_time`, `google_watcher`, `skills.py`) are the priority; glue-layer coverage is secondary.
- If your PR adds a runtime dependency, please justify it in the description. `docs/conventions.md` makes a real effort to keep the dependency surface small.

## The "No" list

These are explicit non-goals. PRs that add them will be declined with a pointer to `CLAUDE.md` and `ROADMAP.md` for the rationale.

- **Multi-user / multi-tenant.** Single user, single device, no auth.
- **New Python worker classes per capability.** Behavior lives in skills, not subclasses.
- **Context compaction.** Day-session resets at the configured wall-clock boundary.
- **Worker durability across process restart.** systemd / launchd restarts cleanly.
- **Mid-stream cloud-service failover.** Lose the current turn; next turn falls back.
- **Retry/backoff on failed scheduled jobs.** Recurring jobs wait for next fire.
- **Distributed deployment.** In-process `AsyncQueueBus` only.
- **Pip-installable Python plugins.** Filesystem skills are the extension surface.
- **App bundle + code signing on macOS.** Not for v1; revisit later if the friction matters.

If you think one of these should be reconsidered, file an issue with the use case before opening the PR — it's a much shorter conversation that way.

## Security

For anything sensitive, see [`SECURITY.md`](SECURITY.md). Don't open public issues for vulnerabilities.
