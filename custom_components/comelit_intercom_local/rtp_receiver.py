"""UDP video receiver — strips ICONA headers, decodes H.264 via PyAV."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import struct

from .protocol import HEADER_SIZE, ICONA_BRIDGE_PORT

_LOGGER = logging.getLogger(__name__)

_MAX_CONSECUTIVE_ERRORS = 5


def _build_control_packet(
    control_req_id: int, udpm_token: int, seq: int
) -> bytes:
    """Build a UDP control/keepalive packet for the video stream.

    From PCAP: [ICONA header with control_req_id] [token LE16] [flag] [seq] [flag] 80
    """
    header = struct.pack("<BBHH2s", 0x00, 0x06, 6, control_req_id, b"\x00\x00")
    body = bytes([
        udpm_token & 0xFF, (udpm_token >> 8) & 0xFF,
        0x00, seq & 0xFF, 0x00, 0x80,
    ])
    return header + body


class _UdpProtocol(asyncio.DatagramProtocol):
    """Async UDP protocol for receiving ICONA-wrapped RTP packets."""

    def __init__(self, receiver: RtpReceiver) -> None:
        self._receiver = receiver

    def connection_made(self, transport) -> None:
        _LOGGER.debug("UDP socket connected: %s", transport.get_extra_info("sockname"))

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        _LOGGER.debug(
            "UDP packet received: %d bytes from %s:%d",
            len(data), addr[0], addr[1],
        )
        self._receiver._on_udp_packet(data)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.error("UDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            _LOGGER.debug("UDP connection lost: %s", exc)


class RtpReceiver:
    """Receives ICONA-wrapped UDP video, decodes H.264 directly via PyAV.

    Flow:
    1. UDP socket connected to device — sends keepalives, receives video
    2. Media packets (matched by media_req_id) get ICONA header + trailer stripped
    3. RTP packets parsed, H.264 NAL units extracted (FU-A reassembled)
    4. Raw H.264 stream fed to PyAV decoder
    5. Decoded frames converted to JPEG
    """

    def __init__(
        self,
        host: str,
        port: int = ICONA_BRIDGE_PORT,
        control_req_id: int = 0,
        media_req_id: int = 0,
        udpm_token: int = 0,
    ) -> None:
        self._host = host
        self._port = port
        self._control_req_id = control_req_id
        self._media_req_id = media_req_id
        self._udpm_token = udpm_token

        # UDP transport to device
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _UdpProtocol | None = None

        # H.264 NAL reassembly
        self._current_fua_nal: bytearray = bytearray()

        # PyAV decoder (lazy-initialized on first NAL)
        self._codec_context = None
        self._decode_task: asyncio.Task | None = None
        self._nal_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)

        # Latest decoded JPEG frame
        self._latest_frame: bytes | None = None
        self._frame_event = asyncio.Event()

        self._running = False
        self._control_seq = 0
        self._media_packet_count = 0
        self._keepalive_task: asyncio.Task | None = None

    async def start_control(self) -> int:
        """Open UDP socket and send 2 discovery packets.

        Call this right after UDPM opens so the device learns our UDP
        port before we send video config. Does NOT start the keepalive
        loop — call start_keepalive() after signaling completes.
        Returns the local port bound.
        """
        loop = asyncio.get_running_loop()
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self),
            remote_addr=(self._host, self._port),
        )
        local_addr = self._transport.get_extra_info("sockname")
        actual_port = local_addr[1] if local_addr else 0
        self._running = True

        self._send_control()
        self._send_control()
        _LOGGER.debug(
            "UDP socket ready: local port %d -> %s:%d "
            "(control=0x%04X, token=0x%04X)",
            actual_port, self._host, self._port,
            self._control_req_id, self._udpm_token,
        )
        return actual_port

    def start_keepalive(self) -> None:
        """Start the continuous keepalive loop (call after video config sent)."""
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        _LOGGER.debug("UDP keepalive loop started")

    def set_media_req_id(self, media_req_id: int) -> None:
        """Set the media request ID once RTPC2 is opened."""
        self._media_req_id = media_req_id
        _LOGGER.debug("Media req_id set to 0x%04X", media_req_id)

    async def start_media(self) -> None:
        """Start the decode task for processing H.264 NAL units.

        Call this after TCP signaling completes and the device starts
        sending video data.
        """
        self._decode_task = asyncio.create_task(self._decode_loop())
        _LOGGER.debug("H.264 decode task started")

    async def start(self) -> int:
        """Start full receiver (control + media). Legacy one-step API."""
        port = await self.start_control()
        await self.start_media()
        return port

    def _send_control(self) -> None:
        """Send a control/keepalive packet to the device."""
        if not self._transport:
            return
        pkt = _build_control_packet(
            self._control_req_id, self._udpm_token, self._control_seq
        )
        self._transport.sendto(pkt)
        _LOGGER.debug("Sent UDP control packet seq=%d", self._control_seq)
        self._control_seq += 1

    async def _keepalive_loop(self) -> None:
        """Send UDP keepalive packets every 1.5s for the session duration.

        PCAP shows the phone sends one control packet every ~1.5 seconds
        throughout the entire call. The device mirrors each packet back.
        """
        try:
            while self._running:
                await asyncio.sleep(1.5)
                self._send_control()
        except asyncio.CancelledError:
            pass

    def receive_tcp_rtp(self, data: bytes) -> None:
        """Process a TCP RTP packet from RTPC2 (ICONA header already stripped).

        The client strips the 8-byte ICONA header before queuing binary data
        on channels, so TCP video arrives as raw RTP starting with 0x80.
        """
        if len(data) < 12:
            return
        self._media_packet_count += 1
        if self._media_packet_count == 1:
            _LOGGER.debug(
                "First TCP media packet received (%d bytes RTP)", len(data)
            )
        self._process_rtp(data)

    def _on_udp_packet(self, data: bytes) -> None:
        """Process a received UDP packet — extract RTP and queue NAL units."""
        if len(data) < HEADER_SIZE + 12:
            return

        req_id = struct.unpack_from("<H", data, 4)[0]

        if req_id == self._media_req_id:
            # Strip 8-byte ICONA header AND Comelit trailer using body_len
            body_len = struct.unpack_from("<H", data, 2)[0]
            raw_rtp = data[HEADER_SIZE:HEADER_SIZE + body_len]

            self._media_packet_count += 1
            if self._media_packet_count == 1:
                _LOGGER.debug(
                    "First UDP media packet received (%d bytes RTP)", len(raw_rtp)
                )

            # Parse RTP header and extract NAL units
            if len(raw_rtp) >= 13:
                self._process_rtp(raw_rtp)

        elif req_id == self._control_req_id:
            _LOGGER.debug("Received UDP control response (%d bytes)", len(data))

    def _process_rtp(self, rtp: bytes) -> None:
        """Parse RTP packet, extract and reassemble H.264 NAL units."""
        byte0 = rtp[0]
        version = (byte0 >> 6) & 0x03
        if version != 2:
            return

        nal_data = rtp[12:]  # Skip 12-byte RTP header
        if not nal_data:
            return

        nal_type = nal_data[0] & 0x1F

        if nal_type in (7, 8):
            # SPS or PPS — single NAL unit, queue with start code
            nal_bytes = b"\x00\x00\x00\x01" + nal_data
            self._queue_nal(nal_bytes)
        elif nal_type == 28:
            # FU-A fragmented NAL unit
            if len(nal_data) < 2:
                return
            fu_indicator = nal_data[0]
            fu_header = nal_data[1]
            start_bit = (fu_header >> 7) & 1
            end_bit = (fu_header >> 6) & 1
            frag_type = fu_header & 0x1F
            nal_ref = fu_indicator & 0xE0

            if start_bit:
                # Start of fragmented NAL — reconstruct NAL header
                reconstructed = bytes([nal_ref | frag_type])
                self._current_fua_nal = bytearray(
                    b"\x00\x00\x00\x01" + reconstructed + nal_data[2:]
                )
            elif self._current_fua_nal:
                # Continuation fragment
                self._current_fua_nal.extend(nal_data[2:])

            if end_bit and self._current_fua_nal:
                self._queue_nal(bytes(self._current_fua_nal))
                self._current_fua_nal = bytearray()
        elif 1 <= nal_type <= 23:
            # Other single NAL unit (IDR=5, non-IDR=1, etc.)
            nal_bytes = b"\x00\x00\x00\x01" + nal_data
            self._queue_nal(nal_bytes)

    def _queue_nal(self, nal_bytes: bytes) -> None:
        """Queue a complete NAL unit for decoding."""
        try:
            self._nal_queue.put_nowait(nal_bytes)
        except asyncio.QueueFull:
            pass  # Drop oldest data if decoder can't keep up

    async def _decode_loop(self) -> None:
        """Background task: decode H.264 NAL units to JPEG frames via PyAV.

        PyAV import and codec creation are offloaded to a thread pool because
        loading the ffmpeg C library can block the event loop for 30-60s on
        aarch64/Python 3.14 (observed in production).
        """
        loop = asyncio.get_running_loop()

        def _init_codec():
            """Import PyAV and create H.264 codec context (runs in thread)."""
            import av  # noqa: PLC0415
            return av, av.CodecContext.create("h264", "r")

        try:
            av, codec = await loop.run_in_executor(None, _init_codec)
        except ImportError:
            _LOGGER.error(
                "PyAV (av) not installed — cannot decode video. "
                "Install with: pip install av"
            )
            return

        h264_buffer = bytearray()
        frame_count = 0
        consecutive_errors = 0

        verbose = _LOGGER.isEnabledFor(logging.DEBUG)

        def _decode_buffer_sync(buf: bytes) -> list[tuple[int, int, bytes]]:
            """Parse + decode + JPEG-encode a buffer. Runs in thread pool.

            Returns list of (width, height, jpeg_bytes) tuples — one per
            decoded frame. Runs blocking C calls off the event loop so the
            asyncio slow-task detector is not triggered.
            """
            import time as _time  # noqa: PLC0415
            results = []
            t0 = _time.monotonic() if verbose else 0.0
            packets = codec.parse(buf)
            t1 = _time.monotonic() if verbose else 0.0
            for packet in packets:
                for frame in codec.decode(packet):
                    t2 = _time.monotonic() if verbose else 0.0
                    jpeg = RtpReceiver._frame_to_jpeg(frame)
                    if jpeg:
                        results.append((frame.width, frame.height, jpeg))
                        if verbose:
                            _LOGGER.debug(
                                "Decode timing: parse=%.3fs decode=%.3fs "
                                "jpeg=%.3fs size=%d",
                                t1 - t0,
                                t2 - t1,
                                _time.monotonic() - t2,
                                len(jpeg),
                            )
            return results

        try:
            while self._running:
                try:
                    nal = await asyncio.wait_for(
                        self._nal_queue.get(), timeout=2.0
                    )
                except TimeoutError:
                    if verbose and frame_count == 0:
                        _LOGGER.debug(
                            "Decode loop: no NALs yet (media_packets=%d)",
                            self._media_packet_count,
                        )
                    continue

                h264_buffer.extend(nal)

                if len(h264_buffer) > 0:
                    buf_snapshot = bytes(h264_buffer)
                    h264_buffer.clear()
                    try:
                        decoded = await loop.run_in_executor(
                            None, _decode_buffer_sync, buf_snapshot
                        )
                        for w, h, jpeg_data in decoded:
                            frame_count += 1
                            self._latest_frame = jpeg_data
                            self._frame_event.set()
                            if verbose and (frame_count <= 5 or frame_count % 50 == 0):
                                _LOGGER.debug(
                                    "Frame %d: %dx%d (%d bytes JPEG), queue=%d",
                                    frame_count, w, h, len(jpeg_data),
                                    self._nal_queue.qsize(),
                                )
                        consecutive_errors = 0
                    except av.error.InvalidDataError:
                        _LOGGER.debug("Invalid H.264 data, skipping")
                        consecutive_errors = 0
                    except Exception:
                        _LOGGER.debug("Decode error", exc_info=True)
                        consecutive_errors += 1
                        if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                            _LOGGER.error(
                                "Decode loop stopping after %d consecutive errors",
                                consecutive_errors,
                            )
                            break

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("Decode loop error", exc_info=True)

        _LOGGER.debug(
            "Decode loop ended: %d frames decoded, %d media packets received",
            frame_count, self._media_packet_count,
        )


    @staticmethod
    def _frame_to_jpeg(frame) -> bytes | None:
        """Convert a PyAV VideoFrame to JPEG bytes via Pillow.

        Uses frame.to_image() (Pillow) instead of creating a new ffmpeg
        MJPEG encoder context per frame. On aarch64, codec context creation
        takes 30-60s, which meant only one frame could be decoded before
        the device's 30s CALL_END timer killed the session.
        """
        try:
            output = io.BytesIO()
            image = frame.to_image()  # Returns PIL.Image (RGB)
            image.save(output, format="JPEG", quality=80)
            return output.getvalue() if output.tell() > 0 else None
        except Exception:
            _LOGGER.debug("JPEG encode error", exc_info=True)
            return None

    async def stop(self) -> None:
        """Stop receiving and clean up.

        Cancelled tasks are awaited with a 2s timeout to allow orderly
        shutdown without risking a 30-40s hang if a task is stuck in C
        code (PyAV decode) or on a dead socket.
        """
        self._running = False

        for task_attr in ("_keepalive_task", "_decode_task"):
            task = getattr(self, task_attr)
            setattr(self, task_attr, None)
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait([task], timeout=2.0)

        if self._transport:
            self._transport.close()
            self._transport = None
        self._protocol = None

        self._latest_frame = None
        _LOGGER.debug(
            "RTP receiver stopped (received %d media packets)",
            self._media_packet_count,
        )

    async def get_jpeg_frame(self, timeout: float = 5.0) -> bytes | None:
        """Wait for the next new JPEG frame and return it.

        Always waits for the frame event — never returns a cached frame
        immediately. This throttles callers to the device's native fps
        (~16fps) and prevents them from spinning in a tight loop that
        floods the TCP send buffer and causes 10-15s write stalls.

        On timeout, returns the last decoded frame (or None if no frame
        has ever been decoded) so callers always have something to show.
        """
        self._frame_event.clear()
        try:
            await asyncio.wait_for(self._frame_event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        return self._latest_frame

    @property
    def running(self) -> bool:
        """Return True if the receiver is active."""
        return self._running

    @property
    def latest_frame(self) -> bytes | None:
        """Return the most recent JPEG frame without waiting."""
        return self._latest_frame
