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

# Data attributes
ATTR_BRIDGE_ID = "bridge_id"
ATTR_DEVICE_ID = "device_id"
