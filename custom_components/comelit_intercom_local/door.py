"""Door open sequences via the shared CTPP channel.

Single public entry point: open_door — reuses an existing CTPP channel when
one is open (VIP listener ON / video active), otherwise opens a transient one.

Per-door sequence:
    regular door:  OPEN + CONFIRM  →  door_init + drain 2 resps  →  OPEN + CONFIRM
    actuator:      actuator_init + drain 2 resps  →  actuator_open + actuator_confirm

The during-call door-open path lives in video_call.py (single 0x1840/0x000D
message on the video CTPP channel) and is NOT used here.
"""

from __future__ import annotations

import logging
import time

from .channels import Channel, ChannelType
from .client import IconaBridgeClient
from .ctpp import ctpp_init_sequence
from .exceptions import DoorOpenError
from .models import DeviceConfig, Door
from .protocol import (
    MessageType,
    encode_actuator_init,
    encode_actuator_open,
    encode_door_init,
    encode_open_door,
)

_LOGGER = logging.getLogger(__name__)

# Timeout for the top-level CTPP init handshake in the standalone path.
DOOR_CTPP_INIT_TIMEOUT = 5.0
# Timeout for the per-door (door_init / actuator_init) response drain.
DOOR_RESPONSE_TIMEOUT = 2.0

async def open_door(
    client: IconaBridgeClient,
    config: DeviceConfig,
    door: Door,
) -> None:
    ctpp = client.get_channel("CTPP")
    opened_channel = ctpp is None
    if ctpp is None:
        ctpp = await open_ctpp_channel(client, config)

    try:
        await _open_door_on_channel(client, ctpp, config.apt_address, door)
        _LOGGER.info("Door '%s' opened successfully (fast path)", door.name)
    except Exception as e:
        raise DoorOpenError(f"Failed to open door '{door.name}': {e}") from e
    finally:
        if opened_channel:
            client.remove_channel("CTPP_DOOR")

async def open_ctpp_channel(
    client: IconaBridgeClient,
    config: DeviceConfig,
) -> Channel:
    """Open a transient CTPP_DOOR channel and run ctpp_init_sequence.

    Used when no CTPP channel is currently open (notifications OFF, no active
    video). Caller is responsible for removing the channel when done.
    """
    apt_addr = config.apt_address
    apt_sub = config.apt_subaddress
    our_addr = f"{apt_addr}{apt_sub}"

    try:
        ctpp = await client.open_channel(
            "CTPP_DOOR", ChannelType.CTPP, extra_data=our_addr
        )
        ts = int(time.time()) & 0xFFFFFFFF
        await ctpp_init_sequence(
            client, ctpp, apt_addr, apt_sub, our_addr, ts,
            response_timeout=DOOR_CTPP_INIT_TIMEOUT,
            send_ack=False,
        )
        return ctpp
    except Exception as e:
        raise DoorOpenError(f"Failed to open door: {e}") from e

async def _open_door_on_channel(
    client: IconaBridgeClient,
    channel: Channel,
    apt_addr: str,
    door: Door
) -> None:
    """Regular-door open sequence on an already-initialized CTPP channel.

    OPEN + CONFIRM  →  door_init + drain 2 resps  →  OPEN + CONFIRM.
    """
    #TODO: verify why we send the open door twice
    # Phase B: Open door + confirm
    if door.is_actuator is False:
        await _send_open_and_confirm(client, channel, apt_addr, door)

    # Phase C: Door-specific init
    await client.send_binary(
        channel,
        encode_actuator_init(apt_addr, door.output_index, door.apt_address)
            if door.is_actuator
            else encode_door_init(apt_addr, door.output_index, door.apt_address)
    )
    for i in range(2):
        resp = await client.read_response(channel, timeout=DOOR_RESPONSE_TIMEOUT)
        _LOGGER.debug(
            "door_init resp %d: %s", i + 1, resp.hex() if resp else "timeout",
        )
        if resp is None:
            _LOGGER.warning("No response to door init (step 2)")

    # Phase D: Open door + confirm again
    if door.is_actuator is False:
        await _send_open_and_confirm(client, channel, apt_addr, door)
    else:
        await client.send_binary(
            channel,
            encode_actuator_open(apt_addr, door.output_index, door.apt_address, confirm=False),
        )
        await client.send_binary(
            channel,
            encode_actuator_open(apt_addr, door.output_index, door.apt_address, confirm=True),
        )

async def _send_open_and_confirm(
    client: IconaBridgeClient,
    channel: Channel,
    apt_addr: str,
    door: Door,
) -> None:
    """Send OPEN_DOOR followed by OPEN_DOOR_CONFIRM."""
    await client.send_binary(
        channel,
        encode_open_door(MessageType.OPEN_DOOR, apt_addr, door.output_index, door.apt_address),
    )
    await client.send_binary(
        channel,
        encode_open_door(MessageType.OPEN_DOOR_CONFIRM, apt_addr, door.output_index, door.apt_address),
    )
