"""Data update coordinator for Obi EnergyTracker."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ObiEnergyTrackerAPI
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

DAYS_OF_HISTORY = 7


class ObiEnergyTrackerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Data update coordinator for Obi EnergyTracker."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: ObiEnergyTrackerAPI,
        config_entry: Any,
    ) -> None:
        """Initialize the coordinator."""
        scan_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
            config_entry=config_entry,
        )
        self.api = api

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API.

        Retrieves:
        - Meter reading (Zählerstand) for the device
        - Hourly energy data for the past 7 days
        """
        try:
            meter = await self.api.async_get_meter_data()
            _LOGGER.debug("Meter data: %s", meter)

            # Fetch hourly data for past days (default 7 days)
            end_date = datetime.now()
            hourly_data = await self.api.async_get_hourly_data(
                start_date=end_date,
                num_days=DAYS_OF_HISTORY,
            )
            _LOGGER.debug(
                "Hourly data fetched: %s", "available" if hourly_data else "none"
            )

            _LOGGER.info(
                "Successfully fetched data: meter=%s, hourly_days=%d",
                "available" if meter else "none",
                DAYS_OF_HISTORY,
            )
        except OSError as err:
            _LOGGER.error("Failed to update data: %s", err)
            raise UpdateFailed(f"Failed to update data: {err}") from err

        return {
            "hourly": hourly_data,
            "meter": meter,
        }
