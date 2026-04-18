"""Button entities for door opening."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import ComelitLocalConfigEntry, ComelitLocalCoordinator
from .models import Door, PushEvent

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ComelitLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up door open and video button entities."""
    coordinator = entry.runtime_data
    config = coordinator.device_config
    if not config:
        return

    entities: list[ButtonEntity] = [
        ComelitDoorButton(coordinator, door, entry.entry_id)
        for door in config.doors
    ]

    if config.doors:
        entities.append(ComelitStartVideoButton(coordinator, entry.entry_id))
        entities.append(ComelitStopVideoButton(coordinator, entry.entry_id))

    async_add_entities(entities)


class ComelitDoorButton(CoordinatorEntity[ComelitLocalCoordinator], ButtonEntity):
    """Button entity to open a Comelit door."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:door-open"

    def __init__(
        self,
        coordinator: ComelitLocalCoordinator,
        door: Door,
        entry_id: str,
    ) -> None:
        """Initialize the door button entity."""
        super().__init__(coordinator)
        self._door = door
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_door_{door.index}"
        self._attr_name = door.name

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info linking this button to the main intercom device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=self.coordinator.device_name,
        )

    async def async_press(self) -> None:
        """Open the door when pressed."""
        _LOGGER.info("Opening door: %s", self._door.name)
        try:
            await self.coordinator.async_open_door(self._door)
        except Exception:
            _LOGGER.exception("Failed to open door %s", self._door.name)
            return

        if self.coordinator.video_session and self.coordinator.video_session.active:
            _LOGGER.info("Door opened — stopping video in 10s")
            self.hass.async_create_task(self._stop_video_after_delay(10))

    async def _stop_video_after_delay(self, delay: int) -> None:
        """Stop the video session after a delay (seconds)."""
        await asyncio.sleep(delay)
        if self.coordinator.video_session and self.coordinator.video_session.active:
            _LOGGER.info("Stopping video after door-open delay")
            self.coordinator.request_video_stop()
            await self.coordinator.async_stop_video()


class ComelitStartVideoButton(CoordinatorEntity[ComelitLocalCoordinator], ButtonEntity):
    """Button entity to start intercom video feed."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:video"
    _attr_name = "Start Video Feed"

    def __init__(
        self,
        coordinator: ComelitLocalCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the start video button entity."""
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_video_start"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info linking this button to the main intercom device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=self.coordinator.device_name,
        )

    async def async_press(self) -> None:
        """Start intercom video when pressed."""
        if not self.coordinator.device_config:
            return
        t0 = time.monotonic()
        _LOGGER.info("Starting intercom video")
        try:
            await self.coordinator.async_start_video()
            _LOGGER.info("Video ready in %.1fs", time.monotonic() - t0)
        except Exception:
            _LOGGER.exception("Failed to start intercom video after %.1fs", time.monotonic() - t0)


class ComelitStopVideoButton(CoordinatorEntity[ComelitLocalCoordinator], ButtonEntity):
    """Button entity to stop intercom video feed."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:video-off"
    _attr_name = "Stop Video Feed"

    def __init__(
        self,
        coordinator: ComelitLocalCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the stop video button entity."""
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_video_stop"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info linking this button to the main intercom device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=self.coordinator.device_name,
        )

    async def async_press(self) -> None:
        """Stop intercom video when pressed."""
        _LOGGER.info("Stopping intercom video")
        try:
            self.coordinator.request_video_stop()
            await self.coordinator.async_stop_video()
        except Exception:
            _LOGGER.exception("Failed to stop intercom video")
