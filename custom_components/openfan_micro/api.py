"""Low-level HTTP API client for OpenFAN Micro.

Features:
- Robust GET handling (JSON or plain text)
- Firmware compatibility (new/legacy fan status/set endpoints)
- Status payload normalization (top-level vs "data" container)
- LED control and 5V/12V supply switching per documented endpoints
"""

from __future__ import annotations

from typing import Any, Tuple, Optional
import logging
import asyncio

import aiohttp

_LOGGER = logging.getLogger(__name__)


class OpenFanApi:
    def __init__(self, base_url: str, session: aiohttp.ClientSession) -> None:
        # Normalize: remove trailing slash
        self._base_url = base_url.rstrip("/")
        self._session = session
        # Tunables populated from options in __init__.py
        self._poll_interval: int = 5
        self._min_pwm: int = 0
        self._failure_threshold: int = 3
        self._stall_consecutive: int = 3
        self._fan_count: int = 1
        self._last_pwm_by_index: dict[int, int] = {}

    # -------------------- HTTP helpers --------------------

    async def _get_any(self, path: str) -> tuple[int, str, Optional[dict]]:
        """HTTP GET that returns (status_code, text, json_or_none).

        We *do not* fail if body is not JSON (some firmwares reply plain 'OK').
        """
        url = f"{self._base_url}{path}"
        async with asyncio.timeout(6):
            async with self._session.get(url) as resp:
                status = resp.status
                text = await resp.text()
                data = None
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    # not JSON (acceptable for 'set' endpoints)
                    pass
        _LOGGER.debug("OpenFAN %s GET %s -> %s %s", self._base_url, path, status, data or text)
        return status, text, data

    async def _get_json(self, path: str) -> dict:
        """HTTP GET that *requires* JSON. Raises on HTTP error or non-JSON."""
        status, text, data = await self._get_any(path)
        if status >= 400:
            _LOGGER.error("OpenFAN %s HTTP %s on %s: %s", self._base_url, status, path, text)
            raise RuntimeError(f"HTTP {status} for {path}")
        if not isinstance(data, dict):
            _LOGGER.error("OpenFAN %s expected JSON on %s but got: %s", self._base_url, path, text)
            raise RuntimeError(f"Non-JSON response for {path}")
        return data

    def _is_ok_payload(self, payload: Optional[dict], text: str = "") -> bool:
        """Return True if payload/text indicates success."""
        if isinstance(payload, dict):
            val = str(payload.get("status", "")).lower()
            if val in ("ok", "success", ""):
                return True
        # Some firmwares just return 'OK' or empty body on success
        if text.strip().upper() in ("OK", "SUCCESS", ""):
            return True
        return False

    # -------------------- FAN PWM / STATUS --------------------

    def _parse_status_payload(self, data: dict) -> Tuple[int, int]:
        """Normalize (rpm, pwm%) from possible layouts."""
        container: dict[str, Any] = data or {}
        if not ("rpm" in container or "pwm_percent" in container or "pwm" in container):
            container = container.get("data", {}) or {}

        rpm_raw = container.get("rpm", 0)
        pwm_raw = container.get("pwm_percent", container.get("pwm", container.get("pwm_value", 0)))

        try:
            rpm = int(float(rpm_raw))
        except Exception:
            rpm = 0
        try:
            pwm = int(float(pwm_raw))
        except Exception:
            pwm = 0

        return max(0, rpm), max(0, min(100, pwm))

    def _parse_multi_fan_payload(self, data: dict) -> dict[int, int]:
        """Normalize a multi-fan RPM payload into {index: rpm}."""
        container: dict[str, Any] = data or {}
        if "data" in container and isinstance(container.get("data"), dict):
            container = container.get("data", {}) or {}

        rpm_by_index: dict[int, int] = {}
        for raw_index, raw_value in container.items():
            try:
                idx = int(raw_index)
            except Exception:
                continue
            try:
                rpm = int(float(raw_value))
            except Exception:
                rpm = 0
            if 0 <= idx <= 9:
                rpm_by_index[idx] = max(0, rpm)
        return rpm_by_index

    async def get_status(self) -> Tuple[int, int]:
        """Return (rpm, pwm_percent) for legacy single-fan firmware."""
        rpm_by_index = await self.get_status_all()
        rpm = rpm_by_index.get(0, 0)
        pwm = int(self._last_pwm_by_index.get(0, 0))
        return rpm, pwm

    async def get_status_all(self) -> dict[int, int]:
        """Return {index: rpm} for all fans; supports single-fan fallback."""
        last_exc: Optional[Exception] = None
        for path in ("/api/v0/fan/status", "/api/v0/fan/0/status"):
            try:
                data = await self._get_json(path)
                if path.endswith("/fan/0/status"):
                    rpm, pwm = self._parse_status_payload(data)
                    self._fan_count = max(self._fan_count, 1)
                    self._last_pwm_by_index[0] = int(pwm)
                    return {0: rpm}
                rpm_by_index = self._parse_multi_fan_payload(data)
                if rpm_by_index:
                    self._fan_count = max(self._fan_count, max(rpm_by_index.keys(), default=0) + 1)
                    return rpm_by_index
                rpm, pwm = self._parse_status_payload(data)
                self._fan_count = max(self._fan_count, 1)
                self._last_pwm_by_index[0] = int(pwm)
                return {0: rpm}
            except Exception as exc:
                last_exc = exc
                _LOGGER.debug(
                    "OpenFAN %s: get_status_all via %s failed: %r", self._base_url, path, exc
                )

        assert last_exc is not None
        raise last_exc

    async def get_status_index(self, index: int) -> Tuple[int, int]:
        """Return (rpm, pwm_percent) for a specific fan index."""
        rpm_by_index = await self.get_status_all()
        rpm = rpm_by_index.get(int(index), 0)
        pwm = int(self._last_pwm_by_index.get(int(index), 0))
        return rpm, pwm

    async def set_pwm(self, value: int) -> dict[str, Any]:
        """Set PWM 0..100 on fan index 0 (legacy)."""
        return await self.set_pwm_index(0, value)

    async def set_pwm_index(self, index: int, value: int) -> dict[str, Any]:
        """Set PWM 0..100 for a specific fan index.

        Treats non-JSON 'OK' responses as success.
        """
        value = max(0, min(100, int(value)))
        idx = int(index)
        last_exc: Optional[Exception] = None
        for path in (
            f"/api/v0/fan/{idx}/pwm?value={value}",
            f"/api/v0/fan/{idx}/set?value={value}",
            f"/api/v0/fan/set?value={value}",
        ):
            try:
                status, text, data = await self._get_any(path)
                if status < 400 and self._is_ok_payload(data, text):
                    self._last_pwm_by_index[idx] = value
                    self._fan_count = max(self._fan_count, idx + 1)
                    return data or {"status": "ok"}
                raise RuntimeError(f"Bad response on {path}: {status} {text!r}")
            except Exception as exc:
                last_exc = exc
                _LOGGER.debug(
                    "OpenFAN %s: set_pwm_index via %s failed: %r", self._base_url, path, exc
                )
        assert last_exc is not None
        raise last_exc

    async def set_pwm_all(self, value: int) -> dict[str, Any]:
        """Set PWM 0..100 for all fans (full app)."""
        value = max(0, min(100, int(value)))
        status, text, data = await self._get_any(f"/api/v0/fan/all/set?value={value}")
        if status >= 400:
            raise RuntimeError(f"Bad response on /api/v0/fan/all/set: {status} {text!r}")
        if self._is_ok_payload(data, text):
            for idx in range(max(1, int(self._fan_count))):
                self._last_pwm_by_index[idx] = value
            return data or {"status": "ok"}
        raise RuntimeError(f"Bad response on /api/v0/fan/all/set: {status} {text!r}")

    async def set_rpm_index(self, index: int, value: int) -> dict[str, Any]:
        """Set target RPM for a specific fan index (full app)."""
        idx = int(index)
        val = int(value)
        status, text, data = await self._get_any(f"/api/v0/fan/{idx}/rpm?value={val}")
        if status >= 400:
            raise RuntimeError(f"Bad response on /api/v0/fan/{idx}/rpm: {status} {text!r}")
        if self._is_ok_payload(data, text):
            self._fan_count = max(self._fan_count, idx + 1)
            return data or {"status": "ok"}
        raise RuntimeError(f"Bad response on /api/v0/fan/{idx}/rpm: {status} {text!r}")

    # -------------------- LED & SUPPLY VOLTAGE --------------------

    async def get_openfan_status(self) -> Tuple[bool, bool]:
        """Return (led_enabled, is_12v) from /api/v0/openfan/status."""
        data = await self._get_json("/api/v0/openfan/status")
        # expected: {"status":"ok","data":{"act_led_enabled":"true","fan_is_12v":"true"}}
        container = data.get("data", data)
        led_raw = str(container.get("act_led_enabled", "false")).strip().lower()
        v12_raw = str(container.get("fan_is_12v", "false")).strip().lower()
        led = led_raw in ("true", "1", "yes", "on")
        is_12v = v12_raw in ("true", "1", "yes", "on")
        return led, is_12v

    async def led_set(self, enabled: bool) -> dict:
        """Enable/disable activity LED (supported firmwares)."""
        path = "/api/v0/led/enable" if enabled else "/api/v0/led/disable"
        status, text, data = await self._get_any(path)
        if status >= 400:
            raise RuntimeError(f"LED set failed: {status} {text}")
        if not self._is_ok_payload(data, text):
            _LOGGER.debug("OpenFAN %s: LED set non-OK body: %s", self._base_url, data or text)
        return data or {"status": "ok"}

    async def set_voltage_12v(self, enabled: bool) -> dict:
        """Switch fan supply to 12V (True) or 5V (False). Requires confirm=true."""
        path = (
            "/api/v0/fan/voltage/high?confirm=true"
            if enabled
            else "/api/v0/fan/voltage/low?confirm=true"
        )
        status, text, data = await self._get_any(path)
        if status >= 400:
            raise RuntimeError(f"Voltage set failed: {status} {text}")
        if not self._is_ok_payload(data, text):
            _LOGGER.debug("OpenFAN %s: voltage set non-OK body: %s", self._base_url, data or text)
        return data or {"status": "ok"}
