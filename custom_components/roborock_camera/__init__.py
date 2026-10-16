"""Roborock live camera as a companion to the built-in Roborock integration."""

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_ROBOROCK_ENTRY_ID, PLATFORMS, ROBOROCK_DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up cameras for an existing Roborock account."""
    roborock_entry = hass.config_entries.async_get_entry(
        entry.data[CONF_ROBOROCK_ENTRY_ID]
    )
    if (
        roborock_entry is None
        or roborock_entry.domain != ROBOROCK_DOMAIN
        or roborock_entry.state is not ConfigEntryState.LOADED
        or getattr(roborock_entry, "runtime_data", None) is None
    ):
        raise ConfigEntryNotReady("Roborock is not ready")

    entry.runtime_data = roborock_entry.runtime_data

    def _reload_when_roborock_unloads() -> None:
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))

    entry.async_on_unload(roborock_entry.async_on_unload(_reload_when_roborock_unloads))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the camera platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
