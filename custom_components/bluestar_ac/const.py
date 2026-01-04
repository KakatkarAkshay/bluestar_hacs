"""Constants for the Bluestar AC integration."""

DOMAIN = "bluestar_ac"

# Device info
MANUFACTURER = "Bluestar"
MODEL = "Smart AC"

# Temperature range
MIN_TEMP = 16
MAX_TEMP = 30

# Fan modes
FAN_MODES = ["auto", "low", "medium", "high", "turbo"]

# API Configuration
DEFAULT_BASE_URL = "https://n3on22cp53.execute-api.ap-south-1.amazonaws.com/prod"
LOGIN_ENDPOINT = "/auth/login"
DEVICES_ENDPOINT = "/things"

# Update interval
DEFAULT_SCAN_INTERVAL = 30
