"""Minimal local RTSP/RTP server for go2rtc.

Serves H.264 + G.711 PCMA over RTSP so that go2rtc (bundled in Home
Assistant) can relay the stream to the browser via WebRTC.

Supports multiple simultaneous clients (go2rtc + HA stream worker both
connect to stream_source() at the same time).  Feed tasks run from start()
to stop() and broadcast RTP to every registered client independently —
no client can steal another's data.

Transport modes:
  - TCP interleaving (RTP/AVP/TCP) — default for go2rtc and FFmpeg
  - UDP unicast (RTP/AVP) — fallback (single client)

Protocol flow (RFC 2326):
    client → OPTIONS  → 200 OK
    client → DESCRIBE → 200 OK + SDP (video H.264 PT96 + audio PCMA PT8)
    client → SETUP video → 200 OK
    client → SETUP audio → 200 OK
    client → PLAY       → 200 OK  [client registered, starts receiving RTP]
    [server broadcasts RTP until TEARDOWN or disconnect]

TCP interleaved RTP (RFC 2326 §10.12):
    $ | channel (1 byte) | length (2 bytes BE) | RTP packet

Audio keepalive:
    When no real audio is available, silent PCMA (0xD5) is sent every ~1s
    so go2rtc and the stream worker stay connected between calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import socket
import struct

_LOGGER = logging.getLogger(__name__)

_MAX_RTP_PAYLOAD = 1400  # bytes — safe MTU headroom
_PCMA_SILENCE = bytes([0xD5] * 160)  # 20ms G.711 A-law silence


@dataclasses.dataclass
class _TcpClient:
    """Per-connection state for one RTSP/TCP client."""

    writer: asyncio.StreamWriter
    video_ch: int | None = None  # interleaved channel for video RTP
    audio_ch: int | None = None  # interleaved channel for audio RTP


class LocalRtspServer:
    """Minimal RTSP server that streams H.264 + G.711 PCMA.

    Supports multiple simultaneous TCP clients — each client registered on
    PLAY receives all RTP independently.  Feed tasks run permanently from
    start() and broadcast to the current client list.

    Usage:
        server = LocalRtspServer()
        url = await server.start()
        receiver.attach_rtsp_queues(server.nal_queue, server.audio_queue)
        # …later…
        await server.stop()
    """

    def __init__(self, bind_host: str = "127.0.0.1") -> None:
        self._bind_host = bind_host
        self._rtsp_port: int = 0
        self._server: asyncio.Server | None = None

        # Incoming media queues — attached to rtp_receiver via attach_rtsp_queues
        self.nal_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=300)
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)

        # UDP sockets — fallback for clients that request UDP transport
        self._video_sock: socket.socket | None = None
        self._audio_sock: socket.socket | None = None
        self._video_server_port: int = 0
        self._audio_server_port: int = 0
        # UDP is single-client (last SETUP wins)
        self._udp_host: str | None = None
        self._udp_video_port: int = 0
        self._udp_audio_port: int = 0

        # Active TCP clients — appended on PLAY, removed on disconnect
        self._active_clients: list[_TcpClient] = []

        # RTP sequence / timestamp state (shared across all clients)
        self._video_seq: int = 0
        self._audio_seq: int = 0
        self._video_ts: int = 0
        self._audio_ts: int = 0
        self._video_ssrc: int = 0xC0DE1234
        self._audio_ssrc: int = 0xA0D10001
        self._session_id: str = "87654321"

        self._running = False
        self._feed_tasks: list[asyncio.Task] = []

    @property
    def rtsp_url(self) -> str:
        """Return the RTSP URL that go2rtc should connect to."""
        return f"rtsp://{self._bind_host}:{self._rtsp_port}/intercom"

    async def start(self) -> str:
        """Start the RTSP server, bind UDP sockets, and start feed tasks.

        Returns the RTSP URL for the camera entity's stream_source.
        Feed tasks run until stop() — they broadcast to whoever is registered.
        """
        self._video_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._video_sock.bind(("0.0.0.0", 0))
        self._video_server_port = self._video_sock.getsockname()[1]

        self._audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._audio_sock.bind(("0.0.0.0", 0))
        self._audio_server_port = self._audio_sock.getsockname()[1]

        self._server = await asyncio.start_server(
            self._handle_client,
            self._bind_host,
            0,
        )
        self._rtsp_port = self._server.sockets[0].getsockname()[1]
        self._running = True

        # Persistent feed tasks — run for the lifetime of the server
        self._feed_tasks = [
            asyncio.create_task(self._video_feed_loop()),
            asyncio.create_task(self._audio_feed_loop()),
        ]

        _LOGGER.info("RTSP server started: %s", self.rtsp_url)
        return self.rtsp_url

    async def stop(self) -> None:
        """Stop the server, feed tasks, and all client connections."""
        self._running = False
        self._active_clients.clear()

        for task in self._feed_tasks:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait([task], timeout=2.0)
        self._feed_tasks.clear()

        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

        for sock_attr in ("_video_sock", "_audio_sock"):
            sock = getattr(self, sock_attr)
            if sock:
                sock.close()
                setattr(self, sock_attr, None)

        _LOGGER.debug("RTSP server stopped")

    def reset(self, renewal: bool = False) -> None:
        """Reset for a new or renewed video call session.

        Always drains stale media from existing queues (drain not replace, so
        RtpReceiver keeps pushing to the same queue objects the feed loops read).

        renewal=False (new call): also resets RTP counters to 0 — go2rtc may
            have reconnected and expects a fresh stream.
        renewal=True (re-establishment): preserves RTP seq/ts so they increment
            monotonically — go2rtc stays connected and won't see a backwards
            timestamp jump ("Timestamp discontinuity").
        """
        drained_nal = 0
        while not self.nal_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.nal_queue.get_nowait()
                drained_nal += 1
        drained_audio = 0
        while not self.audio_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.audio_queue.get_nowait()
                drained_audio += 1
        if not renewal:
            self._video_seq = 0
            self._audio_seq = 0
            self._video_ts = 0
            self._audio_ts = 0
        _LOGGER.debug(
            "RTSP server reset (renewal=%s): drained %d NALs + %d audio, "
            "%d client(s) remain",
            renewal, drained_nal, drained_audio, len(self._active_clients),
        )

    # ------------------------------------------------------------------
    # RTSP request handling
    # ------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one RTSP client connection."""
        peer = writer.get_extra_info("peername")
        client_host = peer[0] if peer else "unknown"
        _LOGGER.debug("RTSP client connected from %s", client_host)

        # Per-client state — independent of every other connection
        client = _TcpClient(writer=writer)
        registered = False

        try:
            while self._running:
                raw = b""
                while b"\r\n\r\n" not in raw:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=30.0)
                    if not chunk:
                        return
                    raw += chunk

                request = raw.decode("utf-8", errors="replace")
                lines = [ln for ln in request.split("\r\n") if ln]
                if not lines:
                    break

                parts = lines[0].split()
                if len(parts) < 2:
                    break
                method, url = parts[0], parts[1]

                headers: dict[str, str] = {}
                for line in lines[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()

                cseq = headers.get("cseq", "1")
                _LOGGER.debug("RTSP %s from %s", method, client_host)

                if method == "OPTIONS":
                    self._send(writer, cseq, extra=(
                        "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN\r\n"
                    ))

                elif method == "DESCRIBE":
                    sdp = self._build_sdp().encode()
                    writer.write(
                        f"RTSP/1.0 200 OK\r\n"
                        f"CSeq: {cseq}\r\n"
                        f"Content-Type: application/sdp\r\n"
                        f"Content-Length: {len(sdp)}\r\n"
                        f"\r\n".encode() + sdp
                    )
                    await writer.drain()

                elif method == "SETUP":
                    transport_hdr = headers.get("transport", "")
                    is_audio = "/audio" in url or "track2" in url
                    transport_resp = self._parse_setup(
                        transport_hdr, is_audio, client, client_host
                    )
                    self._send(writer, cseq, extra=(
                        f"Session: {self._session_id}\r\n"
                        f"{transport_resp}\r\n"
                    ))

                elif method == "PLAY":
                    self._send(writer, cseq, extra=(
                        f"Session: {self._session_id}\r\n"
                        f"Range: npt=0.000-\r\n"
                    ))
                    self._active_clients.append(client)
                    registered = True
                    _LOGGER.info(
                        "RTSP streaming → %s (video_ch=%s audio_ch=%s) "
                        "[%d client(s) total]",
                        client_host, client.video_ch, client.audio_ch,
                        len(self._active_clients),
                    )
                    await self._wait_for_teardown(reader)
                    break

                elif method == "TEARDOWN":
                    self._send(writer, cseq, extra=f"Session: {self._session_id}\r\n")
                    break

                else:
                    writer.write(
                        f"RTSP/1.0 405 Method Not Allowed\r\nCSeq: {cseq}\r\n\r\n"
                        .encode()
                    )
                    await writer.drain()

        except (asyncio.TimeoutError, ConnectionError):
            pass
        except Exception:
            _LOGGER.debug("RTSP client error", exc_info=True)
        finally:
            if registered:
                with contextlib.suppress(ValueError):
                    self._active_clients.remove(client)
                _LOGGER.debug(
                    "RTSP client disconnected from %s [%d client(s) remain]",
                    client_host, len(self._active_clients),
                )
            with contextlib.suppress(Exception):
                writer.close()

    def _parse_setup(
        self,
        transport_hdr: str,
        is_audio: bool,
        client: _TcpClient,
        client_host: str,
    ) -> str:
        """Parse SETUP Transport header, update client state, return response."""
        use_tcp = "RTP/AVP/TCP" in transport_hdr or "interleaved" in transport_hdr

        if use_tcp:
            channel = 0
            for part in transport_hdr.split(";"):
                if "interleaved" in part:
                    channel = int(part.split("=", 1)[1].split("-")[0])
            if is_audio:
                client.audio_ch = channel
            else:
                client.video_ch = channel
            return f"Transport: RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}"
        else:
            client_port = self._parse_client_port(transport_hdr)
            if is_audio:
                self._udp_audio_port = client_port
                server_port = self._audio_server_port
            else:
                self._udp_video_port = client_port
                server_port = self._video_server_port
            self._udp_host = client_host
            return (
                f"Transport: RTP/AVP;unicast;"
                f"client_port={client_port}-{client_port + 1};"
                f"server_port={server_port}-{server_port + 1}"
            )

    @staticmethod
    def _send(writer: asyncio.StreamWriter, cseq: str, extra: str = "") -> None:
        writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{extra}\r\n".encode())

    @staticmethod
    def _parse_client_port(transport_hdr: str) -> int:
        for part in transport_hdr.split(";"):
            if "client_port" in part:
                ports = part.split("=", 1)[1].strip()
                return int(ports.split("-")[0])
        return 0

    def _build_sdp(self) -> str:
        return (
            "v=0\r\n"
            f"o=- 0 0 IN IP4 {self._bind_host}\r\n"
            "s=Comelit Intercom\r\n"
            "t=0 0\r\n"
            "m=video 0 RTP/AVP 96\r\n"
            "c=IN IP4 0.0.0.0\r\n"
            "a=rtpmap:96 H264/90000\r\n"
            "a=fmtp:96 packetization-mode=1\r\n"
            "a=control:video\r\n"
            "m=audio 0 RTP/AVP 8\r\n"
            "c=IN IP4 0.0.0.0\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=control:audio\r\n"
        )

    async def _wait_for_teardown(self, reader: asyncio.StreamReader) -> None:
        """Hold client connection open until TEARDOWN or disconnect."""
        while self._running:
            try:
                data = await asyncio.wait_for(reader.read(256), timeout=10.0)
                if not data or b"TEARDOWN" in data:
                    break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # RTP broadcast
    # ------------------------------------------------------------------

    def _broadcast_rtp(self, pkt: bytes, is_video: bool) -> None:
        """Send one RTP packet to every registered TCP client + UDP client."""
        for c in list(self._active_clients):
            ch = c.video_ch if is_video else c.audio_ch
            if ch is not None and not c.writer.is_closing():
                try:
                    c.writer.write(struct.pack("!BBH", 0x24, ch, len(pkt)) + pkt)
                except Exception:
                    pass

        # UDP fallback — single client (last SETUP wins)
        if self._udp_host:
            port = self._udp_video_port if is_video else self._udp_audio_port
            sock = self._video_sock if is_video else self._audio_sock
            if port and sock:
                with contextlib.suppress(OSError):
                    sock.sendto(pkt, (self._udp_host, port))

    # ------------------------------------------------------------------
    # RTP feed loops — run from start() to stop()
    # ------------------------------------------------------------------

    async def _video_feed_loop(self) -> None:
        """Broadcast H.264 NALs to all registered clients."""
        ts_increment = 5625  # ~16fps → 90000/16
        try:
            while self._running:
                try:
                    nal = await asyncio.wait_for(self.nal_queue.get(), timeout=2.0)
                except TimeoutError:
                    continue

                if nal[:4] == b"\x00\x00\x00\x01":
                    nal_data = nal[4:]
                elif nal[:3] == b"\x00\x00\x01":
                    nal_data = nal[3:]
                else:
                    nal_data = nal

                if not nal_data:
                    continue

                self._send_h264(nal_data)
                self._video_ts = (self._video_ts + ts_increment) & 0xFFFFFFFF

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("Video feed loop error", exc_info=True)

    def _send_h264(self, nal_data: bytes) -> None:
        """Packetize one H.264 NAL unit and broadcast to all clients."""
        if len(nal_data) <= _MAX_RTP_PAYLOAD:
            pkt = _build_rtp(
                pt=96, seq=self._video_seq, ts=self._video_ts,
                ssrc=self._video_ssrc, payload=nal_data, marker=True,
            )
            self._video_seq = (self._video_seq + 1) & 0xFFFF
            self._broadcast_rtp(pkt, is_video=True)
        else:
            # FU-A fragmentation (RFC 6184 §5.8)
            nal_header = nal_data[0]
            nal_type = nal_header & 0x1F
            nal_ref = nal_header & 0xE0
            fu_indicator = nal_ref | 28

            payload = nal_data[1:]
            offset = 0
            first = True
            while offset < len(payload):
                chunk = payload[offset: offset + _MAX_RTP_PAYLOAD - 2]
                offset += len(chunk)
                last = offset >= len(payload)

                fu_header = (0x80 if first else 0x00) | (0x40 if last else 0x00) | nal_type
                fragment = struct.pack("BB", fu_indicator, fu_header) + chunk

                pkt = _build_rtp(
                    pt=96, seq=self._video_seq, ts=self._video_ts,
                    ssrc=self._video_ssrc, payload=fragment, marker=last,
                )
                self._video_seq = (self._video_seq + 1) & 0xFFFF
                self._broadcast_rtp(pkt, is_video=True)
                first = False

    async def _audio_feed_loop(self) -> None:
        """Broadcast G.711 PCMA to all registered clients.

        When no real audio is queued, sends silence every ~1s to keep
        go2rtc and the stream worker alive between calls.
        """
        try:
            while self._running:
                try:
                    payload = await asyncio.wait_for(
                        self.audio_queue.get(), timeout=1.0
                    )
                except TimeoutError:
                    payload = _PCMA_SILENCE

                pkt = _build_rtp(
                    pt=8, seq=self._audio_seq, ts=self._audio_ts,
                    ssrc=self._audio_ssrc, payload=payload, marker=False,
                )
                self._audio_seq = (self._audio_seq + 1) & 0xFFFF
                self._audio_ts = (self._audio_ts + len(payload)) & 0xFFFFFFFF
                self._broadcast_rtp(pkt, is_video=False)

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("Audio feed loop error", exc_info=True)


def _build_rtp(
    pt: int,
    seq: int,
    ts: int,
    ssrc: int,
    payload: bytes,
    marker: bool,
) -> bytes:
    """Build a minimal 12-byte RTP header + payload."""
    first_byte = 0x80  # version=2, no padding, no extension, CC=0
    second_byte = (0x80 if marker else 0x00) | (pt & 0x7F)
    return struct.pack("!BBHII", first_byte, second_byte, seq, ts, ssrc) + payload
