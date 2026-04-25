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
        """_cleanup must still clean up channels even if rtp_receiver.stop() raises.

        VideoCallSession no longer owns the TCP connection — it uses the
        coordinator's shared client and must NOT disconnect it.  Instead it
        calls remove_channel() for each video channel name.
        """
        from custom_components.comelit_intercom_local.video_call import VideoCallSession

        session = VideoCallSession.__new__(VideoCallSession)
        session._active = True
        session._timeout_task = None
        session._tcp_task = None
        session._ctpp_task = None
        session._rtsp_server = None
        session._external_rtsp = False
        session._owns_ctpp = True  # session owns CTPP → cleanup must remove it

        mock_receiver = MagicMock()
        mock_receiver.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        session._rtp_receiver = mock_receiver

        mock_client = MagicMock()
        mock_client.remove_channel = MagicMock()
        session._client = mock_client

        # Should not raise
        await session._cleanup()

        # disconnect must NOT be called — the coordinator owns the connection
        mock_client.disconnect.assert_not_called()
        # Each video channel name must be removed
        removed = {call.args[0] for call in mock_client.remove_channel.call_args_list}
        assert "CTPP" in removed
        assert "UDPM" in removed
        assert session._active is False
        assert session._rtp_receiver is None

    @pytest.mark.asyncio
    async def test_cleanup_cancels_timeout_task(self):
        """_cleanup must cancel the timeout task."""
        session = VideoCallSession.__new__(VideoCallSession)
        session._active = True
        session._rtp_receiver = None
        session._client = None
        session._tcp_task = None
        session._ctpp_task = None
        session._rtsp_server = None
        session._external_rtsp = False

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
        session._rtsp_server = None
        session._external_rtsp = False

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
        session._rtsp_server = None
        session._external_rtsp = False

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
        session._rtsp_server = None
        session._external_rtsp = False

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
        session._rtsp_server = None
        session._external_rtsp = False
        session._ctpp_lock = asyncio.Lock()
        session._call_counter = 0
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

        await session._ctpp_monitor_loop(
            mock_client, mock_ctpp, "SB0000061", "SB100001", 0x10000000,
            rtpc1_server_id=0xABCD, media_req_id=0x1234,
        )

        # An ACK (0x1800 prefix) should have been sent
        assert len(sent_data) == 1
        prefix = struct.unpack_from("<H", sent_data[0], 0)[0]
        assert prefix == 0x1800

    @pytest.mark.asyncio
    async def test_ctpp_call_end_triggers_inline_reestablish(self):
        """0x1840/0x0003 CALL_END should trigger _inline_reestablish, not stop session."""
        import struct

        session = self._make_session()

        mock_client = MagicMock()
        call_end_body = (
            struct.pack("<H", 0x1840)
            + struct.pack("<I", 0x12345678)
            + struct.pack(">H", 0x0003)
        )
        call_count = 0

        async def mock_read_response(channel, timeout=2.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return call_end_body
            session._active = False
            return None

        mock_client.read_response = mock_read_response
        mock_client.send_binary = AsyncMock()

        reestablish_called = False

        async def mock_reestablish(*args, **kwargs):
            nonlocal reestablish_called
            reestablish_called = True
            return 0x10000000  # return updated counter

        session._inline_reestablish = mock_reestablish

        await session._ctpp_monitor_loop(
            mock_client, MagicMock(), "SB0000061", "SB100001", 0x10000000,
            rtpc1_server_id=0xABCD, media_req_id=0x1234,
        )

        assert reestablish_called
        # Session should still be active after successful re-establishment
        # (loop ends only because mock_read_response set _active=False on next iteration)

    @pytest.mark.asyncio
    async def test_ctpp_call_end_reestablish_failure_keeps_loop_running(self):
        """If _inline_reestablish raises, a warning is logged but the loop keeps running."""
        import struct

        session = self._make_session()

        mock_client = MagicMock()
        call_end_body = (
            struct.pack("<H", 0x1840)
            + struct.pack("<I", 0x12345678)
            + struct.pack(">H", 0x0003)
        )
        call_count = 0

        async def mock_read_response(channel, timeout=2.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return call_end_body
            session._active = False
            return None

        mock_client.read_response = mock_read_response
        mock_client.send_binary = AsyncMock()

        async def failing_reestablish(*args, **kwargs):
            raise RuntimeError("re-establish failed")

        session._inline_reestablish = failing_reestablish

        # Must not raise; loop exits on next read returning None
        await session._ctpp_monitor_loop(
            mock_client, MagicMock(), "SB0000061", "SB100001", 0x10000000,
            rtpc1_server_id=0xABCD, media_req_id=0x1234,
        )

    @pytest.mark.asyncio
    async def test_ctpp_call_end_sub_000E_triggers_reestablish(self):
        """0x1840/0x0003/sub=0x000E (door-open triggered CALL_END) must
        trigger inline re-establish — same path as the periodic timer CALL_END.

        PCAP-verified: the device sends this sub-code when a door-open relay
        activates during video; the renewal sequence is required to keep video
        alive, just like a timer-triggered CALL_END (sub=0x0000).
        """
        import struct
        session = self._make_session()

        mock_client = MagicMock()
        call_end_body = (
            struct.pack("<H", 0x1840)
            + struct.pack("<I", 0xDEADBEEF)
            + struct.pack(">H", 0x0003)
            + struct.pack(">H", 0x000E)
            + b"\x00" * 8
        )
        call_count = 0

        async def mock_read_response(channel, timeout=2.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return call_end_body
            session._active = False
            return None

        mock_client.read_response = mock_read_response
        mock_client.send_binary = AsyncMock()

        reestablish_called = False

        async def mock_reestablish(*args, **kwargs):
            nonlocal reestablish_called
            reestablish_called = True
            return 0x10000000

        session._inline_reestablish = mock_reestablish

        await session._ctpp_monitor_loop(
            mock_client, MagicMock(), "SB0000061", "SB100001", 0x10000000,
            rtpc1_server_id=0xABCD, media_req_id=0x1234,
        )

        assert reestablish_called

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

        await session._ctpp_monitor_loop(
            mock_client, MagicMock(), "SB0000061", "SB100001", 0x10000000,
            rtpc1_server_id=0xABCD, media_req_id=0x1234,
        )

        assert len(sent_data) == 0  # no response to device ACKs

    @pytest.mark.asyncio
    async def test_ctpp_0x1860_message_is_bare_acked(self):
        """0x1860 messages in the monitor loop (e.g. stray RTPC link after
        renewal) must be bare-ACKed with 0x1800, not logged as unexpected.

        PCAP-verified: device sends 0x1860/0x000A during renewal; if
        _ack_device_rtpc_link missed it, the monitor loop must still ACK it.
        """
        import struct

        session = self._make_session()
        sent_data = []

        mock_client = MagicMock()
        call_count = 0

        async def mock_read_response(channel, timeout=2.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (
                    struct.pack("<H", 0x1860)
                    + struct.pack("<I", 0xCAFEBABE)
                    + struct.pack(">H", 0x000A)
                )
            session._active = False
            return None

        mock_client.read_response = mock_read_response
        mock_client.send_binary = AsyncMock(side_effect=lambda ch, data: sent_data.append(data))

        await session._ctpp_monitor_loop(
            mock_client, MagicMock(), "SB0000061", "SB100001", 0x10000000,
            rtpc1_server_id=0xABCD, media_req_id=0x1234,
        )

        assert len(sent_data) == 1
        prefix = struct.unpack_from("<H", sent_data[0], 0)[0]
        assert prefix == 0x1800, f"Expected 0x1800 ACK, got 0x{prefix:04X}"


class TestAckDeviceRtpcLink:
    """Tests for _ack_device_rtpc_link — accepts both 0x1840 and 0x1860 prefixes."""

    def _make_session(self) -> "VideoCallSession":
        session = VideoCallSession.__new__(VideoCallSession)
        session._active = True
        session._timeout_task = None
        session._tcp_task = None
        session._ctpp_task = None
        session._rtp_receiver = None
        session._client = None
        session._rtsp_server = None
        session._external_rtsp = False
        session._ctpp_lock = asyncio.Lock()
        session._call_counter = 0
        return session

    @pytest.mark.asyncio
    async def test_accepts_0x1840_rtpc_link(self):
        """ACKs device's 0x1840/0x000A (initial-start RTPC link)."""
        import struct

        session = self._make_session()
        sent_data = []

        mock_client = MagicMock()
        call_count = 0

        async def mock_read_response(channel, timeout=2.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (
                    struct.pack("<H", 0x1840)
                    + struct.pack("<I", 0x11223344)
                    + struct.pack(">H", 0x000A)
                )
            return None

        mock_client.read_response = mock_read_response
        mock_client.send_binary = AsyncMock(side_effect=lambda ch, data: sent_data.append(data))

        result = await session._ack_device_rtpc_link(
            mock_client, MagicMock(), "SB0000061", "SB100001", 0x10000000
        )

        assert len(sent_data) == 1
        prefix = struct.unpack_from("<H", sent_data[0], 0)[0]
        assert prefix == 0x1800

    @pytest.mark.asyncio
    async def test_accepts_0x1860_rtpc_link(self):
        """ACKs device's 0x1860/0x000A (renewal RTPC link, PCAP-verified).

        During inline re-establishment the device sends RTPC link with prefix
        0x1860 instead of 0x1840. The function must accept both and ACK them
        identically.
        """
        import struct

        session = self._make_session()
        sent_data = []

        mock_client = MagicMock()
        call_count = 0

        async def mock_read_response(channel, timeout=2.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (
                    struct.pack("<H", 0x1860)
                    + struct.pack("<I", 0x11223344)
                    + struct.pack(">H", 0x000A)
                )
            return None

        mock_client.read_response = mock_read_response
        mock_client.send_binary = AsyncMock(side_effect=lambda ch, data: sent_data.append(data))

        result = await session._ack_device_rtpc_link(
            mock_client, MagicMock(), "SB0000061", "SB100001", 0x10000000
        )

        assert len(sent_data) == 1, "Device 0x1860/0x000A RTPC link was not ACKed"
        prefix = struct.unpack_from("<H", sent_data[0], 0)[0]
        assert prefix == 0x1800

    @pytest.mark.asyncio
    async def test_skips_0x1800_and_waits_for_000A(self):
        """0x1800 device ACKs before the RTPC link are skipped, not mistaken for it."""
        import struct

        session = self._make_session()
        sent_data = []

        mock_client = MagicMock()
        call_count = 0

        async def mock_read_response(channel, timeout=2.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return struct.pack("<H", 0x1800) + struct.pack("<I", 0) + struct.pack(">H", 0)
            if call_count == 2:
                return (
                    struct.pack("<H", 0x1860)
                    + struct.pack("<I", 0x99887766)
                    + struct.pack(">H", 0x000A)
                )
            return None

        mock_client.read_response = mock_read_response
        mock_client.send_binary = AsyncMock(side_effect=lambda ch, data: sent_data.append(data))

        await session._ack_device_rtpc_link(
            mock_client, MagicMock(), "SB0000061", "SB100001", 0x10000000
        )

        assert len(sent_data) == 1
        prefix = struct.unpack_from("<H", sent_data[0], 0)[0]
        assert prefix == 0x1800


class TestInlineReestablish:
    """Tests for _inline_reestablish — the CALL_END renewal sequence."""

    @pytest.mark.asyncio
    async def test_reestablish_sends_rtpc_link_and_video_config(self):
        """_inline_reestablish must send RTPC_LINK (0x1840/0x000A) followed
        by VIDEO_CONFIG (0x1840 prefix) to re-establish the media session.
        """
        import struct
        from custom_components.comelit_intercom_local.protocol import (
            encode_rtpc_link,
            encode_video_config,
        )

        session = VideoCallSession.__new__(VideoCallSession)
        session._active = True
        session._timeout_task = None
        session._tcp_task = None
        session._ctpp_task = None
        session._rtp_receiver = None
        session._external_rtsp = False
        session._ctpp_lock = asyncio.Lock()
        session._call_counter = 0
        session._rtsp_server = None

        our_addr = "SB0000061"
        entrance_addr = "SB100001"
        rtpc1_server_id = 0xABCD
        media_req_id = 0x1234

        sent_data = []
        read_count = 0

        # Minimal device response sequence for _inline_reestablish:
        # ctpp_init_sequence reads up to 2 responses; call_init reads 1;
        # _run_codec_exchange reads until 0x0002 (call accepted).
        def make_0x1840(action: int) -> bytes:
            return (
                struct.pack("<H", 0x1840)
                + struct.pack("<I", 0xDEADBEEF)
                + struct.pack(">H", action)
            )

        def make_0x1800() -> bytes:
            return struct.pack("<H", 0x1800) + struct.pack("<I", 0) + struct.pack(">H", 0)

        # Response sequence:
        # [0-1] ctpp_init_sequence drain (2 reads)
        # [2]   call_init ACK read
        # [3]   codec exchange: 0x0002 = call accepted
        # [4+]  _ack_device_rtpc_link: returns None (timeout suppressed)
        responses = [
            make_0x1800(),        # ctpp_init drain 1
            make_0x1800(),        # ctpp_init drain 2
            make_0x1840(0x0001),  # call_init ACK (any action)
            make_0x1840(0x0002),  # codec exchange: call accepted
            None,                 # _ack_device_rtpc_link timeout
        ]

        async def mock_read_response(channel, timeout=2.0):
            nonlocal read_count
            if read_count < len(responses):
                resp = responses[read_count]
                read_count += 1
                return resp
            return None

        mock_client = MagicMock()
        mock_client.read_response = mock_read_response
        mock_client.send_binary = AsyncMock(side_effect=lambda ch, data: sent_data.append(data))

        # register_placeholder_channel must return a channel with an open_event
        placeholder = MagicMock()
        placeholder.open_event = asyncio.Event()
        placeholder.open_event.set()  # simulate device opened it immediately
        placeholder.server_channel_id = 0x9999
        mock_client.register_placeholder_channel = MagicMock(return_value=placeholder)

        mock_ctpp = MagicMock()

        # Provide a fixed timestamp so we can compute expected messages
        fixed_ts = 0x01020304
        session._ts = lambda: fixed_ts

        await session._inline_reestablish(
            mock_client, mock_ctpp,
            our_addr, entrance_addr,
            rtpc1_server_id, media_req_id,
            call_counter=0x00010000,
        )

        sent_prefixes = [struct.unpack_from("<H", d, 0)[0] for d in sent_data]
        sent_actions = [
            struct.unpack_from(">H", d, 6)[0] if len(d) >= 8 else 0
            for d in sent_data
        ]

        # RTPC_LINK: prefix 0x1840, action 0x000A
        ACTION_RTPC_LINK = 0x000A
        rtpc_link_messages = [
            d for d in sent_data
            if len(d) >= 8
            and struct.unpack_from("<H", d, 0)[0] == 0x1840
            and struct.unpack_from(">H", d, 6)[0] == ACTION_RTPC_LINK
        ]
        assert rtpc_link_messages, "No RTPC_LINK message (0x1840/0x000A) was sent"

        # VIDEO_CONFIG: prefix 0x1840 (encode_video_config)
        # Verify it matches what encode_video_config would produce (not _resp).
        # encode_video_config uses prefix 0x1840; encode_video_config_resp uses 0x1860.
        video_config_messages = [
            d for d in sent_data
            if len(d) >= 8
            and struct.unpack_from("<H", d, 0)[0] == 0x1840
            and struct.unpack_from(">H", d, 6)[0] not in (ACTION_RTPC_LINK, 0x0000, 0x0070)
        ]
        assert video_config_messages, "No VIDEO_CONFIG message (0x1840) was sent during renewal"
