"""Video call signaling via TCP to trigger UDP video streaming."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import time

from .auth import authenticate
from .channels import Channel, ChannelType
from .client import IconaBridgeClient
from .exceptions import VideoCallError
from .models import DeviceConfig
from .protocol import (
    encode_call_ack,
    encode_call_init,
    encode_call_response_ack,
    encode_ctpp_init,
    encode_rtpc_link,
    encode_video_config,
)
from .rtp_receiver import RtpReceiver

_LOGGER = logging.getLogger(__name__)

VIDEO_RESPONSE_TIMEOUT = 5.0  # device can be slow to respond to CTPP signaling
VIDEO_SESSION_TIMEOUT = 120.0

# CTPP message counter increment constants (from PCAP analysis)
# Bytes [4-5] in CTPP body encode two independent sub-counters:
#   byte[4] increments by 1 → adds 0x00010000 to the LE32 timestamp field
#   byte[5] increments by 1 → adds 0x01000000 to the LE32 timestamp field
_CTR_INCR_BYTE4 = 0x00010000   # only byte[4] increments
_CTR_INCR_BYTE5 = 0x01000000   # only byte[5] increments
_CTR_INCR_BOTH  = 0x01010000   # both byte[4] and byte[5] increment


class VideoCallSession:
    """Manages the TCP signaling and UDP video for a video call.

    Uses a fresh TCP connection (like door.py) to avoid corrupting
    the persistent connection. The sequence (from PCAP analysis):

    1. Connect + authenticate
    2. Open CTPP channel with apt address
    3. Send CTPP init + call initiation
    4. ACK device responses, wait for call acceptance
    5. Open UDPM channel (trailing_byte=1) — extract token from response
    6. Send codec negotiation
    7. Open 2x RTPC channels (trailing_byte=1)
    8. Send RTPC link (using RTPC1 request_id)
    9. Send video config trigger (using RTPC2 request_id)
    10. Start RTP receiver with dynamic IDs from channel setup
    11. Auto-timeout after ~120s
    """

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        config: DeviceConfig,
        auto_timeout: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._config = config
        self._auto_timeout = auto_timeout
        self._client: IconaBridgeClient | None = None
        self._rtp_receiver: RtpReceiver | None = None
        self._timeout_task: asyncio.Task | None = None
        self._tcp_task: asyncio.Task | None = None
        self._ctpp_task: asyncio.Task | None = None
        self._active = False

    @property
    def active(self) -> bool:
        """Return True if the video session is currently active."""
        return self._active

    @property
    def rtp_receiver(self) -> RtpReceiver | None:
        """Return the RTP receiver for getting video frames."""
        return self._rtp_receiver

    def _ts(self) -> int:
        """Return current timestamp for CTPP messages."""
        return int(time.time()) & 0xFFFFFFFF

    async def start(self) -> RtpReceiver:
        """Execute the full TCP signaling sequence and start UDP receiver.

        The signaling flow matches the real Android app (from PCAP analysis):
        1. Auth → open CTPP + CSPB → CTPP init → ACK device responses
        2. Call init → open UDPM → START UDP CONTROL
        3. Wait for call ACK → codec ack → codec exchange (with UDP running)
        4. Open 2x RTPC → RTPC link → video config → device RTPC → start media
        """
        client = IconaBridgeClient(self._host, self._port)
        self._client = client

        try:
            await client.connect()
            await authenticate(client, self._token)

            apt_addr = self._config.apt_address
            apt_sub = self._config.apt_subaddress
            # our_addr = full address of the HA/app unit (apt_address + apt_subaddress)
            # This appears as the FIRST address in all CTPP video messages (PCAP-verified).
            our_addr = f"{apt_addr}{apt_sub}"
            # entrance_addr = the entrance panel address from entrance-address-book.
            # This appears as the SECOND address in call-phase messages (PCAP-verified).
            # For init-phase ACKs, we use apt_addr (without sub) as second address.
            entrance_addr = self._config.caller_address or our_addr
            if not self._config.caller_address:
                _LOGGER.warning(
                    "entrance-address-book is empty — using our_addr as entrance_addr. "
                    "Video call may fail if device requires a distinct entrance address."
                )

            # Step 1: Open CTPP + CSPB channels (PCAP shows both are needed)
            # CRITICAL: Use ChannelType.UAUT (type=7) for ALL channels — the real
            # Android app uses type=7 for everything. Using CTPP=16 may cause the
            # device to handle video calls incorrectly.
            # CTPP extra_data = our_addr (the apartment/HA unit address)
            extra = our_addr
            ctpp = await client.open_channel(
                "CTPP", ChannelType.UAUT, extra_data=extra
            )
            await client.open_channel("CSPB", ChannelType.UAUT)

            # Step 2: CTPP init — device responds with 0x1800 then 0x1860
            # The "timestamp" (bytes 2-5) has session (bytes 2-3) + counter
            # (bytes 4-5). Session must stay consistent, counter must change
            # between messages. PCAP shows counter increments by ~0x10000
            # (byte[4] goes up by 1) between init and ACK.
            init_ts = self._ts()
            init_payload = encode_ctpp_init(apt_addr, apt_sub, init_ts)
            await client.send_binary(ctpp, init_payload)

            # Read exactly the 0x1800 + 0x1860 pair, then ACK immediately
            for _ in range(2):
                resp = await client.read_response(ctpp, timeout=VIDEO_RESPONSE_TIMEOUT)
                if resp and len(resp) >= 2:
                    msg_type = struct.unpack_from("<H", resp, 0)[0]
                    _LOGGER.debug(
                        "CTPP init response: %d bytes, type=0x%04X",
                        len(resp), msg_type,
                    )
            # ACK counter: PCAP shows BOTH byte[4] AND byte[5] increment by 1
            # vs the init timestamp → diff = +0x01010000, NOT +0x00010000.
            # With wrong increment the device ignores our ACKs and retransmits
            # 0x1860 continuously, blocking the codec exchange.
            # PCAP-verified: init-phase ACKs use (our_addr, apt_addr) —
            # first=full apt address, second=apt WITHOUT subaddress.
            ack_ts = init_ts + _CTR_INCR_BOTH
            ack = encode_call_response_ack(our_addr, apt_addr, ack_ts)
            await client.send_binary(ctpp, ack)
            ack2 = encode_call_response_ack(
                our_addr, apt_addr, ack_ts, prefix=0x1820
            )
            await client.send_binary(ctpp, ack2)

            # PCAP shows phone proceeds directly to call init after sending ACKs.

            # Register placeholder for device's RTPC channel early — the device
            # opens its own RTPC DURING codec exchange (~4.8s after connect),
            # before we even open our RTPC1/RTPC2 channels. Must be registered
            # here so _dispatch captures it immediately. With the request_id==0
            # filter in _dispatch, this placeholder will NOT steal RTPC1/RTPC2
            # open responses (those go through the request_id!=0 path).
            device_rtpc = client.register_placeholder_channel("RTPC_DEVICE")

            # Step 3: Send call init — uses a new "session" timestamp
            # (PCAP shows call phase uses different session from init phase)
            # PCAP-verified: call_init uses (our_addr, entrance_addr).
            #
            # CRITICAL: The device uses bytes[2-3] of the CTPP body as a
            # "session ID". Init and call phases MUST have different session IDs
            # (different low 16 bits of the timestamp). Since both init_ts and
            # call_ts are generated from int(time.time()) within the same second,
            # they'll be identical. We add 1 to the low byte to force a different
            # session ID while keeping the same counter starting point (high 16 bits).
            call_ts = (init_ts + 1) & 0xFFFFFFFF
            call_init = encode_call_init(our_addr, entrance_addr, call_ts)
            await client.send_binary(ctpp, call_init)

            # Step 4: Open UDPM immediately after call init (PCAP order)
            udpm = await client.open_channel(
                "UDPM", ChannelType.UAUT, trailing_byte=1
            )
            udpm_token = 0x0000
            if len(udpm.open_response_body) >= 18:
                udpm_token = struct.unpack_from("<H", udpm.open_response_body, 16)[0]
                _LOGGER.debug("UDPM token: 0x%04X", udpm_token)

            # PCAP-verified: control_req_id = UDPM server_channel_id (device-assigned).
            control_req_id = udpm.server_channel_id
            receiver = RtpReceiver(
                self._host, self._port,
                control_req_id=control_req_id,
                media_req_id=0,  # set later after RTPC2 opens
                udpm_token=udpm_token,
            )
            # Open UDP socket + send 2 discovery packets so the device knows
            # our UDP port before video config. Start keepalive immediately so
            # the device doesn't time out during the codec exchange / RTPC setup
            # (which can take 10+ seconds). The PCAP shows keepalives sent
            # throughout the entire session, not just after video starts.
            await receiver.start_control()
            receiver.start_keepalive()
            self._rtp_receiver = receiver

            # Step 5: Wait for device ACK of call init, then send codec msg
            # CRITICAL: Each side maintains its OWN counter independently.
            # PCAP shows client uses call_ts-based counter that increments
            # by 0x10000 per message sent, while device has a completely
            # different counter. We must NEVER adopt the device's counter.
            call_counter = call_ts

            resp1 = await client.read_response(ctpp, timeout=VIDEO_RESPONSE_TIMEOUT)
            if resp1 and len(resp1) >= 6:
                dev_counter = struct.unpack_from("<I", resp1, 2)[0]
                _LOGGER.debug(
                    "Call response: %d bytes, dev_counter=0x%08X, "
                    "our_counter=0x%08X",
                    len(resp1), dev_counter, call_counter,
                )

            # Send codec msg with our own incremented counter.
            # PCAP-verified: only +0x00010000 between call_init and codec
            # (byte[4] increments by 1, byte[5] stays).
            call_counter += _CTR_INCR_BYTE4
            codec_ack = encode_call_ack(our_addr, entrance_addr, call_counter)
            await client.send_binary(ctpp, codec_ack)

            # Step 6: Handle codec exchange — device sends multiple responses.
            # PCAP-verified counter increments:
            #   device 0x0008 (codec) → ACK: +0x01010000 (both byte[4] and byte[5] +1)
            #   device 0x0002 (call accepted) → ACK: +0x01000000 (only byte[5] +1)
            for i in range(10):
                resp = await client.read_response(ctpp, timeout=VIDEO_RESPONSE_TIMEOUT)
                if not resp or len(resp) < 2:
                    break
                msg_type = struct.unpack_from("<H", resp, 0)[0]
                dev_counter = 0
                if len(resp) >= 6:
                    dev_counter = struct.unpack_from("<I", resp, 2)[0]
                action = struct.unpack_from(">H", resp, 6)[0] if len(resp) >= 8 else 0
                _LOGGER.debug(
                    "Codec exchange %d: %d bytes, type=0x%04X action=0x%04X "
                    "dev_counter=0x%08X our_counter=0x%08X",
                    i, len(resp), msg_type, action,
                    dev_counter, call_counter,
                )
                # Skip 0x1860 init retransmits
                if msg_type == 0x1860:
                    _LOGGER.debug("Ignoring init retransmit (0x1860)")
                    continue
                # Skip 0x1800 device ACKs (don't adopt their counter)
                if msg_type == 0x1800:
                    continue
                # Handle 0x1840 data messages
                if msg_type == 0x1840:
                    if action == 0x0008:
                        # Device sent its codec — ACK with bare 0x1800.
                        # PCAP: +0x01010000 (both byte[4] and byte[5] increment by 1).
                        call_counter += _CTR_INCR_BOTH
                        ack = encode_call_response_ack(
                            our_addr, entrance_addr, call_counter
                        )
                        await client.send_binary(ctpp, ack)
                        _LOGGER.debug(
                            "ACKed device codec (0x0008) with 0x1800, "
                            "our_counter=0x%08X", call_counter,
                        )
                    elif action == 0x0002:
                        # "Call accepted" — ACK and exit codec exchange.
                        # PCAP: +0x01000000 (only byte[5] increments by 1).
                        call_counter += _CTR_INCR_BYTE5
                        ack = encode_call_response_ack(
                            our_addr, entrance_addr, call_counter
                        )
                        await client.send_binary(ctpp, ack)
                        _LOGGER.debug("Got call accepted, codec exchange complete")
                        break
                    else:
                        # Other 0x1840 — bare ACK
                        call_counter += _CTR_INCR_BYTE4
                        ack = encode_call_response_ack(
                            our_addr, entrance_addr, call_counter
                        )
                        await client.send_binary(ctpp, ack)

            # Step 7: Open 2 RTPC channels (PCAP shows phone opens both)
            # RTPC1 is used for the link message, RTPC2 for video media
            rtpc1 = await client.open_channel(
                "RTPC", ChannelType.UAUT, trailing_byte=1
            )
            rtpc2 = await client.open_channel(
                "RTPC2", ChannelType.UAUT, trailing_byte=1,
                wire_name="RTPC",
            )
            # PCAP-verified: media_req_id = RTPC2 server_channel_id (device-assigned).
            # In PCAP: RTPC2 server_channel_id=0x606E (= UDPM server_channel_id + 2).
            media_req_id = rtpc2.server_channel_id
            _LOGGER.debug(
                "RTPC channels: rtpc1=0x%04X, rtpc2(media)=0x%04X",
                rtpc1.request_id, media_req_id,
            )

            # Step 8: Send RTPC link (references RTPC1)
            # PCAP shows RTPC link reuses the last counter (no increment).
            # Must use server_channel_id (device-assigned), not local request_id.
            rtpc_link = encode_rtpc_link(
                our_addr, entrance_addr, rtpc1.server_channel_id, call_counter
            )
            await client.send_binary(ctpp, rtpc_link)
            _LOGGER.debug(
                "Sent RTPC link, our_counter=0x%08X", call_counter
            )

            # Step 8b: Send video config IMMEDIATELY after RTPC link — BEFORE waiting
            # for device RTPC. PCAP shows the Android app sends VIDEO_CONFIG as message
            # #17 while device opens its own RTPC at #18. If we wait for device RTPC
            # first, the device doesn't enter the correct state for HANGUP/ZERO recovery.
            # PCAP: call_counter +0x00010000 (byte[4] +1) for video config DATA message.
            call_counter += _CTR_INCR_BYTE4
            vid_config = encode_video_config(
                our_addr, entrance_addr, media_req_id, call_counter
            )
            await client.send_binary(ctpp, vid_config)
            _LOGGER.debug(
                "Sent video config (before device RTPC), our_counter=0x%08X", call_counter
            )

            # Step 9: Now wait for device to open its own RTPC channel, then ACK
            # its CTPP RTPC link message.
            # PCAP sequence after phone's RTPC link + video config:
            #   device CHAN_OPEN (RTPC) → auto-handled by dispatcher
            #   device CTPP 0x1840/0x000A (device's RTPC link)
            #   phone ACK 0x1800 → call_counter +0x01000000 (only byte[5] +1)
            #   device ACK 0x1800

            # Wait for device to open its own RTPC channel
            try:
                await asyncio.wait_for(
                    device_rtpc.open_event.wait(), timeout=VIDEO_RESPONSE_TIMEOUT
                )
                _LOGGER.debug(
                    "Device opened RTPC: 0x%04X", device_rtpc.server_channel_id
                )
            except TimeoutError:
                _LOGGER.warning("Device RTPC channel not received within timeout")
                raise VideoCallError("Device RTPC channel not received")

            # Read and ACK device's CTPP RTPC link (0x1840/0x000A)
            for _ in range(5):
                resp_dev_link = await client.read_response(
                    ctpp, timeout=VIDEO_RESPONSE_TIMEOUT
                )
                if not resp_dev_link or len(resp_dev_link) < 2:
                    break
                msg_type = struct.unpack_from("<H", resp_dev_link, 0)[0]
                action = (
                    struct.unpack_from(">H", resp_dev_link, 6)[0]
                    if len(resp_dev_link) >= 8
                    else 0
                )
                _LOGGER.debug(
                    "Post-video-config: %d bytes type=0x%04X action=0x%04X",
                    len(resp_dev_link), msg_type, action,
                )
                if msg_type == 0x1840 and action == 0x000A:
                    # Device's RTPC link — ACK with +0x01000000
                    call_counter += _CTR_INCR_BYTE5
                    ack = encode_call_response_ack(
                        our_addr, entrance_addr, call_counter
                    )
                    await client.send_binary(ctpp, ack)
                    _LOGGER.debug(
                        "ACKed device RTPC link, our_counter=0x%08X", call_counter
                    )
                    break
                if msg_type == 0x1800:
                    continue  # Skip device ACKs

            # Step 10: Set media req_id and start decoder.
            # (keepalive already started after start_control)
            receiver.set_media_req_id(media_req_id)
            await receiver.start_media()

            # Step 10b: Start TCP video reader for RTPC2.
            # The device sends RTP over TCP (on RTPC2) instead of UDP in some
            # firmware versions. TCP bodies have the ICONA header already stripped
            # by the client, so raw RTP data goes directly to receiver.
            self._tcp_task = asyncio.create_task(
                self._tcp_video_loop(client, rtpc2, receiver)
            )

            # Step 10c: Start CTPP monitor loop.
            # The device sends periodic 0x1840 keepalive/status messages on the
            # CTPP channel during the call. If we don't ACK them, it drops the
            # session after ~30 seconds. This loop reads and ACKs those messages,
            # including performing re-establishment when CALL_END (0x0003) arrives.
            self._ctpp_task = asyncio.create_task(
                self._ctpp_monitor_loop(
                    client, ctpp, our_addr, entrance_addr, call_counter,
                )
            )
            self._active = True

            _LOGGER.debug(
                "RTP receiver fully started: control=0x%04X, "
                "media=0x%04X, udpm_token=0x%04X",
                control_req_id, media_req_id, udpm_token,
            )

            # Step 11: Auto-timeout (skipped when stream handles lifecycle)
            if self._auto_timeout:
                self._timeout_task = asyncio.create_task(self._auto_timeout_loop())

            _LOGGER.info(
                "Video call session started: our_addr=%s entrance=%s",
                our_addr, entrance_addr,
            )
            return receiver

        except Exception as e:
            await self._cleanup()
            raise VideoCallError(
                f"Failed to start video call: {e}"
            ) from e

    async def stop(self) -> None:
        """Stop the video session and clean up."""
        _LOGGER.info("Stopping video call session")
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Clean up all resources.

        Tasks are cancelled with a 2s timeout on each await. Without the
        timeout, awaiting a cancelled task stuck on a dead TCP connection can
        freeze the event loop for 30-40s (observed on Python 3.14/aarch64).
        """
        self._active = False

        for task_attr in ("_timeout_task", "_tcp_task", "_ctpp_task"):
            task = getattr(self, task_attr)
            setattr(self, task_attr, None)
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait([task], timeout=2.0)

        receiver, self._rtp_receiver = self._rtp_receiver, None
        if receiver:
            with contextlib.suppress(Exception):
                await receiver.stop()

        client, self._client = self._client, None
        if client:
            with contextlib.suppress(Exception):
                await client.disconnect()

    @staticmethod
    async def _tcp_video_loop(
        client: IconaBridgeClient,
        rtpc2: Channel,
        receiver: RtpReceiver,
    ) -> None:
        """Read TCP RTP packets from RTPC2 and feed to receiver.

        The device sends RTP directly over TCP on the RTPC2 channel.
        The client strips the ICONA header before queuing, so the queued
        body is raw RTP starting with 0x80 (RTP version 2).
        """
        try:
            while receiver.running:
                data = await client.read_response(rtpc2, timeout=2.0)
                if data and len(data) >= 12:
                    receiver.receive_tcp_rtp(data)
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("TCP video loop error", exc_info=True)

    async def _ctpp_monitor_loop(
        self,
        client: IconaBridgeClient,
        ctpp: "Channel",
        our_addr: str,
        entrance_addr: str,
        call_counter: int,
    ) -> None:
        """Read and ACK incoming CTPP messages during the active video session.

        The device sends periodic 0x1840 messages throughout the call:
        - 0x0000: keepalive — ACK with bare 0x1800
        - 0x0003: CALL_END — device lease timer expired; ACK and stop session
            so camera.py can restart a fresh session immediately.
        0x1800 device ACKs are silently ignored.
        """
        try:
            while self._active:
                resp = await client.read_response(ctpp, timeout=2.0)
                if not resp or len(resp) < 2:
                    continue
                msg_type = struct.unpack_from("<H", resp, 0)[0]
                action = (
                    struct.unpack_from(">H", resp, 6)[0]
                    if len(resp) >= 8 else 0
                )
                if msg_type == 0x1840:
                    if action == 0x0003:
                        # CALL_END: the device has terminated its lease (~30s timer).
                        # PCAP analysis confirms there is no in-session renewal —
                        # the app always starts a completely new session after CALL_END.
                        # ACK it and stop this session; camera.py will restart cleanly.
                        call_counter += _CTR_INCR_BYTE5
                        ack = encode_call_response_ack(our_addr, entrance_addr, call_counter)
                        await client.send_binary(ctpp, ack)
                        _LOGGER.info(
                            "CTPP monitor: CALL_END received — stopping session for restart"
                        )
                        self._active = False
                        return
                    else:
                        # Keepalive (0x0000) or other 0x1840 — bare ACK
                        call_counter += _CTR_INCR_BYTE4
                        ack = encode_call_response_ack(our_addr, entrance_addr, call_counter)
                        await client.send_binary(ctpp, ack)
                        _LOGGER.debug(
                            "CTPP monitor: ACKed 0x1840/0x%04X, counter=0x%08X",
                            action, call_counter,
                        )
                elif msg_type == 0x1800:
                    pass  # device ACK — no response needed
                else:
                    _LOGGER.debug(
                        "CTPP monitor: unexpected type=0x%04X (%d bytes)",
                        msg_type, len(resp),
                    )
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("CTPP monitor loop error", exc_info=True)

    async def _auto_timeout_loop(self) -> None:
        """Automatically stop the session after VIDEO_SESSION_TIMEOUT."""
        try:
            await asyncio.sleep(VIDEO_SESSION_TIMEOUT)
            _LOGGER.info("Video session timed out after %ds", VIDEO_SESSION_TIMEOUT)
            await self._cleanup()
        except asyncio.CancelledError:
            pass
