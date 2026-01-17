"""Coordinator with availability gating, LED/12V state, and stall detection."""

from __future__ import annotations
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OpenFanApi

_LOGGER = logging.getLogger(__name__)


class OpenFanCoordinator(DataUpdateCoordinator[dict]):
    """Poll device: RPM/PWM + LED + 12V, and track failures & stall."""

    def __init__(self, hass: HomeAssistant, api: OpenFanApi) -> None:
        interval = int(getattr(api, "_poll_interval", 5) or 5)
        super().__init__(
            hass,
            _LOGGER,
            name="OpenFAN Micro",
            update_interval=timedelta(seconds=interval),
        )
        self.api = api
        self._consecutive_failures = 0
        self._forced_unavailable = False
        self._consecutive_stall = 0
        self._notified_stall = False
        self._stall_by_index: dict[int, int] = {}
        self._notified_by_index: dict[int, bool] = {}
        self._last_error: str | None = None

    async def _async_update_data(self) -> dict:
        try:
            rpm_by_index = await self.api.get_status_all()
            # clear failure gating
            self._consecutive_failures = 0
            self._forced_unavailable = False
            self._last_error = None

            # LED / 12V (tolerate if firmware does not support)

            led, is_12v = False, False
            try:
                led, is_12v = await self.api.get_openfan_status()
            except Exception as sub_err:
                _LOGGER.debug("OpenFAN Micro: openfan/status fetch failed: %r", sub_err)

            # Stall: per fan if PWM > min and RPM=0 for N cycles
            min_pwm = int(getattr(self.api, "_min_pwm", 0) or 0)
            need = int(getattr(self.api, "_stall_consecutive", 3) or 3)

            fans: dict[int, dict[str, int | bool]] = {}

            for idx, rpm in rpm_by_index.items():
                pwm = int(self.api._last_pwm_by_index.get(int(idx), 0))
                stalled_now = (int(pwm) > max(0, min_pwm)) and int(rpm) == 0
                prev = int(self._stall_by_index.get(idx, 0))
                new_count = (prev + 1) if stalled_now else 0
                self._stall_by_index[idx] = new_count
                stalled_flag = new_count >= need

                if stalled_flag and not bool(self._notified_by_index.get(idx, False)):
                    self._notified_by_index[idx] = True
                    try:
                        self.hass.bus.async_fire(
                            "openfan_micro_stall",
                            {"host": getattr(self.api, "_host", "?"), "fan_index": idx},
                        )
                        self.hass.components.persistent_notification.async_create(
                            f"Fan looks stalled on {getattr(self.api, '_host', '?')} (Fan {idx}, PWM={pwm}%, RPM=0)",
                            title="OpenFAN Micro",
                            notification_id=f"openfan_micro_stall_{getattr(self.api, '_host', '?')}_{idx}",
                        )
                    except Exception:
                        pass
                if not stalled_flag:
                    self._notified_by_index[idx] = False

                fans[int(idx)] = {
                    "rpm": int(max(0, rpm)),
                    "pwm": int(max(0, min(100, pwm))),
                    "stalled": bool(stalled_flag),
                }

            data = {
                "fans": fans,
                "led": bool(led),
                "is_12v": bool(is_12v),
            }
            _LOGGER.debug("OpenFAN Micro update OK (%s): %s", getattr(self.api, "_host", "?"), data)
            return data

        except Exception as err:
            self._last_error = str(err)
            self._consecutive_failures += 1
            fail_thresh = int(getattr(self.api, "_failure_threshold", 3) or 3)
            if self._consecutive_failures >= fail_thresh:
                self._forced_unavailable = True
            _LOGGER.error(
                "OpenFAN Micro update failed (%s): %r", getattr(self.api, "_host", "?"), err
            )
            raise UpdateFailed(f"Failed to update OpenFAN Micro: {err}") from err
