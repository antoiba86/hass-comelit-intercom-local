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
    coordinator._ctpp_init_ts = 0
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
        """async_open_door delegates to open_door with host/port/token/client/config/door."""
        coord = _make_coordinator(with_client=True)
        door = MagicMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.open_door",
            new_callable=AsyncMock,
        ) as mock_open_door:
            await coord.async_open_door(door)

        mock_open_door.assert_awaited_once_with(
            coord.host, coord.port, coord.token, coord._client, coord._config, door
        )

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self):
        """async_open_door raises RuntimeError when client is None."""
        coord = _make_coordinator()
        coord._client = None
        door = MagicMock()

        with pytest.raises(RuntimeError, match="Not connected"):
            await coord.async_open_door(door)


# ---------------------------------------------------------------------------
# Phase 2a — finally-guarded VIP listener restart in async_stop_video
# ---------------------------------------------------------------------------


class TestStopVideoFinally:
    @pytest.mark.asyncio
    async def test_vip_listener_restarted_even_if_session_stop_raises(self):
        """VIP listener is restarted in finally even when session.stop() raises."""
        coord = _make_coordinator(with_client=True)

        mock_session = MagicMock()
        mock_session.stop = AsyncMock(side_effect=RuntimeError("teardown failed"))
        coord._video_session = mock_session

        mock_vip = MagicMock()
        mock_vip.start = AsyncMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VipEventListener",
            return_value=mock_vip,
        ):
            with pytest.raises(RuntimeError, match="teardown failed"):
                await coord.async_stop_video()

        mock_vip.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_vip_listener_restarted_after_normal_stop(self):
        """VIP listener is restarted after a normal (no-exception) session stop."""
        coord = _make_coordinator(with_client=True)

        mock_session = MagicMock()
        mock_session.stop = AsyncMock()
        coord._video_session = mock_session

        mock_vip = MagicMock()
        mock_vip.start = AsyncMock()

        with patch(
            "custom_components.comelit_intercom_local.coordinator.VipEventListener",
            return_value=mock_vip,
        ):
            await coord.async_stop_video()

        mock_vip.start.assert_awaited_once()


# ---------------------------------------------------------------------------
# Phase 2b — reconnect retry loop on ECONNREFUSED
# ---------------------------------------------------------------------------


class TestReconnectRetry:
    def _patch_reconnect_deps(self, mock_client):
        """Return a context manager patching all _reconnect dependencies."""
        return (
            patch(
                "custom_components.comelit_intercom_local.coordinator.IconaBridgeClient",
                return_value=mock_client,
            ),
            patch(
                "custom_components.comelit_intercom_local.coordinator.authenticate",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.comelit_intercom_local.coordinator.get_device_config",
                new_callable=AsyncMock,
                return_value=MagicMock(doors=[], cameras=[]),
            ),
            patch(
                "custom_components.comelit_intercom_local.coordinator.register_push",
                new_callable=AsyncMock,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        )

    @pytest.mark.asyncio
    async def test_retries_on_connection_refused_and_succeeds(self):
        """_reconnect retries connect() on ECONNREFUSED and succeeds on 3rd attempt."""
        coord = _make_coordinator()
        coord.config_entry.options = {"enable_notifications": False}

        attempt = 0

        async def connect_impl():
            nonlocal attempt
            attempt += 1
            if attempt <= 2:
                raise ConnectionRefusedError("ECONNREFUSED")

        mock_client = MagicMock()
        mock_client.connected = True
        mock_client.connect = AsyncMock(side_effect=connect_impl)
        mock_client.set_disconnect_callback = MagicMock()
        mock_client.get_channel = MagicMock(return_value=None)

        p1, p2, p3, p4, p5 = self._patch_reconnect_deps(mock_client)
        with p1, p2, p3, p4, p5:
            await coord._reconnect()

        assert attempt == 3

    @pytest.mark.asyncio
    async def test_raises_update_failed_after_all_attempts_fail(self):
        """_reconnect raises after exhausting all retry attempts."""
        coord = _make_coordinator()
        coord.config_entry.options = {"enable_notifications": False}

        mock_client = MagicMock()
        mock_client.connect = AsyncMock(side_effect=ConnectionRefusedError("ECONNREFUSED"))
        mock_client.disconnect = AsyncMock()
        mock_client.set_disconnect_callback = MagicMock()

        p1, p2, p3, p4, p5 = self._patch_reconnect_deps(mock_client)
        with p1, p2, p3, p4, p5:
            with pytest.raises(ConnectionRefusedError):
                await coord._reconnect()

    @pytest.mark.asyncio
    async def test_auth_failure_not_retried(self):
        """Auth failures are not caught by the connect retry loop."""
        coord = _make_coordinator()
        coord.config_entry.options = {"enable_notifications": False}

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.set_disconnect_callback = MagicMock()

        p1 = patch(
            "custom_components.comelit_intercom_local.coordinator.IconaBridgeClient",
            return_value=mock_client,
        )
        p2 = patch(
            "custom_components.comelit_intercom_local.coordinator.authenticate",
            new_callable=AsyncMock,
            side_effect=RuntimeError("bad token"),
        )
        p3 = patch("asyncio.sleep", new_callable=AsyncMock)

        with p1, p2, p3:
            with pytest.raises(RuntimeError, match="bad token"):
                await coord._reconnect()

        # connect was called exactly once (no retry for auth failures)
        mock_client.connect.assert_awaited_once()
