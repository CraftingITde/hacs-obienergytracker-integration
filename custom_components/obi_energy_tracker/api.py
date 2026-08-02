"""API client for Obi EnergyTracker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession
import jwt

from .const import SUPPORTED_MEASURES

_LOGGER = logging.getLogger(__name__)

# API endpoints
LOGIN_URL = "https://www.obi.de/regi/auth/api/public/login"
ENERGY_TRACKING_URL = "https://energy-tracking-backend.prod-eks.dbs.obi.solutions"

# The meter endpoint returns every reading inside the requested window; six
# hours is generous enough to still contain a value after a short bridge outage.
METER_WINDOW_HOURS = 6

ACCEPT_USER = "application/vnd.obi.companion.energy-tracking.user.v1+json"
ACCEPT_RECORD = (
    "application/vnd.obi.companion.energy-tracking.historical-record.v1+json"
)


class ObiEnergyTrackerError(Exception):
    """Base error for the Obi EnergyTracker API."""


class ObiAuthError(ObiEnergyTrackerError):
    """Raised when the backend rejects the credentials or the token."""


class ObiConnectionError(ObiEnergyTrackerError):
    """Raised when the backend could not be reached or answered unexpectedly."""


def _utc_window(hours: int) -> str:
    """Return an ISO 8601 interval covering the last ``hours`` hours.

    The backend timestamps everything in real UTC (verified against live meter
    records), so the window has to be built from UTC as well. Using local time
    with a 'Z' suffix shifts the window by the local offset, which returns stale
    data west of UTC and no data at all from UTC+6 eastwards.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return f"{start_str}/PT{hours}H"


class ObiEnergyTrackerAPI:
    """API client for Obi EnergyTracker."""

    def __init__(
        self,
        session: ClientSession,
        email: str,
        password: str,
        country: str = "DE",
        bridge_id: str | None = None,
        device_id: str | None = None,
    ) -> None:
        """Initialize the API client."""
        self.session = session
        self.email = email
        self.password = password
        self.country = country
        self.token: str | None = None
        self.bridge_id = bridge_id
        self.device_id = device_id

    async def async_login(self) -> bool:
        """Authenticate with the Obi EnergyTracker API.

        Raises:
            ObiAuthError: the credentials were rejected.
            ObiConnectionError: the backend could not be reached.
        """
        payload = {
            "email": self.email,
            "password": self.password,
            "country": self.country,
        }
        headers = {
            "Accept-Encoding": "gzip",
            "Connection": "Keep-Alive",
            "Content-Type": "application/json",
            "x-app-type": "b2c",
            "x-obi-locale": "de-DE",
            "User-Agent": "heyOBI APP / Android Phone 30",
        }

        try:
            async with self.session.post(
                LOGIN_URL, json=payload, headers=headers
            ) as response:
                if response.status in (400, 401, 403):
                    raise ObiAuthError(
                        f"Credentials rejected (HTTP {response.status})"
                    )
                if response.status != 200:
                    raise ObiConnectionError(
                        f"Unexpected login response (HTTP {response.status})"
                    )
                data = await response.json()
        except (OSError, ClientError) as err:
            raise ObiConnectionError(
                f"Could not reach the login service: {err}"
            ) from err

        self.token = data.get("token")
        if not self.token:
            raise ObiAuthError("No token received from login response")

        _LOGGER.debug("Successfully authenticated with Obi EnergyTracker")
        return True

    @property
    def account_id(self) -> str | None:
        """Return the account id encoded in the current token."""
        if not self.token:
            return None
        try:
            return jwt.decode(self.token, options={"verify_signature": False}).get(
                "accountId"
            )
        except jwt.DecodeError:
            _LOGGER.error("Token is not a valid JWT")
            return None

    async def _async_get(
        self,
        url: str,
        *,
        accept: str,
        params: dict[str, str] | None = None,
        _retry: bool = True,
    ) -> Any:
        """GET a resource, re-authenticating once if the token was rejected.

        The token is valid for months, but it can be revoked server side (for
        example after a password change), so a single silent re-login keeps the
        integration alive instead of returning None until Home Assistant restarts.
        """
        if not self.token:
            await self.async_login()

        headers = {
            "Accept": accept,
            "Accept-Encoding": "gzip",
            "User-Agent": "app_client",
            "Authorization": f"Bearer {self.token}",
            "Connection": "Keep-Alive",
        }

        try:
            async with self.session.get(
                url, params=params, headers=headers
            ) as response:
                if response.status in (401, 403):
                    if _retry:
                        _LOGGER.debug("Token rejected, re-authenticating")
                        self.token = None
                        await self.async_login()
                        return await self._async_get(
                            url, accept=accept, params=params, _retry=False
                        )
                    raise ObiAuthError(
                        f"Still unauthorized after re-login (HTTP {response.status})"
                    )
                return await self._async_parse(response, url)
        except (OSError, ClientError) as err:
            raise ObiConnectionError(f"Request to {url} failed: {err}") from err

    @staticmethod
    async def _async_parse(response: ClientResponse, url: str) -> Any:
        """Return the decoded body of a successful response."""
        if response.status != 200:
            raise ObiConnectionError(
                f"Unexpected response from {url} (HTTP {response.status})"
            )
        return await response.json()

    async def async_get_user_profile(self) -> dict[str, Any]:
        """Return the full user profile including bridge and sensor details."""
        user_id = self.account_id
        if not user_id:
            raise ObiAuthError("No accountId found in token")

        profile = await self._async_get(
            f"{ENERGY_TRACKING_URL}/users/{user_id}", accept=ACCEPT_USER
        )
        if not isinstance(profile, dict):
            raise ObiConnectionError("Unexpected user profile payload")
        return profile

    async def async_get_bridge_info(self) -> dict[str, str] | None:
        """Get bridge and device IDs from the user profile."""
        try:
            profile = await self.async_get_user_profile()
        except ObiEnergyTrackerError as err:
            _LOGGER.error("Error getting bridge info: %s", err)
            return None

        bridge = profile.get("bridge")
        if not bridge:
            _LOGGER.error("No bridge found in user info")
            return None

        self.bridge_id = bridge.get("id")
        sensors = bridge.get("sensors", [])
        if sensors:
            self.device_id = sensors[0].get("id")

        if not self.bridge_id or not self.device_id:
            _LOGGER.error("Could not find bridge_id or device_id")
            return None

        return {"bridge_id": self.bridge_id, "device_id": self.device_id}

    def _historical_url(self, granularity: str) -> str:
        """Build a historical-data URL for the configured bridge/device."""
        if not self.bridge_id or not self.device_id:
            raise ObiConnectionError("bridge_id/device_id are not known yet")
        return (
            f"{ENERGY_TRACKING_URL}/historical-data/"
            f"{self.bridge_id}/{self.device_id}/{granularity}"
        )

    async def async_get_meter_data(self) -> list[dict[str, Any]]:
        """Get meter readings (Zählerstand and Einspeisung) for the last hours."""
        params = {
            "duration": _utc_window(METER_WINDOW_HOURS),
            "measures": ",".join(SUPPORTED_MEASURES),
        }
        data = await self._async_get(
            self._historical_url("meter"), accept=ACCEPT_RECORD, params=params
        )
        records = data if isinstance(data, list) else [data]
        return [r for r in records if isinstance(r, dict)]

    async def async_get_daily_data(self, days: int) -> list[dict[str, Any]]:
        """Get per-day energy totals.

        Each record is the *delta* for that day (not a meter reading), keyed by a
        UTC midnight timestamp. The backend clamps the window to the sensor's
        ``dataVisibleSince``, so asking for more days than available is harmless.

        Note: the ``hourly`` granularity exists but always returns only the
        current hour regardless of the requested window, which is why the daily
        endpoint is used for history instead.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        start_str = start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        params = {
            "duration": f"{start_str}/P{days}D",
            "measures": ",".join(SUPPORTED_MEASURES),
        }
        data = await self._async_get(
            self._historical_url("daily"), accept=ACCEPT_RECORD, params=params
        )
        records = data if isinstance(data, list) else [data]
        return [r for r in records if isinstance(r, dict)]
