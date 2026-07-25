# Agent Operating Guide

This file defines how coding agents should work in this repository.

## Engineering Loop

Follow this loop:

Audit → Prioritize → Plan → Implement → Verify → Report

Do not skip directly from a vague request to implementation.

## Core Principles

* Evidence over guesses
* Simplicity over ceremony
* Small changes over heroic rewrites
* Existing primitives over new abstractions
* Tests and evals over confidence theater
* Preservation before cleanup
* Clear ownership and handoffs
* Documentation must describe reality

## Repository Entry

When beginning work:

* Inspect the current branch
* Inspect the working tree
* Check the upstream relationship
* Identify uncommitted and untracked work
* Review relevant issues and PRs
* Locate project entry points
* Locate tests, evals, CI, and documentation
* Identify the relevant source of truth

Do not modify files during initial orientation unless the task is trivial and explicitly authorized.

## Planning

Before non-trivial implementation, create a focused work packet containing:

* Goal
* Why it matters
* Evidence
* Scope
* Non-goals
* Likely files
* Proposed approach
* Behavior to preserve
* Tests and evals
* Risks
* Validation commands
* Definition of done

Ask for approval before implementation when the work is risky, broad, ambiguous, destructive, or architecture-changing.

## Implementation

* Make the smallest useful change.
* Keep the diff focused.
* Do not fix unrelated issues.
* Do not silently expand scope.
* Preserve public behavior unless change is explicitly required.
* Add tests for important behavior.
* Add regression tests for bugs.
* Add evals when AI or agent behavior changes.
* Update docs when user or developer behavior changes.

## Git And Worktree Safety

Do not perform destructive Git operations without explicit approval.

Never assume a branch, worktree, stash, or untracked file is disposable.

Before deleting or consolidating work:

* Inventory it
* Determine its purpose
* Compare it with `main`
* Identify duplicate or superseding work
* Preserve anything uncertain
* Verify tests and CI
* Require human approval

## Issue And PR Discipline

* Reuse existing issues when possible.
* Do not create duplicate issues.
* One focused issue should usually map to one focused PR.
* Split broad or unrelated work.
* Use research or decision issues when requirements are unresolved.
* Do not open a PR until implementation and validation are complete.

Every implementation issue should contain:

* Summary
* Why it matters
* Scope
* Non-goals
* Acceptance criteria
* Tests
* Evals, when relevant
* Dependencies
* Risks
* Definition of done

## Code Quality

Look for:

* Duplicate logic
* Dead code
* Unused imports and dependencies
* Debug output
* Temporary files
* Commented-out code
* Stale feature flags
* Misleading names
* Oversized modules
* Fragile scripts
* Unnecessary abstraction layers

Do not declare code dead based only on appearance.

Check imports, dynamic registration, configuration, tests, builds, scripts, and runtime entry points first.

Refactoring must unlock something concrete.

## Skills, Workflows, And Automations

Use this model:

* Skills define reusable capabilities.
* Workflows coordinate skills.
* Automations trigger workflows.
* Issues define approved work.
* PRs deliver focused implementation.

Every active skill, workflow, or automation should have:

* A clear purpose
* A clear trigger
* Clear inputs
* Clear outputs
* A clear owner
* A safety boundary
* A validation method
* A known consumer

Anything lacking these should be fixed, merged, paused, archived, replaced, or removed after approval.

## Configuration And Secrets

When Varlock is present:

* Treat its schema as the configuration contract.
* Prefer Varlock-based runtime and validation paths.
* Identify legacy configuration paths that bypass it.
* Never reveal resolved secrets.
* Never commit secret-bearing files.
* Report suspected leaks without reproducing values.

## Documentation

Documentation is part of the implementation.

Keep accurate:

* README files
* Setup instructions
* Architecture docs
* Changelogs
* Configuration docs
* Skill and workflow docs
* Automation docs
* Testing and eval instructions

Do not invent changelog history.

## Verification

Run relevant checks after changes.

Report:

* Commands run
* Results
* Commands not run
* Reason they were not run
* Remaining uncertainty

A task is not complete merely because code was written.

## End Of Session

Leave the repository easier to resume.

Report:

* Current branch and working state
* Work completed
* Files changed
* Validation results
* Remaining risks
* Follow-up work
* Exact recommended next step


## Secrets (Varlock)

- Local secrets for agent/tool use live in gitignored plaintext `.env` / `.env.local` (mode `0600`). Varlock owns `.env.schema` + `load`/`run` injection — not macOS Keychain or Touch ID.
- Agents inspect with `varlock load --agent` and run tools with `varlock run --inject vars -- <command>`.
- Never `cat` `.env` / `.env.local`, never `printenv` secrets, never `varlock reveal` in agent sessions.
- Canonical contract docs: `/Users/kk/Code/kk-kb/docs/AGENT-SECRETS-VARLOCK.md`.
