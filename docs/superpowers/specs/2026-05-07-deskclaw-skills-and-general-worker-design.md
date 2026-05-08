# DeskClaw skills and general-purpose worker — design

Date: 2026-05-07
Status: design (supersedes the worker-shape parts of
`2026-05-06-claude-cli-workers-design.md`)

## Why

DeskClaw is the product vision behind tend: meal plans, fitness check-ins,
calendar/routine work, dashboards. The reference picture is `deskclaw.png` at
the repo root.

Today the only worker driving that vision is `CodingWorker` — useful as an
escape hatch for "build me a small script" but not a model for how meal-plan,
fitness, and calendar workflows should ship. Two options for getting there:

1. Author one specialised worker per domain (meal-plan-worker,
   fitness-worker, calendar-worker, …). Each is a Python class, each ships in
   the repo, each is a release.
2. Ship one general-purpose worker that gets its capabilities from data:
   markdown procedures (skills) plus plain executables (scripts). New
   capabilities are files on disk, authored either by the user or — more
   often — by the worker itself during a task that needs them.

This spec is for option 2. The motivation, by analogy: OpenClaw lets you
converse with the agent and watch it grow new skills from that conversation;
the Skill Workshop plugin authors `SKILL.md` files atomically and they are
visible the next turn. We want the same self-extending behaviour for tend,
but adapted to its smaller surface area (single user, voice-first, no
multi-tenant concerns) and tend's preferred substrate: claude-cli subprocesses
billed against the Pro/Max plan.

## Goals

- One worker class handles the long tail of DeskClaw requests.
- New capabilities ship as data on disk: a `SKILL.md` and optionally one or
  more scripts in a workspace `bin/`.
- The worker can author new skills and scripts during a task that needs them
  (auto mode), subject to a safety scanner.
- Brain stays thin — voice latency wins. Skills are loaded into the
  worker's prompt, never Brain's.
- The user can inspect, list, and remove skills via voice or CLI.

## Non-goals (v1)

- MCP servers. Plain scripts are simpler and cover meal-plan / fitness /
  routine workloads that don't need long-lived auth state. We will revisit
  when the first OAuth-bound integration (Gmail, Calendar, smart-watch)
  lands.
- Per-domain workers. There is exactly one worker class.
- LLM-reviewer-style mining of past transcripts for new skills (analogous to
  OpenClaw's `reviewMode: "llm"`). v1 generates skills only during a task
  that needs one.
- Skill loading into Brain's prompt. Brain still uses `soul.md` only.
- Skill gating (OS / bin / env requires). Every skill is always considered.
- A hot-reload watcher. Each worker run picks up the catalog fresh on
  spawn, which is already the hot path.

## Architecture

```
Hub (audio + STT + TTS + LLMContext, unchanged)
└── Brain (haiku, voice-tight, soul.md only — no skills loaded)
      tools:
        - do_task(request)        ← the one new tool
        - list_skills()           ← voice-side inspection
        - remind_in / start_fresh / list_recent_jobs / session_status /
          continue_session        ← unchanged, worker-agnostic
      └── GeneralWorker (replaces CodingWorker)
            for each request:
              1. enumerate ~/.tend/skills/*/SKILL.md, parse frontmatter
              2. spawn `claude` CLI in ~/.tend/workspace/ with:
                   - preamble: "you are tend's task worker..."
                   - <available-skills> XML catalog (name + description + path)
                   - tools: Bash (allowlisted) + Read + Edit + Write + Grep + Glob
              3. claude decides: matching skill → execute; otherwise → maybe
                 build (auto mode) and execute; or answer inline if one-shot
              4. announce result via TTSSpeakFrame on the bus
              5. persist transcript + outcome via SessionStore
```

### On-disk layout

```
~/.tend/
  skills/                           ← procedures the worker can follow
    meal-plan/
      SKILL.md
      references/                   ← optional supporting prompt fragments
    fitness-check-in/
      SKILL.md
    ...
  skills-quarantined/               ← scanner-blocked skills land here
    <name>/
      SKILL.md
      _findings.json
  workspace/                        ← unchanged base path; see below
    bin/                            ← executables called by skills
      fetch_fridge.sh
      gen_meal_plan.py
      ...
    plans/                          ← worker-written artifacts
    data/                           ← worker-written caches
    ... (anything else the worker decides to make)
  sessions/                         ← unchanged (SessionStore)
```

Skills live outside the workspace deliberately: skills are *how tend
behaves*; the workspace contents are *what tend has built and accumulated*.
The two have different lifecycles — the workspace is more frequently
rewritten by the worker, while skills accrete and represent durable
behaviour.

### `SKILL.md` format

Minimal, AgentSkills-style frontmatter plus a markdown body:

```markdown
---
name: meal-plan
description: Generate a 3-day meal plan from current fridge contents.
---

# Meal Plan

When the user asks for a meal plan:

1. Run `~/.tend/workspace/bin/fetch_fridge.sh` to dump current inventory as JSON.
2. Run `~/.tend/workspace/bin/gen_meal_plan.py --inventory <path>` to generate.
3. Read the JSON result, summarize the 3 days in 1-2 sentences for TTS.
4. Save the full plan to `~/.tend/workspace/plans/<date>.md` for later reference.

Notes:
- The fridge script reads from a Google Sheet — if it errors, tell the user
  the sheet may be unreachable.
- Keep TTS summaries short ("Day 1 is salmon and greens, Day 2…").
```

Required frontmatter: `name`, `description`. No other fields are required
in v1; gating, installer specs, and per-skill config are deliberately out of
scope.

### Skill catalog injection

At spawn time the GeneralWorker walks `~/.tend/skills/*/SKILL.md`, parses
frontmatter only, and emits a compact XML block into the system prompt.
Paths are emitted as absolute filesystem paths (with `~` already expanded)
so claude can pass them straight to `Read` without a shell-expansion step:

```xml
<available-skills>
  <skill>
    <name>meal-plan</name>
    <description>Generate a 3-day meal plan from current fridge contents.</description>
    <path>/home/pi/.tend/skills/meal-plan/SKILL.md</path>
  </skill>
  ...
</available-skills>
```

Claude reads the full body via `Read` only when it picks a skill. Catalog
overhead is roughly 24 tokens per skill plus the field text — predictable,
cheap, and decoupled from skill body length.

### GeneralWorker behaviour

Per-request decision tree, taught by the preamble (claude reasons through
it; nothing in Python forces a path):

```
new request
  │
  ├─ match in <available-skills>?
  │     yes  → Read SKILL.md → follow the procedure → announce
  │
  └─ no match?
        ├─ is the request a recurring workflow (named inputs, repeatable)?
        │     no  → answer inline / refuse politely; do not author a skill
        │     yes → build path
        │
        └─ build path (auto mode):
              1. author scripts under ~/.tend/workspace/bin/ as needed
              2. author ~/.tend/skills/<name>/SKILL.md
              3. scanner over the new SKILL.md and any new scripts
              4. clean       → execute the new skill end-to-end → announce
              4. quarantined → move to ~/.tend/skills-quarantined/<name>/,
                               record findings, fall back to a graceful
                               "I drafted a workflow but flagged it for review"
                               announcement
```

Build-path guardrails baked into the preamble:

- Don't author a skill for one-off requests; only when the workflow plausibly
  recurs.
- Skill names: hyphen-case, lowercase, descriptive (`meal-plan`, not `mp`).
- If a skill with the same name already exists, prefer *append* or *replace*
  a section over silent overwrite.
- Scripts must be self-contained and idempotent where reasonable.
- Do not write secrets or tokens into scripts; read from env.

### Worker → Brain announcement

The existing `on_task_update` channel is reused. The announcement payload
gains one optional field so Brain knows when a new skill was authored in
this turn:

```python
{
  "kind": "announcement",
  "spoken": "Generated a 3-day meal plan, saved to today's file.",
  "context": {
    "session_id": "...",
    "request": "...",
    "skill_used": "meal-plan",          # which skill ran (if any)
    "skills_created": ["meal-plan"],    # NEW: any skills authored this turn
    "skills_quarantined": [],           # NEW: any skills blocked by scanner
  },
}
```

Brain forwards the existing `spoken` payload via `LLMMessagesAppendFrame` as
today; new fields land in the context block so subsequent conversation
turns can reference them naturally ("I built a meal-plan workflow earlier
today, want me to tweak it?").

### Brain tool surface

Concrete additions and removals. Note: `@tool` methods in
pipecat-subagents must surface their result via
`await params.result_callback(value)`; a plain `return` silently does
nothing. Existing Brain tools that rely on return values are broken in
this regard — fixing them is out of scope for this spec but flagged in
the migration table.

```python
# REMOVED
@tool
async def code_in(...)              # replaced by do_task

# ADDED
@tool
async def do_task(self, params: FunctionCallParams, request: str):
    """Hand a task off to the deskclaw worker. Use for anything that
    needs work beyond conversation: meal plans, fitness check-ins,
    calendar work, building tools, running scripts."""
    await self._ensure_general_worker()
    await self.request_task("general", payload={"request": request})
    await params.result_callback(
        f"Got it. Working on '{request[:80]}'..."
    )

@tool
async def list_skills(self, params: FunctionCallParams):
    """List the workflows tend currently knows how to do."""
    # reads ~/.tend/skills/*/SKILL.md frontmatter, returns "name — description" lines
    lines = _enumerate_skills_for_voice()
    await params.result_callback(
        "\n".join(lines) if lines else "I haven't built any workflows yet."
    )
```

The worker is registered under the bus name `"general"`. Brain's
`_ensure_*_worker` helper is renamed to `_ensure_general_worker`.

Unchanged tool *behaviour*: `remind_in`, `start_fresh`,
`list_recent_jobs`, `session_status`, `continue_session`. The
`continue_session` hardcoded worker-name check
(`if match.worker != "coding": ...`) becomes `!= "general"`; coding-worker
sessions written prior to this spec are read-only after migration (the
worker name in their stored row no longer matches; they remain visible to
`list_recent_jobs` for inspection).

### Safety scanner

Pure function over a file's bytes; no side effects. Critical findings
quarantine the proposal; warn findings are recorded but do not block. v1
rules track OpenClaw's Skill Workshop scanner closely.

The scanner runs in two places, layered for defence in depth:

1. **In-claude (preamble-driven).** The worker preamble teaches claude
   that, after authoring any new `SKILL.md` or `bin/` script during a
   build path, it must invoke `tend scan-skill <name>` via Bash before
   executing the new skill. On a critical finding, the preamble tells it
   to move the offending files into the quarantine path and announce a
   graceful fallback instead of running the skill.
2. **Post-hoc (Python-side).** After claude exits, the GeneralWorker
   re-runs the scanner over any files that landed under
   `~/.tend/skills/<name>/` or `~/.tend/workspace/bin/` during this
   session (tracked by mtime > task start). On a critical finding that
   slipped past step 1, the worker moves the skill to quarantine
   retroactively and rewrites the announcement payload's
   `skills_quarantined` field so Brain's context reflects reality. This
   does not undo any execution that already happened, so step 1 is the
   load-bearing check; step 2 is a guardrail against a misbehaving
   model that skipped the in-claude step.

`tend scan-skill <name>` is a new CLI subcommand backed by the same
scanner module; making it CLI-callable means it's also useful for the
user to inspect a skill manually before running it.

v1 rules:

| Rule | Class | Pattern (rough) |
|---|---|---|
| Prompt-injection (ignore-instructions) | critical | `\bignore\b.*\b(prior\|previous\|above\|system)\s+instructions?\b` |
| Prompt-injection (system reference) | critical | references to "system prompt", "developer message", "hidden instructions" |
| Prompt-injection (tool bypass) | critical | "bypass approval", "skip permission", "without asking" |
| Shell-pipe-to-shell | critical | `curl … \| (sh\|bash\|zsh)`; same for `wget` |
| Secret exfiltration | critical | `(curl\|wget\|nc).*\$\{?(ANTHROPIC_API_KEY\|...)` |
| Destructive delete | warn | broad `rm -rf` over `~`, `/`, `$HOME` |
| Unsafe perms | warn | `chmod 777` |

Files scanned: any `SKILL.md` body and any new file under
`~/.tend/workspace/bin/` written during the same task. The scanner runs in
the worker process before claude is invited to execute the new skill.

Quarantine semantics:

- Path: `~/.tend/skills-quarantined/<name>/`.
- A `_findings.json` sidecar records `{rule, line, snippet}` for each hit.
- The worker does not run the new skill; it announces a graceful fallback.
- The user may inspect via the CLI surface below and either delete or
  manually move the directory back into `~/.tend/skills/<name>/` after
  review.

### User-facing inspection

Two surfaces; both small.

Voice (Brain tool):

- `list_skills()` — returns `<name> — <description>` lines for everything
  in `~/.tend/skills/`. Quarantined skills are *not* listed here; the user
  has to ask explicitly.

CLI (extends the existing `tend sessions ...` shape in `cli.py`):

- `tend skills list`
- `tend skills show <name>` — frontmatter + body
- `tend skills cat <name>` — raw `SKILL.md`
- `tend skills rm <name>` — interactive confirmation
- `tend skills quarantined` — list quarantined entries with their findings

`delete_skill` as a Brain tool is deliberately omitted — voice deletion is
too easy to misfire.

## Migration

| File | Change |
|---|---|
| `src/tend/workers/coding.py` | Rename to `general.py`; class becomes `GeneralWorker`. Logic gains skill-catalog injection. |
| `src/tend/workers/claude_cli.py` | No change. |
| `src/tend/skills.py` | **New** — frontmatter parser, catalog loader, scanner, atomic write helper. |
| `src/tend/brain.py` | Replace `code_in` with `do_task`; add `list_skills`; rename `_ensure_coding_worker` to `_ensure_general_worker`; widen `continue_session`'s worker-name check; spawn the worker under name `"worker"`. |
| `tend.toml` | `[workers.coding]` → `[workers.general]`. Same fields. `allowed_tools` unchanged from today's set. |
| `src/tend/cli.py` | Add `tend skills list/show/cat/rm/quarantined` and `tend scan-skill <name>` subcommands. |
| `tests/workers/test_coding.py` | → `test_general.py`; add coverage for catalog injection, the build-path branches, and the scanner behavior. |
| `tests/test_skills.py` | **New** — frontmatter parsing, catalog enumeration, scanner positive + negative cases, atomic write, quarantine path. |
| `docs/superpowers/specs/2026-05-06-claude-cli-workers-design.md` | Add a "superseded for worker shape by 2026-05-07" note at the top. |
| `CLAUDE.md` | Update the workers section: replace ReminderWorker-only language with the GeneralWorker pattern; document the skills directory and the scanner. |
| `~/.claude/projects/-home-pi-hasat/memory/project_deskclaw_scope.md` | Update "coding worker is the escape hatch" — the escape hatch is now folded into the general worker; the workspace mechanics still hold. |

What stays exactly as-is: Hub, audio gates, `SessionStore`,
`ClaudeCliWorker` base, `ReminderWorker` stub, the wake/sleep state machine,
day-session reset, and the Pro/Max plan billing path.

## Open questions / risks

- **Catalog token cost at scale.** With ~50 skills, the catalog is ~1500
  tokens of overhead per worker spawn. Tolerable on opus, may want a
  per-worker model override or catalog truncation by recency at some
  point. Not a v1 concern; flag for revisit.
- **Skill naming collisions.** The worker is told to prefer
  append/replace, but a determined wrong choice can clobber. The atomic
  write helper writes through a temp file + rename; the user can `git
  init ~/.tend/skills/` if they want history. Recommended in the README
  but not enforced.
- **Quarantine recovery UX.** v1 ships only inspection commands. If
  quarantine becomes a regular event, we'll add a `tend skills approve
  <name>` command in a follow-up. Not before evidence it's needed.
- **Scanner false positives.** Regex-based rules will misfire (e.g. a
  `curl | sh` snippet inside a Notes section that explains *what not to
  do*). v1's answer: log the finding, quarantine, let the user move it
  manually. We accept some friction here to keep the auto-build path
  honest.

## What this enables once shipped

Concrete worked example. User says: *"tend, I want a fitness check-in
every Monday morning that pulls my last week's watch data, weighs it
against my goals, and gives me a one-paragraph summary."*

Today: brain has no tool that does this; it would attempt a verbal
answer or refuse.

After this spec: brain dispatches `do_task` → GeneralWorker spawns
claude in the workspace → claude sees no `fitness-check-in` skill → it
authors `~/.tend/workspace/bin/pull_watch_data.py`,
`~/.tend/workspace/bin/weekly_summary.py`,
`~/.tend/skills/fitness-check-in/SKILL.md` → the scanner clears them →
the worker runs the new skill end-to-end → announces the summary →
context is updated. Next Monday morning the user just says *"do my
fitness check-in"* and the catalog match runs the existing skill cold.

The escape hatch is preserved: when the user explicitly asks for build
work ("write me a parser for X"), `do_task` still routes to the same
worker, which authors scripts in the workspace as before. There is no
longer a separate `code_in` path.
