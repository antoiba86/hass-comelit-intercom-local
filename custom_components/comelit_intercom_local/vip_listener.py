"""VIP event listener — monitors a persistent CTPP channel for doorbell and call events.

The Comelit app's PUSH channel is one-shot (FCM token registration, then close).
Actual call events (doorbell ring = CALL_FSM_STATUS_CHANGE / IN_ALERTING) arrive as
binary VIP messages on the CTPP channel. This module opens a CTPP channel with the
apartment's VIP address on the persistent TCP connection and watches for incoming events.

Binary CTPP message format:
  [prefix LE16] [timestamp LE32] [action BE16] [flags/param BE16]
  [extra bytes] [0xFFFFFFFF] [caller\0] [callee\0\0]

Known prefixes (from PCAP analysis):
  0x18C0 = call init (client → server)
  0x1800 = ACK / response
  0x1820 = confirm ACK
  0x1840 = event/notification (server → client, during call)
  0x1860 = VIP event (server → client, call setup / FSM change)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import struct
import time

from .channels import ChannelType
from .client import IconaBridgeClient
from .models import DeviceConfig, PushEvent
from .protocol import encode_call_response_ack, encode_ctpp_init

_LOGGER = logging.getLogger(__name__)

# CTPP prefixes sent by the device
PREFIX_ACK = 0x1800
PREFIX_CONFIRM = 0x1820
PREFIX_EVENT = 0x1840
PREFIX_VIP_EVENT = 0x1860
PREFIX_CALL_INIT = 0x18C0

# VIP FSM action codes (carried in 0x1860 messages)
ACTION_IDLE = 0x0000               # Device returned to idle state
ACTION_IN_ALERTING = 0x0001        # Incoming call / doorbell ring
ACTION_CONNECTED = 0x0002          # Call was answered
ACTION_DOOR_OPENED = 0x0003        # Door opened (OUT_INITIATED, confirmed by testing)
ACTION_OUT_ALERTING = 0x0004       # Outgoing call is ringing
ACTION_CLOSED = 0x0005             # Call ended
ACTION_REGISTRATION_RENEWAL = 0x0010  # Device keepalive — must ACK with 0x1800+0x1820

# Minimum message size: prefix(2) + timestamp(4) + action(2) = 8
MIN_MSG_SIZE = 8


def parse_ctpp_message(data: bytes) -> dict | None:
    """Parse a binary CTPP message into its components.

    Returns a dict with prefix, timestamp, action, addresses, etc.
    Returns None if the data is too short or doesn't look like a CTPP message.
    """
    if len(data) < MIN_MSG_SIZE:
        return None

    prefix = struct.unpack_from("<H", data, 0)[0]
    timestamp = struct.unpack_from("<I", data, 2)[0]
    action = struct.unpack_from(">H", data, 6)[0]

    result: dict = {
        "prefix": prefix,
        "timestamp": timestamp,
        "action": action,
        "raw": data,
    }

    # Extract flags if present (messages with flags are >= 10 bytes)
    if len(data) >= 10:
        result["flags"] = struct.unpack_from(">H", data, 8)[0]

    # Extract VIP addresses (null-terminated ASCII strings starting with "SB")
    addresses: list[str] = []
    i = 0
    while i < len(data) - 1:
        if data[i : i + 2] == b"SB":
            end = data.index(0, i) if 0 in data[i:] else len(data)
            addr = data[i:end].decode("ascii", errors="replace")
            addresses.append(addr)
            i = end + 1
        else:
            i += 1
    result["addresses"] = addresses

    return result


class VipEventListener:
    """Listens for VIP events on a persistent CTPP channel.

    Opens a CTPP channel with the apartment's VIP address so the device
    sends call-related binary events (doorbell ring, call end, etc.).
    """

    def __init__(
        self,
        client: IconaBridgeClient,
        config: DeviceConfig,
        callback: Callable[[PushEvent], None],
    ) -> None:
        self._client = client
        self._config = config
        self._callback = callback
        self._task: asyncio.Task | None = None
        self._running = False
        # Timestamp of the last fired event per type — used to deduplicate
        # repeated transmissions (device retransmits call init every ~1-2s).
        self._last_fired: dict[str, float] = {}
        self._dedup_window: float = 10.0  # seconds

    async def start(self) -> None:
        """Open CTPP + CSPB channels, send init, and start the listener task.

        The PCAP shows the app always opens both CTPP and CSPB, then sends a
        CTPP init (0x18C0 prefix) before any call. The init message is the
        "registration" that tells the device to start sending VIP events.
        Without it, the device never pushes events on the channel.
        """
        apt_addr = self._config.apt_address
        apt_sub = self._config.apt_subaddress
        vip_address = f"{apt_addr}{apt_sub}"

        _LOGGER.info(
            "Opening persistent CTPP + CSPB channels for VIP events (address=%s)",
            vip_address,
        )

        # PCAP shows CTPP channels are opened with type=7 (UAUT), not 16 (CTPP).
        # The device uses the channel NAME to determine purpose, not the type ID.
        self._channel = await self._client.open_channel(
            "CTPP_VIP",
            ChannelType.UAUT,
            extra_data=vip_address,
            wire_name="CTPP",
        )

        # CSPB channel — always opened alongside CTPP in the app (PCAP-verified).
        await self._client.open_channel("CSPB_VIP", ChannelType.UAUT, wire_name="CSPB")

        # Send CTPP init — this is the registration message that primes the
        # device to send VIP events on this channel. Without it, opening the
        # channel alone is not enough (confirmed by testing).
        init_ts = int(time.time()) & 0xFFFFFFFF
        init_msg = encode_ctpp_init(apt_addr, apt_sub, init_ts)
        await self._client.send_binary(self._channel, init_msg)
        _LOGGER.info("VIP: sent CTPP init (ts=0x%08X)", init_ts)

        # Wait for device responses (ACK + confirm) and send our ACKs.
        # The device typically sends 2 responses after init.
        for i in range(2):
            try:
                resp = await asyncio.wait_for(
                    self._channel.response_queue.get(), timeout=10.0
                )
                if resp and len(resp) >= 2:
                    msg_type = struct.unpack_from("<H", resp, 0)[0]
                    _LOGGER.info(
                        "VIP init response %d: %d bytes, type=0x%04X",
                        i + 1, len(resp), msg_type,
                    )
            except TimeoutError:
                _LOGGER.warning("VIP init: timeout waiting for response %d", i + 1)
                break

        # Send ACK pair (0x1800 + 0x1820) to complete the handshake.
        # Uses our_addr (with sub) as caller, apt_addr (without sub) as callee.
        ack_ts = (init_ts + 0x01000000) & 0xFFFFFFFF
        await self._client.send_binary(
            self._channel,
            encode_call_response_ack(vip_address, apt_addr, ack_ts),
        )
        await self._client.send_binary(
            self._channel,
            encode_call_response_ack(vip_address, apt_addr, ack_ts, prefix=0x1820),
        )
        _LOGGER.info("VIP: sent ACK pair (ts=0x%08X)", ack_ts)

        self._running = True
        self._task = asyncio.create_task(self._listen_loop())
        _LOGGER.info("VIP event listener started on CTPP channel")

    async def stop(self) -> None:
        """Stop the listener and close the channel."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _listen_loop(self) -> None:
        """Read binary messages from the CTPP channel and dispatch events."""
        queue = self._channel.response_queue
        while self._running:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=60.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            await self._process_message(data)

    async def _process_message(self, data: bytes) -> None:
        """Parse and dispatch a binary CTPP message."""
        msg = parse_ctpp_message(data)
        if msg is None:
            _LOGGER.debug(
                "VIP: unparseable message (%d bytes): %s",
                len(data),
                data[:40].hex(),
            )
            return

        prefix = msg["prefix"]
        action = msg["action"]
        addresses = msg["addresses"]

        _LOGGER.info(
            "VIP event: prefix=0x%04X action=0x%04X flags=0x%04X addrs=%s (%d bytes)",
            prefix,
            action,
            msg.get("flags", 0),
            addresses,
            len(data),
        )

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("VIP raw: %s", data.hex())

        # 0x1860/0x0010 is the device's periodic registration renewal signal.
        # The app must respond with ACK pair (0x1800 + 0x1820) or the device
        # stops pushing VIP events (doorbell rings, door opens, etc.).
        if prefix == PREFIX_VIP_EVENT and action == ACTION_REGISTRATION_RENEWAL:
            await self._send_renewal_ack(msg)
            return

        # 0x1860/0x0003 is a door-opened event. ACK it immediately so the
        # device clears the channel state — without this the device stays
        # "busy" for a few seconds, blocking the next ring.
        if prefix == PREFIX_VIP_EVENT and action == ACTION_DOOR_OPENED:
            await self._send_event_ack(msg)

        # Detect incoming call / doorbell ring.
        #
        # When someone rings the doorbell, the device sends a CALL_FSM_STATUS_CHANGE
        # event with IN_ALERTING status. Based on APK analysis:
        # - The native library receives this as a binary CTPP message
        # - Converts it to JSON with unit_type_id=1, msg_type_id=0,
        #   call_fsm_status_id=1 (IN_ALERTING)
        #
        # Since we don't have the native library's binary→JSON conversion,
        # we detect incoming calls heuristically:
        # - Device-initiated messages (0x1860, 0x1840, 0x18C0 from device)
        # - With a non-zero action code
        # - That contain our VIP address
        #
        # The 0x1800 prefix (ACK) is NOT an event — it's a response to our
        # messages, so we skip it.
        if prefix in (PREFIX_CALL_INIT, PREFIX_VIP_EVENT, PREFIX_EVENT):
            self._handle_vip_event(msg)

    async def _send_event_ack(self, msg: dict) -> None:
        """Send a single ACK (0x1800) for a device-initiated VIP event.

        Used for events like door_opened (0x1860/0x0003) where the device
        expects acknowledgment to clear the channel state. Without it the
        device stays "busy" for a few seconds, blocking subsequent rings.
        """
        apt_addr = self._config.apt_address
        apt_sub = self._config.apt_subaddress
        vip_address = f"{apt_addr}{apt_sub}"
        entrance_addr = msg["addresses"][0] if msg["addresses"] else apt_addr
        ack_ts = (msg["timestamp"] + 0x01000000) & 0xFFFFFFFF
        try:
            await self._client.send_binary(
                self._channel,
                encode_call_response_ack(vip_address, entrance_addr, ack_ts),
            )
            _LOGGER.debug("VIP: sent event ACK (action=0x%04X, ts=0x%08X)", msg["action"], ack_ts)
        except Exception:
            _LOGGER.warning("VIP: failed to send event ACK", exc_info=True)

    async def _send_renewal_ack(self, msg: dict) -> None:
        """Respond to device's periodic 0x1860/0x0010 registration renewal signal.

        The device sends this message periodically to verify the client is still
        listening. Without the ACK pair response it stops pushing VIP events.
        """
        apt_addr = self._config.apt_address
        apt_sub = self._config.apt_subaddress
        vip_address = f"{apt_addr}{apt_sub}"
        ack_ts = (msg["timestamp"] + 0x01000000) & 0xFFFFFFFF
        try:
            await self._client.send_binary(
                self._channel,
                encode_call_response_ack(vip_address, apt_addr, ack_ts),
            )
            await self._client.send_binary(
                self._channel,
                encode_call_response_ack(vip_address, apt_addr, ack_ts, prefix=0x1820),
            )
            _LOGGER.info("VIP: sent renewal ACK pair (ts=0x%08X)", ack_ts)
        except Exception:
            _LOGGER.warning("VIP: failed to send renewal ACK", exc_info=True)

    def _handle_vip_event(self, msg: dict) -> None:
        """Handle a VIP event that might be a doorbell ring or other call event."""
        prefix = msg["prefix"]
        action = msg["action"]
        addresses = msg["addresses"]

        # A 0x18C0 (call init) from the device means the device is initiating
        # a call to us — this IS the doorbell ring event.
        if prefix == PREFIX_CALL_INIT:
            _LOGGER.debug(
                "CTPP call init received (action=0x%04X, addrs=%s)",
                action,
                addresses,
            )
            self._fire_event("doorbell_ring", addresses)
            return

        # 0x1860 = VIP FSM event. Action encodes the event subtype — see ACTION_* constants.
        if prefix == PREFIX_VIP_EVENT and action != 0:
            _LOGGER.debug(
                "VIP FSM event received: action=0x%04X flags=0x%04X addrs=%s",
                action,
                msg.get("flags", 0),
                addresses,
            )
            if action == ACTION_IN_ALERTING:
                # IN_ALERTING: someone rang the doorbell
                self._fire_event("doorbell_ring", addresses)
            elif action == ACTION_CONNECTED:
                # CONNECTED: call was answered
                pass
            elif action == ACTION_DOOR_OPENED:
                # OUT_INITIATED / door opened (confirmed by testing)
                self._fire_event("door_opened", addresses)
            elif action == ACTION_OUT_ALERTING:
                # OUT_ALERTING: outgoing call is ringing
                pass
            elif action == ACTION_CLOSED:
                # CLOSED: call ended
                pass
            elif action == ACTION_IDLE:
                # IDLE: device returned to idle state
                pass
            else:
                _LOGGER.debug(
                    "VIP FSM event ignored (unknown action=0x%04X)", action
                )
            return

        # 0x1840 events are call-related but may be codec negotiation, config
        # acks, etc. Only log them for now — don't fire events.
        _LOGGER.debug(
            "VIP event (not doorbell): prefix=0x%04X action=0x%04X addrs=%s",
            prefix,
            action,
            addresses,
        )

    def _fire_event(self, event_type: str, addresses: list[str]) -> None:
        """Create and dispatch a PushEvent, deduplicating rapid retransmissions."""
        now = time.time()
        if now - self._last_fired.get(event_type, 0.0) < self._dedup_window:
            _LOGGER.debug("VIP: suppressing duplicate %s event", event_type)
            return
        self._last_fired[event_type] = now
        _LOGGER.info("VIP: firing %s event (addrs=%s)", event_type, addresses)

        caller = addresses[0] if addresses else ""
        event = PushEvent(
            event_type=event_type,
            apt_address=caller,
            timestamp=now,
            raw={"source": "ctpp_vip", "addresses": addresses},
        )
        try:
            self._callback(event)
        except Exception:
            _LOGGER.exception("Error in VIP event callback")
