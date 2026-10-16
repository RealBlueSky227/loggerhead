from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .hardware import ALLOWED_DIGITAL_BCM, DiagnosticHalt

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OneWireTemperatureReading:
    bcm_pin: int
    sensor_id: str
    bus_master: str
    celsius: float
    fahrenheit: float
    crc_valid: bool
    ext_power: int | None = None
    resolution: int | None = None
    power_up_default_suspect: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "driver": "kernel_w1_gpio",
            "bcm_pin": self.bcm_pin,
            "sensor_id": self.sensor_id,
            "bus_master": self.bus_master,
            "celsius": self.celsius,
            "crc_valid": self.crc_valid,
            "ext_power": self.ext_power,
            "resolution": self.resolution,
            "power_up_default_suspect": self.power_up_default_suspect,
        }


@dataclass(frozen=True)
class RuntimeOverlay:
    index: int
    name: str
    params: dict[str, str] = field(default_factory=dict)

    @property
    def gpio(self) -> int | None:
        if self.name != "w1-gpio":
            return None
        raw = self.params.get("gpiopin")
        if raw is None:
            return 4
        try:
            return int(raw, 0)
        except ValueError:
            return None

    def identity(self) -> dict[str, Any]:
        return {"index": self.index, "name": self.name, "params": dict(self.params)}


@dataclass(frozen=True)
class OneWireOverlayResult:
    action: str
    gpio: int
    ok: bool = True
    created: bool = False
    owned: bool = False
    already_active: bool = False
    affected_gpios: tuple[int, ...] = ()
    safe_input: bool | None = None
    message: str = ""
    overlays: tuple[RuntimeOverlay, ...] = ()
    owned_gpios: tuple[int, ...] = ()
    bus_master: str = ""
    owned_bus_masters: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> OneWireOverlayResult:
        overlays = tuple(
            RuntimeOverlay(
                int(item["index"]),
                str(item["name"]),
                {str(key): str(value) for key, value in dict(item.get("params", {})).items()},
            )
            for item in payload.get("overlays", [])
        )
        return cls(
            action=str(payload.get("action", "")),
            gpio=int(payload.get("gpio", -1)),
            ok=bool(payload.get("ok", False)),
            created=bool(payload.get("created", False)),
            owned=bool(payload.get("owned", False)),
            already_active=bool(payload.get("already_active", False)),
            affected_gpios=tuple(int(item) for item in payload.get("affected_gpios", [])),
            safe_input=payload.get("safe_input"),
            message=str(payload.get("message", "")),
            overlays=overlays,
            owned_gpios=tuple(int(item) for item in payload.get("owned_gpios", [])),
            bus_master=str(payload.get("bus_master", "")),
            owned_bus_masters={
                int(key): str(value) for key, value in dict(payload.get("owned_bus_masters", {})).items()
            },
        )


class OneWireOverlayClient(Protocol):
    def activate(self, gpio: int) -> OneWireOverlayResult: ...

    def release(self, gpio: int) -> OneWireOverlayResult: ...

    def status(self) -> OneWireOverlayResult: ...


class HelperOneWireOverlayClient:
    """Least-privilege client for dynamic Raspberry Pi w1-gpio overlays.

    The helper is expected to be installed as a root-owned, non-user-writable
    executable with sudoers allowing only this fixed command. It validates GPIOs
    internally and never accepts arbitrary overlay names or shell strings.
    """

    def __init__(
        self,
        *,
        helper_path: str | None = None,
        sudo_command: str | None = None,
        subprocess_run: Callable[..., Any] = subprocess.run,
        timeout: float = 8.0,
        status_cache_seconds: float = 30.0,
        state_path: Path = Path("/run/loggerhead-onewire/owned.json"),
        overlays_path: Path = Path("/sys/kernel/config/device-tree/overlays"),
        masters_path: Path = Path("/sys/bus/w1/devices"),
    ) -> None:
        self.helper_path = (
            helper_path or shutil.which("loggerhead-onewire-helper") or "/usr/local/sbin/loggerhead-onewire-helper"
        )
        self.sudo_command = sudo_command or shutil.which("sudo") or "/usr/bin/sudo"
        self._subprocess_run = subprocess_run
        self.timeout = timeout
        self._status_cache_seconds = status_cache_seconds
        self._state_path = state_path
        self._overlays_path = overlays_path
        self._masters_path = masters_path
        self._status_lock = threading.RLock()
        self._cached_status: OneWireOverlayResult | None = None
        self._status_signature: tuple | None = None
        self._status_expires = 0.0

    def activate(self, gpio: int) -> OneWireOverlayResult:
        with self._status_lock:
            self._cached_status = None
            return self._call("activate", gpio)

    def release(self, gpio: int) -> OneWireOverlayResult:
        with self._status_lock:
            self._cached_status = None
            return self._call("release", gpio)

    def status(self) -> OneWireOverlayResult:
        with self._status_lock:
            signature = self._status_fingerprint()
            if (
                signature is not None
                and signature == self._status_signature
                and self._cached_status is not None
                and time.monotonic() < self._status_expires
            ):
                return self._cached_status
            self._cached_status = None
            result = self._call("status", None)
            # Cache only if observable ownership evidence stayed unchanged during the call.
            if signature is not None and signature == self._status_fingerprint():
                self._cached_status = result
                self._status_signature = signature
                self._status_expires = time.monotonic() + self._status_cache_seconds
            return result

    def verification_token(self) -> tuple | None:
        """Unprivileged evidence, not a helper call or a time-based lease."""
        return self._status_fingerprint()

    def _status_fingerprint(self) -> tuple | None:
        def stamp(path: Path) -> tuple:
            stat = path.stat()
            return str(path), stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns

        def overlay_evidence(path: Path) -> tuple:
            stat = path.stat()
            contents = []
            # Configfs recreates attribute inodes on readdir and changes directory timestamps.
            # Fingerprint the actual overlay, including its applied state, instead.
            for name in ("dtbo", "path", "status"):
                with (path / name).open("rb") as handle:
                    data = handle.read(262145)
                if len(data) > 262144:
                    raise OSError("Overlay evidence exceeds the cache verification limit")
                contents.append((name, hashlib.sha256(data).digest()))
            return str(path), stat.st_dev, stat.st_ino, tuple(contents)

        try:
            overlays = sorted(self._overlays_path.iterdir())
            masters = sorted(path for path in self._masters_path.iterdir() if path.name.startswith("w1_bus_master"))
            return (
                stamp(self._state_path),
                tuple(overlay_evidence(path) for path in overlays),
                # Slave attachment changes master directory timestamps without
                # changing bus ownership. Detect object replacement, not reads.
                tuple((str(path), str(path.resolve()), path.stat().st_dev, path.stat().st_ino) for path in masters),
            )
        except OSError:
            # Missing/inaccessible evidence disables caching, never ownership checks.
            return None

    def _call(self, operation: str, gpio: int | None) -> OneWireOverlayResult:
        if gpio is not None:
            _validate_gpio(gpio)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise PermissionError(
                "Refusing to manage kernel 1-Wire overlays from a root Loggerhead process; "
                "run Loggerhead unprivileged and install the root-owned helper."
            )
        command = [self.sudo_command, "-n", self.helper_path, operation]
        if gpio is not None:
            command.append(str(gpio))
        try:
            result = self._subprocess_run(command, capture_output=True, check=False, text=True, timeout=self.timeout)
        except Exception as exc:
            raise RuntimeError(f"Kernel 1-Wire helper {operation} failed to start: {exc}") from exc
        stdout = str(result.stdout).strip()
        stderr = str(result.stderr).strip()
        if result.returncode != 0:
            detail = stderr or stdout or f"exit code {result.returncode}"
            raise RuntimeError(f"Kernel 1-Wire helper {operation} failed: {detail}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Kernel 1-Wire helper returned non-JSON output: {stdout!r} {stderr!r}") from exc
        parsed = OneWireOverlayResult.from_payload(payload)
        if not parsed.ok:
            raise RuntimeError(parsed.message or f"Kernel 1-Wire helper {operation} failed.")
        return parsed


class NullOneWireOverlayClient:
    """Test/simulation overlay client that never touches host state."""

    def activate(self, gpio: int) -> OneWireOverlayResult:
        _validate_gpio(gpio)
        return OneWireOverlayResult("activate", gpio, ok=True, owned=False, already_active=True)

    def release(self, gpio: int) -> OneWireOverlayResult:
        _validate_gpio(gpio)
        return OneWireOverlayResult("release", gpio, ok=True, owned=False, safe_input=None)

    def status(self) -> OneWireOverlayResult:
        return OneWireOverlayResult("status", -1, ok=True)


def parse_dtoverlay_list(text: str) -> tuple[RuntimeOverlay, ...]:
    overlays: list[RuntimeOverlay] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        left, right = line.split(":", 1)
        try:
            index = int(left.strip())
        except ValueError:
            continue
        parts = right.strip().split()
        if not parts:
            continue
        name = parts[0]
        params: dict[str, str] = {}
        for part in parts[1:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            params[key.strip()] = value.strip().strip(",")
        overlays.append(RuntimeOverlay(index, name, params))
    return tuple(overlays)


def _validate_gpio(gpio: int) -> None:
    if int(gpio) not in ALLOWED_DIGITAL_BCM:
        raise DiagnosticHalt(f"BCM GPIO {gpio} is not a permitted Loggerhead SensePort digital pin.")
