# tend OSS clerical — implementation plan

> Roadmap item #1 ([`ROADMAP.md`](../../../ROADMAP.md) → "Next: public release (v0.1)"). Pre-public-release hygiene: licensing, repo furniture, packaging backend cleanup. Decisions made in the parent session are recorded under each task.

**Goal:** Bring the repo up to the bar for a public OSS release as documented in [`docs/conventions.md`](../../conventions.md#repo-furniture). No behavior changes; no user-visible runtime changes.

**Spec:** [`docs/conventions.md`](../../conventions.md) already encodes the design (repo furniture list, packaging conventions, license rationale, distribution model). This plan is execution-only. The one design decision that *overrides* conventions.md is the license choice — see Task 1.

---

## Decisions (record)

| Decision | Choice | Rationale |
|---|---|---|
| License | **MIT** (not Apache-2.0) | Personal voice-assistant project, low patent surface (audio/wake-word code is third-party), MIT is simpler for casual contributors and dominant in AI-OSS. Overrides the rationale in `docs/conventions.md:198-200` — that doc is updated in Task 4. |
| PyPI distribution name | **`tend-assistant`** | `tend` is taken on PyPI. Import name remains `tend`. Same pattern as Pillow → PIL, beautifulsoup4 → bs4. |
| Build backend | **hatchling** | Conventions doc already mandates this; `pyproject.toml` was on setuptools — fixing the divergence in this PR. |
| Git history scrub | **Skip** | User's emails (`luthraridhwan@gmail.com`, `ridhwan@soluperts.ca`) are both personal and linked to their public GitHub; no scrub needed. |
| Specs containing "DeskClaw" | **Keep as-is** | Specs are dated decision records (per `conventions.md:250`); rewriting historical artifacts breaks their value. Live references (CLAUDE.md, test docstrings) are updated. |
| `deskclaw.png` | **Delete** | Used by one old spec; can regenerate a tend-branded architecture diagram later if needed. |
| SPDX headers on every source file | **Defer to a follow-up** | Mechanical sweep over ~30 files; adds noise to this PR. Tracked as a follow-up. |
| GitHub repo rename | **Manual user action** | `Soluperts/DeskClaw` → `Soluperts/tend` via `gh repo rename tend` (or GitHub UI). Out of scope for code changes here. |

---

## File structure (created or modified by this plan)

| File | Status | Purpose |
|------|--------|---------|
| `LICENSE` | NEW | MIT license, copyright Ridhwan Luthra. |
| `pyproject.toml` | MODIFY | Distribution name `tend-assistant`; setuptools → hatchling; license = "MIT". |
| `docs/conventions.md` | MODIFY | Apache-2.0 → MIT (rationale + SPDX). Install commands. License rationale section. |
| `README.md` | MODIFY | `pipx install tend` → `pipx install tend-assistant`; `pipx inject tend ...` → `pipx inject tend-assistant ...`. |
| `ROADMAP.md` | MODIFY | Strike completed items from "OSS clerical"; flag rename/SPDX-headers as follow-ups. |
| `CLAUDE.md` | MODIFY | Update live DeskClaw references (the two spec/plan links at the bottom — they're already pointing at "deskclaw-skills..." files, which are real filenames; leave the file paths, but check the wording). |
| `tests/workers/test_general.py` | MODIFY | First-line docstring: "...deskclaw workspace." → "...tend workspace." |
| `deskclaw.png` | DELETE | No longer referenced by anything live. |
| `SECURITY.md` | NEW | Per `conventions.md:182`. Short — email + 7-day SLA. |
| `CONTRIBUTING.md` | NEW | Dev setup + commit style + "No" list quoting CLAUDE.md's non-choices. |
| `.github/dependabot.yml` | NEW | Weekly pip + github-actions. |
| `.github/ISSUE_TEMPLATE/bug_report.yml` | NEW | YAML form schema. |
| `.github/ISSUE_TEMPLATE/feature_request.yml` | NEW | YAML form schema. |
| `.github/PULL_REQUEST_TEMPLATE.md` | NEW | Three-line checklist per `conventions.md:190`. |

CI workflows (`ci.yml`, `release.yml`) and `SECURITY.md`/`SETUP.md` for release-please belong to roadmap item #6 (release machinery) and are explicitly **not** in this PR.

---

## Tasks

### Task 1: Add MIT LICENSE — done in the same commit as Task 4

- [ ] Write `LICENSE` at repo root: standard MIT text, copyright year `2026`, holder `Ridhwan Luthra`.

### Task 2: Migrate pyproject.toml

- [ ] Replace `[build-system]` with `requires = ["hatchling"]`, `build-backend = "hatchling.build"`.
- [ ] Change `name = "tend"` → `name = "tend-assistant"`.
- [ ] Add `license = "MIT"` and `license-files = ["LICENSE"]` (PEP 639 form, supported by hatchling).
- [ ] Add `[tool.hatch.build.targets.wheel] packages = ["src/tend"]`.
- [ ] Delete `[tool.setuptools.packages.find]` and `[tool.setuptools.package-data]`.
- [ ] Hatchling auto-includes everything under `src/tend/` including `_defaults/`; verify with `uv build && unzip -l dist/*.whl` (Task 9).

### Task 3: Delete `deskclaw.png`

- [ ] `git rm deskclaw.png` (or local delete then `git add -u`).

### Task 4: Update `docs/conventions.md`

- [ ] License: Apache-2.0 → MIT throughout (the "Repo furniture" bullet and "License choice rationale" subsection).
- [ ] Rewrite the rationale to reflect the actual decision: MIT is the AI-OSS default, the patent-grant value was not load-bearing for tend specifically.
- [ ] SPDX header instruction: `Apache-2.0` → `MIT`. Add a note that the SPDX sweep is a follow-up, not blocking.
- [ ] Distribution section: `pipx install tend` → `pipx install tend-assistant`. Same for the Homebrew formula naming hint and the WebRTC AEC opt-in command.
- [ ] Build backend already says hatchling — leave it; this PR brings code into agreement.

### Task 5: Update `README.md`

- [ ] Replace `pipx install tend` → `pipx install tend-assistant` (two occurrences: Linux and macOS sections).
- [ ] Replace `pipx inject tend webrtc-audio-processing` → `pipx inject tend-assistant webrtc-audio-processing`.
- [ ] (Cross-check `tend setup`, `tend doctor`, `tend service` references — those are the CLI entry point, NOT the package name, so they stay as `tend`.)

### Task 6: Update `ROADMAP.md`

- [ ] In "Next: public release (v0.1)", item 1 (OSS clerical): strike through completed items; promote the GitHub repo rename and the SPDX-header sweep to a brief follow-up bullet. Leave the rest of the section structure intact.

### Task 7: Update live DeskClaw references

- [ ] `tests/workers/test_general.py`: first-line docstring "...persistent deskclaw workspace" → "...persistent tend workspace".
- [ ] `CLAUDE.md`: only changes if the *wording* references DeskClaw outside of citing the dated spec filenames. Filenames like `2026-05-07-deskclaw-skills-and-general-worker-design.md` stay (they're historical).

### Task 8: Create `.github/` files + `SECURITY.md` + `CONTRIBUTING.md`

- [ ] `.github/dependabot.yml` — weekly, `pip` + `github-actions` ecosystems.
- [ ] `.github/ISSUE_TEMPLATE/bug_report.yml` — YAML form: device (Pi/Mac), what happened, repro steps, `tend doctor` output.
- [ ] `.github/ISSUE_TEMPLATE/feature_request.yml` — YAML form: problem, proposed solution, alternatives, link to CLAUDE.md "non-choices" reminder.
- [ ] `.github/PULL_REQUEST_TEMPLATE.md` — three-line checklist (tests, CHANGELOG, no new deps without discussion).
- [ ] `SECURITY.md` — Email contact, 7-day response SLA, no public issues for vulns, supported versions = "latest only" pre-1.0.
- [ ] `CONTRIBUTING.md` — Dev setup (`uv sync`), test commands (`uv run pytest`), commit style (Conventional Commits), and an explicit "No" list quoting CLAUDE.md's deliberate non-choices.

### Task 9: Verify packaging

- [ ] `rm -rf dist/ && uv build`.
- [ ] `unzip -l dist/tend_assistant-0.1.0-py3-none-any.whl` — confirm:
  - `tend/_defaults/soul.md` present
  - `tend/_defaults/critical-skills/heartbeat/SKILL.md` present
  - No `__pycache__` directories leaked (hatchling cleaner than setuptools here)
  - `tend-0.1.0.dist-info/licenses/LICENSE` present (PEP 639 location for hatchling)
- [ ] `uv pip install --reinstall dist/tend_assistant-0.1.0-py3-none-any.whl` into a throwaway venv (or just `--dry-run` confirm it resolves under the new name).
- [ ] `tend --help` works from the installed wheel (entry point still maps to `tend`).

### Follow-ups not in this PR

1. **GitHub repo rename** — user action: `gh repo rename tend` from inside the repo, then `git remote set-url origin https://github.com/Soluperts/tend.git`. Auto-redirects from `DeskClaw` URLs.
2. **SPDX header sweep** — add `# SPDX-License-Identifier: MIT` to the top of every `.py` file. Mechanical; ~30 files. Either a separate PR or a follow-up commit on the same branch, but kept distinct from the substantive clerical change for review clarity.

---

## Verification at the end

- `uv build` succeeds, wheel contents look right (Task 9).
- `git status` shows a clean checkout after staging.
- `ruff check .` clean.
- `pytest` (or `uv run pytest`) — only ensures we didn't break test imports with the test docstring edit. Full suite needs Pi hardware/cloud keys so this is best-effort locally.
- `git log -1 --stat` shows the expected file set.
