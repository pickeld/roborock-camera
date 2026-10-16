"""Config flow for the Roborock Camera integration."""

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CAMERA_PIN,
    CONF_CAMERA_PIN_HASH,
    CONF_ROBOROCK_ENTRY_ID,
    DOMAIN,
    ROBOROCK_DOMAIN,
)
from .util import camera_pin_digest

_PIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CAMERA_PIN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


def _loaded_roborock_entries(hass) -> list[ConfigEntry]:
    """Return Roborock accounts that are currently running."""
    return [
        entry
        for entry in hass.config_entries.async_entries(ROBOROCK_DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


class RoborockCameraConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up live video against an existing Roborock account."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._roborock_entry_id: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose the Roborock account this camera belongs to."""
        entries = _loaded_roborock_entries(self.hass)
        if not entries:
            return self.async_abort(reason="no_roborock")

        if user_input is not None:
            self._roborock_entry_id = user_input[CONF_ROBOROCK_ENTRY_ID]
            return await self.async_step_pin()

        if len(entries) == 1:
            self._roborock_entry_id = entries[0].entry_id
            return await self.async_step_pin()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ROBOROCK_ENTRY_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"label": entry.title, "value": entry.entry_id}
                                for entry in entries
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for the video PIN and store only the robot's digest."""
        errors: dict[str, str] = {}
        if user_input is not None:
            pin = str(user_input.get(CONF_CAMERA_PIN, "")).strip()
            if not pin:
                errors["base"] = "pin_required"
            else:
                assert self._roborock_entry_id is not None
                await self.async_set_unique_id(self._roborock_entry_id)
                self._abort_if_unique_id_configured()
                roborock_entry = self.hass.config_entries.async_get_entry(
                    self._roborock_entry_id
                )
                title = roborock_entry.title if roborock_entry else "Roborock"
                return self.async_create_entry(
                    title=title,
                    data={CONF_ROBOROCK_ENTRY_ID: self._roborock_entry_id},
                    options={CONF_CAMERA_PIN_HASH: camera_pin_digest(pin)},
                )

        return self.async_show_form(
            step_id="pin",
            data_schema=_PIN_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Allow replacing the video PIN later."""
        return RoborockCameraOptionsFlow()


class RoborockCameraOptionsFlow(OptionsFlow):
    """Replace the stored video PIN."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Store a new digest, or keep the current one when the field is blank."""
        if user_input is not None:
            pin = str(user_input.get(CONF_CAMERA_PIN, "")).strip()
            options = dict(self.config_entry.options)
            if pin:
                options[CONF_CAMERA_PIN_HASH] = camera_pin_digest(pin)
            return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_CAMERA_PIN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
        )
