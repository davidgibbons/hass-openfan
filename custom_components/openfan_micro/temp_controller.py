"""Per-fan temperature controller with piecewise-linear curves and smoothing."""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

_LOGGER = logging.getLogger(__name__)

# Default profile curves
DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "quiet": {
        "temp_curve": "45=25, 60=55, 75=100",
        "temp_integrate_seconds": 60,
        "temp_update_min_interval": 15,
        "temp_deadband_pct": 5,
    },
    "balanced": {
        "temp_curve": "45=35, 60=60, 70=100",
        "temp_integrate_seconds": 30,
        "temp_update_min_interval": 10,
        "temp_deadband_pct": 3,
    },
    "aggressive": {
        "temp_curve": "45=40, 55=70, 65=100",
        "temp_integrate_seconds": 15,
        "temp_update_min_interval": 5,
        "temp_deadband_pct": 2,
    },
}


def parse_curve(txt: str) -> list[tuple[float, int]]:
    """Parse temp curve string like '45=25, 60=55, 75=100' into [(temp, pwm), ...]."""
    pts: list[tuple[float, int]] = []
    for part in [p.strip() for p in txt.split(",") if p.strip()]:
        if "=" in part:
            t, pct = part.split("=", 1)
            try:
                pts.append((float(t.strip()), max(0, min(100, int(pct.strip())))))
            except Exception:
                continue
    pts.sort(key=lambda x: x[0])
    return pts


@dataclass
class FanControllerState:
    """State for a single fan's temperature controller."""

    fan_index: int
    active: bool = False
    temp_entity: str = ""
    temp_curve: str = ""
    profile: str = ""  # Name of applied profile, empty if custom
    temp_integrate_seconds: int = 30
    temp_update_min_interval: int = 10
    temp_deadband_pct: int = 3
    min_pwm: int = 0
    min_pwm_calibrated: bool = False
    temp_avg: Optional[float] = None
    last_target_pwm: Optional[int] = None
    last_applied_pwm: Optional[int] = None
    last_apply_ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for diagnostics/attributes."""
        return {
            "fan_index": self.fan_index,
            "active": self.active,
            "profile": self.profile,
            "temp_entity": self.temp_entity,
            "temp_curve": self.temp_curve,
            "temp_integrate_seconds": self.temp_integrate_seconds,
            "temp_update_min_interval": self.temp_update_min_interval,
            "temp_deadband_pct": self.temp_deadband_pct,
            "min_pwm": self.min_pwm,
            "min_pwm_calibrated": self.min_pwm_calibrated,
            "temp_avg": self.temp_avg,
            "last_target_pwm": self.last_target_pwm,
            "last_applied_pwm": self.last_applied_pwm,
        }


class FanTempController:
    """Temperature controller for a single fan."""

    def __init__(
        self,
        hass: HomeAssistant,
        fan_index: int,
        host: str,
        set_pwm_callback: Callable[[int, int], Any],
        get_options_callback: Callable[[], dict[str, Any]],
    ) -> None:
        self.hass = hass
        self.fan_index = fan_index
        self.host = host
        self._set_pwm = set_pwm_callback
        self._get_options = get_options_callback
        self._temp_buf: deque[tuple[float, float]] = deque(maxlen=512)
        self._unsub_temp: Optional[Callable[[], None]] = None
        self._current_temp_entity: str = ""
        self.state = FanControllerState(fan_index=fan_index)

    def _get_fan_options(self) -> dict[str, Any]:
        """Get merged options for this fan (global + per-fan + profile)."""
        opts = self._get_options()
        fan_opts = (opts.get("fans") or {}).get(str(self.fan_index)) or {}

        # Start with global defaults
        result = {
            "min_pwm": int(opts.get("min_pwm", 0)),
            "min_pwm_calibrated": bool(opts.get("min_pwm_calibrated", False)),
            "temp_entity": (opts.get("temp_entity") or "").strip(),
            "temp_curve": (opts.get("temp_curve") or "").strip(),
            "temp_integrate_seconds": int(opts.get("temp_integrate_seconds", 30)),
            "temp_update_min_interval": int(opts.get("temp_update_min_interval", 10)),
            "temp_deadband_pct": int(opts.get("temp_deadband_pct", 3)),
            "profile": "",
        }

        # Check if fan has a profile selected
        profile_name = fan_opts.get("profile", "").strip()
        if profile_name:
            # Load profile settings (custom profiles from options, or built-in defaults)
            profiles = opts.get("profiles") or {}
            profile_data = profiles.get(profile_name) or DEFAULT_PROFILES.get(profile_name) or {}
            if profile_data:
                result["profile"] = profile_name
                for key in (
                    "temp_curve",
                    "temp_integrate_seconds",
                    "temp_update_min_interval",
                    "temp_deadband_pct",
                ):
                    if key in profile_data:
                        result[key] = profile_data[key]

        # Per-fan overrides (except when using a profile for curve settings)
        if fan_opts.get("min_pwm") is not None:
            result["min_pwm"] = int(fan_opts["min_pwm"])
        if fan_opts.get("min_pwm_calibrated") is not None:
            result["min_pwm_calibrated"] = bool(fan_opts["min_pwm_calibrated"])
        if fan_opts.get("temp_entity"):
            result["temp_entity"] = fan_opts["temp_entity"].strip()

        # Per-fan curve override (only if no profile, or explicit override)
        if not profile_name and fan_opts.get("temp_curve"):
            result["temp_curve"] = fan_opts["temp_curve"]

        return result

    def _averaged_temp(self, now: float, integrate_seconds: int) -> Optional[float]:
        """Return averaged temp over integration window; prune old samples."""
        cutoff = now - max(5, integrate_seconds)
        while self._temp_buf and self._temp_buf[0][0] < cutoff:
            self._temp_buf.popleft()
        if not self._temp_buf:
            return None
        return sum(val for _, val in self._temp_buf) / len(self._temp_buf)

    async def apply(self, trigger: str) -> None:
        """Compute target PWM from temperature and apply with deadband/min-interval."""
        fan_opts = self._get_fan_options()

        min_pwm = fan_opts["min_pwm"]
        min_cal_ok = fan_opts["min_pwm_calibrated"] and min_pwm > 0
        te = fan_opts["temp_entity"]
        pts = parse_curve(fan_opts["temp_curve"])

        # Update state for diagnostics
        self.state.min_pwm = min_pwm
        self.state.min_pwm_calibrated = fan_opts["min_pwm_calibrated"]
        self.state.temp_entity = te
        self.state.temp_curve = fan_opts["temp_curve"]
        self.state.profile = fan_opts["profile"]
        self.state.temp_integrate_seconds = fan_opts["temp_integrate_seconds"]
        self.state.temp_update_min_interval = fan_opts["temp_update_min_interval"]
        self.state.temp_deadband_pct = fan_opts["temp_deadband_pct"]

        # Gate: calibration + config present
        if not (min_cal_ok and te and pts):
            self.state.active = False
            _LOGGER.debug(
                "OpenFAN %s fan[%d] temp-control gated (cal=%s, temp_entity=%s, pts=%d, trig=%s)",
                self.host,
                self.fan_index,
                min_cal_ok,
                bool(te),
                len(pts),
                trigger,
            )
            return

        self.state.active = True
        now = time.monotonic()
        temp = self._averaged_temp(now, fan_opts["temp_integrate_seconds"])

        if temp is None:
            # Try to bootstrap from current state
            st = self.hass.states.get(te)
            if st and st.state not in ("unknown", "unavailable", ""):
                try:
                    val = float(st.state)
                    self._temp_buf.append((now, val))
                    temp = self._averaged_temp(now, fan_opts["temp_integrate_seconds"])
                except Exception:
                    temp = None

        if temp is None:
            _LOGGER.debug(
                "OpenFAN %s fan[%d] temp-control: no temp sample yet (trigger=%s)",
                self.host,
                self.fan_index,
                trigger,
            )
            return

        # Piecewise-linear interpolation on averaged temp
        if temp <= pts[0][0]:
            target = pts[0][1]
        elif temp >= pts[-1][0]:
            target = pts[-1][1]
        else:
            target = pts[0][1]
            for (t1, p1), (t2, p2) in zip(pts, pts[1:]):
                if t1 <= temp <= t2:
                    if t2 == t1:
                        target = max(p1, p2)
                    else:
                        ratio = (temp - t1) / (t2 - t1)
                        target = int(round(p1 + (p2 - p1) * ratio))
                    break

        # Clamp by min (except allow 0 to turn off)
        target = 0 if target == 0 else max(min_pwm, target)
        target = max(0, min(100, target))

        last_applied = self.state.last_applied_pwm
        last_ts = self.state.last_apply_ts
        dead = fan_opts["temp_deadband_pct"]
        min_iv = fan_opts["temp_update_min_interval"]

        # Deadband check
        if last_applied is not None and abs(target - last_applied) < max(0, dead):
            self.state.temp_avg = temp
            self.state.last_target_pwm = target
            return

        # Minimum interval check
        if (now - last_ts) < max(1, min_iv):
            self.state.temp_avg = temp
            self.state.last_target_pwm = target
            return

        # Apply PWM
        await self._set_pwm(self.fan_index, target)
        self.state.temp_avg = temp
        self.state.last_target_pwm = target
        self.state.last_applied_pwm = target
        self.state.last_apply_ts = now

        _LOGGER.debug(
            "OpenFAN %s fan[%d] temp-control APPLY: temp=%.1f°C target=%d%% (min=%d%%, profile=%s, trig=%s)",
            self.host,
            self.fan_index,
            temp,
            target,
            min_pwm,
            self.state.profile or "custom",
            trigger,
        )

    def subscribe_temp_entity(self, temp_entity: str) -> None:
        """Subscribe to temperature entity state changes."""
        if self._unsub_temp:
            try:
                self._unsub_temp()
            except Exception:
                pass
            self._unsub_temp = None

        self._current_temp_entity = temp_entity
        if not temp_entity:
            return

        @callback
        def _on_temp(ev):
            new = ev.data.get("new_state")
            if not new or new.state in (None, "", "unknown", "unavailable"):
                return
            try:
                val = float(new.state)
            except Exception:
                return
            self._temp_buf.append((time.monotonic(), val))
            self.hass.async_create_task(self.apply("state_change"))

        self._unsub_temp = async_track_state_change_event(self.hass, [temp_entity], _on_temp)

    def unsubscribe(self) -> None:
        """Unsubscribe from temperature entity."""
        if self._unsub_temp:
            try:
                self._unsub_temp()
            except Exception:
                pass
            self._unsub_temp = None

    def clear(self) -> None:
        """Clear controller state and unsubscribe."""
        self.unsubscribe()
        self._temp_buf.clear()
        self.state.active = False
        self.state.temp_avg = None
        self.state.last_target_pwm = None
        self.state.last_applied_pwm = None
        self.state.last_apply_ts = 0.0
