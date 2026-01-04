"""The Bluestar AC integration."""
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import CONF_DEVICE_ID, CONF_PASSWORD, Platform

from .const import DOMAIN, DEFAULT_BASE_URL
from .api import BluestarAPI, BluestarAPIError
from .coordinator import BluestarDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bluestar AC from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    config = entry.data
    phone = config.get("phone")
    password = config[CONF_PASSWORD]
    device_id = config[CONF_DEVICE_ID]
    
    api = BluestarAPI(
        phone=phone,
        password=password,
        base_url=DEFAULT_BASE_URL,
    )
    
    try:
        await api.login()
        
        coordinator = BluestarDataUpdateCoordinator(hass, api)
        await coordinator.async_config_entry_first_refresh()
        
        device = coordinator.get_device(device_id)
        if not device:
            await api.close()
            raise ConfigEntryNotReady(f"Device {device_id} not found")
        
    except BluestarAPIError as ex:
        await api.close()
        raise ConfigEntryNotReady(f"Failed to connect: {ex}") from ex
    except Exception as ex:
        try:
            await api.close()
        except:
            pass
        raise ConfigEntryNotReady(f"Setup failed: {ex}") from ex
    
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        if coordinator and hasattr(coordinator, 'api'):
            await coordinator.api.close()
    
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
