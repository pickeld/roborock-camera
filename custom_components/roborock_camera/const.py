"""Constants for the Roborock Camera integration."""

from homeassistant.const import Platform

DOMAIN = "roborock_camera"
ROBOROCK_DOMAIN = "roborock"

CONF_ROBOROCK_ENTRY_ID = "roborock_entry_id"
CONF_CAMERA_PIN = "camera_pin"
CONF_CAMERA_PIN_HASH = "camera_pin_hash"

PLATFORMS: list[Platform] = [Platform.CAMERA]
