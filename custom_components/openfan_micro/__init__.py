"""Setup & services for OpenFAN Micro (Pro Pack with per-fan temp control & profiles)."""

from __future__ import annotations

import logging
import asyncio
from typing import Any, Optional
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from ._device import Device
from .options_flow import OptionsFlowHandler
from .temp_controller import FanTempController, DEFAULT_PROFILES

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.FAN,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
]

# Track if services are already registered (global, not per-entry)
_SERVICES_REGISTERED = False


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the OpenFAN Micro integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create device runtime, forward platforms, wire temperature controllers & services."""
    global _SERVICES_REGISTERED

    host = entry.data.get("host")
    name = entry.data.get("name")
    mac = entry.data.get("mac")
    if not host:
        _LOGGER.error("%s: missing 'host' in config entry", DOMAIN)
        return False

    fan_count = int(entry.data.get("fan_count", 1) or 1)
    dev = Device(hass, host, name, mac=mac, fan_count=fan_count)

    # Apply options to API/coordinator tunables
    opts = entry.options or {}
    dev.api._poll_interval = int(opts.get("poll_interval", 5))
    dev.api._min_pwm = int(opts.get("min_pwm", 0))
    dev.api._failure_threshold = int(opts.get("failure_threshold", 3))
    dev.api._stall_consecutive = int(opts.get("stall_consecutive", 3))

    await dev.async_first_refresh()
    entry.runtime_data = dev

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # --- Per-fan temperature controllers ---
    dev.temp_controllers = {}

    async def set_pwm_and_refresh(fan_index: int, pwm: int) -> None:
        """Set PWM and refresh coordinator."""
        await dev.api.set_pwm_index(fan_index, pwm)
        await dev.coordinator.async_request_refresh()

    def get_options() -> dict[str, Any]:
        """Get current options from entry."""
        return dict(entry.options or {})

    # Create a controller for each fan
    for idx in range(fan_count):
        controller = FanTempController(
            hass=hass,
            fan_index=idx,
            host=host,
            set_pwm_callback=set_pwm_and_refresh,
            get_options_callback=get_options,
        )
        dev.temp_controllers[idx] = controller

        # Subscribe to temp entity if configured
        fan_opts = (opts.get("fans") or {}).get(str(idx)) or {}
        temp_entity = (fan_opts.get("temp_entity") or opts.get("temp_entity") or "").strip()
        if temp_entity:
            controller.subscribe_temp_entity(temp_entity)
            hass.async_create_task(controller.apply("startup"))

    # Periodic re-evaluation for all fans
    min_interval_s = int(opts.get("temp_update_min_interval", 10))

    async def _periodic_temp_control(now) -> None:
        for controller in dev.temp_controllers.values():
            await controller.apply("periodic")

    unsub_tick = async_track_time_interval(
        hass, _periodic_temp_control, timedelta(seconds=max(5, min_interval_s))
    )
    entry.async_on_unload(unsub_tick)

    # Cleanup on unload
    def _cleanup_controllers() -> None:
        for controller in dev.temp_controllers.values():
            controller.unsubscribe()

    entry.async_on_unload(_cleanup_controllers)

    # -------- Helpers to resolve devices/entries and update options --------

    def _entity_owner_entry_id(entity_id: str) -> Optional[str]:
        registry = er.async_get(hass)
        ent = registry.async_get(entity_id)
        return ent.config_entry_id if ent else None

    def _fan_index_from_entity_id(entity_id: str) -> int:
        st = hass.states.get(entity_id)
        if st:
            try:
                return int(st.attributes.get("fan_index", 0))
            except Exception:
                return 0
        return 0

    def _get_entry_by_id(entry_id: str) -> Optional[ConfigEntry]:
        for ce in hass.config_entries.async_entries(DOMAIN):
            if ce.entry_id == entry_id:
                return ce
        return None

    async def _resolve_dev(entity_id: str) -> tuple[Optional[Device], Optional[str]]:
        """Return (Device, owner_config_entry_id) for the given entity_id."""
        owner_id = _entity_owner_entry_id(entity_id)
        if not owner_id:
            _LOGGER.error("openfan_micro: entity_id %s not found in registry", entity_id)
            return None, None
        if owner_id == entry.entry_id:
            return entry.runtime_data, owner_id
        ce = _get_entry_by_id(owner_id)
        dev_rt = getattr(ce, "runtime_data", None) if ce else None
        if dev_rt is None:
            _LOGGER.error("openfan_micro: runtime_data not ready for entry_id=%s", owner_id)
        return dev_rt, owner_id

    def _update_options(
        update: dict[str, Any],
        target_entry_id: Optional[str] = None,
        fan_index: Optional[int] = None,
    ) -> None:
        """Write options to the *owner* entry (not necessarily 'entry')."""
        ce = _get_entry_by_id(target_entry_id) if target_entry_id else entry
        if not ce:
            _LOGGER.error("openfan_micro: target entry not found for options update")
            return
        new_opts = dict(ce.options or {})
        if fan_index is not None:
            fans = dict(new_opts.get("fans") or {})
            fan_key = str(int(fan_index))
            fan_opts = dict(fans.get(fan_key) or {})
            fan_opts.update(update)
            fans[fan_key] = fan_opts
            new_opts["fans"] = fans
        else:
            new_opts.update(update)
        hass.config_entries.async_update_entry(ce, options=new_opts)

    # ------------------- Services -------------------
    # Register services only once (globally), not per entry

    if not _SERVICES_REGISTERED:
        _SERVICES_REGISTERED = True

        async def svc_led_set(call) -> None:
            entity_id = call.data.get("entity_id", "")
            devx, owner_id = await _resolve_dev(entity_id)
            if not devx:
                return
            await devx.api.led_set(bool(call.data["enabled"]))

        async def svc_set_voltage(call) -> None:
            entity_id = call.data.get("entity_id", "")
            devx, owner_id = await _resolve_dev(entity_id)
            if not devx:
                return
            volts = int(call.data["volts"])
            await devx.api.set_voltage_12v(volts == 12)

        async def svc_calibrate_min(call) -> None:
            entity_id = call.data.get("entity_id", "")
            devx, owner_id = await _resolve_dev(entity_id)
            if not devx or not owner_id:
                _LOGGER.error(
                    "openfan_micro.calibrate_min: could not resolve device from entity_id"
                )
                return
            fan_index = _fan_index_from_entity_id(entity_id)
            from_pct = int(call.data.get("from_pct", 10))
            to_pct = int(call.data.get("to_pct", 40))
            step = int(call.data.get("step", 5))
            rpm_thr = int(call.data.get("rpm_threshold", 100))
            margin = int(call.data.get("margin", 5))

            found = None
            for pct in range(from_pct, to_pct + 1, step):
                await devx.api.set_pwm_index(fan_index, pct)
                await asyncio.sleep(max(1, int(devx.api._poll_interval)))
                await devx.coordinator.async_request_refresh()
                data = devx.coordinator.data or {}
                rpm = int((data.get("fans") or {}).get(fan_index, {}).get("rpm") or 0)
                if rpm >= rpm_thr:
                    found = pct
                    break

            if found is not None:
                new_min = max(0, min(100, found + margin))
                _update_options(
                    {"min_pwm": new_min, "min_pwm_calibrated": True},
                    target_entry_id=owner_id,
                    fan_index=fan_index,
                )
                _LOGGER.info(
                    "Calibrated min_pwm=%s for fan %s entry %s", new_min, fan_index, owner_id
                )
            else:
                _LOGGER.warning(
                    "Calibration did not reach RPM threshold; leaving min_pwm unchanged."
                )

        async def svc_set_temp_control(call) -> None:
            entity_id = call.data.get("entity_id", "")
            devx, owner_id = await _resolve_dev(entity_id)
            if not devx or not owner_id:
                _LOGGER.error(
                    "openfan_micro.set_temp_control: could not resolve device from entity_id"
                )
                return

            fan_index = _fan_index_from_entity_id(entity_id)
            update: dict[str, Any] = {}

            if "temp_entity" in call.data:
                update["temp_entity"] = str(call.data.get("temp_entity") or "").strip()
            for k in (
                "temp_curve",
                "temp_integrate_seconds",
                "temp_update_min_interval",
                "temp_deadband_pct",
            ):
                if k in call.data:
                    update[k] = call.data[k]

            # Clear profile if setting custom curve
            if "temp_curve" in update:
                update["profile"] = ""

            _update_options(update, target_entry_id=owner_id, fan_index=fan_index)

            # Update subscription if temp_entity changed
            if "temp_entity" in update and hasattr(devx, "temp_controllers"):
                controller = devx.temp_controllers.get(fan_index)
                if controller:
                    controller.subscribe_temp_entity(update["temp_entity"])
                    await controller.apply("set_temp_control")

        async def svc_clear_temp_control(call) -> None:
            entity_id = call.data.get("entity_id", "")
            devx, owner_id = await _resolve_dev(entity_id)
            if not devx or not owner_id:
                return
            fan_index = _fan_index_from_entity_id(entity_id)
            _update_options(
                {"temp_entity": "", "profile": ""}, target_entry_id=owner_id, fan_index=fan_index
            )

            if hasattr(devx, "temp_controllers"):
                controller = devx.temp_controllers.get(fan_index)
                if controller:
                    controller.clear()

        async def svc_apply_profile(call) -> None:
            """Apply a named profile to a fan."""
            entity_id = call.data.get("entity_id", "")
            profile_name = call.data.get("profile", "").strip()
            devx, owner_id = await _resolve_dev(entity_id)
            if not devx or not owner_id:
                _LOGGER.error(
                    "openfan_micro.apply_profile: could not resolve device from entity_id"
                )
                return

            fan_index = _fan_index_from_entity_id(entity_id)

            # Verify profile exists
            ce = _get_entry_by_id(owner_id)
            if not ce:
                return
            profiles = (ce.options or {}).get("profiles") or {}
            if profile_name not in profiles and profile_name not in DEFAULT_PROFILES:
                _LOGGER.error("openfan_micro.apply_profile: unknown profile '%s'", profile_name)
                return

            _update_options(
                {"profile": profile_name}, target_entry_id=owner_id, fan_index=fan_index
            )

            if hasattr(devx, "temp_controllers"):
                controller = devx.temp_controllers.get(fan_index)
                if controller:
                    await controller.apply("profile_applied")

            _LOGGER.info("Applied profile '%s' to fan %s", profile_name, fan_index)

        async def svc_save_profile(call) -> None:
            """Save current fan settings as a named profile."""
            entity_id = call.data.get("entity_id", "")
            profile_name = call.data.get("profile", "").strip()
            devx, owner_id = await _resolve_dev(entity_id)
            if not devx or not owner_id:
                _LOGGER.error("openfan_micro.save_profile: could not resolve device from entity_id")
                return

            if not profile_name:
                _LOGGER.error("openfan_micro.save_profile: profile name required")
                return

            # Prevent overwriting built-in profiles
            if profile_name in DEFAULT_PROFILES:
                _LOGGER.error(
                    "openfan_micro.save_profile: cannot overwrite built-in profile '%s'",
                    profile_name,
                )
                return

            fan_index = _fan_index_from_entity_id(entity_id)
            ce = _get_entry_by_id(owner_id)
            if not ce:
                return

            opts = ce.options or {}
            fan_opts = (opts.get("fans") or {}).get(str(fan_index)) or {}

            # Build profile data from current settings
            profile_data = {
                "temp_curve": fan_opts.get("temp_curve") or opts.get("temp_curve", ""),
                "temp_integrate_seconds": int(
                    fan_opts.get("temp_integrate_seconds") or opts.get("temp_integrate_seconds", 30)
                ),
                "temp_update_min_interval": int(
                    fan_opts.get("temp_update_min_interval")
                    or opts.get("temp_update_min_interval", 10)
                ),
                "temp_deadband_pct": int(
                    fan_opts.get("temp_deadband_pct") or opts.get("temp_deadband_pct", 3)
                ),
            }

            # Update profiles
            new_opts = dict(opts)
            profiles = dict(new_opts.get("profiles") or {})
            profiles[profile_name] = profile_data
            new_opts["profiles"] = profiles
            hass.config_entries.async_update_entry(ce, options=new_opts)

            _LOGGER.info("Saved profile '%s' from fan %s settings", profile_name, fan_index)

        async def svc_list_profiles(call) -> dict[str, Any]:
            """List available profiles (built-in + custom)."""
            entity_id = call.data.get("entity_id", "")
            devx, owner_id = await _resolve_dev(entity_id)

            result = {"builtin": list(DEFAULT_PROFILES.keys()), "custom": []}
            if devx and owner_id:
                ce = _get_entry_by_id(owner_id)
                if ce:
                    custom = (ce.options or {}).get("profiles") or {}
                    result["custom"] = list(custom.keys())
            return result

        # Register all services
        hass.services.async_register(DOMAIN, "led_set", svc_led_set)
        hass.services.async_register(DOMAIN, "set_voltage", svc_set_voltage)
        hass.services.async_register(DOMAIN, "calibrate_min", svc_calibrate_min)
        hass.services.async_register(DOMAIN, "set_temp_control", svc_set_temp_control)
        hass.services.async_register(DOMAIN, "clear_temp_control", svc_clear_temp_control)
        hass.services.async_register(DOMAIN, "apply_profile", svc_apply_profile)
        hass.services.async_register(DOMAIN, "save_profile", svc_save_profile)
        hass.services.async_register(DOMAIN, "list_profiles", svc_list_profiles)

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to new version.

    Version 1 -> 2: Migrate single-fan options to per-fan structure.
    - min_pwm, min_pwm_calibrated, temp_entity, temp_curve moved to fans["0"]
    - Preserves backward compatibility for existing single-fan users
    """
    _LOGGER.debug("Migrating OpenFAN Micro entry from version %s", entry.version)

    if entry.version == 1:
        # Version 1 -> 2: Migrate to per-fan options structure
        new_options = dict(entry.options or {})

        # Only migrate if "fans" structure doesn't exist yet
        if "fans" not in new_options:
            # Keys that should be per-fan for index 0
            per_fan_keys = [
                "min_pwm",
                "min_pwm_calibrated",
                "temp_entity",
                "temp_curve",
                "temp_integrate_seconds",
                "temp_update_min_interval",
                "temp_deadband_pct",
            ]

            fan_0_opts: dict[str, Any] = {}
            for key in per_fan_keys:
                if key in new_options:
                    fan_0_opts[key] = new_options[key]
                    # Keep global defaults but fan-specific values move to fans["0"]

            if fan_0_opts:
                new_options["fans"] = {"0": fan_0_opts}
                _LOGGER.info(
                    "Migrated single-fan options to per-fan structure for %s: %s",
                    entry.title,
                    list(fan_0_opts.keys()),
                )

        # Update entry to version 2
        hass.config_entries.async_update_entry(
            entry,
            options=new_options,
            version=2,
        )
        _LOGGER.info("Migration to version 2 successful for %s", entry.title)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_get_options_flow(config_entry):
    """Return the options flow handler."""
    return OptionsFlowHandler(config_entry)
