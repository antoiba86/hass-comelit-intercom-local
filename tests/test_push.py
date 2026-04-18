"""Unit tests for push notification parsing and registration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.comelit_intercom_local.push import (
    _parse_push_event,
    register_push,
    send_push_keepalive,
)
from custom_components.comelit_intercom_local.models import DeviceConfig, PushEvent


# ---------------------------------------------------------------------------
# _parse_push_event
# ---------------------------------------------------------------------------


class TestParsePushEvent:
    def test_incoming_call_returns_doorbell_ring(self):
        event = _parse_push_event({"message": "incoming-call", "apt-address": "SB000006"})
        assert event is not None
        assert event.event_type == "doorbell_ring"
        assert event.apt_address == "SB000006"

    def test_push_incoming_call_returns_doorbell_ring(self):
        event = _parse_push_event({"message": "push-incoming-call", "apt-address": "SB000001"})
        assert event is not None
        assert event.event_type == "doorbell_ring"

    def test_missed_call_returns_missed_call(self):
        event = _parse_push_event({"message": "missed-call", "apt-address": "SB000006"})
        assert event is not None
        assert event.event_type == "missed_call"

    def test_push_missed_call_returns_missed_call(self):
        event = _parse_push_event({"message": "push-missed-call"})
        assert event is not None
        assert event.event_type == "missed_call"

    def test_unknown_message_returns_none(self):
        event = _parse_push_event({"message": "some-unknown-type"})
        assert event is None

    def test_empty_message_returns_none(self):
        event = _parse_push_event({})
        assert event is None

    def test_apt_address_defaults_to_empty_string(self):
        event = _parse_push_event({"message": "incoming-call"})
        assert event is not None
        assert event.apt_address == ""

    def test_raw_included_in_event(self):
        raw = {"message": "incoming-call", "apt-address": "SB000006", "extra": "data"}
        event = _parse_push_event(raw)
        assert event is not None
        assert event.raw is raw

    def test_timestamp_is_set(self):
        event = _parse_push_event({"message": "incoming-call"})
        assert event is not None
        assert event.timestamp > 0


# ---------------------------------------------------------------------------
# register_push
# ---------------------------------------------------------------------------


def _make_device_config():
    return DeviceConfig(
        apt_address="SB000006",
        apt_subaddress="1",
        doors=[],
        cameras=[],
    )


class TestRegisterPush:
    @pytest.mark.asyncio
    async def test_register_push_opens_channel_and_sends_json(self):
        """register_push opens the PUSH channel, sends registration, sets callback."""
        fake_channel = MagicMock()
        client = MagicMock()
        client.open_channel = AsyncMock(return_value=fake_channel)
        client.send_json = AsyncMock(return_value={"response-code": 200})
        client.set_push_callback = MagicMock()

        config = _make_device_config()
        received = []

        await register_push(client, config, lambda e: received.append(e))

        client.open_channel.assert_called_once_with("PUSH", client.open_channel.call_args[0][1])
        client.send_json.assert_called_once()
        client.set_push_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_push_callback_fires_on_push(self):
        """The installed callback parses events and calls the user callback."""
        fake_channel = MagicMock()
        client = MagicMock()
        client.open_channel = AsyncMock(return_value=fake_channel)
        client.send_json = AsyncMock(return_value={})

        installed_callback = None

        def capture_callback(cb):
            nonlocal installed_callback
            installed_callback = cb

        client.set_push_callback = capture_callback

        config = _make_device_config()
        received: list[PushEvent] = []

        await register_push(client, config, lambda e: received.append(e))

        assert installed_callback is not None
        installed_callback({"message": "incoming-call", "apt-address": "SB000006"})
        assert len(received) == 1
        assert received[0].event_type == "doorbell_ring"

    @pytest.mark.asyncio
    async def test_register_push_callback_ignores_unknown_events(self):
        """Unknown push message types do not trigger the user callback."""
        fake_channel = MagicMock()
        client = MagicMock()
        client.open_channel = AsyncMock(return_value=fake_channel)
        client.send_json = AsyncMock(return_value={})

        installed_callback = None
        client.set_push_callback = lambda cb: setattr(
            type("_", (), {})(), "_", None
        ) or globals().update({"_cb": cb}) or (lambda: None)()

        # Simpler capture
        captured = []
        client.set_push_callback = lambda cb: captured.append(cb)

        config = _make_device_config()
        received: list[PushEvent] = []

        await register_push(client, config, lambda e: received.append(e))

        cb = captured[0]
        cb({"message": "some-unknown-thing"})
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_register_push_json_contains_apt_address(self):
        """Registration message includes the device apt_address."""
        fake_channel = MagicMock()
        client = MagicMock()
        client.open_channel = AsyncMock(return_value=fake_channel)
        client.send_json = AsyncMock(return_value={})
        client.set_push_callback = MagicMock()

        config = _make_device_config()
        await register_push(client, config, lambda e: None)

        sent_msg = client.send_json.call_args[0][1]
        assert sent_msg["apt-address"] == "SB000006"
        assert sent_msg["apt-subaddress"] == "1"
        assert sent_msg["message"] == "push-info"


# ---------------------------------------------------------------------------
# send_push_keepalive
# ---------------------------------------------------------------------------


class TestSendPushKeepalive:
    @pytest.mark.asyncio
    async def test_raises_when_push_channel_not_open(self):
        """send_push_keepalive raises RuntimeError when PUSH channel is None."""
        client = MagicMock()
        client.get_channel = MagicMock(return_value=None)
        config = _make_device_config()

        with pytest.raises(RuntimeError, match="PUSH channel not open"):
            await send_push_keepalive(client, config)

    @pytest.mark.asyncio
    async def test_sends_push_info_on_existing_channel(self):
        """send_push_keepalive sends push-info JSON on the already-open PUSH channel."""
        channel = MagicMock()
        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)
        client.send_json = AsyncMock(return_value={})
        config = _make_device_config()

        await send_push_keepalive(client, config)

        client.send_json.assert_awaited_once()
        sent_channel, sent_msg = client.send_json.call_args.args
        assert sent_channel is channel
        assert sent_msg["message"] == "push-info"

    @pytest.mark.asyncio
    async def test_keepalive_message_contains_apt_address(self):
        """Keepalive message includes the device apt_address and subaddress."""
        channel = MagicMock()
        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)
        client.send_json = AsyncMock(return_value={})
        config = _make_device_config()

        await send_push_keepalive(client, config)

        sent_msg = client.send_json.call_args.args[1]
        assert sent_msg["apt-address"] == "SB000006"
        assert sent_msg["apt-subaddress"] == "1"

    @pytest.mark.asyncio
    async def test_keepalive_propagates_send_error(self):
        """A send failure propagates so the caller can detect dead connections."""
        channel = MagicMock()
        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)
        client.send_json = AsyncMock(side_effect=OSError("broken pipe"))
        config = _make_device_config()

        with pytest.raises(OSError):
            await send_push_keepalive(client, config)
