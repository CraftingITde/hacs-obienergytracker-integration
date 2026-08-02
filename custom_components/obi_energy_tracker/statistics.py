"""Long-term statistics import for Obi EnergyTracker.

The backend keeps per-day energy totals going back to the day the sensor was
claimed, which is usually long before the integration was installed. Those days
are pushed into Home Assistant as *external* statistics so the Energy Dashboard
shows the full history instead of starting at zero.

The ``hourly`` granularity would be a better fit, but the backend ignores the
requested window there and only ever returns the current hour, so daily buckets
are the finest history available.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import re
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    valid_statistic_id,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import (
    DOMAIN,
    MEASURE_ENERGY,
    MEASURE_NEGATIVE_ENERGY,
    STATISTIC_SUFFIX_ENERGY,
    STATISTIC_SUFFIX_NEGATIVE_ENERGY,
)

_LOGGER = logging.getLogger(__name__)

# measure -> (statistic id suffix, human readable name)
_STATISTICS: dict[str, tuple[str, str]] = {
    MEASURE_ENERGY: (STATISTIC_SUFFIX_ENERGY, "OBI EnergyTracker Bezug"),
    MEASURE_NEGATIVE_ENERGY: (
        STATISTIC_SUFFIX_NEGATIVE_ENERGY,
        "OBI EnergyTracker Einspeisung",
    ),
}


def _slug(value: str) -> str:
    """Reduce a value to the character set statistic ids allow.

    Statistic ids must match ``[\\da-z_]+`` with no leading, trailing or doubled
    underscore, so the dashes of the device UUID have to go.
    """
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.lower())).strip("_")


class ObiStatisticsImporter:
    """Turns daily energy deltas into cumulative external statistics."""

    def __init__(self, hass: HomeAssistant, device_id: str | None) -> None:
        """Initialize the importer."""
        self.hass = hass
        self._device_slug = _slug(device_id or "")
        # statistic_id -> {bucket start -> pushed sum}, so unchanged days are
        # not rewritten on every coordinator refresh.
        self._pushed: dict[str, dict[datetime, float]] = {}

    def statistic_id(self, measure: str) -> str | None:
        """Return the external statistic id for a measure."""
        if not self._device_slug:
            return None
        suffix, _ = _STATISTICS[measure]
        statistic_id = f"{DOMAIN}:{self._device_slug}_{suffix}"
        if not valid_statistic_id(statistic_id):
            _LOGGER.error("Refusing to use invalid statistic id %s", statistic_id)
            return None
        return statistic_id

    async def async_import(self, daily_records: list[dict[str, Any]]) -> None:
        """Import daily records as external statistics."""
        for measure, (_, name) in _STATISTICS.items():
            statistic_id = self.statistic_id(measure)
            if statistic_id is None:
                continue

            points = _daily_points(daily_records, measure)
            if not points:
                continue

            series = await self._async_build_series(statistic_id, points)
            if not series:
                continue

            already = self._pushed.setdefault(statistic_id, {})
            changed = [
                point for point in series if already.get(point["start"]) != point["sum"]
            ]
            if not changed:
                continue

            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=name,
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, metadata, changed)

            for point in changed:
                already[point["start"]] = point["sum"]

            _LOGGER.debug(
                "Imported %d of %d statistic buckets for %s",
                len(changed),
                len(series),
                statistic_id,
            )

    async def _async_build_series(
        self, statistic_id: str, points: list[tuple[datetime, float]]
    ) -> list[StatisticData]:
        """Turn per-day deltas into a cumulative statistics series.

        The backend returns the energy *for* each day, not a meter reading, so
        the running total has to be accumulated here. Restarting that total at
        zero on every run would be wrong as soon as the requested window stops
        reaching back to the very first day — every bucket would then be
        rewritten with a smaller sum, quietly eroding the recorded history. So
        the total is anchored on whatever the recorder already holds and only
        the tail is rebuilt.
        """
        # Two rows: the last bucket is the one to rebuild, the one before it
        # carries the total to continue from. Deriving the baseline by
        # subtracting the current delta from the last sum would be wrong exactly
        # when it matters — the stored sum was computed from the *previous*
        # value of a day that is still growing.
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 2, statistic_id, True, {"sum"}
        )
        rows = sorted((last or {}).get(statistic_id) or [], key=lambda r: r["start"])

        if not rows:
            # Nothing recorded yet: import everything, preceded by a zero anchor
            # so the Energy Dashboard, which reports differences between
            # buckets, does not swallow the very first day.
            return _accumulate(
                points,
                baseline=0.0,
                anchor=StatisticData(
                    start=points[0][0] - timedelta(days=1), state=0.0, sum=0.0
                ),
            )

        last_start = dt_util.utc_from_timestamp(rows[-1]["start"])

        if any(start == last_start for start, _ in points):
            baseline = float(rows[-2].get("sum") or 0.0) if len(rows) > 1 else 0.0
            return _accumulate(
                [p for p in points if p[0] >= last_start], baseline=baseline
            )

        # The last recorded bucket is no longer in the API window, so nothing
        # already stored may be touched — continue strictly after it.
        return _accumulate(
            [p for p in points if p[0] > last_start],
            baseline=float(rows[-1].get("sum") or 0.0),
        )


def _accumulate(
    points: list[tuple[datetime, float]],
    *,
    baseline: float,
    anchor: StatisticData | None = None,
) -> list[StatisticData]:
    """Build cumulative statistic buckets from per-day deltas in Wh."""
    series: list[StatisticData] = [anchor] if anchor else []
    running = baseline
    for start, value in points:
        # Values arrive in Wh; statistics are recorded in kWh.
        running += value / 1000
        series.append(StatisticData(start=start, state=running, sum=running))
    return series


def _daily_points(
    daily_records: list[dict[str, Any]], measure: str
) -> list[tuple[datetime, float]]:
    """Return sorted ``(bucket start, value in Wh)`` pairs for one measure."""
    points: list[tuple[datetime, float]] = []
    for record in daily_records:
        if record.get("measure") != measure:
            continue
        value = record.get("value")
        raw_time = record.get("time")
        if not isinstance(value, (int, float)) or not raw_time:
            continue
        start = dt_util.parse_datetime(raw_time)
        if start is None:
            _LOGGER.debug("Skipping record with unparsable time: %s", raw_time)
            continue
        points.append((dt_util.as_utc(start), float(value)))

    points.sort(key=lambda item: item[0])
    return points
