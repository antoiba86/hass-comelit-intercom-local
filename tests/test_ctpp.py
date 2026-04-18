"""Unit tests for ctpp_init_sequence — no device needed."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from custom_components.comelit_intercom_local.ctpp import (
    _CTR_INCR_BOTH,
    ctpp_init_sequence,
)


def _make_client(responses: list) -> MagicMock:
    """Return a mock client whose read_response yields items from *responses*."""
    client = MagicMock()
    client.send_binary = AsyncMock()
    client.read_response = AsyncMock(side_effect=responses)
    return client


class TestCtrIncrBoth:
    def test_value(self):
        assert _CTR_INCR_BOTH == 0x01010000


class TestCtppInitSequence:
    @pytest.mark.asyncio
    async def test_sends_ctpp_init(self):
        """ctpp_init_sequence must send encode_ctpp_init as the first binary message."""
        client = _make_client([b"\x00\x18" + b"\x00" * 6, None])
        channel = MagicMock()

        with pytest.MonkeyPatch().context() as mp:
            from custom_components.comelit_intercom_local import ctpp as ctpp_mod
            sent = []
            client.send_binary = AsyncMock(side_effect=lambda ch, data: sent.append(data))
            await ctpp_init_sequence(client, channel, "SB000006", 1, "SB0000061", 0x12345678)

        assert len(sent) >= 1

    @pytest.mark.asyncio
    async def test_reads_two_responses(self):
        """ctpp_init_sequence must call read_response exactly twice for the device init responses."""
        resp1 = b"\x00\x18" + struct.pack("<I", 0) + struct.pack(">H", 0)
        resp2 = b"\x20\x18" + struct.pack("<I", 0) + struct.pack(">H", 0)
        client = _make_client([resp1, resp2])
        channel = MagicMock()

        await ctpp_init_sequence(client, channel, "SB000006", 1, "SB0000061", 0x10000000)

        assert client.read_response.await_count == 2

    @pytest.mark.asyncio
    async def test_sends_ack_pair_after_responses(self):
        """After draining 2 responses, two ACK messages (0x1800 and 0x1820) must be sent."""
        resp = b"\x00\x18" + struct.pack("<I", 0) + struct.pack(">H", 0)
        client = _make_client([resp, resp])
        channel = MagicMock()

        sent: list[bytes] = []
        client.send_binary = AsyncMock(side_effect=lambda ch, data: sent.append(data))

        await ctpp_init_sequence(client, channel, "SB000006", 1, "SB0000061", 0x10000000)

        # 1 init + 2 ACKs = 3 total sends
        assert len(sent) == 3
        # Last two must start with 0x1800 and 0x1820
        prefixes = [struct.unpack_from("<H", s, 0)[0] for s in sent[-2:]]
        assert 0x1800 in prefixes
        assert 0x1820 in prefixes

    @pytest.mark.asyncio
    async def test_ack_timestamp_is_init_ts_plus_ctr_incr(self):
        """ACK timestamp must be (init_ts + _CTR_INCR_BOTH) & 0xFFFFFFFF."""
        init_ts = 0x12000000
        resp = b"\x00\x18" + struct.pack("<I", 0) + struct.pack(">H", 0)
        client = _make_client([resp, resp])
        channel = MagicMock()

        sent: list[bytes] = []
        client.send_binary = AsyncMock(side_effect=lambda ch, data: sent.append(data))

        await ctpp_init_sequence(client, channel, "SB000006", 1, "SB0000061", init_ts)

        expected_ts = (init_ts + _CTR_INCR_BOTH) & 0xFFFFFFFF
        # The ACK messages are encode_call_response_ack; check LE32 at offset 2
        for ack in sent[-2:]:
            actual_ts = struct.unpack_from("<I", ack, 2)[0]
            assert actual_ts == expected_ts

    @pytest.mark.asyncio
    async def test_timeout_on_no_response_is_handled(self):
        """If read_response returns None (timeout), sequence continues without raising."""
        client = _make_client([None, None])
        channel = MagicMock()

        # Must not raise
        await ctpp_init_sequence(
            client, channel, "SB000006", 1, "SB0000061", 0x10000000,
            response_timeout=0.1,
        )

    @pytest.mark.asyncio
    async def test_ack_ts_wraps_at_32_bits(self):
        """Timestamp addition must wrap at 32 bits."""
        init_ts = 0xFFFF0000
        resp = b"\x00\x18" + struct.pack("<I", 0) + struct.pack(">H", 0)
        client = _make_client([resp, resp])
        channel = MagicMock()

        sent: list[bytes] = []
        client.send_binary = AsyncMock(side_effect=lambda ch, data: sent.append(data))

        await ctpp_init_sequence(client, channel, "SB000006", 1, "SB0000061", init_ts)

        expected_ts = (init_ts + _CTR_INCR_BOTH) & 0xFFFFFFFF
        for ack in sent[-2:]:
            actual_ts = struct.unpack_from("<I", ack, 2)[0]
            assert actual_ts == expected_ts
