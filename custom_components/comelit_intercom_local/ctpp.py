"""CTPP channel helpers: shared init/handshake sequence.

All CTPP consumers (VIP listener, video session, standalone door open) use
ctpp_init_sequence() so the registration handshake is implemented exactly once.
"""

from __future__ import annotations

import logging
import struct

from .channels import Channel
from .client import IconaBridgeClient
from .protocol import encode_call_response_ack, encode_ctpp_init

_LOGGER = logging.getLogger(__name__)

# Both sub-counters increment by 1 (bytes[4] and bytes[5] of the CTPP body).
# Used to compute the ACK timestamp offset from the init timestamp.
# Value matches PCAP-verified video session analysis (_CTR_INCR_BOTH in video_call.py).
_CTR_INCR_BOTH = 0x01010000


async def ctpp_init_sequence(
    client: IconaBridgeClient,
    channel: Channel,
    apt_addr: str,
    apt_sub: int,
    our_addr: str,
    timestamp: int,
    response_timeout: float = 5.0,
) -> None:
    """Full CTPP handshake: init → drain 2 responses → send ACK pair (0x1800 + 0x1820).

    This is the common registration sequence run once per TCP connection
    (when notifications are enabled) or per standalone door open (when they
    are not). All CTPP consumers share this single implementation.

    Args:
        client: the shared ICONA Bridge client.
        channel: the already-open CTPP channel.
        apt_addr: apartment address without subaddress (e.g. "SB000006").
        apt_sub: apartment subaddress integer (e.g. 1).
        our_addr: full address including subaddress (e.g. "SB0000061").
        timestamp: LE32 timestamp to embed in the init message.
        response_timeout: seconds to wait for each device response.
    """
    await client.send_binary(channel, encode_ctpp_init(apt_addr, apt_sub, timestamp))
    _LOGGER.debug("CTPP init sent (ts=0x%08X)", timestamp)

    for i in range(2):
        resp = await client.read_response(channel, timeout=response_timeout)
        if resp and len(resp) >= 2:
            msg_type = struct.unpack_from("<H", resp, 0)[0]
            _LOGGER.debug(
                "CTPP init response %d: %d bytes, type=0x%04X",
                i + 1, len(resp), msg_type,
            )
        else:
            _LOGGER.debug("CTPP init response %d: no response (timeout)", i + 1)

    ack_ts = (timestamp + _CTR_INCR_BOTH) & 0xFFFFFFFF
    await client.send_binary(
        channel, encode_call_response_ack(our_addr, apt_addr, ack_ts)
    )
    await client.send_binary(
        channel, encode_call_response_ack(our_addr, apt_addr, ack_ts, prefix=0x1820)
    )
    _LOGGER.debug("CTPP ACK pair sent (ack_ts=0x%08X)", ack_ts)
