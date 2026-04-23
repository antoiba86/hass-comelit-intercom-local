"""End-to-end door open flow tests.

These tests exercise the full chain:
    open_door → open_ctpp_channel → ctpp_init_sequence → _open_door_on_channel

Only the TCP client is mocked. No intermediate functions are patched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.door import open_door
from custom_components.comelit_intercom_local.exceptions import DoorOpenError
from custom_components.comelit_intercom_local.models import DeviceConfig, Door


def _make_door(*, is_actuator: bool = False) -> Door:
    return Door(
        id=0, index=0, name="Test Door",
        apt_address="SB100001", output_index=1,
        is_actuator=is_actuator,
    )


def _make_config() -> DeviceConfig:
    return DeviceConfig(
        apt_address="SB000006", apt_subaddress=1,
        doors=[], cameras=[],
    )


def _make_client(*, ctpp_channel=None) -> MagicMock:
    """Mock client with only TCP methods stubbed."""
    client = MagicMock()
    client.get_channel = MagicMock(return_value=ctpp_channel)
    client.open_channel = AsyncMock(return_value=MagicMock())
    client.send_binary = AsyncMock()
    client.read_response = AsyncMock(return_value=None)
    client.remove_channel = MagicMock()
    return client


# ---------------------------------------------------------------------------
# Standalone path (no CTPP channel open)
# ---------------------------------------------------------------------------


class TestStandalonePath:
    @pytest.mark.asyncio
    async def test_regular_door_send_count(self):
        """ctpp_init + OPEN+CONFIRM + door_init + OPEN+CONFIRM = 6 sends."""
        client = _make_client()
        await open_door(client, _make_config(), _make_door())
        assert client.send_binary.await_count == 6

    @pytest.mark.asyncio
    async def test_regular_door_read_count(self):
        """2 ctpp drain + 2 door_init drain = 4 reads."""
        client = _make_client()
        await open_door(client, _make_config(), _make_door())
        assert client.read_response.await_count == 4

    @pytest.mark.asyncio
    async def test_no_ack_pair_sent(self):
        """ctpp_init_sequence must be called with send_ack=False on the standalone path.

        The real function still runs (this is a spy, not a stub) — we just
        capture the kwargs to verify the argument is passed correctly.
        """
        import custom_components.comelit_intercom_local.door as door_mod
        from custom_components.comelit_intercom_local.ctpp import ctpp_init_sequence as real_fn

        captured: dict = {}

        async def spy(*args, **kwargs):
            captured.update(kwargs)
            return await real_fn(*args, **kwargs)

        client = _make_client()
        with patch.object(door_mod, "ctpp_init_sequence", spy):
            await open_door(client, _make_config(), _make_door())

        assert captured.get("send_ack") is False

    @pytest.mark.asyncio
    async def test_actuator_send_count(self):
        """ctpp_init + actuator_init + actuator_open × 2 = 4 sends."""
        client = _make_client()
        await open_door(client, _make_config(), _make_door(is_actuator=True))
        assert client.send_binary.await_count == 4

    @pytest.mark.asyncio
    async def test_actuator_read_count(self):
        """2 ctpp drain + 2 actuator_init drain = 4 reads."""
        client = _make_client()
        await open_door(client, _make_config(), _make_door(is_actuator=True))
        assert client.read_response.await_count == 4

    @pytest.mark.asyncio
    async def test_opens_ctpp_door_channel(self):
        """Standalone path opens a CTPP_DOOR channel."""
        client = _make_client()
        await open_door(client, _make_config(), _make_door())
        open_calls = [c.args[0] for c in client.open_channel.call_args_list]
        assert "CTPP_DOOR" in open_calls

    @pytest.mark.asyncio
    async def test_removes_ctpp_door_on_success(self):
        """CTPP_DOOR channel is removed after a successful door open."""
        client = _make_client()
        await open_door(client, _make_config(), _make_door())
        client.remove_channel.assert_called_with("CTPP_DOOR")

    @pytest.mark.asyncio
    async def test_removes_ctpp_door_on_failure(self):
        """CTPP_DOOR channel is removed even when the door command fails."""
        client = _make_client()
        # First send (ctpp_init) succeeds; second send (OPEN_DOOR) fails.
        client.send_binary = AsyncMock(side_effect=[None, OSError("network error")])
        with pytest.raises(DoorOpenError):
            await open_door(client, _make_config(), _make_door())
        client.remove_channel.assert_called_with("CTPP_DOOR")


# ---------------------------------------------------------------------------
# Fast path (existing CTPP channel)
# ---------------------------------------------------------------------------


class TestFastPath:
    @pytest.mark.asyncio
    async def test_regular_door_send_count(self):
        """OPEN+CONFIRM + door_init + OPEN+CONFIRM = 5 sends (no ctpp_init)."""
        client = _make_client(ctpp_channel=MagicMock())
        await open_door(client, _make_config(), _make_door())
        assert client.send_binary.await_count == 5

    @pytest.mark.asyncio
    async def test_regular_door_read_count(self):
        """2 door_init drain reads only (no ctpp drain)."""
        client = _make_client(ctpp_channel=MagicMock())
        await open_door(client, _make_config(), _make_door())
        assert client.read_response.await_count == 2

    @pytest.mark.asyncio
    async def test_actuator_send_count(self):
        """actuator_init + actuator_open × 2 = 3 sends."""
        client = _make_client(ctpp_channel=MagicMock())
        await open_door(client, _make_config(), _make_door(is_actuator=True))
        assert client.send_binary.await_count == 3

    @pytest.mark.asyncio
    async def test_no_channel_open(self):
        """Fast path must not open any new channels."""
        client = _make_client(ctpp_channel=MagicMock())
        await open_door(client, _make_config(), _make_door())
        client.open_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_remove_channel(self):
        """Fast path must not remove any channels."""
        client = _make_client(ctpp_channel=MagicMock())
        await open_door(client, _make_config(), _make_door())
        client.remove_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_existing_channel_for_all_sends(self):
        """All sends go through the existing CTPP channel object."""
        channel = MagicMock()
        client = _make_client(ctpp_channel=channel)
        await open_door(client, _make_config(), _make_door())
        for call in client.send_binary.call_args_list:
            assert call.args[0] is channel
