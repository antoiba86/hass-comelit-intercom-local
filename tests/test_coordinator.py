"""Unit tests for ComelitLocalCoordinator — no device or HA runtime needed."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.coordinator import ComelitLocalCoordinator


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
    coordinator._video_start_lock = asyncio.Lock()
    coordinator._video_ready_event = asyncio.Event()
    coordinator._rtsp_server = None
    coordinator._rtsp_url = None
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
# async_stop_video
# ---------------------------------------------------------------------------


class TestAsyncStopVideo:
    @pytest.mark.asyncio
    async def test_stop_video_stops_session(self):
        coord = _make_coordinator()
        mock_session = MagicMock()
        mock_session.stop = AsyncMock()
        coord._video_session = mock_session

        await coord.async_stop_video()

        mock_session.stop.assert_awaited_once()
        assert coord._video_session is None

    @pytest.mark.asyncio
    async def test_stop_video_clears_ready_event(self):
        """async_stop_video clears the _video_ready_event so stream_source re-waits."""
        coord = _make_coordinator()
        coord._video_ready_event.set()
        mock_session = MagicMock()
        mock_session.stop = AsyncMock()
        coord._video_session = mock_session

        await coord.async_stop_video()

        assert not coord._video_ready_event.is_set()

    @pytest.mark.asyncio
    async def test_stop_video_noop_when_no_session(self):
        """async_stop_video is safe to call when there is no active session."""
        coord = _make_coordinator()
        coord._video_session = None
        await coord.async_stop_video()  # must not raise


# ---------------------------------------------------------------------------
# async_start_video
# ---------------------------------------------------------------------------


class TestAsyncStartVideo:
    @pytest.mark.asyncio
    async def test_start_video_sets_session(self):
        """async_start_video stores the new session in _video_session."""
        coord = _make_coordinator()
        mock_session = MagicMock()
        mock_session.start = AsyncMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
            return_value=mock_session,
        ):
            await coord.async_start_video(auto_timeout=True)

        assert coord._video_session is mock_session

    @pytest.mark.asyncio
    async def test_start_video_fires_ready_event(self):
        """async_start_video sets _video_ready_event after session starts."""
        coord = _make_coordinator()
        mock_session = MagicMock()
        mock_session.start = AsyncMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
            return_value=mock_session,
        ):
            await coord.async_start_video(auto_timeout=True)

        assert coord._video_ready_event.is_set()

    @pytest.mark.asyncio
    async def test_start_video_drops_concurrent_call(self):
        """A second async_start_video while one is in progress is dropped, not queued."""
        coord = _make_coordinator()
        started = asyncio.Event()
        unblock = asyncio.Event()

        async def slow_start():
            started.set()
            await unblock.wait()

        mock_session = MagicMock()
        mock_session.start = AsyncMock(side_effect=slow_start)

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
            return_value=mock_session,
        ):
            task1 = asyncio.create_task(coord.async_start_video())
            await started.wait()  # first call is inside the lock

            # Second call should be rejected immediately (lock is held)
            with pytest.raises(RuntimeError, match="already in progress"):
                await coord.async_start_video()

            unblock.set()
            await task1

    @pytest.mark.asyncio
    async def test_start_video_resets_stopped_flag(self):
        """async_start_video clears _video_stopped_by_user before starting."""
        coord = _make_coordinator()
        coord._video_stopped_by_user = True
        mock_session = MagicMock()
        mock_session.start = AsyncMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
            return_value=mock_session,
        ):
            await coord.async_start_video()

        assert coord._video_stopped_by_user is False
