"""Constants for the obienergytracker integration."""

DOMAIN = "obi_energy_tracker"

# Config constants
CONF_COUNTRY = "country"
CONF_BRIDGE_ID = "bridge_id"
CONF_DEVICE_ID = "device_id"
CONF_SCAN_INTERVAL = "scan_interval"

# Default values
DEFAULT_COUNTRY = "DE"
DEFAULT_SCAN_INTERVAL = 300  # 5 minutes
MIN_SCAN_INTERVAL = 30  # seconds

# The bridge uploads a new reading every `uploadInterval` seconds (300 by
# default), so polling faster than that only produces duplicate values.
DEFAULT_UPLOAD_INTERVAL = 300

# Data attributes
ATTR_BRIDGE_ID = "bridge_id"
ATTR_DEVICE_ID = "device_id"

# Coordinator data keys
DATA_METER = "meter"
DATA_PROFILE = "profile"
DATA_DAILY = "daily"

# Measures supported by the backend. Anything else is rejected with
# "Invalid Measure Name!" — verified against the live API.
MEASURE_ENERGY = "energy"
MEASURE_NEGATIVE_ENERGY = "negative_energy"
SUPPORTED_MEASURES = (MEASURE_ENERGY, MEASURE_NEGATIVE_ENERGY)

# How far back daily history is requested for the statistics backfill. The
# backend clamps this to the sensor's `dataVisibleSince` anyway.
STATISTICS_BACKFILL_DAYS = 730

# Suffixes for the external statistic ids. The device id is prefixed at runtime
# so two accounts (or two meters) never write into the same statistic.
STATISTIC_SUFFIX_ENERGY = "energy_import"
STATISTIC_SUFFIX_NEGATIVE_ENERGY = "energy_export"
