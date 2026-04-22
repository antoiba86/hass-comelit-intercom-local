"""Door open sequences via the shared CTPP channel.

Two public entry points match the two environments the coordinator can be in:

    open_door_fast        — a CTPP channel is already open (VIP listener ON)
    open_door_standalone  — no CTPP channel is open; open a transient one

Both paths run the same 6-step per-door sequence originally proven before
f260750 (which over-simplified it and broke door opens on some firmwares):

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


async def open_door_fast(
    client: IconaBridgeClient,
    config: DeviceConfig,
    door: Door,
) -> None:
    """Open a door by reusing the already-open CTPP channel.

    Used when the VIP listener has an active CTPP session (notifications ON,
    no active video). The top-level ctpp_init_sequence has already run at
    listener startup, so only the per-door sequence is needed here.
    """
    ctpp = client.get_channel("CTPP")
    if ctpp is None:
        raise DoorOpenError("CTPP channel not open — cannot use fast door open path")

    try:
        if door.is_actuator:
            await _open_actuator_on_channel(client, ctpp, config.apt_address, door)
        else:
            await _open_regular_on_channel(client, ctpp, config.apt_address, door)
        _LOGGER.info("Door '%s' opened successfully (fast path)", door.name)
    except Exception as e:
        raise DoorOpenError(f"Failed to open door '{door.name}': {e}") from e


async def open_door_standalone(
    client: IconaBridgeClient,
    config: DeviceConfig,
    door: Door,
) -> None:
    """Open a door by opening a transient CTPP channel with full init.

    Used when no CTPP channel is currently open (notifications OFF, no active
    video). Opens CTPP_DOOR + CSPB_DOOR, runs ctpp_init_sequence, then the
    per-door sequence, and removes the transient channels.
    """
    apt_addr = config.apt_address
    apt_sub = config.apt_subaddress
    our_addr = f"{apt_addr}{apt_sub}"

    try:
        ctpp = await client.open_channel(
            "CTPP_DOOR", ChannelType.UAUT, extra_data=our_addr
        )
        await client.open_channel("CSPB_DOOR", ChannelType.UAUT)
        ts = int(time.time()) & 0xFFFFFFFF
        await ctpp_init_sequence(
            client, ctpp, apt_addr, apt_sub, our_addr, ts,
            response_timeout=DOOR_CTPP_INIT_TIMEOUT,
        )

        if door.is_actuator:
            await _open_actuator_on_channel(client, ctpp, apt_addr, door)
        else:
            await _open_regular_on_channel(client, ctpp, apt_addr, door)

        _LOGGER.info("Door '%s' opened successfully (standalone path)", door.name)
    except Exception as e:
        raise DoorOpenError(f"Failed to open door '{door.name}': {e}") from e
    finally:
        client.remove_channel("CTPP_DOOR")
        client.remove_channel("CSPB_DOOR")


async def _open_regular_on_channel(
    client: IconaBridgeClient,
    channel: Channel,
    apt_addr: str,
    door: Door,
) -> None:
    """Regular-door open sequence on an already-initialized CTPP channel.

    OPEN + CONFIRM  →  door_init + drain 2 resps  →  OPEN + CONFIRM.
    """
    await _send_open_and_confirm(client, channel, apt_addr, door)

    await client.send_binary(
        channel,
        encode_door_init(apt_addr, door.output_index, door.apt_address),
    )
    for i in range(2):
        resp = await client.read_response(channel, timeout=DOOR_RESPONSE_TIMEOUT)
        _LOGGER.debug(
            "door_init resp %d: %s", i + 1, resp.hex() if resp else "timeout",
        )

    await _send_open_and_confirm(client, channel, apt_addr, door)


async def _open_actuator_on_channel(
    client: IconaBridgeClient,
    channel: Channel,
    apt_addr: str,
    door: Door,
) -> None:
    """Actuator-specific open sequence on an already-initialized CTPP channel.

    actuator_init + drain 2 resps  →  actuator_open + actuator_confirm.
    """
    await client.send_binary(
        channel,
        encode_actuator_init(apt_addr, door.output_index, door.apt_address),
    )
    for i in range(2):
        resp = await client.read_response(channel, timeout=DOOR_RESPONSE_TIMEOUT)
        _LOGGER.debug(
            "actuator_init resp %d: %s", i + 1, resp.hex() if resp else "timeout",
        )

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
