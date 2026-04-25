"""Unit tests for button entities."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.button import (
    ComelitDoorButton,
    ComelitStopVideoButton,
)
from custom_components.comelit_intercom_local.models import Door


def _make_stop_button() -> ComelitStopVideoButton:
    """Create a ComelitStopVideoButton with a mocked coordinator."""
    coordinator = MagicMock()
    coordinator.async_stop_video = AsyncMock()
    coordinator.request_video_stop = MagicMock()

    btn = ComelitStopVideoButton.__new__(ComelitStopVideoButton)
    btn.coordinator = coordinator
    return btn


class TestComelitStopVideoButton:
    @pytest.mark.asyncio
    async def test_press_calls_request_video_stop_before_async_stop(self):
        """request_video_stop() must be called before async_stop_video().

        This ensures the prewarm loop sees the flag and aborts before
        async_stop_video() cancels it, preventing a race where a new session
        is established right after stop.
        """
        btn = _make_stop_button()
        call_order = []

        btn.coordinator.request_video_stop = MagicMock(
            side_effect=lambda: call_order.append("request_stop")
        )
        btn.coordinator.async_stop_video = AsyncMock(
            side_effect=lambda: call_order.append("async_stop")
        )

        await btn.async_press()

        assert call_order == ["request_stop", "async_stop"]

    @pytest.mark.asyncio
    async def test_press_does_not_raise_on_exception(self):
        """async_press must not propagate exceptions."""
        btn = _make_stop_button()
        btn.coordinator.async_stop_video = AsyncMock(
            side_effect=RuntimeError("stop failed")
        )

        await btn.async_press()  # should not raise


# ---------------------------------------------------------------------------
# ComelitDoorButton
# ---------------------------------------------------------------------------


def _make_door_button() -> ComelitDoorButton:
    coordinator = MagicMock()
    coordinator.async_open_door = AsyncMock()
    coordinator.request_video_stop = MagicMock()
    coordinator.async_stop_video = AsyncMock()
    coordinator.video_session = None

    door = Door(id=0, index=0, name="Main Gate", apt_address="SB100001", output_index=0)
    btn = ComelitDoorButton.__new__(ComelitDoorButton)
    btn.coordinator = coordinator
    btn._door = door
    btn.hass = MagicMock()
    return btn


class TestComelitDoorButton:
    @pytest.mark.asyncio
    async def test_press_calls_open_door(self):
        """async_press calls coordinator.async_open_door with the door."""
        btn = _make_door_button()
        await btn.async_press()
        btn.coordinator.async_open_door.assert_awaited_once_with(btn._door)

    @pytest.mark.asyncio
    async def test_press_does_not_schedule_stop_when_no_session(self):
        """No delayed stop is scheduled when no video session is active."""
        btn = _make_door_button()
        btn.coordinator.video_session = None

        await btn.async_press()

        btn.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_press_schedules_delayed_stop_when_session_active(self):
        """A delayed stop task is scheduled when a video session is active."""
        btn = _make_door_button()
        session = MagicMock()
        session.active = True
        btn.coordinator.video_session = session

        # Capture and close the coroutine so it doesn't leak
        created_coros = []
        def capture_task(coro):
            created_coros.append(coro)
        btn.hass.async_create_task = capture_task

        await btn.async_press()

        assert len(created_coros) == 1
        # Close the coroutine to prevent "was never awaited" warning
        created_coros[0].close()

    @pytest.mark.asyncio
    async def test_press_does_not_raise_on_door_open_failure(self):
        """async_press swallows exceptions from async_open_door."""
        btn = _make_door_button()
        btn.coordinator.async_open_door = AsyncMock(side_effect=RuntimeError("timeout"))

        await btn.async_press()  # must not raise

    @pytest.mark.asyncio
    async def test_stop_video_after_delay_stops_active_session(self):
        """_stop_video_after_delay calls async_stop_video once the delay elapses.

        The user-initiated-stop flag is set earlier, in async_press, so this
        method only needs to invoke async_stop_video after the sleep.
        """
        btn = _make_door_button()
        session = MagicMock()
        session.active = True
        btn.coordinator.video_session = session

        btn.coordinator.async_stop_video = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await btn._stop_video_after_delay(10)

        btn.coordinator.async_stop_video.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_press_marks_stop_user_initiated_before_delay(self):
        """Door press must call request_video_stop immediately so a CALL_END
        arriving before the 10s delay isn't auto-restarted.
        """
        btn = _make_door_button()
        session = MagicMock()
        session.active = True
        btn.coordinator.video_session = session

        # Silence the scheduled background task
        btn.hass.async_create_task = lambda coro: coro.close()

        btn.coordinator.request_video_stop = MagicMock()

        await btn.async_press()

        btn.coordinator.request_video_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_video_after_delay_noop_when_session_gone(self):
        """_stop_video_after_delay does nothing if session ends before the delay."""
        btn = _make_door_button()
        btn.coordinator.video_session = None  # session already gone

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await btn._stop_video_after_delay(10)

        btn.coordinator.async_stop_video.assert_not_awaited()
