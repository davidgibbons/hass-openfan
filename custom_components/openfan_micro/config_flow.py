"""Config Flow for OpenFAN Micro.

Minimal UI flow: just ask for URL and optional Name.
We probe the device once to validate connectivity.
"""

from __future__ import annotations

import logging
import voluptuous as vol
from typing import Any
from urllib.parse import urlparse

import aiohttp

from homeassistant import config_entries, exceptions
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from ._device import OpenFanDevice

_LOGGER = logging.getLogger(__name__)


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidUrl(exceptions.HomeAssistantError):
    """Error to indicate the URL is invalid."""


def _validate_url(url: str) -> str:
    """Validate and normalize URL. Returns normalized URL or raises InvalidUrl."""
    url = url.strip()
    if not url:
        raise InvalidUrl("URL is required")

    # Add http:// if no scheme provided
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise InvalidUrl("Invalid URL format")

    # Normalize: remove trailing slash
    return url.rstrip("/")


DATA_SCHEMA = vol.Schema(
    {
        vol.Required("url", description={"suggested_value": "http://192.168.1.100"}): str,
        vol.Optional("name"): str,
    }
)


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    url = _validate_url(data["url"])
    name = (data.get("name") or f"OpenFAN Micro {url}").strip()

    dev = OpenFanDevice(hass, url, name)
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

    return {"title": name, "url": url, "name": name, "rpm": rpm, "fan_count": fan_count}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenFAN Micro."""

    VERSION = 3  # Bumped for host -> url migration

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA)

        try:
            info = await _validate_input(self.hass, user_input)
        except InvalidUrl:
            return self.async_show_form(
                step_id="user", data_schema=DATA_SCHEMA, errors={"base": "invalid_url"}
            )
        except (OSError, TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning(
                "Cannot connect to OpenFAN device at %s: %s", user_input.get("url"), err
            )
            return self.async_show_form(
                step_id="user", data_schema=DATA_SCHEMA, errors={"base": "cannot_connect"}
            )
        except Exception as err:
            _LOGGER.exception(
                "Error validating OpenFAN device at %s: %s", user_input.get("url"), err
            )
            return self.async_show_form(
                step_id="user", data_schema=DATA_SCHEMA, errors={"base": "unknown"}
            )
        except Exception as err:
            _LOGGER.exception(
                "Error validating OpenFAN device at %s: %s", user_input.get("url"), err
            )
            return self.async_show_form(
                step_id="user", data_schema=DATA_SCHEMA, errors={"base": "unknown"}
            )

        # Use URL as unique_id (1 URL = 1 device)
        await self.async_set_unique_id(info["url"])
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=info["title"],
            data={
                "url": info["url"],
                "name": info["name"],
                "fan_count": info.get("fan_count", 1),
            },
        )
