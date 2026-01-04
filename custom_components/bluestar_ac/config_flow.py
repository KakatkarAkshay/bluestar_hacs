"""Config flow for Bluestar AC integration."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_DEVICE_ID, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, DEFAULT_BASE_URL
from .api import BluestarAPI, BluestarAPIError

_LOGGER = logging.getLogger(__name__)


class BluestarACConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Bluestar AC."""
    
    VERSION = 1
    
    def __init__(self):
        """Initialize the config flow."""
        self._phone = None
        self._password = None
        self._devices = []
        self._api = None
    
    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step - login credentials."""
        errors = {}
        
        if user_input is not None:
            self._phone = user_input["phone"]
            self._password = user_input[CONF_PASSWORD]
            
            try:
                self._api = BluestarAPI(
                    phone=self._phone,
                    password=self._password,
                    base_url=DEFAULT_BASE_URL,
                )
                
                await self._api.login()
                devices_data = await self._api.get_devices()
                self._devices = devices_data.get("things", [])
                
                if not self._devices:
                    errors["base"] = "no_devices"
                else:
                    # Move to device selection step
                    return await self.async_step_device()
                    
            except BluestarAPIError as e:
                if e.status_code == 401:
                    errors["base"] = "invalid_auth"
                else:
                    errors["base"] = "connection_failed"
            except Exception:
                errors["base"] = "unknown"
            finally:
                if self._api and errors:
                    await self._api.close()
                    self._api = None
        
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("phone"): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )
    
    async def async_step_device(self, user_input=None) -> FlowResult:
        """Handle device selection step."""
        errors = {}
        
        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            
            # Find device name
            device_name = "AC"
            for device in self._devices:
                if device.get("thing_id") == device_id:
                    device_name = device.get("user_config", {}).get("name", "AC")
                    break
            
            # Close API
            if self._api:
                await self._api.close()
                self._api = None
            
            # Set unique ID
            unique_id = f"bluestar_ac_{device_id}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            
            return self.async_create_entry(
                title=f"Bluestar {device_name}",
                data={
                    "phone": self._phone,
                    CONF_PASSWORD: self._password,
                    CONF_DEVICE_ID: device_id,
                }
            )
        
        # Build device options
        device_options = {
            device.get("thing_id"): device.get("user_config", {}).get("name", device.get("thing_id"))
            for device in self._devices
        }
        
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema({
                vol.Required(CONF_DEVICE_ID): vol.In(device_options),
            }),
            errors=errors,
        )
    
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow."""
        return BluestarACOptionsFlow()


class BluestarACOptionsFlow(config_entries.OptionsFlow):
    """Handle options."""
    
    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="init", data_schema=vol.Schema({}))
