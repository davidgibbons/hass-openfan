"""Stall detector binary sensor."""

from __future__ import annotations
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import logging

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add: AddEntitiesCallback
) -> None:
    dev = getattr(entry, "runtime_data", None)
    if dev is None:
        _LOGGER.error("OpenFAN Micro: runtime_data is None (binary_sensor)")
        return
    fan_count = int(getattr(dev, "fan_count", 1) or 1)
    async_add([OpenFanStallBinarySensor(dev, entry, idx) for idx in range(fan_count)])


class OpenFanStallBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_icon = "mdi:alert"
    _attr_has_entity_name = True

    def __init__(self, device, entry: ConfigEntry, index: int) -> None:
        super().__init__(device.coordinator)
        self._device = device
        self._entry = entry
        self._index = int(index)
        self._host = getattr(device, "host", "unknown")
        base_name = getattr(device, "name", "OpenFAN Micro")
        # Use alias from options if present, otherwise default naming
        fans_opts = (entry.options or {}).get("fans") or {}
        fan_opts = fans_opts.get(str(self._index)) or {}
        alias = fan_opts.get("alias", "").strip()
        if alias:
            self._attr_name = f"{alias} Stall"
        else:
            self._attr_name = (
                f"{base_name} Stall"
                if self._index == 0
                else f"{base_name} Fan {self._index + 1} Stall"
            )
        if self._index == 0:
            self._attr_unique_id = f"openfan_micro_stall_{self._host}"
        else:
            self._attr_unique_id = f"openfan_micro_stall_{self._host}_{self._index}"
        self._attr_device_info = self._device.device_info()
        self._attr_entity_registry_visible_default = True

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        fans = data.get("fans") if isinstance(data, dict) else None
        if isinstance(fans, dict):
            entry = fans.get(self._index) or {}
            return bool(entry.get("stalled", False))
        return bool(data.get("stalled", False))

    @property
    def available(self) -> bool:
        base = super().available
        forced = getattr(self.coordinator, "_forced_unavailable", False)
        return base and not forced

    @property
    def device_info(self):
        return self._device.device_info()
