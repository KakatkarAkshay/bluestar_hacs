"""Config flow for Bluestar AC integration."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    DEFAULT_BASE_URL,
)
from .api import BluestarAPI, BluestarAPIError

_LOGGER = logging.getLogger(__name__)

class BluestarACConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Bluestar AC."""
    
    VERSION = 1
    
    def __init__(self):
        """Initialize the config flow."""
        self._errors = {}
    
    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        self._errors = {}
        
        if user_input is not None:
            try:
                # Get phone/username (accept both for compatibility)
                phone = user_input.get("phone") or user_input.get("username")
                password = user_input["password"]
                base_url = user_input.get("base_url", DEFAULT_BASE_URL)
                
                # Test the connection using BluestarAPI
                api = BluestarAPI(
                    phone=phone,
                    password=password,
                    base_url=base_url,
                )
                
                try:
                    # Test login
                    await api.login()
                    
                    # Test getting devices
                    devices_data = await api.get_devices()
                    things = devices_data.get("things", [])
                    
                    # Check if device_id is provided and valid
                    device_id = user_input.get("device_id", "").strip()
                    if device_id:
                        # Verify device exists
                        device_ids = [device.get("thing_id") for device in things]
                        if device_id not in device_ids:
                            self._errors["base"] = "device_not_found"
                            await api.close()
                            return self.async_show_form(
                                step_id="user",
                                data_schema=vol.Schema({
                                    vol.Required("phone", default=phone): str,
                                    vol.Required("password"): str,
                                    vol.Optional("device_id", default=device_id): str,
                                    vol.Optional("base_url", default=base_url): str,
                                }),
                                errors=self._errors,
                            )
                    else:
                        # If no device_id provided, use first device
                        if things:
                            device_id = things[0].get("thing_id")
                            _LOGGER.info(f"Using first device: {device_id}")
                        else:
                            self._errors["base"] = "no_devices"
                            await api.close()
                            return self.async_show_form(
                                step_id="user",
                                data_schema=vol.Schema({
                                    vol.Required("phone", default=phone): str,
                                    vol.Required("password"): str,
                                    vol.Optional("device_id"): str,
                                    vol.Optional("base_url", default=base_url): str,
                                }),
                                errors=self._errors,
                            )
                    
                    await api.close()
                    
                    # Create unique ID
                    unique_id = f"bluestar_ac_{device_id}"
                    
                    # Check if already configured
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()
                    
                    # Get device name for title
                    device_name = "AC"
                    for device in things:
                        if device.get("thing_id") == device_id:
                            device_name = device.get("user_config", {}).get("name", "AC")
                            break
                    
                    # Create config entry
                    return self.async_create_entry(
                        title=f"Bluestar AC {device_name}",
                        data={
                            "phone": phone,
                            "password": password,
                            "device_id": device_id,
                            "base_url": base_url,
                        }
                    )
                    
                except BluestarAPIError as e:
                    _LOGGER.error("API error during config: %s", e)
                    if e.status_code == 401:
                        self._errors["base"] = "invalid_auth"
                    else:
                        self._errors["base"] = "connection_failed"
                        await api.close()
                except Exception as ex:
                    _LOGGER.error("Config flow error: %s", ex, exc_info=True)
                    self._errors["base"] = "unknown"
                    try:
                        await api.close()
                    except:
                        pass
            except Exception as ex:
                _LOGGER.error("Config flow error: %s", ex, exc_info=True)
                self._errors["base"] = "unknown"
        
        # Show the form
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("phone"): str,
                vol.Required("password"): str,
                vol.Optional("device_id", default=""): str,
                vol.Optional("base_url", default=DEFAULT_BASE_URL): str,
            }),
            errors=self._errors,
        )
    
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return BluestarACOptionsFlow()

class BluestarACOptionsFlow(config_entries.OptionsFlow):
    """Handle options."""
    
    async def async_step_init(self, user_input=None):
        """Manage the options."""
        # No options to configure currently
        if user_input is not None:
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
        )
