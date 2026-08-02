"""Data update coordinator for Obi EnergyTracker."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ObiAuthError, ObiConnectionError, ObiEnergyTrackerAPI
from .const import (
    CONF_SCAN_INTERVAL,
    DATA_DAILY,
    DATA_METER,
    DATA_PROFILE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STATISTICS_BACKFILL_DAYS,
)
from .statistics import ObiStatisticsImporter

_LOGGER = logging.getLogger(__name__)

# Daily totals only change once a day (plus the running total for today), so
# they are refreshed far less often than the meter reading.
DAILY_REFRESH_INTERVAL = timedelta(minutes=15)


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
        self._statistics = ObiStatisticsImporter(hass, api.device_id)
        self._daily_cache: list[dict[str, Any]] = []
        self._daily_fetched_at: datetime | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch meter readings, device state and daily history."""
        try:
            profile = await self.api.async_get_user_profile()
            meter = await self.api.async_get_meter_data()
            daily = await self._async_get_daily()
        except ObiAuthError as err:
            # Triggers Home Assistant's reauth flow instead of silently failing.
            raise ConfigEntryAuthFailed(str(err)) from err
        except ObiConnectionError as err:
            raise UpdateFailed(str(err)) from err

        if daily:
            try:
                await self._statistics.async_import(daily)
            except (HomeAssistantError, ValueError, KeyError) as err:
                # Never let a statistics problem take the entities down with it.
                _LOGGER.warning("Could not import long-term statistics: %s", err)

        _LOGGER.debug(
            "Update complete: %d meter record(s), %d daily record(s)",
            len(meter),
            len(daily),
        )

        return {
            DATA_METER: meter,
            DATA_PROFILE: profile,
            DATA_DAILY: daily,
        }

    async def _async_get_daily(self) -> list[dict[str, Any]]:
        """Return daily history, refreshed at most every few minutes."""
        now = dt_util.utcnow()
        if (
            self._daily_fetched_at is not None
            and now - self._daily_fetched_at < DAILY_REFRESH_INTERVAL
        ):
            return self._daily_cache

        self._daily_cache = await self.api.async_get_daily_data(
            STATISTICS_BACKFILL_DAYS
        )
        self._daily_fetched_at = now
        return self._daily_cache

    @property
    def bridge_data(self) -> dict[str, Any]:
        """Return the bridge section of the user profile."""
        if not self.data:
            return {}
        return self.data.get(DATA_PROFILE, {}).get("bridge") or {}

    @property
    def sensor_data(self) -> dict[str, Any]:
        """Return the profile entry for the configured sensor.

        The bridge can carry several sensors, so it is matched by id rather than
        just taking the first one.
        """
        sensors = self.bridge_data.get("sensors") or []
        for sensor in sensors:
            if isinstance(sensor, dict) and sensor.get("id") == self.api.device_id:
                return sensor
        return sensors[0] if sensors and isinstance(sensors[0], dict) else {}
