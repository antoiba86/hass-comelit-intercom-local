"""Unit tests for door open sequences — no device needed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.door import open_door
from custom_components.comelit_intercom_local.exceptions import DoorOpenError
from custom_components.comelit_intercom_local.models import DeviceConfig, Door

HOST = "127.0.0.1"
PORT = 64100
TOKEN = "test_token"


def _make_door(*, is_actuator: bool = False, output_index: int = 0) -> Door:
    return Door(
        id=0,
        index=0,
        name="Main Door",
        apt_address="SB100001",
        output_index=output_index,
        is_actuator=is_actuator,
    )


def _make_config() -> DeviceConfig:
    return DeviceConfig(
        apt_address="SB000006",
        apt_subaddress=1,
        doors=[],
        cameras=[],
    )


def _make_client(ctpp_channel=None) -> MagicMock:
    client = MagicMock()
    client.send_binary = AsyncMock()
    client.read_response = AsyncMock(return_value=None)
    client.get_channel = MagicMock(return_value=ctpp_channel)
    client.open_channel = AsyncMock(return_value=MagicMock())
    client.remove_channel = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# open_door — fast path (existing CTPP channel reused)
# ---------------------------------------------------------------------------


class TestOpenDoorFastPath:
    @pytest.mark.asyncio
    async def test_sends_full_sequence_for_normal_door(self):
        """open_door runs OPEN+CONFIRM → door_init → OPEN+CONFIRM on the existing CTPP channel."""
        channel = MagicMock()
        client = _make_client(ctpp_channel=channel)
        config = _make_config()
        door = _make_door()

        await open_door(HOST, PORT, TOKEN, client, config, door)

        # OPEN + CONFIRM + door_init + OPEN + CONFIRM = 5 sends
        assert client.send_binary.await_count == 5
        # door_init is followed by draining 2 responses
        assert client.read_response.await_count == 2

    @pytest.mark.asyncio
    async def test_uses_existing_ctpp_channel(self):
        """open_door uses the channel returned by get_channel('CTPP')."""
        channel = MagicMock()
        client = _make_client(ctpp_channel=channel)
        config = _make_config()
        door = _make_door()

        await open_door(HOST, PORT, TOKEN, client, config, door)

        client.get_channel.assert_called_with("CTPP")
        for c in client.send_binary.call_args_list:
            assert c.args[0] is channel

    @pytest.mark.asyncio
    async def test_does_not_create_new_client_on_fast_path(self):
        """open_door does not open a new connection when a CTPP channel is already available."""
        channel = MagicMock()
        client = _make_client(ctpp_channel=channel)
        config = _make_config()
        door = _make_door()

        with patch(
            "custom_components.comelit_intercom_local.door.IconaBridgeClient"
        ) as mock_cls:
            await open_door(HOST, PORT, TOKEN, client, config, door)

        mock_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_actuator_sequence_for_actuator_door(self):
        """open_door routes actuator doors through the actuator-specific sequence."""
        channel = MagicMock()
        client = _make_client(ctpp_channel=channel)
        config = _make_config()
        door = _make_door(is_actuator=True)

        await open_door(HOST, PORT, TOKEN, client, config, door)

        # actuator_init + actuator_open + actuator_confirm = 3 sends
        assert client.send_binary.await_count == 3
        assert client.read_response.await_count == 2

    @pytest.mark.asyncio
    async def test_wraps_exception_in_door_open_error(self):
        """Any send failure is wrapped in DoorOpenError."""
        channel = MagicMock()
        client = _make_client(ctpp_channel=channel)
        client.send_binary = AsyncMock(side_effect=OSError("network error"))
        config = _make_config()
        door = _make_door()

        with pytest.raises(DoorOpenError, match="Failed to open door"):
            await open_door(HOST, PORT, TOKEN, client, config, door)


# ---------------------------------------------------------------------------
# open_door — standalone path (no CTPP channel open, new client created)
# ---------------------------------------------------------------------------


def _standalone_patches():
    """Return a tuple of patches needed for the standalone path tests.

    Patches IconaBridgeClient, authenticate, and ctpp_init_sequence so no
    real I/O happens. Callers are responsible for setting the mock_cls
    return_value to a _make_client() instance before calling open_door.
    """
    return (
        patch("custom_components.comelit_intercom_local.door.IconaBridgeClient"),
        patch("custom_components.comelit_intercom_local.door.authenticate", new_callable=AsyncMock),
        patch("custom_components.comelit_intercom_local.door.ctpp_init_sequence", new_callable=AsyncMock),
    )


class TestOpenDoorStandalonePath:
    @pytest.mark.asyncio
    async def test_creates_new_client_when_no_ctpp(self):
        """open_door creates a new IconaBridgeClient using host/port when no CTPP is open."""
        client = _make_client(ctpp_channel=None)
        config = _make_config()
        door = _make_door()

        p_cls, p_auth, p_init = _standalone_patches()
        with p_cls as mock_cls, p_auth, p_init:
            mock_cls.return_value = _make_client()
            await open_door(HOST, PORT, TOKEN, client, config, door)

        mock_cls.assert_called_once_with(HOST, PORT)

    @pytest.mark.asyncio
    async def test_connects_and_authenticates_new_client(self):
        """open_door connects and authenticates the internally created client."""
        client = _make_client(ctpp_channel=None)
        config = _make_config()
        door = _make_door()

        p_cls, p_auth, p_init = _standalone_patches()
        with p_cls as mock_cls, p_auth as mock_auth, p_init:
            mock_inner = _make_client()
            mock_cls.return_value = mock_inner
            await open_door(HOST, PORT, TOKEN, client, config, door)

        mock_inner.connect.assert_awaited_once()
        mock_auth.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_calls_ctpp_init_sequence(self):
        """open_door calls ctpp_init_sequence on the standalone path."""
        client = _make_client(ctpp_channel=None)
        config = _make_config()
        door = _make_door()

        p_cls, p_auth, p_init = _standalone_patches()
        with p_cls as mock_cls, p_auth, p_init as mock_init:
            mock_cls.return_value = _make_client()
            await open_door(HOST, PORT, TOKEN, client, config, door)

        mock_init.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sends_full_sequence_after_init(self):
        """After handshake, OPEN+CONFIRM → door_init → OPEN+CONFIRM = 5 sends on the new client."""
        client = _make_client(ctpp_channel=None)
        config = _make_config()
        door = _make_door()

        p_cls, p_auth, p_init = _standalone_patches()
        with p_cls as mock_cls, p_auth, p_init:
            mock_inner = _make_client()
            mock_cls.return_value = mock_inner
            await open_door(HOST, PORT, TOKEN, client, config, door)

        assert mock_inner.send_binary.await_count == 5
        assert mock_inner.read_response.await_count == 2

    @pytest.mark.asyncio
    async def test_actuator_sends_three_commands(self):
        """Actuator: actuator_init + actuator_open + actuator_confirm = 3 sends."""
        client = _make_client(ctpp_channel=None)
        config = _make_config()
        door = _make_door(is_actuator=True)

        p_cls, p_auth, p_init = _standalone_patches()
        with p_cls as mock_cls, p_auth, p_init:
            mock_inner = _make_client()
            mock_cls.return_value = mock_inner
            await open_door(HOST, PORT, TOKEN, client, config, door)

        assert mock_inner.send_binary.await_count == 3
        assert mock_inner.read_response.await_count == 2

    @pytest.mark.asyncio
    async def test_disconnects_new_client_in_finally(self):
        """open_door removes the CTPP channel and disconnects the new client when done."""
        client = _make_client(ctpp_channel=None)
        config = _make_config()
        door = _make_door()

        p_cls, p_auth, p_init = _standalone_patches()
        with p_cls as mock_cls, p_auth, p_init:
            mock_inner = _make_client()
            mock_cls.return_value = mock_inner
            await open_door(HOST, PORT, TOKEN, client, config, door)

        mock_inner.remove_channel.assert_called_with("CTPP")
        mock_inner.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnects_new_client_on_failure(self):
        """The new client is always disconnected, even when door send fails."""
        client = _make_client(ctpp_channel=None)
        config = _make_config()
        door = _make_door()

        p_cls, p_auth, p_init = _standalone_patches()
        with p_cls as mock_cls, p_auth, p_init:
            mock_inner = _make_client()
            mock_inner.send_binary = AsyncMock(side_effect=OSError("bang"))
            mock_cls.return_value = mock_inner
            with pytest.raises(DoorOpenError):
                await open_door(HOST, PORT, TOKEN, client, config, door)

        mock_inner.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wraps_exception_in_door_open_error(self):
        """Any failure during connection or door sequence is wrapped in DoorOpenError."""
        client = _make_client(ctpp_channel=None)
        config = _make_config()
        door = _make_door()

        p_cls, p_auth, p_init = _standalone_patches()
        with p_cls as mock_cls, p_auth, p_init:
            mock_inner = _make_client()
            mock_inner.connect = AsyncMock(side_effect=OSError("cannot connect"))
            mock_cls.return_value = mock_inner
            with pytest.raises(DoorOpenError, match="Failed to open door"):
                await open_door(HOST, PORT, TOKEN, client, config, door)
