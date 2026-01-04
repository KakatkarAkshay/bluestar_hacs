"""Climate platform for Bluestar Smart AC integration."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_DEVICE_ID,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    MANUFACTURER,
    MODEL,
    MIN_TEMP,
    MAX_TEMP,
    FAN_MODES,
)
from .coordinator import BluestarDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Map HVAC modes to Bluestar mode values
HVAC_MODE_TO_BLUESTAR = {
    HVACMode.AUTO: 0,
    HVACMode.COOL: 2,
    HVACMode.DRY: 3,
    HVACMode.FAN_ONLY: 4,
}

# Map Bluestar mode values to HVAC modes
BLUESTAR_TO_HVAC_MODE = {
    0: HVACMode.AUTO,
    2: HVACMode.COOL,
    3: HVACMode.DRY,
    4: HVACMode.FAN_ONLY,
}

# Map fan modes to Bluestar values
FAN_MODE_TO_BLUESTAR = {
    "auto": 0,
    "low": 2,
    "medium": 3,
    "high": 5,
}

# Map Bluestar values to fan modes
BLUESTAR_TO_FAN_MODE = {
    0: "auto",
    2: "low",
    3: "medium",
    5: "high",
}

# Default fan speed and temperature per mode
DEFAULT_MODE_SETTINGS = {
    HVACMode.AUTO: {"fan_speed": 0, "temperature": 24.0},    # auto fan
    HVACMode.COOL: {"fan_speed": 3, "temperature": 24.0},    # medium fan
    HVACMode.DRY: {"fan_speed": 2, "temperature": 24.0},     # low fan
    HVACMode.FAN_ONLY: {"fan_speed": 3, "temperature": 24.0}, # medium fan
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Bluestar AC climate entities."""
    coordinator: BluestarDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
    
    device = coordinator.get_device(device_id)
    if device:
        async_add_entities([BluestarClimateEntity(coordinator, device_id, entry)])
        _LOGGER.info(f"✅ Added climate entity for device: {device_id}")
    else:
        _LOGGER.error(f"❌ Device {device_id} not found in coordinator data")


class BluestarClimateEntity(CoordinatorEntity, ClimateEntity, RestoreEntity):
    """Bluestar AC climate entity."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1.0
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.COOL, HVACMode.DRY, HVACMode.FAN_ONLY]
    _attr_fan_modes = FAN_MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator: BluestarDataUpdateCoordinator,
        device_id: str,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._entry = entry
        self._attr_unique_id = f"{device_id}_climate"
        
        # Get device info
        device = coordinator.get_device(device_id)
        device_name = device.get("name", "AC") if device else "AC"
        
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": device_name,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
        }
        
        # Local state storage - persisted across restarts
        # Stores fan_speed and temperature per HVAC mode
        self._mode_settings: dict[str, dict[str, Any]] = {}
        
        # Current locally tracked state (used when MQTT state is stale/unavailable)
        self._local_power: bool | None = None
        self._local_mode: int | None = None
        self._local_fan_speed: int | None = None
        self._local_temperature: float | None = None
        
        # Last known mode before turning off (to restore on turn_on)
        self._last_active_mode: HVACMode = HVACMode.COOL
        
        _LOGGER.info(f"🌡️ Created climate entity for {device_name} ({device_id})")

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        
        # Restore previous state
        if (last_state := await self.async_get_last_state()) is not None:
            _LOGGER.info(f"🔄 Restoring state for {self._device_id}: {last_state.state}")
            
            # Restore mode settings from attributes
            if last_state.attributes:
                attrs = last_state.attributes
                
                # Restore per-mode settings
                if "mode_settings" in attrs:
                    try:
                        self._mode_settings = attrs["mode_settings"]
                        _LOGGER.info(f"📦 Restored mode settings: {self._mode_settings}")
                    except Exception as e:
                        _LOGGER.warning(f"⚠️ Could not restore mode settings: {e}")
                
                # Restore last active mode
                if "last_active_mode" in attrs:
                    try:
                        self._last_active_mode = HVACMode(attrs["last_active_mode"])
                    except ValueError:
                        self._last_active_mode = HVACMode.COOL
                
                # Restore local state
                if "local_fan_speed" in attrs:
                    self._local_fan_speed = attrs["local_fan_speed"]
                if "local_temperature" in attrs:
                    self._local_temperature = attrs["local_temperature"]
        
        # Initialize mode settings with defaults if not restored
        for mode in [HVACMode.AUTO, HVACMode.COOL, HVACMode.DRY, HVACMode.FAN_ONLY]:
            if mode.value not in self._mode_settings:
                defaults = DEFAULT_MODE_SETTINGS.get(mode, {"fan_speed": 3, "temperature": 24.0})
                self._mode_settings[mode.value] = defaults.copy()
        
        # Sync local state with device state
        self._sync_local_state_from_device()

    def _sync_local_state_from_device(self) -> None:
        """Sync local state from device state."""
        state = self._get_device_state()
        if state:
            # Update local state from device
            if self._local_power is None:
                self._local_power = state.get("power", False)
            if self._local_mode is None:
                self._local_mode = state.get("mode", 2)
            if self._local_fan_speed is None:
                self._local_fan_speed = state.get("fan_speed", 3)
            if self._local_temperature is None:
                temp = state.get("temperature", "24")
                try:
                    self._local_temperature = float(temp)
                except (ValueError, TypeError):
                    self._local_temperature = 24.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes for persistence."""
        return {
            "mode_settings": self._mode_settings,
            "last_active_mode": self._last_active_mode.value if self._last_active_mode else HVACMode.COOL.value,
            "local_fan_speed": self._local_fan_speed,
            "local_temperature": self._local_temperature,
            "device_id": self._device_id,
        }

    def _get_device_state(self) -> dict[str, Any] | None:
        """Get current device state from coordinator."""
        device = self.coordinator.get_device(self._device_id)
        return device.get("state") if device else None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        device = self.coordinator.get_device(self._device_id)
        if not device:
            return False
        state = device.get("state", {})
        return state.get("connected", False)

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC mode."""
        state = self._get_device_state()
        if not state:
            return HVACMode.OFF
        
        power = state.get("power", False)
        if not power:
            return HVACMode.OFF
        
        mode = state.get("mode", 2)
        return BLUESTAR_TO_HVAC_MODE.get(mode, HVACMode.COOL)

    @property
    def current_temperature(self) -> float | None:
        """Return current temperature."""
        state = self._get_device_state()
        if not state:
            return None
        
        current_temp = state.get("current_temp", "27.5")
        try:
            return float(current_temp)
        except (ValueError, TypeError):
            return None

    @property
    def target_temperature(self) -> float | None:
        """Return target temperature."""
        state = self._get_device_state()
        if not state:
            return self._local_temperature or 24.0
        
        temp = state.get("temperature", "24")
        try:
            device_temp = float(temp)
            # Update local state
            self._local_temperature = device_temp
            return device_temp
        except (ValueError, TypeError):
            return self._local_temperature or 24.0

    @property
    def fan_mode(self) -> str | None:
        """Return current fan mode."""
        state = self._get_device_state()
        if not state:
            return BLUESTAR_TO_FAN_MODE.get(self._local_fan_speed, "medium")
        
        fan_speed = state.get("fan_speed", 3)
        # Update local state
        self._local_fan_speed = fan_speed
        return BLUESTAR_TO_FAN_MODE.get(fan_speed, "medium")

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode."""
        _LOGGER.warning(f"❄️ Setting HVAC mode: {hvac_mode}")
        
        if hvac_mode == HVACMode.OFF:
            # Turn off - just send power off
            await self.coordinator.set_power(self._device_id, False)
            self._local_power = False
        else:
            # Save current mode settings before switching
            current_hvac_mode = self.hvac_mode
            if current_hvac_mode != HVACMode.OFF:
                self._save_current_mode_settings(current_hvac_mode)
                self._last_active_mode = current_hvac_mode
            
            # Get saved settings for the new mode
            mode_settings = self._get_mode_settings(hvac_mode)
            fan_speed = mode_settings.get("fan_speed", self._local_fan_speed or 3)
            temperature = mode_settings.get("temperature", self._local_temperature or 24.0)
            
            # Build mode payload - KEEP THIS EXACT FORMAT AS REQUIRED
            bluestar_mode = HVAC_MODE_TO_BLUESTAR.get(hvac_mode, 2)
            mode_data = {
                "pow": 1,
                "mode": {
                    "value": bluestar_mode,
                    "fspd": fan_speed,
                    "stemp": f"{float(temperature):.1f}",
                }
            }
            
            _LOGGER.warning(f"📤 Mode control data: {mode_data}")
            
            await self.coordinator.control_device(self._device_id, mode_data)
            
            # Update local state
            self._local_power = True
            self._local_mode = bluestar_mode
            self._local_fan_speed = fan_speed
            self._local_temperature = temperature
            self._last_active_mode = hvac_mode
        
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn on the AC."""
        _LOGGER.warning(f"🔌 Turning ON AC - restoring mode: {self._last_active_mode}")
        
        # Turn on with last active mode
        await self.async_set_hvac_mode(self._last_active_mode or HVACMode.COOL)

    async def async_turn_off(self) -> None:
        """Turn off the AC."""
        _LOGGER.warning(f"🔌 Turning OFF AC")
        
        # Save current settings before turning off
        current_mode = self.hvac_mode
        if current_mode != HVACMode.OFF:
            self._save_current_mode_settings(current_mode)
            self._last_active_mode = current_mode
        
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        
        _LOGGER.warning(f"🌡️ Setting temperature: {temperature}")
        
        await self.coordinator.set_temperature(self._device_id, temperature)
        
        # Update local state
        self._local_temperature = float(temperature)
        
        # Save to current mode settings
        current_mode = self.hvac_mode
        if current_mode != HVACMode.OFF:
            if current_mode.value not in self._mode_settings:
                self._mode_settings[current_mode.value] = {}
            self._mode_settings[current_mode.value]["temperature"] = float(temperature)
        
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan mode."""
        _LOGGER.warning(f"🌀 Setting fan mode: {fan_mode}")
        
        bluestar_fan = FAN_MODE_TO_BLUESTAR.get(fan_mode, 3)
        await self.coordinator.set_fan_mode(self._device_id, bluestar_fan)
        
        # Update local state
        self._local_fan_speed = bluestar_fan
        
        # Save to current mode settings
        current_mode = self.hvac_mode
        if current_mode != HVACMode.OFF:
            if current_mode.value not in self._mode_settings:
                self._mode_settings[current_mode.value] = {}
            self._mode_settings[current_mode.value]["fan_speed"] = bluestar_fan
        
        self.async_write_ha_state()

    def _save_current_mode_settings(self, hvac_mode: HVACMode) -> None:
        """Save current fan speed and temperature for the given mode."""
        if hvac_mode == HVACMode.OFF:
            return
        
        state = self._get_device_state()
        if not state:
            return
        
        fan_speed = self._local_fan_speed or state.get("fan_speed", 3)
        temp = self._local_temperature
        if temp is None:
            try:
                temp = float(state.get("temperature", "24"))
            except (ValueError, TypeError):
                temp = 24.0
        
        if hvac_mode.value not in self._mode_settings:
            self._mode_settings[hvac_mode.value] = {}
        
        self._mode_settings[hvac_mode.value]["fan_speed"] = fan_speed
        self._mode_settings[hvac_mode.value]["temperature"] = temp
        
        _LOGGER.debug(f"💾 Saved settings for {hvac_mode}: fan={fan_speed}, temp={temp}")

    def _get_mode_settings(self, hvac_mode: HVACMode) -> dict[str, Any]:
        """Get saved settings for a mode, or defaults."""
        if hvac_mode.value in self._mode_settings:
            return self._mode_settings[hvac_mode.value]
        
        # Return defaults
        defaults = DEFAULT_MODE_SETTINGS.get(hvac_mode, {"fan_speed": 3, "temperature": 24.0})
        return defaults.copy()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Sync local state from device when we get updates
        state = self._get_device_state()
        if state:
            # Update local tracking with device state
            device_power = state.get("power", False)
            device_mode = state.get("mode", 2)
            device_fan = state.get("fan_speed", 3)
            
            try:
                device_temp = float(state.get("temperature", "24"))
            except (ValueError, TypeError):
                device_temp = 24.0
            
            # Only update if device reports it's on (to avoid stale state)
            if device_power:
                self._local_power = True
                self._local_mode = device_mode
                self._local_fan_speed = device_fan
                self._local_temperature = device_temp
                
                # Update mode settings with current values
                hvac_mode = BLUESTAR_TO_HVAC_MODE.get(device_mode, HVACMode.COOL)
                if hvac_mode.value not in self._mode_settings:
                    self._mode_settings[hvac_mode.value] = {}
                self._mode_settings[hvac_mode.value]["fan_speed"] = device_fan
                self._mode_settings[hvac_mode.value]["temperature"] = device_temp
            else:
                self._local_power = False
        
        self.async_write_ha_state()

