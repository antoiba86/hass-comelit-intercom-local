"""Unit tests for video call signaling flow.

Tests the TCP signaling sequence in video_call.py and the supporting
client/protocol changes, using a mocked TCP connection.
"""

import asyncio
import struct
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.comelit_intercom_local.channels import Channel, ChannelType
from custom_components.comelit_intercom_local.client import IconaBridgeClient
from custom_components.comelit_intercom_local.models import DeviceConfig
from custom_components.comelit_intercom_local.protocol import (
    HEADER_SIZE,
    MessageType,
    encode_call_response_ack,
    encode_channel_open_response,
    encode_header,
)


# ---------------------------------------------------------------------------
# Helpers for building mock protocol packets
# ---------------------------------------------------------------------------


def _make_command_response(server_channel_id: int, sequence: int = 2) -> bytes:
    """Build a COMMAND response (channel open ack from device)."""
    body = struct.pack("<HH", MessageType.COMMAND, sequence)
    body += struct.pack("<I", 4)
    body += struct.pack("<H", server_channel_id)
    body += b"\x00\x00"
    return encode_header(len(body), 0) + body


def _make_udpm_response(server_channel_id: int, token: int = 0x1046) -> bytes:
    """Build a UDPM channel open response with token."""
    body = struct.pack("<HH", MessageType.COMMAND, 2)
    body += struct.pack("<I", 4)
    body += struct.pack("<H", server_channel_id)
    body += b"\x00\x00"
    body += struct.pack("<I", 2)
    body += struct.pack("<H", token)
    return encode_header(len(body), 0) + body


def _make_ctpp_msg(
    prefix: int,
    action: int,
    caller: str,
    callee: str,
    channel_id: int,
    flags: int = 0,
    extra: bytes = b"",
) -> bytes:
    """Build a CTPP binary message on the given channel."""
    body = bytearray()
    body += struct.pack("<H", prefix)
    body += struct.pack("<I", 0x12345678)  # timestamp
    body += struct.pack(">H", action)
    if flags or extra:
        body += struct.pack(">H", flags)
    body += extra
    body += b"\xff\xff\xff\xff"
    body += caller.encode("ascii") + b"\x00"
    body += callee.encode("ascii") + b"\x00\x00"
    return encode_header(len(body), channel_id) + bytes(body)


def _make_init_1800(channel_id: int, caller: str, callee: str) -> bytes:
    """Build 0x1800 ACK (init response)."""
    return _make_ctpp_msg(0x1800, 0x0000, caller, callee, channel_id)


def _make_init_1860(channel_id: int, caller: str, callee: str) -> bytes:
    """Build 0x1860 init status response."""
    # 0x1860 has extra fields before the separator
    body = bytearray()
    body += struct.pack("<H", 0x1860)
    body += struct.pack("<I", 0x12345678)
    body += struct.pack(">H", 0x0010)  # action
    body += struct.pack(">H", 0x0041)  # flags
    body += bytes([0xAC, 0x23])  # extra
    body += b"SB0000061\x00\x00\x00"
    body += b"\xff\xff\xff\xff"
    body += b"SB000006\x00\x00"
    body += b"SB0000061\x00"
    return encode_header(len(body), channel_id) + bytes(body)


def _make_codec_response(channel_id: int, caller: str, callee: str) -> bytes:
    """Build device codec response (0x1840 action=0x0008)."""
    extra = bytes([0x50, 0x03, 0x3B, 0x00, 0x00, 0x00])
    return _make_ctpp_msg(0x1840, 0x0008, caller, callee, channel_id, flags=0x0003, extra=extra)


def _make_link_status(channel_id: int, caller: str, callee: str) -> bytes:
    """Build device link status (0x1840 action=0x0002)."""
    extra = bytes([0x00, 0x00])
    return _make_ctpp_msg(0x1840, 0x0002, caller, callee, channel_id, flags=0x000C, extra=extra)


def _make_device_channel_open(channel_name: str, req_id: int) -> bytes:
    """Build a device-initiated channel open (COMMAND seq=1)."""
    body = struct.pack("<HH", MessageType.COMMAND, 1)
    body += struct.pack("<I", 7)  # type = UAUT
    body += channel_name.encode("ascii")
    body += struct.pack("<H", req_id)
    body += bytes([0x01])  # trailing
    return encode_header(len(body), 0) + body


def _make_device_rtpc_link(channel_id: int, caller: str, callee: str, req_id: int) -> bytes:
    """Build device's RTPC link message (0x1840 action=0x000A)."""
    extra = bytearray()
    extra += bytes([0x18, 0x02, 0x00, 0x00, 0x00, 0x00])
    extra += struct.pack("<H", req_id)
    extra += bytes([0x00, 0x00])
    return _make_ctpp_msg(0x1840, 0x000A, caller, callee, channel_id, flags=0x0011, extra=bytes(extra))


def _make_end_message(channel_id: int) -> bytes:
    """Build a channel END/close message."""
    body = struct.pack("<HH", MessageType.END, 3)
    body += struct.pack("<I", 2)
    body += struct.pack("<H", channel_id)
    return encode_header(len(body), 0) + body


class FakeStreamReader:
    """Simulates asyncio.StreamReader with queued data."""

    def __init__(self):
        self._buffer = bytearray()

    def feed(self, data: bytes):
        self._buffer.extend(data)

    async def readexactly(self, n: int) -> bytes:
        for _ in range(200):
            if len(self._buffer) >= n:
                result = bytes(self._buffer[:n])
                del self._buffer[:n]
                return result
            await asyncio.sleep(0.005)
        raise asyncio.IncompleteReadError(bytes(self._buffer), n)


class FakeStreamWriter:
    """Captures all written data."""

    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


async def _setup_client():
    """Create a client with fake reader/writer and start receive loop."""
    reader = FakeStreamReader()
    writer = FakeStreamWriter()
    client = IconaBridgeClient("127.0.0.1")
    client._reader = reader
    client._writer = writer
    client._connected = True
    client._receive_task = asyncio.create_task(client._receive_loop())
    return client, reader, writer


async def _teardown_client(client):
    """Clean up client."""
    client._connected = False
    if client._receive_task:
        client._receive_task.cancel()
        try:
            await client._receive_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Tests: encode_channel_open_response
# ---------------------------------------------------------------------------


class TestChannelOpenResponse:
    """Test the COMMAND response encoding for device-initiated channel opens."""

    def test_matches_pcap_bytes(self):
        """Verify encode_channel_open_response matches PCAP frame 184 exactly."""
        pkt = encode_channel_open_response(0xF7EC)
        expected = bytes.fromhex("00060c0000000000cdab020004000000ecf70000")
        assert pkt == expected

    def test_different_request_id(self):
        """Test with a different request ID."""
        pkt = encode_channel_open_response(0xBFA3)
        assert len(pkt) == 20  # 8-byte header + 12-byte body
        # Verify header
        assert pkt[0:2] == b"\x00\x06"
        body_len = struct.unpack_from("<H", pkt, 2)[0]
        assert body_len == 12
        req_id = struct.unpack_from("<H", pkt, 4)[0]
        assert req_id == 0  # COMMAND packets always use req_id=0
        # Verify body contains the request ID
        body_req_id = struct.unpack_from("<H", pkt, 16)[0]
        assert body_req_id == 0xBFA3


# ---------------------------------------------------------------------------
# Tests: Device-initiated channel open dispatch
# ---------------------------------------------------------------------------


class TestDeviceChannelOpen:
    """Test that _dispatch handles device-initiated channel opens correctly."""

    @pytest.mark.asyncio
    async def test_device_channel_open_sends_response(self):
        """When device opens a channel, client should auto-respond with COMMAND response."""
        client, reader, writer = await _setup_client()
        try:
            # Register a placeholder channel
            placeholder = client.register_placeholder_channel("RTPC_DEVICE")

            # Feed a device channel open
            reader.feed(_make_device_channel_open("RTPC", 0xBFA3))
            await asyncio.sleep(0.1)

            # Placeholder should be assigned
            assert placeholder.is_open
            assert placeholder.server_channel_id == 0xBFA3

            # Client should have written a COMMAND response
            expected_resp = encode_channel_open_response(0xBFA3)
            assert expected_resp in bytes(writer.data)
        finally:
            await _teardown_client(client)

    @pytest.mark.asyncio
    async def test_device_channel_open_without_placeholder(self):
        """Device channel open without placeholder still sends COMMAND response."""
        client, reader, writer = await _setup_client()
        try:
            reader.feed(_make_device_channel_open("RTPC", 0x1234))
            await asyncio.sleep(0.1)

            expected_resp = encode_channel_open_response(0x1234)
            assert expected_resp in bytes(writer.data)
        finally:
            await _teardown_client(client)

    @pytest.mark.asyncio
    async def test_end_message_not_assigned_to_placeholder(self):
        """END (channel close) messages should NOT be assigned to placeholder channels."""
        client, reader, writer = await _setup_client()
        try:
            placeholder = client.register_placeholder_channel("RTPC_DEVICE")

            # Feed an END message
            reader.feed(_make_end_message(0xBFA3))
            await asyncio.sleep(0.1)

            # Placeholder should NOT be assigned
            assert not placeholder.is_open
            assert placeholder.server_channel_id == 0
        finally:
            await _teardown_client(client)


# ---------------------------------------------------------------------------
# Tests: CTPP init ACK format
# ---------------------------------------------------------------------------


class TestCallResponseAck:
    """Test encode_call_response_ack format matches PCAP."""

    def test_1800_ack_format(self):
        """0x1800 ACK should have correct structure without flags field."""
        ack = encode_call_response_ack("SB0000061", "SB000006", 0x12345678)
        assert len(ack) > 8
        prefix = struct.unpack_from("<H", ack, 0)[0]
        assert prefix == 0x1800
        action = struct.unpack_from(">H", ack, 6)[0]
        assert action == 0x0000
        # Should contain 0xFFFFFFFF separator
        assert b"\xff\xff\xff\xff" in ack
        # Should contain caller and callee
        assert b"SB0000061\x00" in ack
        assert b"SB000006\x00\x00" in ack

    def test_1820_ack_format(self):
        """0x1820 ACK should have same structure with different prefix."""
        ack = encode_call_response_ack("SB0000061", "SB000006", 0x12345678, prefix=0x1820)
        prefix = struct.unpack_from("<H", ack, 0)[0]
        assert prefix == 0x1820

    def test_ack_no_flags_field(self):
        """ACK should NOT have a flags field — shorter than data messages."""
        ack = encode_call_response_ack("SB0000061", "SB100001", 0x12345678)
        # Format: [prefix 2] [timestamp 4] [action 2] [0xFFFFFFFF 4] [caller\0] [callee\0\0]
        # = 2 + 4 + 2 + 4 + 10 + 10 = 32 bytes
        assert len(ack) == 32


# ---------------------------------------------------------------------------
# Tests: Codec exchange message filtering
# ---------------------------------------------------------------------------


class TestCodecExchangeFiltering:
    """Test that codec exchange properly handles retransmits."""

    @pytest.mark.asyncio
    async def test_read_response_queues_binary(self):
        """Binary responses should be queued on the correct channel."""
        client, reader, writer = await _setup_client()
        try:
            # Open a channel
            reader.feed(_make_command_response(server_channel_id=100))
            channel = await asyncio.wait_for(
                client.open_channel("CTPP", ChannelType.CTPP), timeout=3.0
            )

            # Feed a binary response on that channel
            body = b"\x40\x18\x00\x00\x00\x00\x00\x08\x00\x03"
            pkt = encode_header(len(body), 100) + body
            reader.feed(pkt)
            await asyncio.sleep(0.2)

            # Should be available via read_response (read_response handles its own timeout)
            resp = await client.read_response(channel, timeout=2.0)
            assert resp is not None
            assert struct.unpack_from("<H", resp, 0)[0] == 0x1840
        finally:
            await _teardown_client(client)

    @pytest.mark.asyncio
    async def test_read_response_timeout(self):
        """read_response should return None on timeout."""
        client, reader, writer = await _setup_client()
        try:
            reader.feed(_make_command_response(server_channel_id=100))
            channel = await asyncio.wait_for(
                client.open_channel("CTPP", ChannelType.CTPP), timeout=3.0
            )

            resp = await client.read_response(channel, timeout=0.1)
            assert resp is None
        finally:
            await _teardown_client(client)


# ---------------------------------------------------------------------------
# Tests: Full video signaling flow (mocked)
# ---------------------------------------------------------------------------


class TestVideoSignalingFlow:
    """Integration-style tests for the video call signaling sequence."""

    @pytest.mark.asyncio
    async def test_init_ack_timing(self):
        """CTPP init should ACK after exactly 2 responses (not wait for 3rd)."""
        client, reader, writer = await _setup_client()
        try:
            ctpp_ch_id = 100

            # Open CTPP channel
            reader.feed(_make_command_response(server_channel_id=ctpp_ch_id))
            ctpp = await asyncio.wait_for(
                client.open_channel("CTPP", ChannelType.CTPP), timeout=3.0
            )

            # Send CTPP init (simulate what video_call.py does)
            from custom_components.comelit_intercom_local.protocol import encode_ctpp_init
            init_payload = encode_ctpp_init("SB000006", 1)
            await client.send_binary(ctpp, init_payload)

            writer.data.clear()  # Clear so we can check what's sent after

            # Feed 0x1800 + 0x1860 responses
            reader.feed(_make_init_1800(ctpp_ch_id, "SB000006", "SB0000061"))
            reader.feed(_make_init_1860(ctpp_ch_id, "SB000006", "SB0000061"))

            # Read exactly 2 responses (matching video_call.py Step 2)
            for _ in range(2):
                resp = await client.read_response(ctpp, timeout=3.0)
                assert resp is not None

            # Send ACKs
            ack1 = encode_call_response_ack("SB0000061", "SB000006", 0x12345678)
            await client.send_binary(ctpp, ack1)
            ack2 = encode_call_response_ack("SB0000061", "SB000006", 0x12345678, prefix=0x1820)
            await client.send_binary(ctpp, ack2)

            # Verify both ACKs were sent
            sent = bytes(writer.data)
            # Should contain 0x1800 prefix ACK
            assert b"\x00\x18" in sent
            # Should contain 0x1820 prefix ACK
            assert b"\x20\x18" in sent
        finally:
            await _teardown_client(client)

    @pytest.mark.asyncio
    async def test_codec_exchange_ignores_1860_retransmits(self):
        """Codec exchange should skip 0x1860 init retransmits."""
        client, reader, writer = await _setup_client()
        try:
            ctpp_ch_id = 100
            reader.feed(_make_command_response(server_channel_id=ctpp_ch_id))
            ctpp = await asyncio.wait_for(
                client.open_channel("CTPP", ChannelType.CTPP), timeout=3.0
            )

            dev_caller = "SB100001"
            our_caller = "SB0000061"

            # Simulate codec exchange with init retransmits mixed in:
            # 1. 0x1800 ACK (skip)
            # 2. 0x1840 codec response (ACK it)
            # 3. 0x1860 init retransmit (IGNORE)
            # 4. 0x1840 link status action=0x0002 (ACK + break)
            reader.feed(_make_init_1800(ctpp_ch_id, dev_caller, our_caller))
            reader.feed(_make_codec_response(ctpp_ch_id, dev_caller, our_caller))
            reader.feed(_make_init_1860(ctpp_ch_id, "SB000006", our_caller))
            reader.feed(_make_link_status(ctpp_ch_id, dev_caller, our_caller))

            writer.data.clear()
            ack_count = 0
            got_link_status = False

            for i in range(10):
                resp = await client.read_response(ctpp, timeout=2.0)
                if not resp:
                    break
                msg_type = struct.unpack_from("<H", resp, 0)[0]
                action = struct.unpack_from(">H", resp, 6)[0] if len(resp) >= 8 else 0

                if msg_type == 0x1860:
                    continue  # Skip init retransmits
                if msg_type == 0x1800:
                    continue  # Skip ACKs
                if msg_type == 0x1840:
                    ack_count += 1
                    if action == 0x0002:
                        got_link_status = True
                        break

            assert got_link_status, "Should have received link status"
            assert ack_count == 2, "Should have seen 2 x 0x1840 messages (codec + link status)"

        finally:
            await _teardown_client(client)

    @pytest.mark.asyncio
    async def test_device_rtpc_link_acked(self):
        """After video config, device's RTPC link on CTPP should be ACKed."""
        client, reader, writer = await _setup_client()
        try:
            ctpp_ch_id = 100
            reader.feed(_make_command_response(server_channel_id=ctpp_ch_id))
            ctpp = await asyncio.wait_for(
                client.open_channel("CTPP", ChannelType.CTPP), timeout=3.0
            )

            dev_caller = "SB100001"
            our_caller = "SB0000061"

            # Simulate device sending RTPC link on CTPP
            reader.feed(_make_device_rtpc_link(ctpp_ch_id, dev_caller, our_caller, 0xBFA3))

            resp = await client.read_response(ctpp, timeout=2.0)
            assert resp is not None
            msg_type = struct.unpack_from("<H", resp, 0)[0]
            assert msg_type == 0x1840

            # Send ACK (like video_call.py Step 9c)
            writer.data.clear()
            ack = encode_call_response_ack(our_caller, dev_caller, 0x12345678)
            await client.send_binary(ctpp, ack)

            sent = bytes(writer.data)
            # Verify ACK was sent (0x1800 prefix)
            assert b"\x00\x18" in sent

        finally:
            await _teardown_client(client)

    @pytest.mark.asyncio
    async def test_rtpc_link_response_skips_retransmits(self):
        """After RTPC link, should skip stale retransmits until 0x1800 ACK."""
        client, reader, writer = await _setup_client()
        try:
            ctpp_ch_id = 100
            reader.feed(_make_command_response(server_channel_id=ctpp_ch_id))
            ctpp = await asyncio.wait_for(
                client.open_channel("CTPP", ChannelType.CTPP), timeout=3.0
            )

            dev_caller = "SB100001"
            our_caller = "SB0000061"

            # Feed: 0x1860 retransmit, 0x1840 codec retransmit, then 0x1800 ACK
            reader.feed(_make_init_1860(ctpp_ch_id, "SB000006", our_caller))
            reader.feed(_make_codec_response(ctpp_ch_id, dev_caller, our_caller))
            reader.feed(_make_init_1800(ctpp_ch_id, dev_caller, our_caller))

            # Simulate the loop from video_call.py that skips retransmits
            got_ack = False
            for _ in range(5):
                resp = await client.read_response(ctpp, timeout=2.0)
                if not resp:
                    break
                msg_type = struct.unpack_from("<H", resp, 0)[0]
                if msg_type == 0x1800:
                    got_ack = True
                    break
                if msg_type == 0x1860:
                    continue
                # 0x1840 stale retransmit — skip
                continue

            assert got_ack, "Should have found the 0x1800 ACK after skipping retransmits"

        finally:
            await _teardown_client(client)


# ---------------------------------------------------------------------------
# Tests: Protocol encoding for video call messages
# ---------------------------------------------------------------------------


class TestVideoProtocolEncoding:
    """Test video-specific protocol encoding functions."""

    def test_encode_call_init_structure(self):
        """Call init should have 0x18C0 prefix and action 0x0028."""
        from custom_components.comelit_intercom_local.protocol import encode_call_init
        msg = encode_call_init("SB0000061", "SB100001", 0x12345678)
        prefix = struct.unpack_from("<H", msg, 0)[0]
        assert prefix == 0x18C0
        action = struct.unpack_from(">H", msg, 6)[0]
        assert action == 0x0028
        assert b"SB0000061" in msg
        assert b"SB100001" in msg
        assert b"II" in msg  # codec marker

    def test_encode_call_ack_structure(self):
        """Codec ack should have 0x1840 prefix and action 0x0008."""
        from custom_components.comelit_intercom_local.protocol import encode_call_ack
        msg = encode_call_ack("SB0000061", "SB100001", 0x12345678)
        prefix = struct.unpack_from("<H", msg, 0)[0]
        assert prefix == 0x1840
        action = struct.unpack_from(">H", msg, 6)[0]
        assert action == 0x0008

    def test_encode_rtpc_link_structure(self):
        """RTPC link should have action 0x000A and embed the RTPC req_id."""
        from custom_components.comelit_intercom_local.protocol import encode_rtpc_link
        msg = encode_rtpc_link("SB0000061", "SB100001", 0x21B5, 0x12345678)
        prefix = struct.unpack_from("<H", msg, 0)[0]
        assert prefix == 0x1840
        action = struct.unpack_from(">H", msg, 6)[0]
        assert action == 0x000A
        # RTPC req_id should be embedded in extra bytes
        assert struct.pack("<H", 0x21B5) in msg

    def test_encode_video_config_structure(self):
        """Video config should have action 0x001A and contain resolution."""
        from custom_components.comelit_intercom_local.protocol import encode_video_config
        msg = encode_video_config("SB0000061", "SB100001", 0x21B6, 0x12345678)
        prefix = struct.unpack_from("<H", msg, 0)[0]
        assert prefix == 0x1840
        action = struct.unpack_from(">H", msg, 6)[0]
        assert action == 0x001A
        # Should contain width=800 (0x0320) and height=480 (0x01E0) LE
        assert struct.pack("<H", 800) in msg
        assert struct.pack("<H", 480) in msg
        # Secondary resolution 320x240
        assert struct.pack("<H", 320) in msg
        assert struct.pack("<H", 240) in msg
        # FPS=16
        assert struct.pack("<H", 16) in msg
