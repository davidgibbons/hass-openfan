"""RPM sensor for OpenFAN Micro."""

from __future__ import annotations
from typing import Any
import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    device = getattr(entry, "runtime_data", None)
    if device is None:
        _LOGGER.error("OpenFAN Micro: runtime_data is None (sensor)")
        return
    fan_count = int(getattr(device, "fan_count", 1) or 1)
    async_add_entities([OpenFanRpmSensor(device, entry, idx) for idx in range(fan_count)])


class OpenFanRpmSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "rpm"
    _attr_icon = "mdi:fan"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_has_entity_name = True

    def __init__(self, device, entry: ConfigEntry, index: int) -> None:
        super().__init__(device.coordinator)
        self._device = device
        self._entry = entry
        self._index = int(index)
        self._host = getattr(device, "host", "unknown")
        base_name = getattr(device, "name", None) or f"OpenFAN Micro {self._host}"
        # Use alias from options if present, otherwise default naming
        fans_opts = (entry.options or {}).get("fans") or {}
        fan_opts = fans_opts.get(str(self._index)) or {}
        alias = fan_opts.get("alias", "").strip()
        if alias:
            self._attr_name = f"{alias} RPM"
        else:
            self._attr_name = (
                f"{base_name} RPM" if self._index == 0 else f"{base_name} Fan {self._index + 1} RPM"
            )
        self._attr_device_info = self._device.device_info()
        self._attr_entity_registry_visible_default = True
        if self._index == 0:
            self._attr_unique_id = f"openfan_micro_rpm_{self._host}"
        else:
            self._attr_unique_id = f"openfan_micro_rpm_{self._host}_{self._index}"

    @property
    def device_info(self) -> dict[str, Any] | None:
        try:
            return self._device.device_info()
        except Exception:
            return None

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data or {}
        fans = data.get("fans") if isinstance(data, dict) else None
        if isinstance(fans, dict):
            entry = fans.get(self._index) or {}
            try:
                return int(entry.get("rpm") or 0)
            except Exception:
                return 0
        try:
            return int(data.get("rpm") or 0)
        except Exception:
            return 0
