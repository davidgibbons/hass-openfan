"""Config Flow for OpenFAN Micro.

Minimal UI flow: just ask for Host and optional Name.
We probe the device once to validate connectivity.
"""

from __future__ import annotations

import voluptuous as vol
from typing import Any

from homeassistant import config_entries, exceptions
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from ._device import OpenFanDevice


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


DATA_SCHEMA = vol.Schema({vol.Required("host"): str, vol.Optional("name"): str})


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    host = data["host"].strip()
    name = (data.get("name") or f"OpenFAN Micro {host}").strip()

    dev = OpenFanDevice(hass, host, name)
    # First refresh will fetch status once (raises on network error).
    await dev.async_first_refresh()
    rpm = 0
    data = dev.coordinator_data or {}
    fans = data.get("fans") if isinstance(data, dict) else None
    if isinstance(fans, dict):
        try:
            rpm = int((fans.get(0) or {}).get("rpm") or 0)
        except Exception:
            rpm = 0
    else:
        try:
            rpm = int(data.get("rpm") or 0)
        except Exception:
            rpm = 0
    fan_count = 1
    try:
        fan_count = max(1, int(getattr(dev.api, "_fan_count", 1) or 1))
    except Exception:
        fan_count = 1

    return {"title": name, "host": host, "name": name, "rpm": rpm, "fan_count": fan_count}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenFAN Micro."""

    VERSION = 2  # Bumped for multi-fan options migration

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA)

        try:
            info = await _validate_input(self.hass, user_input)
        except Exception:
            # Unknown error; show generic error code.
            return self.async_show_form(
                step_id="user", data_schema=DATA_SCHEMA, errors={"base": "unknown"}
            )

        # Use host as unique_id (1 host = 1 device)
        await self.async_set_unique_id(info["host"])
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=info["title"],
            data={
                "host": info["host"],
                "name": info["name"],
                "fan_count": info.get("fan_count", 1),
            },
        )
