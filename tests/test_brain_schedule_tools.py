"""Tests for Brain's scheduling tools (schedule / list / cancel)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tend.brain import Brain


def _params():
    p = MagicMock()
    p.result_callback = AsyncMock()
    return p


@pytest.fixture
def scheduler():
    s = MagicMock()
    s.add_job = MagicMock()
    s.cancel_job = MagicMock(return_value=True)
    s.list_jobs = MagicMock(return_value=[])
    return s


@pytest.fixture
def brain(scheduler):
    bus = MagicMock()
    return Brain(
        "brain", bus=bus, llm_service=MagicMock(),
        store=MagicMock(), scheduler=scheduler,
    )


async def test_schedule_calls_scheduler(brain, scheduler):
    p = _params()
    job = MagicMock()
    job.name = "lunch"
    job.id = "abcdef0123"
    scheduler.add_job.return_value = job
    await brain.schedule(p, when="0 12 * * *", request="lunch plan",
                         name="lunch")
    scheduler.add_job.assert_called_once()
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["when"] == "0 12 * * *"
    assert kwargs["request"] == "lunch plan"
    assert kwargs["name"] == "lunch"
    assert kwargs["source"] == "voice"
    p.result_callback.assert_awaited_once()
    msg = p.result_callback.call_args.args[0]
    assert "lunch" in msg


async def test_schedule_invalid_when_returns_error_message(brain, scheduler):
    from tend.cron_time import InvalidWhen
    scheduler.add_job.side_effect = InvalidWhen("bad")
    p = _params()
    await brain.schedule(p, when="nonsense", request="x")
    msg = p.result_callback.call_args.args[0]
    assert "couldn't parse" in msg.lower() or "didn't" in msg.lower()


async def test_list_schedules_empty(brain, scheduler):
    scheduler.list_jobs.return_value = []
    p = _params()
    await brain.list_schedules(p)
    msg = p.result_callback.call_args.args[0]
    assert "no" in msg.lower() and ("schedule" in msg.lower())


async def test_list_schedules_summarises(brain, scheduler):
    job1 = MagicMock()
    job1.name = "lunch"
    job1.kind = "cron"
    job1.schedule = "0 12 * * *"
    job1.source = "voice"

    job2 = MagicMock()
    job2.name = "ping"
    job2.kind = "every"
    job2.schedule = "30m"
    job2.source = "cli"

    scheduler.list_jobs.return_value = [job1, job2]
    p = _params()
    await brain.list_schedules(p)
    msg = p.result_callback.call_args.args[0]
    assert "lunch" in msg
    assert "ping" in msg


async def test_cancel_schedule_known(brain, scheduler):
    scheduler.cancel_job.return_value = True
    p = _params()
    await brain.cancel_schedule(p, name_or_id="lunch")
    scheduler.cancel_job.assert_called_once_with("lunch")
    msg = p.result_callback.call_args.args[0]
    assert "cancelled" in msg.lower() or "removed" in msg.lower()


async def test_cancel_schedule_unknown(brain, scheduler):
    scheduler.cancel_job.return_value = False
    p = _params()
    await brain.cancel_schedule(p, name_or_id="nope")
    msg = p.result_callback.call_args.args[0]
    assert "find" in msg.lower() or "no" in msg.lower()
