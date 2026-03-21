"""Unit tests for button entities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.comelit_intercom_local.button import ComelitStopVideoButton


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
