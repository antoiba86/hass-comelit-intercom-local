"""Door open sequence via the CTPP channel."""

from __future__ import annotations

import logging

from .channels import Channel, ChannelType
from .client import IconaBridgeClient
from .exceptions import DoorOpenError
from .models import DeviceConfig, Door
from .protocol import (
    MessageType,
    encode_actuator_init,
    encode_actuator_open,
    encode_ctpp_init,
    encode_door_init,
    encode_open_door,
)

_LOGGER = logging.getLogger(__name__)

# Responses to CTPP init and door-specific init are informational only —
# the actual door relay is triggered by the fire-and-forget open+confirm
# commands in Phase B/D.  The device does not respond to init messages
# while a video session is active (it has one CTPP session per address),
# so keeping this short avoids 4 × 2s = 8s of spurious waiting during
# concurrent video.  0.5s is enough for the normal (no-video) case where
# the device responds within a few hundred milliseconds.
DOOR_RESPONSE_TIMEOUT = 0.5


async def open_door(
    client: IconaBridgeClient,
    config: DeviceConfig,
    door: Door,
) -> None:
    """Open a door using the shared coordinator TCP connection.

    Reuses the already-authenticated client so no second TCP connection
    is opened (the device only accepts one at a time). The full sequence:
    1. Open CTPP_DOOR channel with apt address
    2. Send init sequence
    3. Send open + confirm
    4. Send door-specific init
    5. Send open + confirm again
    6. Release the channel (without disconnecting the shared client)
    """
    try:
        if door.is_actuator:
            await _open_actuator(client, config, door)
        else:
            await _open_regular_door(client, config, door)

        _LOGGER.info("Door '%s' opened successfully", door.name)
    except Exception as e:
        raise DoorOpenError(f"Failed to open door '{door.name}': {e}") from e
    finally:
        # Always release the channel so the next press can reopen it.
        client.remove_channel("CTPP_DOOR")


async def _open_regular_door(
    client: IconaBridgeClient, config: DeviceConfig, door: Door
) -> None:
    """Execute the regular door open sequence (6-step)."""
    apt_addr = config.apt_address
    apt_sub = config.apt_subaddress

    # Open CTPP channel with apt address as extra data
    extra = f"{apt_addr}{apt_sub}"
    channel = await client.open_channel("CTPP_DOOR", ChannelType.CTPP, extra_data=extra, wire_name="CTPP")

    # Phase A: CTPP init
    init_payload = encode_ctpp_init(apt_addr, apt_sub)
    await client.send_binary(channel, init_payload)
    # Read 2 responses (timeout is OK)
    resp = await client.read_response(channel, timeout=DOOR_RESPONSE_TIMEOUT)
    _LOGGER.debug("CTPP init resp1: %s", resp.hex() if resp else None)
    if resp is None:
        _LOGGER.warning("No response to CTPP init (step 1)")
    resp = await client.read_response(channel, timeout=DOOR_RESPONSE_TIMEOUT)
    _LOGGER.debug("CTPP init resp2: %s", resp.hex() if resp else None)
    if resp is None:
        _LOGGER.warning("No response to CTPP init (step 2)")

    # Phase B: Open door + confirm
    await _send_open_and_confirm(client, channel, apt_addr, door)

    # Phase C: Door-specific init
    door_init = encode_door_init(apt_addr, door.output_index, door.apt_address)
    await client.send_binary(channel, door_init)
    resp = await client.read_response(channel, timeout=DOOR_RESPONSE_TIMEOUT)
    _LOGGER.debug("Door init resp1: %s", resp.hex() if resp else None)
    if resp is None:
        _LOGGER.warning("No response to door init (step 1)")
    resp = await client.read_response(channel, timeout=DOOR_RESPONSE_TIMEOUT)
    _LOGGER.debug("Door init resp2: %s", resp.hex() if resp else None)
    if resp is None:
        _LOGGER.warning("No response to door init (step 2)")

    # Phase D: Open door + confirm again
    await _send_open_and_confirm(client, channel, apt_addr, door)


async def _send_open_and_confirm(
    client: IconaBridgeClient, channel: Channel, apt_addr: str, door: Door
) -> None:
    """Send OPEN_DOOR followed by OPEN_DOOR_CONFIRM."""
    open_payload = encode_open_door(
        MessageType.OPEN_DOOR, apt_addr, door.output_index, door.apt_address
    )
    await client.send_binary(channel, open_payload)

    confirm_payload = encode_open_door(
        MessageType.OPEN_DOOR_CONFIRM, apt_addr, door.output_index, door.apt_address
    )
    await client.send_binary(channel, confirm_payload)


async def _open_actuator(
    client: IconaBridgeClient, config: DeviceConfig, door: Door
) -> None:
    """Execute the actuator door open sequence."""
    apt_addr = config.apt_address
    apt_sub = config.apt_subaddress

    extra = f"{apt_addr}{apt_sub}"
    channel = await client.open_channel("CTPP_DOOR", ChannelType.CTPP, extra_data=extra, wire_name="CTPP")

    # Phase A: CTPP init (same as regular door)
    init_payload = encode_ctpp_init(apt_addr, apt_sub)
    await client.send_binary(channel, init_payload)
    resp = await client.read_response(channel, timeout=DOOR_RESPONSE_TIMEOUT)
    _LOGGER.debug("CTPP init resp1: %s", resp.hex() if resp else None)
    if resp is None:
        _LOGGER.warning("No response to CTPP init (step 1)")
    resp = await client.read_response(channel, timeout=DOOR_RESPONSE_TIMEOUT)
    _LOGGER.debug("CTPP init resp2: %s", resp.hex() if resp else None)
    if resp is None:
        _LOGGER.warning("No response to CTPP init (step 2)")

    # Actuator init
    act_init = encode_actuator_init(apt_addr, door.output_index, door.apt_address)
    await client.send_binary(channel, act_init)
    resp = await client.read_response(channel, timeout=DOOR_RESPONSE_TIMEOUT)
    _LOGGER.debug("Actuator init resp1: %s", resp.hex() if resp else None)
    if resp is None:
        _LOGGER.warning("No response to actuator init (step 1)")
    resp = await client.read_response(channel, timeout=DOOR_RESPONSE_TIMEOUT)
    _LOGGER.debug("Actuator init resp2: %s", resp.hex() if resp else None)
    if resp is None:
        _LOGGER.warning("No response to actuator init (step 2)")

    # Actuator open + confirm
    open_payload = encode_actuator_open(apt_addr, door.output_index, door.apt_address, confirm=False)
    await client.send_binary(channel, open_payload)

    confirm_payload = encode_actuator_open(apt_addr, door.output_index, door.apt_address, confirm=True)
    await client.send_binary(channel, confirm_payload)
