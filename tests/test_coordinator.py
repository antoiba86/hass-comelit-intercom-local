"""Unit tests for ComelitLocalCoordinator — no device or HA runtime needed."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.coordinator import ComelitLocalCoordinator
from custom_components.comelit_intercom_local.const import PREWARM_DELAY_SECONDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coordinator() -> ComelitLocalCoordinator:
    """Create a coordinator with all HA dependencies mocked out."""
    hass = MagicMock()
    hass.loop = asyncio.get_event_loop()
    coordinator = ComelitLocalCoordinator.__new__(ComelitLocalCoordinator)
    coordinator.hass = hass
    coordinator.host = "127.0.0.1"
    coordinator.port = 64100
    coordinator.token = "fake_token"
    coordinator._client = None
    coordinator._config = MagicMock()
    coordinator._video_session = None
    coordinator._video_stopped_by_user = False
    coordinator._prewarm_task = None
    coordinator._push_callbacks = {}
    coordinator.logger = MagicMock()
    return coordinator


# ---------------------------------------------------------------------------
# request_video_stop / video_stopped_by_user
# ---------------------------------------------------------------------------


class TestRequestVideoStop:
    def test_flag_starts_false(self):
        coord = _make_coordinator()
        assert coord.video_stopped_by_user is False

    def test_request_video_stop_sets_flag(self):
        coord = _make_coordinator()
        coord.request_video_stop()
        assert coord.video_stopped_by_user is True

    def test_async_start_video_resets_flag(self):
        """async_start_video must clear the stopped-by-user flag."""
        coord = _make_coordinator()
        coord._video_stopped_by_user = True

        mock_session = MagicMock()
        mock_session.start = AsyncMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
            return_value=mock_session,
        ):
            asyncio.get_event_loop().run_until_complete(
                coord.async_start_video(auto_timeout=True)
            )

        assert coord.video_stopped_by_user is False


# ---------------------------------------------------------------------------
# _cancel_prewarm
# ---------------------------------------------------------------------------


class TestCancelPrewarm:
    def test_cancel_prewarm_noop_when_no_task(self):
        coord = _make_coordinator()
        coord._cancel_prewarm()  # should not raise
        assert coord._prewarm_task is None

    @pytest.mark.asyncio
    async def test_cancel_prewarm_cancels_running_task(self):
        coord = _make_coordinator()
        cancelled = asyncio.Event()

        async def long_task():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        coord._prewarm_task = asyncio.create_task(long_task())
        await asyncio.sleep(0)

        coord._cancel_prewarm()

        assert coord._prewarm_task is None
        # Give the task a chance to handle CancelledError
        await asyncio.sleep(0.05)
        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_cancel_prewarm_ignores_done_task(self):
        coord = _make_coordinator()

        async def done_task():
            return

        task = asyncio.create_task(done_task())
        await task  # let it finish
        coord._prewarm_task = task
        coord._cancel_prewarm()  # should not raise


# ---------------------------------------------------------------------------
# async_stop_video
# ---------------------------------------------------------------------------


class TestAsyncStopVideo:
    @pytest.mark.asyncio
    async def test_stop_video_cancels_prewarm(self):
        """async_stop_video must cancel the prewarm task."""
        coord = _make_coordinator()
        cancelled = asyncio.Event()

        async def long_task():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        coord._prewarm_task = asyncio.create_task(long_task())
        await asyncio.sleep(0)

        await coord.async_stop_video()

        await asyncio.sleep(0.05)
        assert cancelled.is_set()
        assert coord._prewarm_task is None

    @pytest.mark.asyncio
    async def test_stop_video_stops_session(self):
        coord = _make_coordinator()
        mock_session = MagicMock()
        mock_session.stop = AsyncMock()
        coord._video_session = mock_session

        await coord.async_stop_video()

        mock_session.stop.assert_awaited_once()
        assert coord._video_session is None


# ---------------------------------------------------------------------------
# async_start_video — prewarm scheduling
# ---------------------------------------------------------------------------


class TestAsyncStartVideo:
    @pytest.mark.asyncio
    async def test_start_video_schedules_prewarm_when_no_timeout(self):
        """auto_timeout=False should schedule a prewarm task."""
        coord = _make_coordinator()
        mock_session = MagicMock()
        mock_session.start = AsyncMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
            return_value=mock_session,
        ):
            await coord.async_start_video(auto_timeout=False)

        assert coord._prewarm_task is not None
        coord._cancel_prewarm()

    @pytest.mark.asyncio
    async def test_start_video_does_not_schedule_prewarm_when_auto_timeout(self):
        """auto_timeout=True (button-triggered) should NOT schedule a prewarm."""
        coord = _make_coordinator()
        mock_session = MagicMock()
        mock_session.start = AsyncMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
            return_value=mock_session,
        ):
            await coord.async_start_video(auto_timeout=True)

        assert coord._prewarm_task is None


# ---------------------------------------------------------------------------
# _prewarm_loop
# ---------------------------------------------------------------------------


class TestPrewarmLoop:
    @pytest.mark.asyncio
    async def test_prewarm_loop_exits_when_cancelled_during_sleep(self):
        """If CancelledError arrives during the initial sleep, loop exits cleanly."""
        coord = _make_coordinator()

        task = asyncio.create_task(coord._prewarm_loop())
        await asyncio.sleep(0)  # let loop start and reach asyncio.sleep(PREWARM_DELAY)
        task.cancel()
        await asyncio.sleep(0)

        # Task should finish without error
        assert task.done()

    @pytest.mark.asyncio
    async def test_prewarm_loop_aborts_when_stopped_by_user(self):
        """If video was stopped by user before sleep ends, loop exits without starting."""
        coord = _make_coordinator()
        coord._video_stopped_by_user = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await coord._prewarm_loop()

        # No new session should have been created (config not needed)
        assert coord._video_session is None

    @pytest.mark.asyncio
    async def test_prewarm_loop_swaps_session_on_success(self):
        """A successful pre-warm atomically replaces the old session."""
        coord = _make_coordinator()

        old_session = MagicMock()
        old_session.stop = AsyncMock()
        coord._video_session = old_session

        new_session = MagicMock()
        new_receiver = MagicMock()
        new_receiver.get_jpeg_frame = AsyncMock(return_value=b"\xff\xd8\xff\xd9")
        new_session.start = AsyncMock()
        new_session.rtp_receiver = new_receiver

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch(
                "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
                return_value=new_session,
            ):
                # Run one iteration (without the recursive self._prewarm_task assignment)
                with patch.object(coord, "_prewarm_loop", wraps=coord._prewarm_loop) as mock_loop:
                    # Prevent infinite recursion: cancel the recursive task immediately
                    original_create_task = asyncio.get_running_loop().create_task
                    created_tasks = []

                    def patched_create_task(coro, **kwargs):
                        t = original_create_task(coro, **kwargs)
                        created_tasks.append(t)
                        t.cancel()  # Cancel the recursive prewarm immediately
                        return t

                    with patch.object(
                        asyncio.get_running_loop(), "create_task", side_effect=patched_create_task
                    ):
                        await coord._prewarm_loop()

        # New session should be the active one
        assert coord._video_session is new_session

    @pytest.mark.asyncio
    async def test_prewarm_loop_discards_new_session_if_user_stopped(self):
        """If user stopped video during prewarm establishment, new session is discarded."""
        coord = _make_coordinator()
        coord._video_session = None

        new_session = MagicMock()
        new_session.start = AsyncMock()
        new_session.stop = AsyncMock()
        new_session.rtp_receiver = None

        async def set_stopped_during_start():
            coord._video_stopped_by_user = True

        new_session.start = AsyncMock(side_effect=set_stopped_during_start)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch(
                "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
                return_value=new_session,
            ):
                await coord._prewarm_loop()

        # Session should NOT have been swapped in
        assert coord._video_session is None
