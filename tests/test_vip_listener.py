"""Unit tests for VipEventListener and parse_ctpp_message."""

from __future__ import annotations

import asyncio
import struct
import time
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from custom_components.comelit_intercom_local.models import DeviceConfig, PushEvent
from custom_components.comelit_intercom_local.vip_listener import (
    ACTION_CLOSED,
    ACTION_CONNECTED,
    ACTION_DOOR_OPENED,
    ACTION_IDLE,
    ACTION_IN_ALERTING,
    ACTION_OUT_ALERTING,
    ACTION_REGISTRATION_RENEWAL,
    MIN_MSG_SIZE,
    PREFIX_ACK,
    PREFIX_CALL_INIT,
    PREFIX_CONFIRM,
    PREFIX_EVENT,
    PREFIX_VIP_EVENT,
    VipEventListener,
    parse_ctpp_message,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctpp_msg(
    prefix: int,
    timestamp: int,
    action: int,
    flags: int | None = None,
    addresses: list[str] | None = None,
) -> bytes:
    """Build a binary CTPP message for testing."""
    buf = bytearray()
    buf += struct.pack("<H", prefix)
    buf += struct.pack("<I", timestamp)
    buf += struct.pack(">H", action)
    if flags is not None:
        buf += struct.pack(">H", flags)
    if addresses:
        buf += b"\xff\xff\xff\xff"
        for addr in addresses:
            buf += addr.encode("ascii") + b"\x00"
    return bytes(buf)


def _make_config(apt_address: str = "SB000006", apt_subaddress: int = 1) -> DeviceConfig:
    return DeviceConfig(apt_address=apt_address, apt_subaddress=apt_subaddress)


def _make_listener(
    callback=None, apt_address: str = "SB000006", apt_subaddress: int = 1
) -> VipEventListener:
    client = MagicMock()
    client.send_binary = AsyncMock()
    config = _make_config(apt_address, apt_subaddress)
    cb = callback or MagicMock()
    listener = VipEventListener(client, config, cb)
    # Attach a fake open channel so send_binary works
    listener._channel = MagicMock()
    listener._channel.response_queue = asyncio.Queue()
    return listener


# ---------------------------------------------------------------------------
# parse_ctpp_message
# ---------------------------------------------------------------------------


class TestParseCtppMessage:
    def test_returns_none_for_too_short(self):
        for n in range(MIN_MSG_SIZE):
            assert parse_ctpp_message(b"\x00" * n) is None

    def test_parses_prefix_le16(self):
        data = _make_ctpp_msg(PREFIX_VIP_EVENT, 0x12345678, 0x0001)
        msg = parse_ctpp_message(data)
        assert msg["prefix"] == PREFIX_VIP_EVENT

    def test_parses_timestamp_le32(self):
        data = _make_ctpp_msg(PREFIX_VIP_EVENT, 0xDEADBEEF, 0x0002)
        msg = parse_ctpp_message(data)
        assert msg["timestamp"] == 0xDEADBEEF

    def test_parses_action_be16(self):
        data = _make_ctpp_msg(PREFIX_VIP_EVENT, 0, ACTION_IN_ALERTING)
        msg = parse_ctpp_message(data)
        assert msg["action"] == ACTION_IN_ALERTING

    def test_no_flags_when_less_than_10_bytes(self):
        # 8 bytes exactly — no flags
        data = b"\x60\x18" + struct.pack("<I", 0) + struct.pack(">H", 0)
        assert len(data) == MIN_MSG_SIZE
        msg = parse_ctpp_message(data)
        assert "flags" not in msg

    def test_parses_flags_when_ge_10_bytes(self):
        data = _make_ctpp_msg(PREFIX_VIP_EVENT, 0, 0, flags=0xABCD)
        msg = parse_ctpp_message(data)
        assert msg["flags"] == 0xABCD

    def test_extracts_sb_addresses(self):
        data = _make_ctpp_msg(
            PREFIX_VIP_EVENT, 0, 0, flags=0, addresses=["SB000001", "SB000006"]
        )
        msg = parse_ctpp_message(data)
        assert "SB000001" in msg["addresses"]
        assert "SB000006" in msg["addresses"]

    def test_no_addresses_when_none_present(self):
        data = _make_ctpp_msg(PREFIX_VIP_EVENT, 0, 0, flags=0)
        msg = parse_ctpp_message(data)
        assert msg["addresses"] == []

    def test_raw_bytes_included(self):
        data = _make_ctpp_msg(PREFIX_CALL_INIT, 42, 0)
        msg = parse_ctpp_message(data)
        assert msg["raw"] == data

    def test_minimum_size_exactly_parses(self):
        data = b"\x60\x18\x00\x00\x00\x00\x00\x01"  # 8 bytes
        msg = parse_ctpp_message(data)
        assert msg is not None
        assert msg["action"] == 1


# ---------------------------------------------------------------------------
# VipEventListener._fire_event — deduplication
# ---------------------------------------------------------------------------


class TestFireEvent:
    def test_fires_callback_with_push_event(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._fire_event("doorbell_ring", ["SB000001"])

        cb.assert_called_once()
        event: PushEvent = cb.call_args[0][0]
        assert event.event_type == "doorbell_ring"
        assert event.apt_address == "SB000001"

    def test_first_address_used_as_apt_address(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._fire_event("doorbell_ring", ["SB000001", "SB000006"])

        event: PushEvent = cb.call_args[0][0]
        assert event.apt_address == "SB000001"

    def test_empty_addresses_gives_empty_apt_address(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._fire_event("doorbell_ring", [])

        event: PushEvent = cb.call_args[0][0]
        assert event.apt_address == ""

    def test_duplicate_within_window_suppressed(self):
        cb = MagicMock()
        listener = _make_listener(cb)
        listener._dedup_window = 10.0

        listener._fire_event("doorbell_ring", [])
        listener._fire_event("doorbell_ring", [])

        cb.assert_called_once()

    def test_different_event_types_not_deduplicated(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._fire_event("doorbell_ring", [])
        listener._fire_event("door_opened", [])

        assert cb.call_count == 2

    def test_fires_again_after_dedup_window(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        # Pre-seed the last_fired time so it appears old
        listener._last_fired["doorbell_ring"] = time.time() - 20.0
        listener._dedup_window = 10.0

        listener._fire_event("doorbell_ring", [])

        cb.assert_called_once()

    def test_raw_includes_addresses_and_source(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._fire_event("doorbell_ring", ["SB000001"])

        event: PushEvent = cb.call_args[0][0]
        assert event.raw["source"] == "ctpp_vip"
        assert event.raw["addresses"] == ["SB000001"]

    def test_callback_exception_does_not_propagate(self):
        cb = MagicMock(side_effect=RuntimeError("boom"))
        listener = _make_listener(cb)

        # Should not raise
        listener._fire_event("doorbell_ring", [])


# ---------------------------------------------------------------------------
# VipEventListener._handle_vip_event
# ---------------------------------------------------------------------------


class TestHandleVipEvent:
    def _msg(self, prefix: int, action: int, addresses: list[str] | None = None) -> dict:
        return {
            "prefix": prefix,
            "timestamp": 0,
            "action": action,
            "flags": 0,
            "addresses": addresses or [],
        }

    def test_call_init_fires_doorbell_ring(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._handle_vip_event(self._msg(PREFIX_CALL_INIT, 0))

        cb.assert_called_once()
        assert cb.call_args[0][0].event_type == "doorbell_ring"

    def test_vip_event_in_alerting_fires_doorbell_ring(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._handle_vip_event(self._msg(PREFIX_VIP_EVENT, ACTION_IN_ALERTING))

        cb.assert_called_once()
        assert cb.call_args[0][0].event_type == "doorbell_ring"

    def test_vip_event_door_opened_fires_door_opened(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._handle_vip_event(self._msg(PREFIX_VIP_EVENT, ACTION_DOOR_OPENED))

        cb.assert_called_once()
        assert cb.call_args[0][0].event_type == "door_opened"

    def test_vip_event_connected_does_not_fire(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._handle_vip_event(self._msg(PREFIX_VIP_EVENT, ACTION_CONNECTED))

        cb.assert_not_called()

    def test_vip_event_closed_does_not_fire(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._handle_vip_event(self._msg(PREFIX_VIP_EVENT, ACTION_CLOSED))

        cb.assert_not_called()

    def test_vip_event_idle_does_not_fire(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._handle_vip_event(self._msg(PREFIX_VIP_EVENT, ACTION_IDLE))

        cb.assert_not_called()

    def test_vip_event_out_alerting_does_not_fire(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._handle_vip_event(self._msg(PREFIX_VIP_EVENT, ACTION_OUT_ALERTING))

        cb.assert_not_called()

    def test_vip_event_zero_action_does_not_fire(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._handle_vip_event(self._msg(PREFIX_VIP_EVENT, 0))

        cb.assert_not_called()

    def test_prefix_event_with_nonzero_action_does_not_fire(self):
        """0x1840 events are call-related internals, not user-visible events."""
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._handle_vip_event(self._msg(PREFIX_EVENT, 0x0001))

        cb.assert_not_called()

    def test_ack_prefix_does_not_fire(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        listener._handle_vip_event(self._msg(PREFIX_ACK, 0x0001))

        cb.assert_not_called()


# ---------------------------------------------------------------------------
# VipEventListener._process_message
# ---------------------------------------------------------------------------


class TestProcessMessage:
    @pytest.mark.asyncio
    async def test_too_short_message_ignored(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        await listener._process_message(b"\x60\x18")  # only 2 bytes

        cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_registration_renewal_sends_ack_pair_not_event(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        data = _make_ctpp_msg(PREFIX_VIP_EVENT, 0x12345678, ACTION_REGISTRATION_RENEWAL, flags=0)
        await listener._process_message(data)

        # No user-visible event
        cb.assert_not_called()
        # send_binary called twice (ACK + CONFIRM)
        assert listener._client.send_binary.await_count == 2

    @pytest.mark.asyncio
    async def test_door_opened_sends_ack_and_fires_event(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        data = _make_ctpp_msg(
            PREFIX_VIP_EVENT, 0x12345678, ACTION_DOOR_OPENED, flags=0,
            addresses=["SB000006"]
        )
        await listener._process_message(data)

        # Event fired
        cb.assert_called_once()
        assert cb.call_args[0][0].event_type == "door_opened"
        # ACK sent once
        listener._client.send_binary.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_call_init_fires_doorbell_ring(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        data = _make_ctpp_msg(PREFIX_CALL_INIT, 0xABCD, 0, flags=0)
        await listener._process_message(data)

        cb.assert_called_once()
        assert cb.call_args[0][0].event_type == "doorbell_ring"

    @pytest.mark.asyncio
    async def test_in_alerting_fires_doorbell_ring(self):
        cb = MagicMock()
        listener = _make_listener(cb)

        data = _make_ctpp_msg(PREFIX_VIP_EVENT, 0, ACTION_IN_ALERTING, flags=0)
        await listener._process_message(data)

        cb.assert_called_once()
        assert cb.call_args[0][0].event_type == "doorbell_ring"

    @pytest.mark.asyncio
    async def test_renewal_ack_uses_incremented_timestamp(self):
        """Renewal ACK timestamp must be msg timestamp + 0x01000000."""
        cb = MagicMock()
        listener = _make_listener(cb)

        ts = 0x12000000
        data = _make_ctpp_msg(PREFIX_VIP_EVENT, ts, ACTION_REGISTRATION_RENEWAL, flags=0)

        sent_payloads: list[bytes] = []

        async def capture_send(channel, payload):
            sent_payloads.append(payload)

        listener._client.send_binary = AsyncMock(side_effect=capture_send)
        await listener._process_message(data)

        assert len(sent_payloads) == 2
        # Verify expected ACK timestamp (ts + 0x01000000) is embedded LE32 at offset 2
        expected_ts = (ts + 0x01000000) & 0xFFFFFFFF
        for payload in sent_payloads:
            actual_ts = struct.unpack_from("<I", payload, 2)[0]
            assert actual_ts == expected_ts

    @pytest.mark.asyncio
    async def test_send_ack_failure_does_not_raise(self):
        """ACK send failure is logged but must not propagate."""
        cb = MagicMock()
        listener = _make_listener(cb)
        listener._client.send_binary = AsyncMock(side_effect=OSError("network error"))

        data = _make_ctpp_msg(PREFIX_VIP_EVENT, 0, ACTION_DOOR_OPENED, flags=0)
        # Must not raise
        await listener._process_message(data)


# ---------------------------------------------------------------------------
# VipEventListener.stop
# ---------------------------------------------------------------------------


class TestVipListenerStop:
    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        listener = _make_listener()
        listener._running = True

        # Plant a long-running task
        async def _forever():
            await asyncio.sleep(9999)

        listener._task = asyncio.create_task(_forever())
        await listener.stop()

        assert listener._task is None
        assert listener._running is False

    @pytest.mark.asyncio
    async def test_stop_safe_when_no_task(self):
        listener = _make_listener()
        listener._running = False
        listener._task = None
        await listener.stop()  # must not raise


# ---------------------------------------------------------------------------
# VipEventListener._listen_loop
# ---------------------------------------------------------------------------


class TestListenLoop:
    @pytest.mark.asyncio
    async def test_listen_loop_dispatches_message(self):
        cb = MagicMock()
        listener = _make_listener(cb)
        listener._running = True

        data = _make_ctpp_msg(PREFIX_VIP_EVENT, 0, ACTION_IN_ALERTING, flags=0)

        async def _stop_after_one():
            # Let the loop run one iteration, then stop it
            await asyncio.sleep(0)
            listener._running = False

        await listener._channel.response_queue.put(data)
        await asyncio.gather(
            listener._listen_loop(),
            _stop_after_one(),
        )

        cb.assert_called_once()
        assert cb.call_args[0][0].event_type == "doorbell_ring"
