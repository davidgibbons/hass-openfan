"""Fan entity for OpenFAN Micro with min-PWM clamp and debug attributes."""

from __future__ import annotations
from typing import Any
from urllib.parse import urlparse
import logging

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add: AddEntitiesCallback
) -> None:
    device = getattr(entry, "runtime_data", None)
    if device is None:
        _LOGGER.error("OpenFAN Micro: runtime_data is None (fan)")
        return
    fan_count = int(getattr(device, "fan_count", 1) or 1)
    async_add([OpenFan(device, entry, idx) for idx in range(fan_count)])


class OpenFan(CoordinatorEntity, FanEntity):
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    )
    _attr_has_entity_name = True

    def __init__(self, device, entry: ConfigEntry, index: int) -> None:
        super().__init__(device.coordinator)
        self._device = device
        self._entry = entry
        self._index = int(index)
        url = getattr(device, "url", "")
        self._host_id = urlparse(url).netloc or url or "unknown"
        base_name = getattr(device, "name", "OpenFAN Micro")
        # Use alias from options if present, otherwise default naming
        fans_opts = (entry.options or {}).get("fans") or {}
        fan_opts = fans_opts.get(str(self._index)) or {}
        alias = fan_opts.get("alias", "").strip()
        if alias:
            self._attr_name = alias
        else:
            self._attr_name = (
                base_name if self._index == 0 else f"{base_name} Fan {self._index + 1}"
            )
        if self._index == 0:
            self._attr_unique_id = f"openfan_micro_{self._host_id}"
        else:
            self._attr_unique_id = f"openfan_micro_{self._host_id}_{self._index}"
        self._attr_device_info = self._device.device_info()
        self._attr_entity_registry_visible_default = True
        self._attr_should_poll = False

    @property
    def device_info(self) -> dict[str, Any] | None:
        return self._device.device_info()

    @property
    def available(self) -> bool:
        base = super().available
        forced = getattr(self.coordinator, "_forced_unavailable", False)
        return base and not forced

    # ---- state ----

    @property
    def percentage(self) -> int | None:
        data = self.coordinator.data or {}
        fans = data.get("fans") if isinstance(data, dict) else None
        if isinstance(fans, dict):
            entry = fans.get(self._index) or {}
            if "pwm" in entry:
                return int(entry.get("pwm") or 0)
            return None
        if "pwm" in data:
            try:
                return int(data.get("pwm") or 0)
            except Exception:
                return 0
        return None

    @property
    def is_on(self) -> bool | None:
        p = self.percentage
        return None if p is None else (p > 0)

    # ---- control ----

    async def async_set_percentage(self, percentage: int) -> None:
        opts = self._entry.options or {}
        per_fan_opts = (opts.get("fans") or {}).get(str(self._index), {})
        min_pwm = int(per_fan_opts.get("min_pwm", opts.get("min_pwm", 0)))
        if int(percentage) > 0:
            percentage = max(min_pwm, int(percentage))

        await self._device.api.set_pwm_index(self._index, int(percentage))
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, percentage: int | None = None, **kwargs) -> None:
        if percentage is None:
            percentage = max(1, self._entry.options.get("min_pwm", 0) or 1)
        await self.async_set_percentage(int(percentage))

    async def async_turn_off(self, **kwargs) -> None:
        await self._device.api.set_pwm_index(self._index, 0)
        await self.coordinator.async_request_refresh()

    # ---- attributes ----

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        opts = self._entry.options or {}
        fan_opts = (opts.get("fans") or {}).get(str(self._index)) or {}

        # Get controller state for this specific fan
        controllers = getattr(self._device, "temp_controllers", {})
        controller = controllers.get(self._index)
        ctrl_state = controller.state.to_dict() if controller else {}

        return {
            "fan_index": int(self._index),
            "min_pwm": int(
                ctrl_state.get("min_pwm", fan_opts.get("min_pwm", opts.get("min_pwm", 0)))
            ),
            "min_pwm_calibrated": bool(
                ctrl_state.get(
                    "min_pwm_calibrated",
                    fan_opts.get("min_pwm_calibrated", opts.get("min_pwm_calibrated", False)),
                )
            ),
            "profile": ctrl_state.get("profile", fan_opts.get("profile", "")),
            "temp_control_active": bool(ctrl_state.get("active", False)),
            "temp_entity": ctrl_state.get("temp_entity")
            or fan_opts.get("temp_entity")
            or opts.get("temp_entity", ""),
            "temp_curve": ctrl_state.get("temp_curve")
            or fan_opts.get("temp_curve")
            or opts.get("temp_curve", ""),
            "temp_avg": ctrl_state.get("temp_avg"),
            "last_target_pwm": ctrl_state.get("last_target_pwm"),
            "last_applied_pwm": ctrl_state.get("last_applied_pwm"),
            "temp_update_min_interval": int(
                ctrl_state.get(
                    "temp_update_min_interval",
                    fan_opts.get(
                        "temp_update_min_interval", opts.get("temp_update_min_interval", 10)
                    ),
                )
            ),
            "temp_deadband_pct": int(
                ctrl_state.get(
                    "temp_deadband_pct",
                    fan_opts.get("temp_deadband_pct", opts.get("temp_deadband_pct", 3)),
                )
            ),
        }
