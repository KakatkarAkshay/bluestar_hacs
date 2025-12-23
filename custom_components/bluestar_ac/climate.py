"""Climate platform for Bluestar AC."""
import logging
from typing import Any, Optional

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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    MIN_TEMP,
    MAX_TEMP,
    DEFAULT_TEMPERATURE,
)
from .coordinator import BluestarDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Mode mapping from Bluestar API to Home Assistant
# AC only supports: Fan (0), Cool (2), Dry (3), Auto (4)
BLUESTAR_TO_HA_MODE = {
    0: HVACMode.FAN_ONLY,  # Fan
    2: HVACMode.COOL,       # Cool
    3: HVACMode.DRY,        # Dry
    4: HVACMode.AUTO,       # Auto
}

HA_TO_BLUESTAR_MODE = {
    HVACMode.FAN_ONLY: 0,
    HVACMode.COOL: 2,
    HVACMode.DRY: 3,
    HVACMode.AUTO: 4,
}

# Fan speed mapping
BLUESTAR_TO_HA_FAN = {
    2: "low",
    3: "medium",
    4: "high",
    6: "turbo",
    7: "auto",
}

HA_TO_BLUESTAR_FAN = {v: k for k, v in BLUESTAR_TO_HA_FAN.items()}

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Bluestar AC climate platform."""
    coordinator: BluestarDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    device_id = config_entry.data[CONF_DEVICE_ID]
    _LOGGER.info(f"Setting up climate entity for device: {device_id}")
    
    entity = BluestarACClimate(coordinator, device_id)
    async_add_entities([entity], True)

class BluestarACClimate(CoordinatorEntity, ClimateEntity):
    """Representation of a Bluestar AC climate entity."""
    
    def __init__(self, coordinator: BluestarDataUpdateCoordinator, device_id: str):
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"bluestar_ac_{device_id}"
        self._previous_temperature: Optional[float] = None
        self._previous_fan_mode: Optional[int] = None
        
        # Set supported features
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE |
            ClimateEntityFeature.FAN_MODE |
            ClimateEntityFeature.SWING_MODE |
            ClimateEntityFeature.PRESET_MODE
        )
        
        # Set temperature unit and step
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_min_temp = MIN_TEMP
        self._attr_max_temp = MAX_TEMP
        self._attr_target_temperature_step = 1
        
        # Set available modes (only Off and Cool)
        self._attr_hvac_modes = [
            HVACMode.OFF,
            HVACMode.COOL,
        ]
        self._attr_fan_modes = ["auto", "low", "medium", "high", "turbo"]
        self._attr_swing_modes = ["off", "horizontal", "vertical", "both"]
        self._attr_preset_modes = ["none", "eco", "turbo", "sleep"]
        
    @property
    def device_info(self):
        """Return device information."""
        device = self.coordinator.get_device(self._device_id)
        if device:
            return {
                "identifiers": {(DOMAIN, self._device_id)},
                "name": device.get("name", f"Bluestar AC {self._device_id}"),
                "manufacturer": "Bluestar",
                "model": "Smart AC",
            }
        return None
    
    @property
    def name(self) -> str:
        """Return the name of the entity."""
        device = self.coordinator.get_device(self._device_id)
        if device:
            return device.get("name", f"Bluestar AC {self._device_id}")
        return f"Bluestar AC {self._device_id}"
    
    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not self.coordinator.data:
            return False
        device = self.coordinator.get_device(self._device_id)
        if device:
            return device.get("state", {}).get("connected", False)
        return False
    
    @property
    def current_temperature(self) -> Optional[float]:
        """Return the current temperature."""
        device = self.coordinator.get_device(self._device_id)
        if device:
            state = device.get("state", {})
            current_temp = state.get("current_temp")
            if current_temp:
                try:
                    return float(current_temp)
                except (ValueError, TypeError):
                    pass
        return None
    
    @property
    def target_temperature(self) -> Optional[float]:
        """Return the target temperature."""
        device = self.coordinator.get_device(self._device_id)
        if device:
            state = device.get("state", {})
            target_temp = state.get("temperature")
            if target_temp:
                try:
                    return float(target_temp)
                except (ValueError, TypeError):
                    pass
        return DEFAULT_TEMPERATURE
    
    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC mode."""
        device = self.coordinator.get_device(self._device_id)
        if device:
            state = device.get("state", {})
            power = state.get("power", False)
            if not power:
                return HVACMode.OFF
            mode = state.get("mode", 2)
            # Mode 1 doesn't exist on this AC, default to Cool if we see it
            if mode == 1:
                mode = 2
            # Only support OFF and COOL - map all other modes (Fan, Dry, Auto) to COOL
            ha_mode = BLUESTAR_TO_HA_MODE.get(mode, HVACMode.COOL)
            if ha_mode not in [HVACMode.OFF, HVACMode.COOL]:
                return HVACMode.COOL
            return ha_mode
        return HVACMode.OFF
    
    @property
    def fan_mode(self) -> Optional[str]:
        """Return current fan mode."""
        device = self.coordinator.get_device(self._device_id)
        if device:
            state = device.get("state", {})
            fan_speed = state.get("fan_speed", 7)
            return BLUESTAR_TO_HA_FAN.get(fan_speed, "auto")
        return "auto"
    
    @property
    def swing_mode(self) -> Optional[str]:
        """Return current swing mode."""
        device = self.coordinator.get_device(self._device_id)
        if device:
            state = device.get("state", {})
            hswing = state.get("horizontal_swing", 1)
            vswing = state.get("vertical_swing", 1)
            # Device uses inverted values: 0=ON, 1=OFF
            hswing_on = hswing == 0
            vswing_on = vswing == 0
            if hswing_on and vswing_on:
                return "both"
            elif hswing_on:
                return "horizontal"
            elif vswing_on:
                return "vertical"
        return "off"
    
    @property
    def preset_mode(self) -> Optional[str]:
        """Return current preset mode."""
        device = self.coordinator.get_device(self._device_id)
        if device:
            state = device.get("state", {})
            turbo = state.get("turbo", 0)
            esave = state.get("esave", 0)
            sleep = state.get("sleep", 0)
            
            if turbo == 3:
                return "turbo"
            elif esave == 1:
                return "eco"
            elif sleep == 1:
                return "sleep"
        return "none"
    
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        _LOGGER.warning(f"🌡️ Setting temperature: {kwargs}")
        try:
            if ATTR_TEMPERATURE in kwargs:
                temperature = round(kwargs[ATTR_TEMPERATURE])
                await self.coordinator.set_temperature(self._device_id, temperature)
        except Exception as e:
            _LOGGER.error(f"❌ set_temperature failed: {e}", exc_info=True)
            raise
    
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        _LOGGER.warning(f"❄️ Setting HVAC mode: {hvac_mode}")
        try:
            if hvac_mode == HVACMode.OFF:
                await self.coordinator.set_power(self._device_id, False)
            elif hvac_mode == HVACMode.COOL:
                # Only support COOL mode (Bluestar mode 2)
                await self.coordinator.control_device(self._device_id, {"pow": 1, "mode": 2})
            else:
                # If somehow an unsupported mode is requested, default to COOL
                _LOGGER.warning(f"⚠️ Unsupported mode {hvac_mode} requested, defaulting to COOL")
                await self.coordinator.control_device(self._device_id, {"pow": 1, "mode": 2})
        except Exception as e:
            _LOGGER.error(f"❌ set_hvac_mode failed: {e}", exc_info=True)
            raise
    
    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode."""
        _LOGGER.warning(f"🌀 Setting fan mode: {fan_mode}")
        try:
            bluestar_fan = HA_TO_BLUESTAR_FAN.get(fan_mode, 7)
            await self.coordinator.set_fan_mode(self._device_id, bluestar_fan)
        except Exception as e:
            _LOGGER.error(f"❌ set_fan_mode failed: {e}", exc_info=True)
            raise
    
    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set new target swing mode."""
        _LOGGER.warning(f"🔄 Setting swing mode: {swing_mode}")
        try:
            # Device uses inverted values: 0=ON, 1=OFF
            control_data = {}
            if swing_mode == "off":
                control_data = {"hswing": 1, "vswing": 1}
            elif swing_mode == "horizontal":
                control_data = {"hswing": 0, "vswing": 1}
            elif swing_mode == "vertical":
                control_data = {"hswing": 1, "vswing": 0}
            elif swing_mode == "both":
                control_data = {"hswing": 0, "vswing": 0}
            
            if control_data:
                await self.coordinator.control_device(self._device_id, control_data)
        except Exception as e:
            _LOGGER.error(f"❌ set_swing_mode failed: {e}", exc_info=True)
            raise
    
    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        _LOGGER.warning(f"🎯 Setting preset mode: {preset_mode}")
        try:
            control_data = {
                "esave": 0,
                "turbo": 0,
                "sleep": 0,
            }
            
            current_preset = self.preset_mode
            
            if preset_mode in ["eco", "turbo"]:
                if current_preset == "none" or current_preset is None:
                    device = self.coordinator.get_device(self._device_id)
                    if device:
                        state = device.get("state", {})
                        current_temp = state.get("temperature")
                        current_fan = state.get("fan_speed", 7)
                        
                        if current_temp:
                            try:
                                self._previous_temperature = float(current_temp)
                            except (ValueError, TypeError):
                                pass
                        self._previous_fan_mode = current_fan
                        _LOGGER.warning(f"🎯 Stored previous temperature: {self._previous_temperature}, fan_mode: {self._previous_fan_mode}")
            
            if preset_mode == "eco":
                control_data["esave"] = 1
            elif preset_mode == "turbo":
                control_data["turbo"] = 3
            elif preset_mode == "sleep":
                control_data["sleep"] = 1
            elif preset_mode == "none":
                if self._previous_temperature is not None:
                    control_data["stemp"] = f"{self._previous_temperature:.1f}"
                    _LOGGER.warning(f"🎯 Restoring previous temperature: {self._previous_temperature}")
                    self._previous_temperature = None
                if self._previous_fan_mode is not None:
                    control_data["fspd"] = self._previous_fan_mode
                    _LOGGER.warning(f"🎯 Restoring previous fan_mode: {self._previous_fan_mode}")
                    self._previous_fan_mode = None
            else:
                _LOGGER.warning(f"⚠️ Unknown preset mode: {preset_mode}")
                return
            
            await self.coordinator.control_device(self._device_id, control_data)
        except Exception as e:
            _LOGGER.error(f"❌ set_preset_mode failed: {e}", exc_info=True)
            raise
    
