"""Base entity for Obi EnergyTracker."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ObiEnergyTrackerCoordinator

MANUFACTURER = "OBI"


class ObiEnergyTrackerEntity(CoordinatorEntity[ObiEnergyTrackerCoordinator]):
    """Common base for all Obi EnergyTracker entities.

    Entities are attached to the *sensor* device, which in turn hangs off the
    bridge via ``via_device``. Both ids come from the account, so several
    accounts or several sensors no longer collide on a shared identifier.
    """

    _attr_has_entity_name = True

    # Entities whose whole purpose is to report the sensor's reachability must
    # stay available when it goes offline, or they would hide their own answer.
    _survives_sensor_offline = False

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.api.device_id}_{key}"
        self._attr_translation_key = key
        self._last_write: tuple[Any, ...] | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device this entity belongs to."""
        sensor = self.coordinator.sensor_data
        device_id = self.coordinator.api.device_id or "unknown"
        bridge_id = self.coordinator.api.bridge_id

        info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            manufacturer=MANUFACTURER,
            name=sensor.get("displayName") or "OBI EnergyTracker",
            model=sensor.get("model") or "EnergyTracker Sensor",
            sw_version=sensor.get("firmwareVersion"),
            hw_version=sensor.get("hardwareVersion"),
        )
        if bridge_id:
            # The bridge is registered as its own device during setup.
            info["via_device"] = (DOMAIN, bridge_id)
        return info

    @property
    def available(self) -> bool:
        """Return whether the sensor is reachable and reporting."""
        if not super().available:
            return False
        if self._survives_sensor_offline:
            return True
        sensor = self.coordinator.sensor_data
        # `isOnline` is absent on older payloads — treat that as "assume online".
        return sensor.get("isOnline", True) is not False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state only when something the user can see actually changed.

        The bridge uploads a new reading every five minutes and repeats the
        previous value in between, so this keeps identical readings from
        producing a stream of no-op state writes. Availability is part of the
        comparison so an entity going offline still propagates immediately.
        """
        current = self._state_fingerprint()
        if current != self._last_write:
            self._last_write = current
            self.async_write_ha_state()

    def _state_fingerprint(self) -> tuple[Any, ...]:
        """Return the values that decide whether a state write is needed."""
        return (self.available,)
