"""Unit tests for door open sequences — no device needed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.door import open_door_fast, open_door_standalone
from custom_components.comelit_intercom_local.exceptions import DoorOpenError
from custom_components.comelit_intercom_local.models import DeviceConfig, Door


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
    client.get_channel = MagicMock(return_value=ctpp_channel)
    client.open_channel = AsyncMock(return_value=MagicMock())
    client.remove_channel = MagicMock()
    return client


# ---------------------------------------------------------------------------
# open_door_fast
# ---------------------------------------------------------------------------


class TestOpenDoorFast:
    @pytest.mark.asyncio
    async def test_raises_when_ctpp_not_open(self):
        """open_door_fast raises DoorOpenError when no CTPP channel is available."""
        client = _make_client(ctpp_channel=None)
        config = _make_config()
        door = _make_door()

        with pytest.raises(DoorOpenError, match="CTPP channel not open"):
            await open_door_fast(client, config, door)

    @pytest.mark.asyncio
    async def test_sends_open_and_confirm_twice_for_normal_door(self):
        """open_door_fast sends OPEN_DOOR + CONFIRM twice on the existing CTPP channel."""
        channel = MagicMock()
        client = _make_client(ctpp_channel=channel)
        config = _make_config()
        door = _make_door()

        await open_door_fast(client, config, door)

        # 2 pairs of (OPEN_DOOR, CONFIRM) = 4 sends total
        assert client.send_binary.await_count == 4

    @pytest.mark.asyncio
    async def test_uses_existing_ctpp_channel(self):
        """open_door_fast uses the channel returned by get_channel('CTPP')."""
        channel = MagicMock()
        client = _make_client(ctpp_channel=channel)
        config = _make_config()
        door = _make_door()

        await open_door_fast(client, config, door)

        client.get_channel.assert_called_with("CTPP")
        # All sends must use the same channel object
        for c in client.send_binary.call_args_list:
            assert c.args[0] is channel

    @pytest.mark.asyncio
    async def test_sends_actuator_commands_for_actuator_door(self):
        """open_door_fast routes actuator doors through _send_actuator_open."""
        channel = MagicMock()
        client = _make_client(ctpp_channel=channel)
        config = _make_config()
        door = _make_door(is_actuator=True)

        await open_door_fast(client, config, door)

        # Actuator sends 2 messages (open + confirm), not 4
        assert client.send_binary.await_count == 2

    @pytest.mark.asyncio
    async def test_wraps_exception_in_door_open_error(self):
        """Any send failure is wrapped in DoorOpenError."""
        channel = MagicMock()
        client = _make_client(ctpp_channel=channel)
        client.send_binary = AsyncMock(side_effect=OSError("network error"))
        config = _make_config()
        door = _make_door()

        with pytest.raises(DoorOpenError, match="Failed to open door"):
            await open_door_fast(client, config, door)


# ---------------------------------------------------------------------------
# open_door_standalone
# ---------------------------------------------------------------------------


class TestOpenDoorStandalone:
    @pytest.mark.asyncio
    async def test_opens_ctpp_door_channel(self):
        """open_door_standalone opens a transient CTPP_DOOR channel."""
        client = _make_client()
        config = _make_config()
        door = _make_door()

        with patch("custom_components.comelit_intercom_local.door.ctpp_init_sequence", new_callable=AsyncMock):
            await open_door_standalone(client, config, door)

        open_calls = [c.args[0] for c in client.open_channel.call_args_list]
        assert "CTPP_DOOR" in open_calls

    @pytest.mark.asyncio
    async def test_removes_channels_in_finally(self):
        """CTPP_DOOR and CSPB_DOOR are always removed, even on failure."""
        client = _make_client()
        client.send_binary = AsyncMock(side_effect=OSError("bang"))
        config = _make_config()
        door = _make_door()

        with patch("custom_components.comelit_intercom_local.door.ctpp_init_sequence", new_callable=AsyncMock):
            with pytest.raises(DoorOpenError):
                await open_door_standalone(client, config, door)

        removed = {c.args[0] for c in client.remove_channel.call_args_list}
        assert "CTPP_DOOR" in removed
        assert "CSPB_DOOR" in removed

    @pytest.mark.asyncio
    async def test_calls_ctpp_init_sequence(self):
        """open_door_standalone calls ctpp_init_sequence for the handshake."""
        client = _make_client()
        config = _make_config()
        door = _make_door()

        with patch(
            "custom_components.comelit_intercom_local.door.ctpp_init_sequence",
            new_callable=AsyncMock,
        ) as mock_init:
            await open_door_standalone(client, config, door)

        mock_init.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sends_open_commands_after_init(self):
        """After init, open_door_standalone sends OPEN_DOOR + CONFIRM twice."""
        client = _make_client()
        config = _make_config()
        door = _make_door()

        with patch("custom_components.comelit_intercom_local.door.ctpp_init_sequence", new_callable=AsyncMock):
            await open_door_standalone(client, config, door)

        assert client.send_binary.await_count == 4

    @pytest.mark.asyncio
    async def test_actuator_sends_two_commands(self):
        """Actuator door sends only 2 commands (open + confirm), not 4."""
        client = _make_client()
        config = _make_config()
        door = _make_door(is_actuator=True)

        with patch("custom_components.comelit_intercom_local.door.ctpp_init_sequence", new_callable=AsyncMock):
            await open_door_standalone(client, config, door)

        assert client.send_binary.await_count == 2

    @pytest.mark.asyncio
    async def test_wraps_exception_in_door_open_error(self):
        """Any failure is wrapped in DoorOpenError."""
        client = _make_client()
        client.open_channel = AsyncMock(side_effect=OSError("cannot connect"))
        config = _make_config()
        door = _make_door()

        with pytest.raises(DoorOpenError, match="Failed to open door"):
            await open_door_standalone(client, config, door)
