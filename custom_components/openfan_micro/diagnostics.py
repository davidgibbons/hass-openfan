"""Diagnostics for OpenFAN Micro."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from .temp_controller import DEFAULT_PROFILES


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    dev = getattr(entry, "runtime_data", None)
    coordinator = getattr(dev, "coordinator", None) if dev else None
    coordinator_data = coordinator.data if coordinator else None

    # Get per-fan controller states
    controller_states: dict[str, Any] = {}
    if dev and hasattr(dev, "temp_controllers"):
        for idx, controller in dev.temp_controllers.items():
            controller_states[str(idx)] = controller.state.to_dict()

    # Get custom profiles
    custom_profiles = (entry.options or {}).get("profiles") or {}

    return {
        "title": entry.title,
        "host": entry.data.get("host"),
        "fan_count": entry.data.get("fan_count", 1),
        "options": entry.options,
        "coordinator_data": coordinator_data,
        "controller_states": controller_states,
        "builtin_profiles": list(DEFAULT_PROFILES.keys()),
        "custom_profiles": list(custom_profiles.keys()),
        "notes": "controller_states includes per-fan temp control state with profile, target/applied PWM, temp average, and gating flags.",
    }
