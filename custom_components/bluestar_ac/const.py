"""Constants for the Bluestar AC integration."""
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
)

DOMAIN = "bluestar_ac"

# Configuration keys
CONF_AUTH_ID = "auth_id"
CONF_AUTH_TYPE = "auth_type"
DEFAULT_AUTH_TYPE = "bluestar"

# Device info
MANUFACTURER = "Bluestar"
MODEL = "Smart AC"

# Supported features
SUPPORT_TARGET_TEMPERATURE = 1
SUPPORT_FAN_MODE = 2
SUPPORT_SWING_MODE = 4
SUPPORT_PRESET_MODE = 8

# Temperature range
MIN_TEMP = 16
MAX_TEMP = 30

# Fan modes
FAN_MODES = ["auto", "low", "medium", "high"]

# Swing modes
SWING_MODES = ["off", "horizontal", "vertical", "both"]

# Swing options for select entities
SWING_OPTIONS = [
    {"label": "Off", "value": 0},
    {"label": "On", "value": 1},
]

# Swing value to label mapping
SWING_VALUE_TO_LABEL = {
    0: "Off",
    1: "On",
}

# Swing label to value mapping
SWING_LABEL_TO_VALUE = {
    "Off": 0,
    "On": 1,
}

# Preset modes
PRESET_MODES = ["none", "eco", "turbo", "sleep"]

# HVAC modes
HVAC_MODES = ["off", "auto", "cool", "dry", "fan"]

# Default values
DEFAULT_TEMPERATURE = 24
DEFAULT_FAN_MODE = "auto"
DEFAULT_SWING_MODE = "off"
DEFAULT_PRESET_MODE = "none"

# API Configuration
DEFAULT_BASE_URL = "https://n3on22cp53.execute-api.ap-south-1.amazonaws.com/prod"
LOGIN_ENDPOINT = "/auth/login"
DEVICES_ENDPOINT = "/things"
CONTROL_ENDPOINT = "/things/{device_id}/control"
PREFERENCES_ENDPOINT = "/things/{device_id}/preferences"
STATE_ENDPOINT = "/things/{device_id}/state"

# Update interval
DEFAULT_SCAN_INTERVAL = 30

