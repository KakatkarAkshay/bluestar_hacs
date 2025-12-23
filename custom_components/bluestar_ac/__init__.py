"""The Bluestar AC integration."""
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    Platform,
)

from .const import (
    DOMAIN,
    DEFAULT_BASE_URL,
)
from .api import BluestarAPI, BluestarAPIError
from .coordinator import BluestarDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bluestar AC from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    config = entry.data
    phone = config.get("phone") or config.get("username")  # Support both for compatibility
    password = config[CONF_PASSWORD]
    device_id = config[CONF_DEVICE_ID]
    base_url = config.get("base_url", DEFAULT_BASE_URL)
    
    # Create API client
    api = BluestarAPI(
        phone=phone,
        password=password,
        base_url=base_url,
    )
    
    try:
        # Test connection by logging in
        _LOGGER.info("Testing connection to Bluestar API...")
        await api.login()
        _LOGGER.info("Successfully logged in to Bluestar API")
        
        # Create coordinator
        coordinator = BluestarDataUpdateCoordinator(hass, api)
        
        # Perform initial data fetch
        await coordinator.async_config_entry_first_refresh()
        
        # Verify device exists
        device = coordinator.get_device(device_id)
        if not device:
            _LOGGER.error("Device %s not found in account", device_id)
            await api.close()
            raise ConfigEntryNotReady(f"Device {device_id} not found in account")
        
        _LOGGER.info("Device %s found: %s", device_id, device.get("name", "Unknown"))
        
    except BluestarAPIError as ex:
        _LOGGER.error("Failed to connect to Bluestar API: %s", ex)
        await api.close()
        raise ConfigEntryNotReady(f"Failed to connect: {ex}") from ex
    except Exception as ex:
        _LOGGER.error("Failed to set up Bluestar AC: %s", ex, exc_info=True)
        try:
            await api.close()
        except:
            pass
        raise ConfigEntryNotReady(f"Setup failed: {ex}") from ex
    
    # Store coordinator in hass data
    hass.data[DOMAIN][entry.entry_id] = coordinator
    
    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        # Close API connection
        if coordinator and hasattr(coordinator, 'api'):
            await coordinator.api.close()
    
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)

