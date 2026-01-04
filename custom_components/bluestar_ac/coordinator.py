"""Data update coordinator for Bluestar Smart AC integration."""

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

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
        self._last_mqtt_client = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        
        self._setup_mqtt_callback()

    def _setup_mqtt_callback(self) -> None:
        """Set up MQTT message callback."""
        if self.api.mqtt_client:
            self.api.mqtt_client.set_message_callback(self._handle_mqtt_message)
            self._last_mqtt_client = self.api.mqtt_client

    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch device list from API. State comes from MQTT."""
        try:
            if not self.api.session_token:
                await self.api.login()
                self._setup_mqtt_callback()
                self._mqtt_subscribed = False
            
            if self.api.mqtt_client and self.api.mqtt_client != self._last_mqtt_client:
                self._setup_mqtt_callback()
                self._mqtt_subscribed = False
            
            data = await self.api.get_devices()
            self.devices = {device["thing_id"]: device for device in data.get("things", [])}
            
            processed_devices = {}
            for device_id, device in self.devices.items():
                existing_state = {}
                if self.data and device_id in self.data.get("devices", {}):
                    existing_state = self.data["devices"][device_id].get("state", {})
                
                processed_devices[device_id] = {
                    "id": device_id,
                    "name": device.get("user_config", {}).get("name", "AC"),
                    "type": "ac",
                    "state": existing_state if existing_state else {
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
                        "connected": True,
                        "rssi": -45,
                        "error": 0,
                        "source": "unknown",
                        "timestamp": 0,
                    },
                    "raw_device": device,
                }
            
            # Update self.data BEFORE requesting MQTT states so the callback can update it
            result = {"devices": processed_devices}
            self.data = result
            
            if not self._mqtt_subscribed and self.api.mqtt_client:
                device_ids = list(processed_devices.keys())
                try:
                    await self.api.subscribe_to_devices(device_ids)
                    self._mqtt_subscribed = True
                    await self.api.request_device_states(device_ids)
                    await asyncio.sleep(1.0)
                except Exception as error:
                    _LOGGER.warning(f"Failed to subscribe to MQTT: {error}")
            
            return result

        except BluestarAPIError as err:
            raise UpdateFailed(f"Error communicating with Bluestar API: {err}")
        except Exception as err:
            _LOGGER.exception("Unexpected error: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}")

    async def control_device(self, device_id: str, control_data: Dict[str, Any]) -> Dict[str, Any]:
        """Control a device via MQTT."""
        try:
            if not self.api.session_token:
                await self.api.login()
                self._setup_mqtt_callback()
                self._mqtt_subscribed = False
            
            result = await self.api.control_device(device_id, control_data)
            
            # Optimistic update for instant UI feedback
            if self.data and device_id in self.data.get("devices", {}):
                device_state = self.data["devices"][device_id]["state"]
                
                for key, value in control_data.items():
                    if key == "pow":
                        device_state["power"] = value == 1
                    elif key == "mode":
                        if isinstance(value, dict) and "value" in value:
                            device_state["mode"] = value["value"]
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
                
                self.async_update_listeners()
            
            return result

        except BluestarAPIError as err:
            _LOGGER.error(f"Control failed for {device_id}: {err}")
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

    async def set_power(self, device_id: str, power: bool) -> None:
        """Set power state."""
        await self.control_device(device_id, {"pow": 1 if power else 0})

    async def set_temperature(self, device_id: str, temperature: float) -> None:
        """Set target temperature."""
        await self.control_device(device_id, {"stemp": temperature})

    async def set_fan_mode(self, device_id: str, fan_mode: int) -> None:
        """Set fan mode."""
        await self.control_device(device_id, {"fspd": fan_mode})

    def _handle_mqtt_message(self, device_id: str, payload: Dict[str, Any]) -> None:
        """Handle MQTT state report and update device state."""
        try:
            if not self.data or device_id not in self.data.get("devices", {}):
                return
            
            device_state = self.data["devices"][device_id]["state"]
            
            if "pow" in payload:
                device_state["power"] = payload["pow"] == 1
            if "mode" in payload:
                mode_value = payload["mode"]
                if isinstance(mode_value, dict) and "value" in mode_value:
                    device_state["mode"] = mode_value["value"]
                    if "fspd" in mode_value:
                        device_state["fan_speed"] = mode_value["fspd"]
                    if "stemp" in mode_value:
                        device_state["temperature"] = str(mode_value["stemp"])
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
            
            device_state["connected"] = True
            self.hass.loop.call_soon_threadsafe(self.async_update_listeners)
            
        except Exception as error:
            _LOGGER.warning(f"Error handling MQTT message: {error}")
