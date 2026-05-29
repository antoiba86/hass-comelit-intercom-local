"""Configuration retrieval and parsing via the UCFG channel."""

from __future__ import annotations

import logging

from .channels import ChannelType, ViperMessageId
from .client import IconaBridgeClient
from .const import is_verbose_logging
from .exceptions import ProtocolError
from .models import Camera, DeviceConfig, Door

_LOGGER = logging.getLogger(__name__)


async def get_device_config(client: IconaBridgeClient) -> DeviceConfig:
    """Fetch and parse device configuration from the UCFG channel."""
    channel = await client.open_channel("UCFG", ChannelType.UCFG)

    msg = {
        "message": "get-configuration",
        "addressbooks": "all",
        "message-type": "request",
        "message-id": int(ViperMessageId.UCFG),
    }

    response = await client.send_json(channel, msg)
    if is_verbose_logging():
        _LOGGER.debug("Config response keys: %s", list(response.keys()))

    code = response.get("response-code", 0)
    if code != 200:
        raise ProtocolError(f"Config request returned code {code}")

    return _parse_config(response)


def _parse_config(data: dict) -> DeviceConfig:
    """Parse the raw config JSON into a DeviceConfig."""
    config = DeviceConfig(raw=data)

    vip = data.get("vip", {})
    config.apt_address = vip.get("apt-address", "")
    config.apt_subaddress = vip.get("apt-subaddress", 0)

    user_params = vip.get("user-parameters", {})

    # Parse caller address from entrance-address-book (indoor/app unit address)
    entrance_book = user_params.get("entrance-address-book", [])
    if entrance_book:
        config.caller_address = entrance_book[0].get("apt-address", "")
        if is_verbose_logging():
            _LOGGER.debug("Caller address from entrance-address-book: %s", config.caller_address)

    # Parse doors from opendoor-address-book
    door_index = 0
    for item in user_params.get("opendoor-address-book", []):
        config.doors.append(
            Door(
                id=item.get("id", door_index),
                index=door_index,
                name=item.get("name", ""),
                apt_address=item.get("apt-address", ""),
                output_index=item.get("output-index", 0),
                secure_mode=item.get("secure-mode", False),
                is_actuator=False,
            )
        )
        door_index += 1

    # Parse actuator doors
    for item in user_params.get("actuator-address-book", []):
        config.doors.append(
            Door(
                id=item.get("id", door_index),
                index=door_index,
                name=item.get("name", ""),
                apt_address=item.get("apt-address", ""),
                output_index=item.get("output-index", 0),
                secure_mode=item.get("secure-mode", False),
                is_actuator=True,
                module_index=item.get("module-index", 0),
            )
        )

    # Parse cameras from rtsp-camera-address-book
    for item in user_params.get("rtsp-camera-address-book", []):
        config.cameras.append(
            Camera(
                id=item.get("id", 0),
                name=item.get("name", ""),
                rtsp_url=item.get("rtsp-url", ""),
                rtsp_user=item.get("rtsp-user", ""),
                rtsp_password=item.get("rtsp-password", ""),
            )
        )

    if is_verbose_logging():
        _LOGGER.info("Parsed config: %d doors, %d cameras", len(config.doors), len(config.cameras))
    return config
