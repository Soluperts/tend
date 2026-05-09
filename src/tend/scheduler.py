"""Scheduler — proactive job dispatch agent.

Owns wall-clock time. Reads cron job definitions and runtime state from
CronStore on init. Runs an asyncio loop that sleeps until the next
fire time, dispatches the job, recomputes next-fire, and repeats. One-
shot `at` jobs auto-delete after fire (regardless of outcome).

Public API:
- add_job(when, request, name, source, ...) -> CronJob
- cancel_job(name_or_id) -> bool
- list_jobs() -> list[CronJob]
- find_by_source(source) -> list[CronJob]
- fire_missed_now() — call once at boot for at-jobs whose target time
  has already passed (subject to missed_at_policy).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from loguru import logger
from pipecat_subagents.agents import BaseAgent
from pipecat_subagents.bus import AgentBus

from tend.cron_store import CronJob, CronStore, JobState
from tend.cron_time import next_fire_at, parse_when


MissedPolicy = Literal["run-on-restart", "skip"]


class Scheduler(BaseAgent):
    def __init__(
        self,
        name: str,
        *,
        bus: AgentBus,
        store: CronStore,
        dispatch: Callable[[str, dict], Awaitable[None]],
        default_tz: str = "UTC",
        missed_at_policy: MissedPolicy = "run-on-restart",
    ):
        super().__init__(name, bus=bus)
        self._store = store
        self._dispatch = dispatch
        self._default_tz = default_tz
        self._missed_policy = missed_at_policy
        self._loop_task: asyncio.Task | None = None
        self._wakeup = asyncio.Event()

    # ---- public API ----

    def add_job(
        self,
        *,
        when: str,
        request: str,
        name: str,
        source: str,
        tz: str | None = None,
        payload_extras: dict | None = None,
    ) -> CronJob:
        # Validate everything before persisting — a bad tz would otherwise
        # leave a half-written row in jobs.json.
        kind, schedule = parse_when(when)
        effective_tz = tz or self._default_tz
        nfa = next_fire_at(
            kind, schedule, effective_tz, dt.datetime.now(tz=ZoneInfo("UTC")),
        )
        payload: dict = {"request": request}
        if payload_extras:
            payload.update(payload_extras)
        job = self._store.add_job(
            name=name, kind=kind, schedule=schedule, tz=effective_tz,
            payload=payload, source=source, enabled=True,
        )
        self._store.set_state(
            job.id,
            JobState(next_run_at=nfa.isoformat()),
        )
        self._wakeup.set()
        return job

    def cancel_job(self, name_or_id: str) -> bool:
        jobs = self._store.load_jobs()
        match = next(
            (j for j in jobs
             if j.name == name_or_id or j.id.startswith(name_or_id)),
            None,
        )
        if match is None:
            return False
        ok = self._store.remove_job(match.id)
        if ok:
            self._store.remove_state(match.id)
            self._wakeup.set()
        return ok

    def list_jobs(self) -> list[CronJob]:
        return self._store.load_jobs()

    def find_by_source(self, source: str) -> list[CronJob]:
        return self._store.find_by_source(source)

    # ---- lifecycle ----

    async def on_ready(self) -> None:
        await super().on_ready()
        # Fire missed at-jobs now that the bus is alive and dispatch can land.
        await self.fire_missed_now()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def on_stopped(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
        await super().on_stopped()

    async def fire_missed_now(self) -> None:
        """Run once at startup. For each enabled `at` job whose target
        time is already past, either fire-and-delete or skip-and-delete
        based on missed_at_policy. Recurring jobs simply have their
        next_run_at recomputed; no catch-up firings."""
        now = dt.datetime.now(tz=ZoneInfo("UTC"))
        for job in self._store.load_jobs():
            if not job.enabled:
                continue
            if job.kind != "at":
                # Refresh next_run_at for recurring jobs.
                nfa = next_fire_at(job.kind, job.schedule, job.tz or self._default_tz, now)
                st = self._store.get_state(job.id)
                self._store.set_state(
                    job.id,
                    JobState(
                        last_run_at=st.last_run_at,
                        last_run_status=st.last_run_status,
                        last_error=st.last_error,
                        next_run_at=nfa.isoformat(),
                        consecutive_errors=st.consecutive_errors,
                    ),
                )
                continue
            target = dt.datetime.fromisoformat(job.schedule)
            if target > now:
                continue
            if self._missed_policy == "run-on-restart":
                logger.info(f"firing missed at-job {job.name!r}")
                await self._fire_job(job)
            else:
                logger.info(f"skipping missed at-job {job.name!r}")
                self._store.remove_job(job.id)
                self._store.remove_state(job.id)

    # ---- internals ----

    async def _run_loop(self) -> None:
        while True:
            now = dt.datetime.now(tz=ZoneInfo("UTC"))
            jobs = [j for j in self._store.load_jobs() if j.enabled]
            soonest = self._next_due_after(jobs, now)
            if soonest is None:
                # No jobs; sleep up to a minute then re-check.
                wait_s = 60.0
            else:
                job, when = soonest
                wait_s = max(0.0, (when - now).total_seconds())
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=wait_s)
                # Wakeup signal — re-evaluate.
                self._wakeup.clear()
                continue
            except asyncio.TimeoutError:
                pass
            # Timed out — soonest is due.
            if soonest is None:
                continue
            job, _ = soonest
            await self._fire_job(job)

    def _next_due_after(
        self, jobs: list[CronJob], now: dt.datetime,
    ) -> tuple[CronJob, dt.datetime] | None:
        candidates: list[tuple[dt.datetime, CronJob]] = []
        for j in jobs:
            st = self._store.get_state(j.id)
            if st.next_run_at:
                candidates.append((dt.datetime.fromisoformat(st.next_run_at), j))
            else:
                nfa = next_fire_at(
                    j.kind, j.schedule, j.tz or self._default_tz, now,
                )
                candidates.append((nfa, j))
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0])
        when, job = candidates[0]
        return job, when

    async def _fire_job(self, job: CronJob) -> None:
        st = self._store.get_state(job.id)
        try:
            await self._dispatch("general", job.payload)
            new_state = JobState(
                last_run_at=dt.datetime.now(tz=ZoneInfo("UTC")).isoformat(),
                last_run_status="succeeded",
                last_error=None,
                next_run_at=None,
                consecutive_errors=0,
            )
        except Exception as e:
            logger.exception(f"scheduler dispatch failed for {job.name!r}")
            new_state = JobState(
                last_run_at=dt.datetime.now(tz=ZoneInfo("UTC")).isoformat(),
                last_run_status="failed",
                last_error=str(e),
                next_run_at=None,
                consecutive_errors=st.consecutive_errors + 1,
            )

        if job.kind == "at":
            self._store.remove_job(job.id)
            self._store.remove_state(job.id)
            return

        nfa = next_fire_at(
            job.kind, job.schedule, job.tz or self._default_tz,
            dt.datetime.now(tz=ZoneInfo("UTC")),
        )
        self._store.set_state(
            job.id,
            JobState(
                last_run_at=new_state.last_run_at,
                last_run_status=new_state.last_run_status,
                last_error=new_state.last_error,
                next_run_at=nfa.isoformat(),
                consecutive_errors=new_state.consecutive_errors,
            ),
        )
