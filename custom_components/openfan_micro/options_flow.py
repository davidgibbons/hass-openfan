"""Options flow for OpenFAN Micro (polling, thresholds, temp curve + smoothing + per-fan aliases + profiles)."""

from __future__ import annotations
from typing import Any, Dict
import voluptuous as vol
from homeassistant import config_entries

from .const import DOMAIN
from .temp_controller import DEFAULT_PROFILES

DEFAULTS = {
    "poll_interval": 5,
    "min_pwm": 0,
    "temp_entity": "",
    "temp_curve": "45=25, 65=55, 70=100",  # C=%
    "temp_integrate_seconds": 30,
    "temp_update_min_interval": 10,
    "temp_deadband_pct": 3,
    "failure_threshold": 3,
    "stall_consecutive": 3,
}


def _global_schema(options: dict):
    """Schema for global device settings."""
    return vol.Schema(
        {
            vol.Optional(
                "poll_interval", default=options.get("poll_interval", DEFAULTS["poll_interval"])
            ): vol.All(int, vol.Range(min=2, max=60)),
            vol.Optional("min_pwm", default=options.get("min_pwm", DEFAULTS["min_pwm"])): vol.All(
                int, vol.Range(min=0, max=60)
            ),
            vol.Optional(
                "temp_entity", default=options.get("temp_entity", DEFAULTS["temp_entity"])
            ): str,
            vol.Optional(
                "temp_curve", default=options.get("temp_curve", DEFAULTS["temp_curve"])
            ): str,
            vol.Optional(
                "temp_integrate_seconds",
                default=options.get("temp_integrate_seconds", DEFAULTS["temp_integrate_seconds"]),
            ): vol.All(int, vol.Range(min=5, max=900)),
            vol.Optional(
                "temp_update_min_interval",
                default=options.get(
                    "temp_update_min_interval", DEFAULTS["temp_update_min_interval"]
                ),
            ): vol.All(int, vol.Range(min=2, max=300)),
            vol.Optional(
                "temp_deadband_pct",
                default=options.get("temp_deadband_pct", DEFAULTS["temp_deadband_pct"]),
            ): vol.All(int, vol.Range(min=0, max=20)),
            vol.Optional(
                "failure_threshold",
                default=options.get("failure_threshold", DEFAULTS["failure_threshold"]),
            ): vol.All(int, vol.Range(min=1, max=10)),
            vol.Optional(
                "stall_consecutive",
                default=options.get("stall_consecutive", DEFAULTS["stall_consecutive"]),
            ): vol.All(int, vol.Range(min=1, max=10)),
        }
    )


def _fan_select_schema(fan_count: int, options: dict):
    """Schema to select which fan to configure."""
    fans_opts = options.get("fans") or {}
    choices = {}
    for i in range(fan_count):
        alias = (fans_opts.get(str(i)) or {}).get("alias", "")
        if alias:
            choices[str(i)] = f"Fan {i + 1}: {alias}"
        else:
            choices[str(i)] = f"Fan {i + 1}"
    return vol.Schema(
        {
            vol.Required("fan_index"): vol.In(choices),
        }
    )


def _get_profile_choices(options: dict) -> dict[str, str]:
    """Get available profiles (builtin + custom)."""
    choices = {"": "(Custom / No Profile)"}
    # Add built-in profiles
    for name in DEFAULT_PROFILES:
        choices[name] = f"{name.title()} (Built-in)"
    # Add custom profiles
    custom_profiles = options.get("profiles") or {}
    for name in custom_profiles:
        if name not in DEFAULT_PROFILES:
            choices[name] = f"{name} (Custom)"
    return choices


def _fan_settings_schema(fan_index: int, options: dict):
    """Schema for per-fan settings (alias, profile, min_pwm, temp control)."""
    fans_opts = options.get("fans") or {}
    fan_opts = fans_opts.get(str(fan_index)) or {}
    profile_choices = _get_profile_choices(options)
    current_profile = fan_opts.get("profile", "")

    return vol.Schema(
        {
            vol.Optional("alias", default=fan_opts.get("alias", "")): str,
            vol.Optional("profile", default=current_profile): vol.In(profile_choices),
            vol.Optional(
                "min_pwm",
                default=fan_opts.get("min_pwm", options.get("min_pwm", DEFAULTS["min_pwm"])),
            ): vol.All(int, vol.Range(min=0, max=60)),
            vol.Optional("temp_entity", default=fan_opts.get("temp_entity", "")): str,
            vol.Optional("temp_curve", default=fan_opts.get("temp_curve", "")): str,
        }
    )


class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry
        self._selected_fan_index: int | None = None
        self._pending_options: dict[str, Any] = {}

    async def async_step_init(self, user_input: Dict[str, Any] | None = None):
        """Step 1: Global settings."""
        if user_input is not None:
            merged = dict(self.entry.options or {})
            merged.update(user_input)
            self._pending_options = merged

            # Check if multi-fan device
            fan_count = self.entry.data.get("fan_count", 1)
            if fan_count > 1:
                return await self.async_step_fan_select()
            return self.async_create_entry(title="", data=merged)

        return self.async_show_form(
            step_id="init",
            data_schema=_global_schema(self.entry.options or {}),
            description_placeholders={"fan_count": str(self.entry.data.get("fan_count", 1))},
        )

    async def async_step_fan_select(self, user_input: Dict[str, Any] | None = None):
        """Step 2: Select which fan to configure (multi-fan only)."""
        fan_count = self.entry.data.get("fan_count", 1)

        if user_input is not None:
            self._selected_fan_index = int(user_input["fan_index"])
            return await self.async_step_fan_settings()

        return self.async_show_form(
            step_id="fan_select",
            data_schema=_fan_select_schema(fan_count, self._pending_options),
            description_placeholders={"fan_count": str(fan_count)},
        )

    async def async_step_fan_settings(self, user_input: Dict[str, Any] | None = None):
        """Step 3: Configure selected fan (alias, profile, per-fan options)."""
        fan_index = self._selected_fan_index
        if fan_index is None:
            return self.async_abort(reason="no_fan_selected")

        if user_input is not None:
            # Store per-fan settings under options["fans"][index]
            fans = dict(self._pending_options.get("fans") or {})
            fan_key = str(fan_index)
            fan_opts = dict(fans.get(fan_key) or {})
            fan_opts.update(user_input)

            # Remove empty values
            if not fan_opts.get("alias", "").strip():
                fan_opts.pop("alias", None)
            if not fan_opts.get("profile", "").strip():
                fan_opts.pop("profile", None)
            if not fan_opts.get("temp_entity", "").strip():
                fan_opts.pop("temp_entity", None)
            if not fan_opts.get("temp_curve", "").strip():
                fan_opts.pop("temp_curve", None)

            fans[fan_key] = fan_opts
            self._pending_options["fans"] = fans

            # Ask if user wants to configure another fan
            fan_count = self.entry.data.get("fan_count", 1)
            if fan_count > 1:
                return await self.async_step_configure_another()

            return self.async_create_entry(title="", data=self._pending_options)

        return self.async_show_form(
            step_id="fan_settings",
            data_schema=_fan_settings_schema(fan_index, self._pending_options),
            description_placeholders={"fan_index": str(fan_index + 1)},
        )

    async def async_step_configure_another(self, user_input: Dict[str, Any] | None = None):
        """Ask if user wants to configure another fan."""
        if user_input is not None:
            if user_input.get("configure_another", False):
                return await self.async_step_fan_select()
            return self.async_create_entry(title="", data=self._pending_options)

        return self.async_show_form(
            step_id="configure_another",
            data_schema=vol.Schema(
                {
                    vol.Optional("configure_another", default=False): bool,
                }
            ),
        )
