"""Binary sensor entities for Comelit intercom diagnostics."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import ComelitLocalConfigEntry, ComelitLocalCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ComelitLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up diagnostic binary sensor entities."""
    coordinator = entry.runtime_data
    async_add_entities([ComelitNotificationSensor(coordinator, entry.entry_id)])


class ComelitNotificationSensor(BinarySensorEntity):
    """Binary sensor reflecting whether the VIP notification listener is active."""

    _attr_has_entity_name = True
    _attr_name = "Notification Service"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ComelitLocalCoordinator, entry_id: str) -> None:
        self._coordinator = coordinator
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_notification_service"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=self._coordinator.device_name,
        )

    @property
    def available(self) -> bool:
        return self._coordinator.notifications_enabled

    @property
    def is_on(self) -> bool:
        return self._coordinator.vip_listener_active

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.add_vip_state_change_callback(self._on_vip_state_change))

    @callback
    async def _on_vip_state_change(self) -> None:
        self.async_write_ha_state()
