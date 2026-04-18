"""Door open sequences via the shared CTPP channel."""

from __future__ import annotations

import logging

from .channels import ChannelType
from .client import IconaBridgeClient
from .ctpp import ctpp_init_sequence
from .exceptions import DoorOpenError
from .models import DeviceConfig, Door
from .protocol import (
    MessageType,
    encode_actuator_open,
    encode_open_door,
)

_LOGGER = logging.getLogger(__name__)

# Timeout for CTPP init responses in the standalone (no-VIP) path.
# The device responds quickly on a fresh CTPP session.
DOOR_CTPP_INIT_TIMEOUT = 5.0


async def open_door_fast(
    client: IconaBridgeClient,
    config: DeviceConfig,
    door: Door,
) -> None:
    """Open a door by reusing the already-open CTPP channel.

    Used when the VIP listener has an active CTPP session (notifications ON,
    no active video). Skips the init handshake entirely — the channel is
    already registered with the device — and fires OPEN_DOOR + CONFIRM
    directly (~30ms total).
    """
    ctpp = client.get_channel("CTPP")
    if ctpp is None:
        raise DoorOpenError("CTPP channel not open — cannot use fast door open path")

    try:
        if door.is_actuator:
            await _send_actuator_open(client, ctpp, config.apt_address, door)
        else:
            await _send_open_and_confirm(client, ctpp, config.apt_address, door)
            await _send_open_and_confirm(client, ctpp, config.apt_address, door)
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
    video). Opens CTPP_DOOR, runs ctpp_init_sequence (gaining the proper ACK
    pair that was missing in the old 6-step flow), sends OPEN_DOOR + CONFIRM,
    then closes the channel.
    """
    apt_addr = config.apt_address
    apt_sub = config.apt_subaddress
    our_addr = f"{apt_addr}{apt_sub}"

    try:
        import time
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
            await _send_actuator_open(client, ctpp, apt_addr, door)
        else:
            await _send_open_and_confirm(client, ctpp, apt_addr, door)
            await _send_open_and_confirm(client, ctpp, apt_addr, door)

        _LOGGER.info("Door '%s' opened successfully (standalone path)", door.name)
    except Exception as e:
        raise DoorOpenError(f"Failed to open door '{door.name}': {e}") from e
    finally:
        client.remove_channel("CTPP_DOOR")
        client.remove_channel("CSPB_DOOR")


async def _send_open_and_confirm(
    client: IconaBridgeClient,
    channel,
    apt_addr: str,
    door: Door,
) -> None:
    """Send OPEN_DOOR followed by OPEN_DOOR_CONFIRM (fire-and-forget)."""
    await client.send_binary(
        channel,
        encode_open_door(MessageType.OPEN_DOOR, apt_addr, door.output_index, door.apt_address),
    )
    await client.send_binary(
        channel,
        encode_open_door(MessageType.OPEN_DOOR_CONFIRM, apt_addr, door.output_index, door.apt_address),
    )


async def _send_actuator_open(
    client: IconaBridgeClient,
    channel,
    apt_addr: str,
    door: Door,
) -> None:
    """Send actuator open followed by actuator confirm (fire-and-forget)."""
    await client.send_binary(
        channel,
        encode_actuator_open(apt_addr, door.output_index, door.apt_address, confirm=False),
    )
    await client.send_binary(
        channel,
        encode_actuator_open(apt_addr, door.output_index, door.apt_address, confirm=True),
    )
