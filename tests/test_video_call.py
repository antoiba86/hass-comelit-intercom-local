"""Unit tests for VideoCallSession — no device needed."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.exceptions import VideoCallError
from custom_components.comelit_intercom_local.video_call import (
    VideoCallSession,
    _CTR_INCR_BOTH,
    _CTR_INCR_BYTE4,
    _CTR_INCR_BYTE5,
)


class TestCounterIncrementConstants:
    def test_ctr_incr_both_equals_byte4_plus_byte5(self):
        assert _CTR_INCR_BOTH == _CTR_INCR_BYTE4 + _CTR_INCR_BYTE5

    def test_ctr_incr_byte4_is_correct(self):
        assert _CTR_INCR_BYTE4 == 0x00010000

    def test_ctr_incr_byte5_is_correct(self):
        assert _CTR_INCR_BYTE5 == 0x01000000


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_called_even_when_rtp_receiver_stop_raises(self):
        """_cleanup must still disconnect the client even if rtp_receiver.stop() raises."""
        session = VideoCallSession.__new__(VideoCallSession)
        session._active = True
        session._timeout_task = None
        session._tcp_task = None
        session._ctpp_task = None

        mock_receiver = MagicMock()
        mock_receiver.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        session._rtp_receiver = mock_receiver

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()
        session._client = mock_client

        # Should not raise
        await session._cleanup()

        mock_client.disconnect.assert_awaited_once()
        assert session._active is False
        assert session._rtp_receiver is None
        assert session._client is None

    @pytest.mark.asyncio
    async def test_cleanup_cancels_timeout_task(self):
        """_cleanup must cancel the timeout task."""
        session = VideoCallSession.__new__(VideoCallSession)
        session._active = True
        session._rtp_receiver = None
        session._client = None
        session._tcp_task = None
        session._ctpp_task = None

        cancelled = asyncio.Event()

        async def long_task():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        session._timeout_task = asyncio.create_task(long_task())
        await asyncio.sleep(0)  # let the task start before cleanup cancels it

        await session._cleanup()

        assert cancelled.is_set()
        assert session._timeout_task is None

    @pytest.mark.asyncio
    async def test_cleanup_cancels_ctpp_task(self):
        """_cleanup must cancel the ctpp monitor task."""
        session = VideoCallSession.__new__(VideoCallSession)
        session._active = True
        session._rtp_receiver = None
        session._client = None
        session._tcp_task = None
        session._timeout_task = None

        cancelled = asyncio.Event()

        async def long_task():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        session._ctpp_task = asyncio.create_task(long_task())
        await asyncio.sleep(0)

        await session._cleanup()

        assert cancelled.is_set()
        assert session._ctpp_task is None

    @pytest.mark.asyncio
    async def test_cleanup_is_idempotent(self):
        """Calling _cleanup twice must not raise."""
        session = VideoCallSession.__new__(VideoCallSession)
        session._active = True
        session._timeout_task = None
        session._tcp_task = None
        session._ctpp_task = None
        session._rtp_receiver = None
        session._client = None

        await session._cleanup()
        await session._cleanup()  # should not raise

    @pytest.mark.asyncio
    async def test_stop_callable_when_inactive(self):
        """stop() must not raise even when the session was never active."""
        session = VideoCallSession.__new__(VideoCallSession)
        session._active = False
        session._timeout_task = None
        session._tcp_task = None
        session._ctpp_task = None
        session._rtp_receiver = None
        session._client = None

        await session.stop()  # should not raise


class TestCtppMonitorLoop:
    """Tests for the CTPP monitor loop that ACKs device messages during a call."""

    def _make_session(self) -> "VideoCallSession":
        session = VideoCallSession.__new__(VideoCallSession)
        session._active = True
        session._timeout_task = None
        session._tcp_task = None
        session._ctpp_task = None
        session._rtp_receiver = None
        session._client = None
        return session

    @pytest.mark.asyncio
    async def test_ctpp_keepalive_is_acked(self):
        """0x1840/0x0000 keepalive should be ACKed with 0x1800."""
        import struct
        from custom_components.comelit_intercom_local.protocol import encode_call_response_ack

        session = self._make_session()

        sent_data = []

        mock_client = MagicMock()
        keepalive_body = struct.pack("<H", 0x1840) + struct.pack("<I", 0x12345678) + struct.pack(">H", 0x0000)

        call_count = 0

        async def mock_read_response(channel, timeout=2.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return keepalive_body
            session._active = False  # stop after first message
            return None

        mock_client.read_response = mock_read_response
        mock_client.send_binary = AsyncMock(side_effect=lambda ch, data: sent_data.append(data))

        mock_ctpp = MagicMock()

        await session._ctpp_monitor_loop(mock_client, mock_ctpp, "SB0000061", "SB100001", 0x10000000)

        # An ACK (0x1800 prefix) should have been sent
        assert len(sent_data) == 1
        prefix = struct.unpack_from("<H", sent_data[0], 0)[0]
        assert prefix == 0x1800

    @pytest.mark.asyncio
    async def test_ctpp_call_end_stops_session(self):
        """0x1840/0x0003 CALL_END should ACK and set _active=False."""
        import struct

        session = self._make_session()

        sent_data = []

        mock_client = MagicMock()
        call_end_body = struct.pack("<H", 0x1840) + struct.pack("<I", 0x12345678) + struct.pack(">H", 0x0003)

        async def mock_read_response(channel, timeout=2.0):
            return call_end_body

        mock_client.read_response = mock_read_response
        mock_client.send_binary = AsyncMock(side_effect=lambda ch, data: sent_data.append(data))

        mock_ctpp = MagicMock()

        await session._ctpp_monitor_loop(mock_client, mock_ctpp, "SB0000061", "SB100001", 0x10000000)

        assert session._active is False
        assert len(sent_data) == 1  # exactly one ACK sent

    @pytest.mark.asyncio
    async def test_ctpp_device_acks_are_ignored(self):
        """0x1800 device ACKs should not trigger any response."""
        import struct

        session = self._make_session()

        sent_data = []

        mock_client = MagicMock()
        call_count = 0

        async def mock_read_response(channel, timeout=2.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return struct.pack("<H", 0x1800) + struct.pack("<I", 0x12345678) + struct.pack(">H", 0x0000)
            session._active = False
            return None

        mock_client.read_response = mock_read_response
        mock_client.send_binary = AsyncMock(side_effect=lambda ch, data: sent_data.append(data))

        await session._ctpp_monitor_loop(mock_client, MagicMock(), "SB0000061", "SB100001", 0x10000000)

        assert len(sent_data) == 0  # no response to device ACKs
