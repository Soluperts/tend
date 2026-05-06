"""Tests for ReminderWorker — sleep, then publish TTS + task_update + task_response."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import TTSSpeakFrame
from pipecat_subagents.bus.messages import BusFrameMessage, BusTaskRequestMessage


@pytest.fixture
def mock_bus():
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.send = AsyncMock()
    return bus


def _request(seconds: float, what: str) -> BusTaskRequestMessage:
    msg = MagicMock(spec=BusTaskRequestMessage)
    msg.task_id = "task-123"
    msg.payload = {"seconds": seconds, "what": what}
    return msg


async def test_reminder_completes_and_announces(mock_bus):
    from tend.workers.reminder import ReminderWorker

    worker = ReminderWorker("reminder", bus=mock_bus)
    worker.send_task_update = AsyncMock()
    worker.send_task_response = AsyncMock()

    await worker.handle_reminder(_request(seconds=0.01, what="drink water"))

    # TTSSpeakFrame published to voice bridge
    publish_calls = mock_bus.publish.await_args_list
    assert any(
        isinstance(c.args[0], BusFrameMessage)
        and isinstance(c.args[0].frame, TTSSpeakFrame)
        and "drink water" in c.args[0].frame.text
        and c.args[0].bridge == "voice"
        for c in publish_calls
    )

    # task_update sent with kind="announcement"
    update_call = worker.send_task_update.await_args
    assert update_call.args[0] == "task-123"
    update = update_call.args[1]
    assert update["kind"] == "announcement"
    assert "drink water" in update["spoken"]
    assert update["context"]["what"] == "drink water"

    # task_response sent
    worker.send_task_response.assert_awaited_once()
    response_call = worker.send_task_response.await_args
    assert response_call.args[0] == "task-123"
    assert response_call.args[1] == {"delivered": True}


async def test_reminder_handles_cancellation_silently(mock_bus):
    """If the task is cancelled mid-sleep, no announcement is published."""
    import asyncio
    from tend.workers.reminder import ReminderWorker

    worker = ReminderWorker("reminder", bus=mock_bus)
    worker.send_task_update = AsyncMock()
    worker.send_task_response = AsyncMock()

    task = asyncio.create_task(
        worker.handle_reminder(_request(seconds=10, what="never"))
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # No publishes, no task_update, no response (cancelled cleanly)
    mock_bus.publish.assert_not_awaited()
    worker.send_task_update.assert_not_awaited()
    worker.send_task_response.assert_not_awaited()


async def test_reminder_reports_error_on_exception(mock_bus, monkeypatch):
    """If something goes wrong (other than cancellation), publish error TTS + error update."""
    import asyncio
    from tend.workers.reminder import ReminderWorker

    worker = ReminderWorker("reminder", bus=mock_bus)
    worker.send_task_update = AsyncMock()
    worker.send_task_response = AsyncMock()

    # Force an exception during the sleep.
    async def fake_sleep(_):
        raise RuntimeError("boom")
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await worker.handle_reminder(_request(seconds=10, what="thing"))

    # Error TTS published
    publish_calls = mock_bus.publish.await_args_list
    assert any(
        isinstance(c.args[0].frame, TTSSpeakFrame)
        and "couldn" in c.args[0].frame.text.lower()
        for c in publish_calls
    )

    # Error update sent
    update = worker.send_task_update.await_args.args[1]
    assert update["kind"] == "error"

    # Error response sent
    response_call = worker.send_task_response.await_args
    assert "error" in response_call.args[1]
