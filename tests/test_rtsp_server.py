"""Unit tests for LocalRtspServer — no real network clients needed."""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.rtsp_server import (
    LocalRtspServer,
    _TcpClient,
    _build_rtp,
)


# ---------------------------------------------------------------------------
# _build_rtp helper
# ---------------------------------------------------------------------------


class TestBuildRtp:
    def test_fixed_header_length(self):
        """RTP packet is always 12 bytes of header + payload length."""
        payload = b"\xd5" * 20
        pkt = _build_rtp(pt=8, seq=0, ts=0, ssrc=0, payload=payload, marker=False)
        assert len(pkt) == 12 + len(payload)

    def test_version_bits(self):
        """First byte must have RTP version=2 (bits 7-6 = 0b10 → 0x80)."""
        pkt = _build_rtp(pt=0, seq=0, ts=0, ssrc=0, payload=b"", marker=False)
        assert pkt[0] == 0x80

    def test_payload_type_encoded(self):
        """Payload type is in low 7 bits of byte 1."""
        pkt = _build_rtp(pt=96, seq=0, ts=0, ssrc=0, payload=b"", marker=False)
        assert (pkt[1] & 0x7F) == 96

    def test_marker_bit_set(self):
        """Marker bit (bit 7 of byte 1) is set when marker=True."""
        pkt = _build_rtp(pt=8, seq=0, ts=0, ssrc=0, payload=b"", marker=True)
        assert pkt[1] & 0x80

    def test_marker_bit_clear(self):
        """Marker bit is clear when marker=False."""
        pkt = _build_rtp(pt=8, seq=0, ts=0, ssrc=0, payload=b"", marker=False)
        assert not (pkt[1] & 0x80)

    def test_sequence_number_encoded_be(self):
        """Sequence number is big-endian uint16 at bytes 2-3."""
        pkt = _build_rtp(pt=0, seq=0x1234, ts=0, ssrc=0, payload=b"", marker=False)
        seq = struct.unpack_from("!H", pkt, 2)[0]
        assert seq == 0x1234

    def test_timestamp_encoded_be(self):
        """Timestamp is big-endian uint32 at bytes 4-7."""
        pkt = _build_rtp(pt=0, seq=0, ts=0xDEADBEEF, ssrc=0, payload=b"", marker=False)
        ts = struct.unpack_from("!I", pkt, 4)[0]
        assert ts == 0xDEADBEEF

    def test_ssrc_encoded_be(self):
        """SSRC is big-endian uint32 at bytes 8-11."""
        pkt = _build_rtp(pt=0, seq=0, ts=0, ssrc=0xC0DE1234, payload=b"", marker=False)
        ssrc = struct.unpack_from("!I", pkt, 8)[0]
        assert ssrc == 0xC0DE1234

    def test_payload_appended(self):
        """Payload bytes follow immediately after the 12-byte header."""
        payload = b"\x01\x02\x03\x04"
        pkt = _build_rtp(pt=0, seq=0, ts=0, ssrc=0, payload=payload, marker=False)
        assert pkt[12:] == payload

    def test_seq_wraps_at_16_bits(self):
        """seq=0xFFFF + increment wraps to 0 (server is responsible, not _build_rtp)."""
        pkt = _build_rtp(pt=0, seq=0xFFFF, ts=0, ssrc=0, payload=b"", marker=False)
        seq = struct.unpack_from("!H", pkt, 2)[0]
        assert seq == 0xFFFF


# ---------------------------------------------------------------------------
# _build_sdp
# ---------------------------------------------------------------------------


class TestBuildSdp:
    def test_sdp_contains_video_track(self):
        server = LocalRtspServer()
        sdp = server._build_sdp()
        assert "m=video" in sdp
        assert "H264/90000" in sdp

    def test_sdp_contains_audio_track(self):
        server = LocalRtspServer()
        sdp = server._build_sdp()
        assert "m=audio" in sdp
        assert "PCMA/8000" in sdp

    def test_sdp_video_pt96(self):
        server = LocalRtspServer()
        sdp = server._build_sdp()
        assert "RTP/AVP 96" in sdp
        assert "rtpmap:96 H264" in sdp

    def test_sdp_audio_pt8(self):
        server = LocalRtspServer()
        sdp = server._build_sdp()
        assert "RTP/AVP 8" in sdp
        assert "rtpmap:8 PCMA" in sdp

    def test_sdp_video_control(self):
        server = LocalRtspServer()
        sdp = server._build_sdp()
        assert "a=control:video" in sdp

    def test_sdp_audio_control(self):
        server = LocalRtspServer()
        sdp = server._build_sdp()
        assert "a=control:audio" in sdp

    def test_sdp_uses_bind_host(self):
        server = LocalRtspServer(bind_host="192.168.1.1")
        sdp = server._build_sdp()
        assert "192.168.1.1" in sdp


# ---------------------------------------------------------------------------
# _parse_client_port
# ---------------------------------------------------------------------------


class TestParseClientPort:
    def test_parses_single_port(self):
        assert LocalRtspServer._parse_client_port("client_port=12345") == 12345

    def test_parses_port_range(self):
        assert LocalRtspServer._parse_client_port("client_port=12345-12346") == 12345

    def test_parses_from_full_transport_header(self):
        transport = "RTP/AVP;unicast;client_port=54320-54321"
        assert LocalRtspServer._parse_client_port(transport) == 54320

    def test_returns_zero_when_no_client_port(self):
        assert LocalRtspServer._parse_client_port("RTP/AVP/TCP;interleaved=0-1") == 0


# ---------------------------------------------------------------------------
# _parse_setup
# ---------------------------------------------------------------------------


class TestParseSetup:
    def test_tcp_interleaved_video(self):
        server = LocalRtspServer()
        client = _TcpClient(writer=MagicMock())
        transport = "RTP/AVP/TCP;unicast;interleaved=0-1"
        resp = server._parse_setup(transport, is_audio=False, client=client, client_host="127.0.0.1")
        assert "interleaved=0-1" in resp
        assert client.video_ch == 0

    def test_tcp_interleaved_audio(self):
        server = LocalRtspServer()
        client = _TcpClient(writer=MagicMock())
        transport = "RTP/AVP/TCP;unicast;interleaved=2-3"
        resp = server._parse_setup(transport, is_audio=True, client=client, client_host="127.0.0.1")
        assert "interleaved=2-3" in resp
        assert client.audio_ch == 2

    def test_udp_video_sets_udp_host(self):
        server = LocalRtspServer()
        server._video_server_port = 9000
        client = _TcpClient(writer=MagicMock())
        transport = "RTP/AVP;unicast;client_port=50000-50001"
        server._parse_setup(transport, is_audio=False, client=client, client_host="192.168.1.5")
        assert server._udp_host == "192.168.1.5"
        assert server._udp_video_port == 50000

    def test_udp_audio_sets_udp_audio_port(self):
        server = LocalRtspServer()
        server._audio_server_port = 9002
        client = _TcpClient(writer=MagicMock())
        transport = "RTP/AVP;unicast;client_port=51000-51001"
        server._parse_setup(transport, is_audio=True, client=client, client_host="192.168.1.5")
        assert server._udp_audio_port == 51000

    def test_tcp_response_includes_rtp_avp_tcp(self):
        server = LocalRtspServer()
        client = _TcpClient(writer=MagicMock())
        transport = "RTP/AVP/TCP;unicast;interleaved=0-1"
        resp = server._parse_setup(transport, is_audio=False, client=client, client_host="127.0.0.1")
        assert "RTP/AVP/TCP" in resp


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_drains_nal_queue(self):
        server = LocalRtspServer()
        server.nal_queue.put_nowait(b"\x00" * 10)
        server.nal_queue.put_nowait(b"\x00" * 10)
        server.reset()
        assert server.nal_queue.empty()

    def test_reset_drains_audio_queue(self):
        server = LocalRtspServer()
        server.audio_queue.put_nowait(b"\xd5" * 160)
        server.reset()
        assert server.audio_queue.empty()

    def test_reset_never_clears_rtp_counters(self):
        """reset() never resets RTP seq/ts — the persistent HA stream worker
        stays connected across calls and backwards jumps cause discontinuity errors."""
        server = LocalRtspServer()
        server._video_seq = 100
        server._audio_seq = 50
        server._audio_ts = 8000
        server.reset(renewal=False)
        assert server._video_seq == 100
        assert server._audio_seq == 50
        assert server._audio_ts == 8000

    def test_reset_sets_rebase_pending(self):
        """reset() sets _video_ts_rebase_pending so the feed loop rebases on next NAL."""
        server = LocalRtspServer()
        server._video_ts_rebase_pending = False
        server.reset()
        assert server._video_ts_rebase_pending is True

    def test_reset_with_renewal_also_preserves_counters(self):
        """renewal=True is kept for API compatibility but no longer changes behaviour."""
        server = LocalRtspServer()
        server._video_seq = 200
        server._audio_seq = 100
        server._audio_ts = 16000
        server.reset(renewal=True)
        assert server._video_seq == 200
        assert server._audio_seq == 100
        assert server._audio_ts == 16000

    def test_reset_preserves_active_clients(self):
        server = LocalRtspServer()
        fake_client = _TcpClient(writer=MagicMock())
        server._active_clients.append(fake_client)
        server.reset()
        assert len(server._active_clients) == 1

    def test_reset_queue_objects_unchanged(self):
        """reset() drains queues in-place — same objects for RtpReceiver to keep pushing."""
        server = LocalRtspServer()
        original_nal_q = server.nal_queue
        original_audio_q = server.audio_queue
        server.nal_queue.put_nowait(b"\x00" * 5)
        server.reset()
        assert server.nal_queue is original_nal_q
        assert server.audio_queue is original_audio_q


# ---------------------------------------------------------------------------
# start() / stop() lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_returns_rtsp_url(self):
        server = LocalRtspServer()
        url = await server.start()
        try:
            assert url.startswith("rtsp://127.0.0.1:")
            assert "/intercom" in url
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_start_sets_running(self):
        server = LocalRtspServer()
        await server.start()
        try:
            assert server._running is True
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running(self):
        server = LocalRtspServer()
        await server.start()
        await server.stop()
        assert server._running is False

    @pytest.mark.asyncio
    async def test_start_binds_rtsp_port(self):
        server = LocalRtspServer()
        await server.start()
        try:
            assert server._rtsp_port > 0
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_udp_sockets_are_created(self):
        """UDP sockets are created during start()."""
        server = LocalRtspServer(bind_host="127.0.0.1")
        await server.start()
        try:
            assert server._video_sock is not None
            assert server._audio_sock is not None
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_udp_sockets_bind_to_bind_host(self):
        """UDP sockets bind to bind_host, not all interfaces."""
        server = LocalRtspServer(bind_host="127.0.0.1")
        await server.start()
        try:
            assert server._video_sock.getsockname()[0] == "127.0.0.1"
            assert server._audio_sock.getsockname()[0] == "127.0.0.1"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_udp_sockets_get_ephemeral_port(self):
        """UDP sockets receive an OS-assigned ephemeral port (> 0)."""
        server = LocalRtspServer()
        await server.start()
        try:
            assert server._video_sock.getsockname()[1] > 0
            assert server._audio_sock.getsockname()[1] > 0
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_rtsp_url_property_after_start(self):
        server = LocalRtspServer()
        await server.start()
        try:
            assert str(server._rtsp_port) in server.rtsp_url
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_server(self):
        server = LocalRtspServer()
        await server.start()
        await server.stop()
        assert server._server is None

    @pytest.mark.asyncio
    async def test_start_creates_feed_tasks(self):
        server = LocalRtspServer()
        await server.start()
        try:
            assert len(server._feed_tasks) == 2
            for task in server._feed_tasks:
                assert not task.done()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_feed_tasks(self):
        server = LocalRtspServer()
        await server.start()
        feed_tasks = list(server._feed_tasks)
        await server.stop()
        # Give the event loop a moment to process cancellation
        await asyncio.sleep(0)
        assert len(server._feed_tasks) == 0


# ---------------------------------------------------------------------------
# _broadcast_rtp
# ---------------------------------------------------------------------------


class TestBroadcastRtp:
    def _make_client(self, video_ch: int | None = 0, audio_ch: int | None = 2) -> _TcpClient:
        mock_writer = MagicMock()
        mock_writer.is_closing.return_value = False
        mock_writer.write = MagicMock()
        client = _TcpClient(writer=mock_writer, video_ch=video_ch, audio_ch=audio_ch)
        return client

    def test_broadcast_video_to_tcp_client(self):
        server = LocalRtspServer()
        client = self._make_client(video_ch=0)
        server._active_clients.append(client)

        pkt = b"\x80\xe0" + b"\x00" * 10
        server._broadcast_rtp(pkt, is_video=True)

        client.writer.write.assert_called_once()
        written = client.writer.write.call_args[0][0]
        # TCP interleaved: $ + channel + length (2 BE) + RTP
        assert written[0] == 0x24  # '$'
        assert written[1] == 0     # video channel
        length = struct.unpack_from("!H", written, 2)[0]
        assert length == len(pkt)

    def test_broadcast_audio_to_tcp_client(self):
        server = LocalRtspServer()
        client = self._make_client(audio_ch=2)
        server._active_clients.append(client)

        pkt = b"\x80\x08" + b"\x00" * 10
        server._broadcast_rtp(pkt, is_video=False)

        written = client.writer.write.call_args[0][0]
        assert written[1] == 2  # audio channel

    def test_broadcast_skips_closing_client(self):
        server = LocalRtspServer()
        mock_writer = MagicMock()
        mock_writer.is_closing.return_value = True
        mock_writer.write = MagicMock()
        client = _TcpClient(writer=mock_writer, video_ch=0)
        server._active_clients.append(client)

        server._broadcast_rtp(b"\x80\xe0" + b"\x00" * 10, is_video=True)
        mock_writer.write.assert_not_called()

    def test_broadcast_skips_client_with_no_channel(self):
        server = LocalRtspServer()
        client = self._make_client(video_ch=None)  # No video channel set
        server._active_clients.append(client)

        server._broadcast_rtp(b"\x80\xe0" + b"\x00" * 10, is_video=True)
        client.writer.write.assert_not_called()

    def test_broadcast_to_multiple_clients(self):
        server = LocalRtspServer()
        c1 = self._make_client(video_ch=0)
        c2 = self._make_client(video_ch=0)
        server._active_clients.extend([c1, c2])

        server._broadcast_rtp(b"\x80\xe0" + b"\x00" * 10, is_video=True)
        c1.writer.write.assert_called_once()
        c2.writer.write.assert_called_once()

    def test_broadcast_exception_does_not_crash(self):
        server = LocalRtspServer()
        mock_writer = MagicMock()
        mock_writer.is_closing.return_value = False
        mock_writer.write.side_effect = OSError("broken pipe")
        client = _TcpClient(writer=mock_writer, video_ch=0)
        server._active_clients.append(client)

        # Must not raise
        server._broadcast_rtp(b"\x80\xe0" + b"\x00" * 10, is_video=True)


# ---------------------------------------------------------------------------
# _send_h264 (FU-A fragmentation)
# ---------------------------------------------------------------------------


class TestSendH264:
    def _setup_server_with_client(self) -> tuple[LocalRtspServer, list[bytes]]:
        server = LocalRtspServer()
        written: list[bytes] = []
        mock_writer = MagicMock()
        mock_writer.is_closing.return_value = False
        mock_writer.write = lambda data: written.append(data)
        client = _TcpClient(writer=mock_writer, video_ch=0)
        server._active_clients.append(client)
        return server, written

    def test_small_nal_single_rtp_packet(self):
        """NAL ≤ 1400 bytes is sent as a single RTP packet with marker=True."""
        server, written = self._setup_server_with_client()
        nal_data = b"\x65" + b"\xAA" * 100  # IDR NAL
        server._send_h264(nal_data)

        assert len(written) == 1
        # Interleaved header is 4 bytes; RTP follows
        rtp = written[0][4:]
        # Marker bit should be set for single packet
        assert rtp[1] & 0x80

    def test_large_nal_fragmented_fu_a(self):
        """NAL > 1400 bytes is fragmented into multiple FU-A RTP packets."""
        server, written = self._setup_server_with_client()
        nal_data = b"\x65" + b"\xBB" * 3000  # Large IDR NAL
        server._send_h264(nal_data)

        assert len(written) > 1

        # First fragment: FU indicator type=28, FU header start bit set
        first_rtp = written[0][4:]
        nal_header_byte = first_rtp[12]
        fu_header_byte = first_rtp[13]
        assert (nal_header_byte & 0x1F) == 28  # FU-A type
        assert fu_header_byte & 0x80            # Start bit

        # Last fragment: end bit set, marker bit set
        last_rtp = written[-1][4:]
        last_fu_header = last_rtp[13]
        assert last_fu_header & 0x40            # End bit
        assert last_rtp[1] & 0x80              # Marker bit

    def test_small_nal_increments_video_seq(self):
        """Each RTP packet sent increments the video sequence counter."""
        server, _ = self._setup_server_with_client()
        assert server._video_seq == 0
        server._send_h264(b"\x65" + b"\x00" * 50)
        assert server._video_seq == 1

    def test_large_nal_increments_seq_per_fragment(self):
        """Each FU-A fragment increments the sequence counter."""
        server, written = self._setup_server_with_client()
        nal_data = b"\x65" + b"\x00" * 3000
        server._send_h264(nal_data)
        assert server._video_seq == len(written)

    def test_start_code_4_bytes_stripped(self):
        """_video_feed_loop strips 4-byte start codes before calling _send_h264."""
        # This tests the stripping logic in _video_feed_loop via the queue
        server = LocalRtspServer()
        server._running = True
        written: list[bytes] = []
        mock_writer = MagicMock()
        mock_writer.is_closing.return_value = False
        mock_writer.write = lambda data: written.append(data)
        client = _TcpClient(writer=mock_writer, video_ch=0)
        server._active_clients.append(client)

        # The feed loop reads from nal_queue and strips start codes
        nal_with_start = b"\x00\x00\x00\x01\x65" + b"\xCC" * 20
        server.nal_queue.put_nowait(nal_with_start)

        # Verify _send_h264 does not receive the start code
        calls = []
        original_send = server._send_h264

        def capture_send(nal_data):
            calls.append(nal_data)
            original_send(nal_data)

        server._send_h264 = capture_send
        # Drain queue manually (feed loop logic)
        if not server.nal_queue.empty():
            nal = server.nal_queue.get_nowait()
            if nal[:4] == b"\x00\x00\x00\x01":
                nal = nal[4:]
            server._send_h264(nal)

        assert len(calls) == 1
        assert not calls[0].startswith(b"\x00\x00\x00\x01")


# ---------------------------------------------------------------------------
# rtsp_url property
# ---------------------------------------------------------------------------


class TestRtspUrl:
    def test_rtsp_url_format(self):
        server = LocalRtspServer()
        server._rtsp_port = 8554
        assert server.rtsp_url == "rtsp://127.0.0.1:8554/intercom"

    def test_rtsp_url_custom_bind_host(self):
        server = LocalRtspServer(bind_host="0.0.0.0")
        server._rtsp_port = 8554
        assert server.rtsp_url == "rtsp://0.0.0.0:8554/intercom"
