"""The obienergytracker integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ObiAuthError, ObiConnectionError, ObiEnergyTrackerAPI
from .const import CONF_BRIDGE_ID, CONF_COUNTRY, CONF_DEVICE_ID, DOMAIN
from .coordinator import ObiEnergyTrackerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

MANUFACTURER = "OBI"

# Identifiers used before entities were keyed by the actual device id.
LEGACY_DEVICE_ID = "obi_energy_tracker"
LEGACY_UNIQUE_IDS = {
    "obi_meter_reading": "meter_reading",
    "obi_feed_in_reading": "feed_in_reading",
}


type ObiEnergyTrackerConfigEntry = ConfigEntry[ObiEnergyTrackerCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: ObiEnergyTrackerConfigEntry
) -> bool:
    """Set up obienergytracker from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    api = ObiEnergyTrackerAPI(
        session=session,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        country=entry.data.get(CONF_COUNTRY, "DE"),
        bridge_id=entry.data.get(CONF_BRIDGE_ID),
        device_id=entry.data.get(CONF_DEVICE_ID),
    )

    _async_migrate_legacy_ids(hass, entry)

    try:
        await api.async_login()
    except ObiAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ObiConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = ObiEnergyTrackerCoordinator(hass, api, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    _async_register_bridge(hass, entry, coordinator)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


@callback
def _async_migrate_legacy_ids(
    hass: HomeAssistant, entry: ObiEnergyTrackerConfigEntry
) -> None:
    """Move entities off the old hard-coded identifiers.

    Earlier versions used the fixed device identifier ``obi_energy_tracker`` and
    unique ids like ``obi_meter_reading``, which collide as soon as a second
    account or a second meter sensor is set up. Rewriting the registry in place
    keeps the existing entity ids — and with them the recorded history and the
    long-term statistics — instead of creating a second set of entities.
    """
    device_id = entry.data.get(CONF_DEVICE_ID)
    if not device_id:
        return

    device_registry = dr.async_get(hass)
    legacy_device = device_registry.async_get_device(
        identifiers={(DOMAIN, LEGACY_DEVICE_ID)}
    )
    if legacy_device and not device_registry.async_get_device(
        identifiers={(DOMAIN, device_id)}
    ):
        device_registry.async_update_device(
            legacy_device.id, new_identifiers={(DOMAIN, device_id)}
        )
        _LOGGER.debug("Migrated device identifier to %s", device_id)

    entity_registry = er.async_get(hass)
    for legacy_unique_id, key in LEGACY_UNIQUE_IDS.items():
        entity_id = entity_registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, legacy_unique_id
        )
        if not entity_id:
            continue
        new_unique_id = f"{device_id}_{key}"
        if entity_registry.async_get_entity_id(Platform.SENSOR, DOMAIN, new_unique_id):
            # A newer entity already claimed the id; leave the old one alone
            # rather than raising and blocking setup.
            _LOGGER.debug(
                "Skipping migration of %s, target id already taken", entity_id
            )
            continue
        entity_registry.async_update_entity(entity_id, new_unique_id=new_unique_id)
        _LOGGER.debug("Migrated %s to unique id %s", entity_id, new_unique_id)


def _async_register_bridge(
    hass: HomeAssistant,
    entry: ObiEnergyTrackerConfigEntry,
    coordinator: ObiEnergyTrackerCoordinator,
) -> None:
    """Register the bridge so the meter sensor can hang off it via `via_device`."""
    bridge = coordinator.bridge_data
    bridge_id = bridge.get("id") or entry.data.get(CONF_BRIDGE_ID)
    if not bridge_id:
        return

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, bridge_id)},
        manufacturer=MANUFACTURER,
        name=bridge.get("displayName") or bridge.get("label") or "OBI Bridge",
        model="EnergyTracker Bridge",
        sw_version=bridge.get("firmwareVersion"),
        hw_version=bridge.get("hardwareVersion"),
    )


async def _async_update_listener(
    hass: HomeAssistant, entry: ObiEnergyTrackerConfigEntry
) -> None:
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: ObiEnergyTrackerConfigEntry
) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
