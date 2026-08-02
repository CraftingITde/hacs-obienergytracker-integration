"""Diagnostics support for Obi EnergyTracker."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ObiEnergyTrackerConfigEntry
from .const import CONF_COUNTRY, DATA_DAILY, DATA_METER, DATA_PROFILE

# Everything that identifies the account or the household.
TO_REDACT = {
    "email",
    "password",
    "ecomId",
    "ecom_id",
    "givenName",
    "wifiSSID",
    "id",
    "bridge_id",
    "device_id",
    "btChallengeId",
    "label",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ObiEnergyTrackerConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Uses the data the coordinator already holds instead of performing a fresh
    login, so downloading diagnostics never costs an extra authentication round
    trip and reflects exactly what the entities are seeing.
    """
    coordinator = config_entry.runtime_data
    data = coordinator.data or {}

    return async_redact_data(
        {
            "config_entry_data": {
                **config_entry.data,
                "country": config_entry.data.get(CONF_COUNTRY),
            },
            "options": dict(config_entry.options),
            "last_update_success": coordinator.last_update_success,
            "profile": data.get(DATA_PROFILE),
            "meter_record_count": len(data.get(DATA_METER) or []),
            "meter_latest": (data.get(DATA_METER) or [])[-2:],
            "daily_record_count": len(data.get(DATA_DAILY) or []),
            "daily_latest": (data.get(DATA_DAILY) or [])[-4:],
        },
        TO_REDACT,
    )
