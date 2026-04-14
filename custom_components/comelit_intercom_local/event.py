"""Event entity for doorbell ring notifications."""

from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import ComelitLocalConfigEntry, ComelitLocalCoordinator
from .models import PushEvent

_LOGGER = logging.getLogger(__name__)

EVENT_TYPES = ["doorbell_ring", "missed_call"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ComelitLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the doorbell event entity."""
    coordinator = entry.runtime_data
    async_add_entities([ComelitDoorbellEvent(coordinator, entry.entry_id)])


class ComelitDoorbellEvent(EventEntity):
    """Event entity that fires on doorbell ring."""

    _attr_has_entity_name = True
    _attr_translation_key = "doorbell"
    _attr_event_types = EVENT_TYPES
    _attr_icon = "mdi:doorbell"

    def __init__(
        self,
        coordinator: ComelitLocalCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the doorbell event entity."""
        self._coordinator = coordinator
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_doorbell"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info linking this event to the main intercom device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=self._coordinator.device_name,
        )

    async def async_added_to_hass(self) -> None:
        """Register push callback when added to HA."""
        self.async_on_remove(self._coordinator.add_push_callback(self._on_push))

    @callback
    def _on_push(self, event: PushEvent) -> None:
        """Handle a push event from the device."""
        if event.event_type in EVENT_TYPES:
            self._trigger_event(event.event_type, {"apt_address": event.apt_address})
            self.async_write_ha_state()
            _LOGGER.info("Doorbell event fired: %s", event.event_type)
