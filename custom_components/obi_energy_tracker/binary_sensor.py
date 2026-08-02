"""Binary sensor platform for Obi EnergyTracker."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ObiEnergyTrackerConfigEntry
from .coordinator import ObiEnergyTrackerCoordinator
from .entity import ObiEnergyTrackerEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ObiEnergyTrackerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors from a config entry."""
    async_add_entities([ObiOnlineBinarySensor(config_entry.runtime_data)])


class ObiOnlineBinarySensor(ObiEnergyTrackerEntity, BinarySensorEntity):
    """Reports whether the meter sensor is currently online."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _survives_sensor_offline = True

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, "online")

    @property
    def is_on(self) -> bool | None:
        """Return True while the sensor is online."""
        value = self.coordinator.sensor_data.get("isOnline")
        return value if isinstance(value, bool) else None

    def _state_fingerprint(self) -> tuple[Any, ...]:
        """Include the online state in the change detection."""
        return (self.available, self.is_on)
