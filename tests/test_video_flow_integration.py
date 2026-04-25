"""Integration test for the video call flow.

Exercises VideoCallSession against a FakeComelitDevice (in-process asyncio TCP
server) that speaks the real ICONA Bridge protocol. Only external network I/O
is mocked:
  - RtpReceiver UDP socket methods (start_control, start_keepalive, start_media,
    wait_for_first_video, stop)
  - LocalRtspServer (no real RTSP port needed)

The test exercises the full path:
  authenticate → get_device_config → VideoCallSession.start()
  → CALL_END arrives → _inline_reestablish (renewal)
  → session still active → stop()
"""

from __future__ import annotations

import asyncio
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.auth import authenticate
from custom_components.comelit_intercom_local.client import IconaBridgeClient
from custom_components.comelit_intercom_local.config_reader import get_device_config
from custom_components.comelit_intercom_local.rtp_receiver import RtpReceiver
from custom_components.comelit_intercom_local.video_call import VideoCallSession


# ---------------------------------------------------------------------------
# Wire-protocol helpers (mirrors protocol.py without importing it)
# ---------------------------------------------------------------------------

_MAGIC = b"\x00\x06"


def _hdr(body: bytes | bytearray, req_id: int = 0) -> bytes:
    """Build an 8-byte ICONA Bridge packet header."""
    return (
        _MAGIC
        + struct.pack("<H", len(body))
        + struct.pack("<H", req_id)
        + b"\x00\x00"
    )


def _pkt(body: bytes | bytearray, req_id: int = 0) -> bytes:
    """Build a complete ICONA packet (header + body)."""
    body = bytes(body)
    return _hdr(body, req_id) + body


def _command_response(server_ch_id: int, extra: bytes = b"") -> bytes:
    """COMMAND response body sent by the device to acknowledge a channel open."""
    body = bytearray(10)
    struct.pack_into("<H", body, 0, 0xABCD)  # COMMAND
    struct.pack_into("<H", body, 2, 2)        # seq=2
    struct.pack_into("<H", body, 8, server_ch_id)
    return _pkt(bytes(body) + extra)


def _ctpp_pkt(server_ch_id: int, prefix: int, action: int, flags: int = 0) -> bytes:
    """Minimal CTPP binary packet the fake device sends on the CTPP channel.

    Structure matches what VideoCallSession parses:
      prefix(LE16) + ts(LE32) + action(BE16) + flags(BE16) + 0xFFFFFFFF + addrs
    """
    caller = "SB100001"
    callee = "SB0000061"
    body = bytearray()
    body += struct.pack("<H", prefix)
    body += struct.pack("<I", 0)            # timestamp (device-side, ignored by client)
    body += struct.pack(">H", action)
    body += struct.pack(">H", flags)
    body += b"\xff\xff\xff\xff"
    body += caller.encode() + b"\x00"
    body += callee.encode() + b"\x00\x00"
    return _pkt(bytes(body), server_ch_id)


def _device_rtpc_open(dev_req_id: int) -> bytes:
    """Device-initiated RTPC channel open.

    The client's _dispatch reads dev_req_id from body[-3:-1] (the fallback path
    used when there is no null terminator after the 4-char channel name).
    """
    body = bytearray()
    body += struct.pack("<HH", 0xABCD, 1)  # COMMAND, seq=1
    body += struct.pack("<I", 7)            # ChannelType.UAUT = 7
    body += b"RTPC"                         # 4-char name, no null
    body += struct.pack("<H", dev_req_id)   # request_id (landed at body[-3:-1])
    body += bytes([1])                      # trailing_byte
    return _pkt(bytes(body), 0)


# ---------------------------------------------------------------------------
# FakeComelitDevice — asyncio TCP server
# ---------------------------------------------------------------------------


class FakeComelitDevice:
    """In-process ICONA Bridge server for integration tests.

    Responds to channel opens, auth/config JSON, and the binary CTPP
    signaling sequence used by VideoCallSession. After the first HANGUP/ZERO
    (which signals "call accepted"), schedules a CALL_END after 200 ms to
    trigger _inline_reestablish. Sets renewal_done when it receives the
    renewal answer_peer (0x1860 with ACTION_PEER at body[8:10]).
    """

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None

        # Server-side channel ID counter (starts above real device range)
        self._next_id: int = 0x6060
        # server_channel_id → logical channel name (UAUT, UCFG, CTPP, …)
        self._ch: dict[int, str] = {}
        self._ctpp_id: int = 0

        # State counters for dispatching the correct response per phase
        self._ctpp_init_count: int = 0
        self._video_config_count: int = 0
        self._hangup_zero_count: int = 0

        # Set when the renewal answer_peer (0x1860/0x0070) is received
        self.renewal_done: asyncio.Event = asyncio.Event()

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _alloc(self) -> int:
        sid = self._next_id
        self._next_id += 1
        return sid

    def _write(self, pkt: bytes) -> None:
        if self._writer:
            self._writer.write(pkt)

    async def _flush(self) -> None:
        if self._writer:
            await self._writer.drain()

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writer = writer
        try:
            while True:
                hdr = await reader.readexactly(8)
                body_len = struct.unpack_from("<H", hdr, 2)[0]
                req_id = struct.unpack_from("<H", hdr, 4)[0]
                body = await reader.readexactly(body_len) if body_len else b""
                await self._dispatch(req_id, body)
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass
        finally:
            writer.close()

    # ------------------------------------------------------------------
    # Packet dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, req_id: int, body: bytes) -> None:
        if req_id == 0:
            await self._on_channel_open(body)
        elif body and body[:1] == b"{":
            await self._on_json(req_id, body)
        else:
            await self._on_binary(req_id, body)

    async def _on_channel_open(self, body: bytes) -> None:
        if len(body) < 12:
            return
        msg_type = struct.unpack_from("<H", body, 0)[0]
        seq = struct.unpack_from("<H", body, 2)[0]
        if msg_type != 0xABCD or seq != 1:
            return  # not a client-initiated channel open

        name = body[8:12].decode("ascii", errors="ignore").rstrip("\x00")
        sid = self._alloc()
        self._ch[sid] = name
        if name == "CTPP":
            self._ctpp_id = sid

        # UDPM response carries a 2-byte token at offset 16 of the body.
        # VideoCallSession extracts: udpm_token = LE16 at open_response_body[16:18].
        extra = bytes(6) + struct.pack("<H", 0x1234) if name == "UDPM" else b""
        self._write(_command_response(sid, extra))
        await self._flush()

    async def _on_json(self, req_id: int, body: bytes) -> None:
        ch_name = self._ch.get(req_id, "")
        if ch_name == "UAUT":
            resp: dict = {
                "message": "access",
                "message-type": "response",
                "message-id": 1,
                "response-code": 200,
                "response-string": "Access Granted",
            }
        elif ch_name == "UCFG":
            resp = {
                "message": "get-configuration",
                "message-type": "response",
                "message-id": 3,
                "response-code": 200,
                "vip": {
                    "apt-address": "SB000006",
                    "apt-subaddress": 1,
                    "user-parameters": {
                        "entrance-address-book": [{"apt-address": "SB100001"}],
                        "opendoor-address-book": [
                            {
                                "id": 0,
                                "name": "Actuator",
                                "apt-address": "SB000006",
                                "output-index": 1,
                                "secure-mode": False,
                            }
                        ],
                        "actuator-address-book": [],
                        "rtsp-camera-address-book": [],
                    },
                },
            }
        else:
            return
        encoded = json.dumps(resp, separators=(",", ":")).encode()
        self._write(_pkt(encoded, req_id))
        await self._flush()

    async def _on_binary(self, req_id: int, body: bytes) -> None:
        if self._ch.get(req_id) != "CTPP" or len(body) < 8:
            return
        prefix = struct.unpack_from("<H", body, 0)[0]
        action = struct.unpack_from(">H", body, 6)[0]

        if prefix == 0x18C0:
            # Distinguish ctpp_init (FLAGS1=[0x00,0x11]) from call_init ([0x00,0x28])
            if body[6:8] == b"\x00\x11":
                await self._on_ctpp_init()
            elif body[6:8] == b"\x00\x28":
                await self._on_call_init()

        elif prefix == 0x1840:
            if action == 0x0008:                  # codec_ack
                await self._on_codec_ack()
            elif action == 0x001A:                # VIDEO_CONFIG
                await self._on_video_config()
            elif action == 0x0000:                # HANGUP/ZERO (call accepted signal)
                await self._on_hangup_zero()
            # 0x000A (rtpc_link from client), other → ignore

        elif prefix == 0x1860 and len(body) >= 10:
            # Renewal answer_peer: ACTION_PEER (0x0070) is at body[8:10] in
            # encode_answer_peer (not at body[6:8] as in _build_ctpp_video_msg).
            if struct.unpack_from(">H", body, 8)[0] == 0x0070:
                self.renewal_done.set()

        # 0x1800 / 0x1820 ACKs from client → ignore

    # ------------------------------------------------------------------
    # CTPP message handlers
    # ------------------------------------------------------------------

    async def _on_ctpp_init(self) -> None:
        """Respond with 2 dummy ACKs so ctpp_init_sequence doesn't wait 5s each."""
        self._ctpp_init_count += 1
        # 0x1800 ACK (first response consumed by read_response_ctpp)
        self._write(_ctpp_pkt(self._ctpp_id, 0x1800, 0x0000))
        # 0x1860/0x0010 renewal signal (second response)
        self._write(_ctpp_pkt(self._ctpp_id, 0x1860, 0x0010))
        await self._flush()

    async def _on_call_init(self) -> None:
        """Respond with a bare 0x1800 ACK (read by VideoCallSession as resp1)."""
        self._write(_ctpp_pkt(self._ctpp_id, 0x1800, 0x0000))
        await self._flush()

    async def _on_codec_ack(self) -> None:
        """Respond with call-accepted (0x1840/0x0002) to complete codec exchange."""
        self._write(_ctpp_pkt(self._ctpp_id, 0x1840, 0x0002))
        await self._flush()

    async def _on_video_config(self) -> None:
        """After VIDEO_CONFIG: send device RTPC link then device-initiated RTPC open.

        Send 0x1840/0x000A FIRST so it lands in the CTPP response_queue before
        the device RTPC open sets device_rtpc.open_event. That way
        _ack_device_rtpc_link reads from the queue immediately after waking up.
        """
        self._video_config_count += 1
        # 1. Device's CTPP RTPC link message → queued in CTPP.response_queue
        self._write(_ctpp_pkt(self._ctpp_id, 0x1840, 0x000A))
        # 2. Device-initiated RTPC channel open → assigns placeholder in client
        dev_req_id = 0xBE00 + self._video_config_count
        self._write(_device_rtpc_open(dev_req_id))
        await self._flush()

    async def _on_hangup_zero(self) -> None:
        """After the first HANGUP/ZERO, schedule a CALL_END to trigger renewal."""
        self._hangup_zero_count += 1
        if self._hangup_zero_count == 1:
            asyncio.create_task(self._send_call_end(delay=0.2))

    async def _send_call_end(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._write(_ctpp_pkt(self._ctpp_id, 0x1840, 0x0003))  # CALL_END
        await self._flush()


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_video_flow_start_renewal_stop():
    """Full video flow: start → CALL_END → inline re-establishment → stop.

    FakeComelitDevice drives the entire ICONA/CTPP signaling exchange.
    Only external I/O (UDP sockets, RTSP server) is mocked.
    """
    device = FakeComelitDevice()
    await device.start()

    client = IconaBridgeClient(device.host, device.port)
    await client.connect()

    mock_rtsp = MagicMock()
    mock_rtsp.start = AsyncMock()
    mock_rtsp.stop = AsyncMock()
    mock_rtsp.reset = MagicMock()
    mock_rtsp.nal_queue = asyncio.Queue()
    mock_rtsp.audio_queue = asyncio.Queue()
    mock_rtsp.rtp_queue = asyncio.Queue()

    try:
        await authenticate(client, "fake_token_32chars_exactly_pad00")
        config = await get_device_config(client)

        session = VideoCallSession(
            client=client,
            config=config,
            auto_timeout=False,
        )

        with (
            patch(
                "custom_components.comelit_intercom_local.video_call.LocalRtspServer",
                return_value=mock_rtsp,
            ),
            patch.object(RtpReceiver, "start_control", new_callable=AsyncMock),
            patch.object(RtpReceiver, "start_keepalive"),
            patch.object(RtpReceiver, "start_media", new_callable=AsyncMock),
            patch.object(
                RtpReceiver,
                "wait_for_first_video",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(RtpReceiver, "stop", new_callable=AsyncMock),
        ):
            await session.start()
            assert session.active is True

            # FakeDevice sends CALL_END 200 ms after the first HANGUP/ZERO.
            # _inline_reestablish completes and sets renewal_done via the
            # renewal answer_peer (0x1860/0x0070).
            await asyncio.wait_for(device.renewal_done.wait(), timeout=10.0)
            assert session.active is True

            await session.stop()

        assert session.active is False

    finally:
        await client.disconnect()
        await device.stop()


@pytest.mark.asyncio
async def test_video_flow_session_stops_cleanly_without_renewal():
    """Session can be stopped before any CALL_END arrives."""
    device = FakeComelitDevice()
    await device.start()

    client = IconaBridgeClient(device.host, device.port)
    await client.connect()

    mock_rtsp = MagicMock()
    mock_rtsp.start = AsyncMock()
    mock_rtsp.stop = AsyncMock()
    mock_rtsp.reset = MagicMock()
    mock_rtsp.nal_queue = asyncio.Queue()
    mock_rtsp.audio_queue = asyncio.Queue()
    mock_rtsp.rtp_queue = asyncio.Queue()

    try:
        await authenticate(client, "fake_token_32chars_exactly_pad00")
        config = await get_device_config(client)

        session = VideoCallSession(
            client=client,
            config=config,
            auto_timeout=False,
        )

        with (
            patch(
                "custom_components.comelit_intercom_local.video_call.LocalRtspServer",
                return_value=mock_rtsp,
            ),
            patch.object(RtpReceiver, "start_control", new_callable=AsyncMock),
            patch.object(RtpReceiver, "start_keepalive"),
            patch.object(RtpReceiver, "start_media", new_callable=AsyncMock),
            patch.object(
                RtpReceiver,
                "wait_for_first_video",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(RtpReceiver, "stop", new_callable=AsyncMock),
        ):
            await session.start()
            assert session.active is True

            # Stop before CALL_END fires (it is scheduled 200 ms after
            # HANGUP/ZERO — stop immediately).
            await session.stop()

        assert session.active is False

    finally:
        await client.disconnect()
        await device.stop()
