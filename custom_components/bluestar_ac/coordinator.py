"""Data update coordinator for Bluestar Smart AC integration."""

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BluestarAPI, BluestarAPIError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class BluestarDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Bluestar API."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: BluestarAPI,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ):
        """Initialize the coordinator."""
        self.api = api
        self.devices: Dict[str, Any] = {}
        self._mqtt_subscribed = False
        self._last_mqtt_client = None  # Track MQTT client instance to detect reinit
        

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        
        # Set up MQTT message callback if MQTT is available
        self._setup_mqtt_callback()

    def _setup_mqtt_callback(self) -> None:
        """Set up MQTT message callback on the current MQTT client."""
        if self.api.mqtt_client:
            self.api.mqtt_client.set_message_callback(self._handle_mqtt_message)
            self._last_mqtt_client = self.api.mqtt_client
            _LOGGER.debug("📡 MQTT message callback configured")

    async def _async_update_data(self) -> Dict[str, Any]:
        """Update data via library."""
        _LOGGER.debug("C1 coordinator _async_update_data() start")
        try:
            # Ensure we're logged in before making requests
            if not self.api.session_token:
                _LOGGER.debug("C2 re-logging in to API")
                await self.api.login()
                # Re-setup MQTT callback after login creates new client
                self._setup_mqtt_callback()
                self._mqtt_subscribed = False  # Force resubscribe after new client
            
            # Check if MQTT client changed (due to relogin)
            if self.api.mqtt_client and self.api.mqtt_client != self._last_mqtt_client:
                _LOGGER.info("🔄 MQTT client changed, reconfiguring callback...")
                self._setup_mqtt_callback()
                self._mqtt_subscribed = False  # Force resubscribe
            
            _LOGGER.debug("C3 fetching devices from API")
            # Get device list only - state comes from MQTT
            data = await self.api.get_devices()
            
            _LOGGER.debug("C4 processing device data")
            # Extract device info only (ID, name) - NOT state (HTTP API state is unreliable)
            self.devices = {device["thing_id"]: device for device in data.get("things", [])}
            
            # Process device data for easier access
            # HTTP API is only used for device info (ID, name) - NOT for state
            # All state comes from MQTT which is reliable
            processed_devices = {}
            for device_id, device in self.devices.items():
                # Check if we already have state from MQTT for this device
                existing_state = {}
                if self.data and device_id in self.data.get("devices", {}):
                    existing_state = self.data["devices"][device_id].get("state", {})
                
                processed_devices[device_id] = {
                    "id": device_id,
                    "name": device.get("user_config", {}).get("name", "AC"),
                    "type": "ac",
                    "state": existing_state if existing_state else {
                        # Default state only used before first MQTT update
                        "power": False,
                        "mode": 2,
                        "temperature": "24",
                        "current_temp": "27.5",
                        "fan_speed": 2,
                        "vertical_swing": 0,
                        "horizontal_swing": 0,
                        "display": True,
                        "esave": 0,
                        "turbo": 0,
                        "sleep": 0,
                        "connected": False,
                        "rssi": -45,
                        "error": 0,
                        "source": "unknown",
                        "timestamp": 0,
                    },
                    "raw_device": device,
                }
            
            _LOGGER.debug("C5 coordinator got %d devices: %s", len(processed_devices), str(list(processed_devices.keys()))[:200])
            
            # Subscribe to MQTT state reports for all devices
            if not self._mqtt_subscribed and self.api.mqtt_client:
                device_ids = list(processed_devices.keys())
                try:
                    await self.api.subscribe_to_devices(device_ids)
                    self._mqtt_subscribed = True
                    _LOGGER.info(f"✅ Subscribed to MQTT state reports for {len(device_ids)} device(s)")
                except Exception as error:
                    _LOGGER.warning(f"⚠️ Failed to subscribe to MQTT devices: {error}")
            
            return {
                "devices": processed_devices,
                "raw_data": data,
            }

        except BluestarAPIError as err:
            _LOGGER.exception("C6 coordinator BluestarAPIError: %s", err)
            raise UpdateFailed(f"Error communicating with Bluestar API: {err}")
        except Exception as err:
            _LOGGER.exception("C7 coordinator unexpected error: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}")

    async def control_device(self, device_id: str, control_data: Dict[str, Any]) -> Dict[str, Any]:
        """Control a device."""
        _LOGGER.warning("=" * 80)
        _LOGGER.warning(f"🎛️ COORDINATOR.control_device CALLED - device_id: {device_id}")
        _LOGGER.warning(f"🎛️ Control data: {json.dumps(control_data, indent=2)}")
        try:
            # Ensure we're logged in before making requests
            if not self.api.session_token:
                _LOGGER.warning("🔐 No session token, logging in...")
                await self.api.login()
                # Re-setup MQTT callback after login creates new client
                self._setup_mqtt_callback()
                self._mqtt_subscribed = False
            
            _LOGGER.warning("📤 Calling api.control_device...")
            result = await self.api.control_device(device_id, control_data)
            _LOGGER.warning(f"📥 api.control_device returned: {json.dumps(result, indent=2, default=str)}")
            
            # Optimistic update: update local state immediately for instant UI feedback
            # MQTT will confirm the actual state shortly
            if self.data and device_id in self.data.get("devices", {}):
                device_data = self.data["devices"][device_id]
                device_state = device_data["state"]
                
                for key, value in control_data.items():
                    if key == "pow":
                        device_state["power"] = value == 1
                    elif key == "mode":
                        # Extract mode value if it's a dictionary, otherwise use the value directly
                        if isinstance(value, dict) and "value" in value:
                            device_state["mode"] = value["value"]
                            # Also update fan_speed and temperature from mode object
                            if "fspd" in value:
                                device_state["fan_speed"] = value["fspd"]
                            if "stemp" in value:
                                device_state["temperature"] = str(value["stemp"])
                        else:
                            device_state["mode"] = value
                    elif key == "stemp":
                        device_state["temperature"] = str(value)
                    elif key == "fspd":
                        device_state["fan_speed"] = value
                    elif key == "vswing":
                        device_state["vertical_swing"] = value
                    elif key == "hswing":
                        device_state["horizontal_swing"] = value
                    elif key == "display":
                        device_state["display"] = value != 0
                    elif key in ["esave", "turbo", "sleep"]:
                        device_state[key] = value
                
                # Notify HA that state changed
                self.async_update_listeners()
            
            return result

        except BluestarAPIError as err:
            _LOGGER.error(f"Control failed for device {device_id}: {err}")
            raise

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Get device data by ID."""
        if not self.data:
            return None
        return self.data.get("devices", {}).get(device_id)

    def get_device_state(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Get device state by ID."""
        device = self.get_device(device_id)
        return device.get("state") if device else None

    def get_all_devices(self) -> Dict[str, Any]:
        """Get all devices."""
        if not self.data:
            return {}
        return self.data.get("devices", {})

    async def set_mode(self, device_id: str, mode_data: Dict[str, Any]) -> None:
        """Set HVAC mode for a device.
        
        Args:
            device_id: The device ID
            mode_data: Dictionary with mode settings (e.g., {"pow": 1, "mode": 2})
        """
        _LOGGER.warning(f"🎛️ COORDINATOR.set_mode CALLED - device_id: {device_id}, mode_data: {json.dumps(mode_data, indent=2)}")
        await self.control_device(device_id, mode_data)

    async def set_temperature(self, device_id: str, temperature: float) -> None:
        """Set target temperature for a device.
        
        Args:
            device_id: The device ID
            temperature: Target temperature
        """
        _LOGGER.warning(f"🌡️ COORDINATOR.set_temperature CALLED - device_id: {device_id}, temperature: {temperature}")
        await self.control_device(device_id, {"stemp": temperature})

    async def set_fan_mode(self, device_id: str, fan_mode: int) -> None:
        """Set fan mode for a device.
        
        Args:
            device_id: The device ID
            fan_mode: Fan mode value (from FAN_MODE_TO_BLUESTAR)
        """
        _LOGGER.warning(f"🌀 COORDINATOR.set_fan_mode CALLED - device_id: {device_id}, fan_mode: {fan_mode}")
        await self.control_device(device_id, {"fspd": fan_mode})

    async def set_power(self, device_id: str, power: bool) -> None:
        """Set power state for a device.
        
        Args:
            device_id: The device ID
            power: True to turn on, False to turn off
        """
        _LOGGER.warning(f"🔌 COORDINATOR.set_power CALLED - device_id: {device_id}, power: {power}")
        await self.control_device(device_id, {"pow": 1 if power else 0})
    
    def _handle_mqtt_message(self, device_id: str, payload: Dict[str, Any]) -> None:
        """Handle MQTT state report message and update coordinator data.
        
        This is called from the MQTT callback thread, so we need to schedule
        the update on the Home Assistant event loop.
        """
        try:
            if not self.data:
                _LOGGER.debug(f"📥 MQTT message but no data yet: {device_id}")
                return
            
            if device_id not in self.data.get("devices", {}):
                _LOGGER.debug(f"📥 MQTT message for unknown device: {device_id}")
                return
            
            # Update device state from MQTT payload
            device_data = self.data["devices"][device_id]
            device_state = device_data["state"]
            
            # Map MQTT payload fields to our state structure
            # MQTT is reliable - trust it completely for all state
            if "pow" in payload:
                device_state["power"] = payload["pow"] == 1
            
            if "mode" in payload:
                # Extract mode value if it's a dictionary, otherwise use the value directly
                mode_value = payload["mode"]
                if isinstance(mode_value, dict) and "value" in mode_value:
                    device_state["mode"] = mode_value["value"]
                else:
                    device_state["mode"] = mode_value
            if "stemp" in payload:
                device_state["temperature"] = str(payload["stemp"])
            if "ctemp" in payload:
                device_state["current_temp"] = str(payload["ctemp"])
            if "fspd" in payload:
                device_state["fan_speed"] = payload["fspd"]
            if "vswing" in payload:
                device_state["vertical_swing"] = payload["vswing"]
            if "hswing" in payload:
                device_state["horizontal_swing"] = payload["hswing"]
            if "display" in payload:
                device_state["display"] = payload["display"] != 0
            if "esave" in payload:
                device_state["esave"] = payload["esave"]
            if "turbo" in payload:
                device_state["turbo"] = payload["turbo"]
            if "sleep" in payload:
                device_state["sleep"] = payload["sleep"]
            if "rssi" in payload:
                device_state["rssi"] = payload["rssi"]
            if "err" in payload:
                device_state["error"] = payload["err"]
            if "src" in payload:
                device_state["source"] = payload["src"]
            if "ts" in payload:
                device_state["timestamp"] = payload["ts"]
            
            # Update connected status (assume connected if we're receiving MQTT messages)
            device_state["connected"] = True
            
            _LOGGER.debug(f"📥 Updated device {device_id} state from MQTT")
            
            # Schedule the update on the Home Assistant event loop
            # This is called from MQTT callback thread, so we need to use call_soon_threadsafe
            self.hass.loop.call_soon_threadsafe(self.async_update_listeners)
            
        except Exception as error:
            _LOGGER.warning(f"⚠️ Error handling MQTT message: {error}")



