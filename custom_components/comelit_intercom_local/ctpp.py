"""CTPP channel helpers: shared init/handshake sequence.

All CTPP consumers (VIP listener, video session, standalone door open) use
ctpp_init_sequence() so the registration handshake is implemented exactly once.
"""

from __future__ import annotations

import logging
import struct

from .channels import Channel
from .client import IconaBridgeClient
from .const import is_verbose_logging
from .protocol import encode_call_response_ack, encode_ctpp_init

_LOGGER = logging.getLogger(__name__)

# Only the high sub-counter increments (byte[4] of the LE32 timestamp).
# Used to compute the ACK timestamp offset for VIP CTPP channels (doorbell listener).
# Note: video sessions use 0x01010000 (both sub-counters) — see video_call.py.
_VIP_ACK_TS_INCR = 0x01000000

# Minimum response length: prefix(2) + timestamp(4) + action(2) = 8 bytes.
_CTPP_RESPONSE_MIN_LEN = 8

# Prefix for VIP event messages (device → client).
_PREFIX_VIP_EVENT = 0x1860


async def ctpp_init_sequence(
    client: IconaBridgeClient,
    channel: Channel,
    apt_addr: str,
    apt_sub: int,
    our_addr: str,
    timestamp: int | None = None,
    response_timeout: float = 5.0,
    send_ack: bool = True,
) -> None:
    """CTPP handshake: init → drain 2 responses → optionally send ACK pair.

    The ACK pair (0x1800 + 0x1820) is required for VIP listener and video
    sessions but must NOT be sent for standalone door opens — the original
    door open flow never sent it.

    Args:
        client: the shared ICONA Bridge client.
        channel: the already-open CTPP channel.
        apt_addr: apartment address without subaddress (e.g. "SB000006").
        apt_sub: apartment subaddress integer (e.g. 1).
        our_addr: full address including subaddress (e.g. "SB0000061").
        timestamp: LE32 timestamp to embed in the init message.
        response_timeout: seconds to wait for each device response.
        send_ack: send the ACK pair after draining responses (default True).
    """
    init_payload = encode_ctpp_init(apt_addr, apt_sub, timestamp)
    await client.send_binary(channel, init_payload)

    fast_ack_sent = await read_response_ctpp(
        client,
        channel,
        response_timeout,
        ack_config={"our_addr": our_addr, "apt_addr": apt_addr, "init_ts": timestamp},
    )

    if send_ack and not fast_ack_sent:
        assert timestamp is not None
        ack_ts = (timestamp + _VIP_ACK_TS_INCR) & 0xFFFFFFFF
        await client.send_binary(channel, encode_call_response_ack(our_addr, apt_addr, ack_ts))
        await client.send_binary(channel, encode_call_response_ack(our_addr, apt_addr, ack_ts, prefix=0x1820))
        if is_verbose_logging():
            _LOGGER.debug(
                "CTPP ACK pair sent (init_ts=0x%08X ack_ts=0x%08X)",
                timestamp,
                ack_ts,
            )


async def read_response_ctpp(
    client: IconaBridgeClient,
    channel: Channel,
    response_timeout: float = 5.0,
    ack_config: dict | None = None,
) -> bool:
    """Drain up to 2 device responses after CTPP init.

    The device typically sends [0x1800 init ACK][0x1860 initial-burst renewal]
    in quick succession. If a renewal (0x1860) is seen, send the ACK pair
    immediately using init_ts + _VIP_ACK_TS_INCR (PCAP-verified: ACK timestamp
    must be derived from our init timestamp, never from the device's timestamp).

    Returns True if the ACK pair was sent (caller must not send it again).
    """
    for i in range(2):
        resp = await client.read_response(channel, timeout=response_timeout)
        if resp and len(resp) >= _CTPP_RESPONSE_MIN_LEN:
            prefix = struct.unpack_from("<H", resp, 0)[0]
            resp_ts = struct.unpack_from("<I", resp, 2)[0]
            action = struct.unpack_from(">H", resp, 6)[0]
            if is_verbose_logging():
                _LOGGER.debug(
                    "CTPP init response %d: %d bytes, prefix=0x%04X ts=0x%08X action=0x%04X",
                    i + 1,
                    len(resp),
                    prefix,
                    resp_ts,
                    action,
                )
        else:
            if is_verbose_logging():
                _LOGGER.debug("CTPP init response %d: no response (timeout)", i + 1)

    return False
