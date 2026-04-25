"""Unit tests for ComelitLocalCoordinator — no device or HA runtime needed."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.coordinator import ComelitLocalCoordinator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coordinator(*, with_client: bool = False) -> ComelitLocalCoordinator:
    """Create a coordinator with all HA dependencies mocked out."""
    hass = MagicMock()
    coordinator = ComelitLocalCoordinator.__new__(ComelitLocalCoordinator)
    coordinator.hass = hass
    coordinator.host = "127.0.0.1"
    coordinator.port = 64100
    coordinator.token = "fake_token"
    coordinator.device_name = "Comelit Intercom"
    # Mock config entry with options (notifications enabled by default)
    config_entry = MagicMock()
    config_entry.options = {"enable_notifications": True}
    coordinator.config_entry = config_entry
    # Client — some tests need a connected client
    if with_client:
        mock_client = MagicMock()
        mock_client.connected = True
        mock_client.get_channel = MagicMock(return_value=None)
        mock_client.remove_channel = MagicMock()
        coordinator._client = mock_client
    else:
        coordinator._client = None
    coordinator._config = MagicMock()
    coordinator._video_session = None
    coordinator._vip_listener = None
    coordinator._video_stopped_by_user = False
    coordinator._video_start_lock = asyncio.Lock()
    coordinator._video_ready_event = asyncio.Event()
    coordinator._rtsp_server = None
    coordinator._rtsp_url = None
    coordinator._push_callbacks = {}
    coordinator._on_stop_video = {}
    coordinator._on_video_state_change = {}
    coordinator._keepalive_task = None
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

    @pytest.mark.asyncio
    async def test_async_start_video_resets_flag(self):
        """async_start_video(by_user=True) must clear the stopped-by-user flag."""
        coord = _make_coordinator(with_client=True)
        coord._video_stopped_by_user = True

        mock_session = MagicMock()
        mock_session.start = AsyncMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
            return_value=mock_session,
        ):
            await coord.async_start_video(auto_timeout=True, by_user=True)

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
        coord = _make_coordinator(with_client=True)
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
        coord = _make_coordinator(with_client=True)
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
        coord = _make_coordinator(with_client=True)
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
        """async_start_video(by_user=True) clears _video_stopped_by_user before starting."""
        coord = _make_coordinator(with_client=True)
        coord._video_stopped_by_user = True
        mock_session = MagicMock()
        mock_session.start = AsyncMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
            return_value=mock_session,
        ):
            await coord.async_start_video(by_user=True)

        assert coord._video_stopped_by_user is False


# ---------------------------------------------------------------------------
# Callback registration — add_stop_video_callback / add_video_state_change_callback
# ---------------------------------------------------------------------------


class TestCallbackRegistration:
    @pytest.mark.asyncio
    async def test_stop_video_callback_called_on_stop(self):
        """Callbacks registered via add_stop_video_callback fire during async_stop_video."""
        coord = _make_coordinator()
        mock_session = MagicMock()
        mock_session.stop = AsyncMock()
        coord._video_session = mock_session

        fired = []
        async def cb():
            fired.append(True)

        coord.add_stop_video_callback(cb)
        await coord.async_stop_video()

        assert fired == [True]

    @pytest.mark.asyncio
    async def test_stop_video_callback_remove_works(self):
        """The remove callable returned by add_stop_video_callback prevents future calls."""
        coord = _make_coordinator()
        mock_session = MagicMock()
        mock_session.stop = AsyncMock()
        coord._video_session = mock_session

        fired = []
        async def cb():
            fired.append(True)

        remove = coord.add_stop_video_callback(cb)
        remove()
        await coord.async_stop_video()

        assert fired == []

    @pytest.mark.asyncio
    async def test_stop_video_callback_exception_does_not_abort_stop(self):
        """An exception in a stop-video callback must not prevent the session from stopping."""
        coord = _make_coordinator()
        mock_session = MagicMock()
        mock_session.stop = AsyncMock()
        coord._video_session = mock_session

        async def bad_cb():
            raise RuntimeError("callback error")

        coord.add_stop_video_callback(bad_cb)
        await coord.async_stop_video()  # must not raise

        mock_session.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_video_state_change_callback_called_after_start(self):
        """add_video_state_change_callback fires after async_start_video completes."""
        coord = _make_coordinator(with_client=True)
        mock_session = MagicMock()
        mock_session.start = AsyncMock()

        fired = []
        async def cb():
            fired.append(True)

        coord.add_video_state_change_callback(cb)

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
            return_value=mock_session,
        ):
            await coord.async_start_video()

        assert fired == [True]

    @pytest.mark.asyncio
    async def test_video_state_change_callback_called_after_stop(self):
        """add_video_state_change_callback fires after async_stop_video completes."""
        coord = _make_coordinator()
        mock_session = MagicMock()
        mock_session.stop = AsyncMock()
        coord._video_session = mock_session

        fired = []
        async def cb():
            fired.append(True)

        coord.add_video_state_change_callback(cb)
        await coord.async_stop_video()

        assert fired == [True]

    @pytest.mark.asyncio
    async def test_video_state_change_callback_remove_works(self):
        """The remove callable prevents future firings."""
        coord = _make_coordinator(with_client=True)
        mock_session = MagicMock()
        mock_session.start = AsyncMock()

        fired = []
        async def cb():
            fired.append(True)

        remove = coord.add_video_state_change_callback(cb)
        remove()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VideoCallSession",
            return_value=mock_session,
        ):
            await coord.async_start_video()

        assert fired == []


# ---------------------------------------------------------------------------
# async_open_door dispatch
# ---------------------------------------------------------------------------


class TestAsyncOpenDoor:
    @pytest.mark.asyncio
    async def test_uses_video_session_when_active(self):
        """async_open_door delegates to video session when one is active."""
        coord = _make_coordinator(with_client=True)
        door = MagicMock()
        door.output_index = 0

        session = MagicMock()
        session.active = True
        session.async_open_door_on_ctpp = AsyncMock()
        coord._video_session = session
        coord._config.apt_address = "SB000006"
        coord._config.apt_subaddress = 1
        coord._config.caller_address = None

        await coord.async_open_door(door)

        session.async_open_door_on_ctpp.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uses_open_door_when_no_video_session(self):
        """async_open_door delegates to open_door when no video session is active."""
        coord = _make_coordinator(with_client=True)
        door = MagicMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.open_door",
            new_callable=AsyncMock,
        ) as mock_open_door:
            await coord.async_open_door(door)

        mock_open_door.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self):
        """async_open_door raises RuntimeError when client is None."""
        coord = _make_coordinator()
        coord._client = None
        door = MagicMock()

        with pytest.raises(RuntimeError, match="Not connected"):
            await coord.async_open_door(door)
