from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hardware import (
    BUZZER_PWM_BCM,
    MCP23017_ADDRESS,
    PH_EZO_I2C_ADDRESS,
    STEPPERS,
    AlarmPriority,
    DiagnosticHalt,
    LevelState,
    RelayAssignment,
    StepperAssignment,
)

LOGGER = logging.getLogger(__name__)


class HardwareUnavailable(RuntimeError):
    """Raised when a hardware operation is requested without available Pi libraries."""


class HardwareFault(RuntimeError):
    """Raised when actuator hardware cannot be proven to be in the requested state."""


class HydrosTripleClassifier:
    """Classifies Hydros Triple PWM periods into water-level states.

    Implements SRS 2.3.2.1 through 2.3.2.4.
    """

    # HYDROS PWM is captured on rising edges only, so these windows are full
    # rising-to-rising periods. Older SRS values represented half-cycle edge
    # intervals and would misclassify a measured dry period near 50 ms.
    MEASUREMENT_METHOD = "rising_to_rising_period_us"
    WINDOWS: tuple[tuple[LevelState, int, int], ...] = (
        (LevelState.HIGH, 2000, 3000),
        (LevelState.NORMAL, 4000, 6000),
        (LevelState.LOW, 8000, 12000),
        (LevelState.DRY, 40000, 60000),
    )

    def __init__(self, *, debounce_samples: int = 3, activity_timeout: float = 2.0) -> None:
        self.debounce_samples = max(1, debounce_samples)
        self.activity_timeout = activity_timeout
        self._last_edge = time.monotonic()
        self._candidate: LevelState = LevelState.UNKNOWN
        self._candidate_count = 0
        self.state = LevelState.UNKNOWN

    @classmethod
    def classify_period_us(cls, period_us: float) -> LevelState:
        for state, low, high in cls.WINDOWS:
            if low <= period_us <= high:
                return state
        return LevelState.UNKNOWN

    def observe_period_us(self, period_us: float) -> LevelState:
        self._last_edge = time.monotonic()
        candidate = self.classify_period_us(period_us)
        if candidate == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = candidate
            self._candidate_count = 1
        if self._candidate_count >= self.debounce_samples:
            self.state = candidate
        return self.state

    def activity_state(self) -> LevelState:
        if time.monotonic() - self._last_edge > self.activity_timeout:
            self.state = LevelState.INACTIVE
        return self.state


class HydrosPulseReader:
    """Reads HYDROS triple-optical PWM periods with pigpio edge callbacks."""

    def __init__(
        self,
        bcm_pin: int,
        classifier: HydrosTripleClassifier,
        *,
        simulation: bool = False,
        pigpio_module: Any | None = None,
    ) -> None:
        self.bcm_pin = bcm_pin
        self.classifier = classifier
        self.simulation = simulation
        self.pi = None
        self._callback = None
        self._last_rising_tick: int | None = None
        self._pulse_count = 0
        self._last_period_us: float | None = None
        self._last_frequency_hz: float | None = None
        self._last_edge_ts = 0.0
        self._last_valid_edge_ts = 0.0
        self._invalid_frequency_count = 0
        self._lock = threading.RLock()
        if not simulation:
            try:
                pigpio = pigpio_module
                if pigpio is None:
                    import pigpio as pigpio_import  # type: ignore

                    pigpio = pigpio_import
                self.pi = pigpio.pi()
                if not self.pi.connected:
                    raise HardwareUnavailable("pigpiod is not connected.")
                self.pi.set_mode(self.bcm_pin, getattr(pigpio, "INPUT", 0))
                if hasattr(self.pi, "set_pull_up_down") and hasattr(pigpio, "PUD_UP"):
                    self.pi.set_pull_up_down(self.bcm_pin, pigpio.PUD_UP)
                edge = getattr(pigpio, "RISING_EDGE", 1)
                self._callback = self.pi.callback(self.bcm_pin, edge, self._on_edge)
            except Exception as exc:
                raise HardwareUnavailable(f"HYDROS PWM capture unavailable on BCM {self.bcm_pin}: {exc}") from exc

    @staticmethod
    def _tick_diff(previous: int, current: int) -> int:
        return (current - previous) & 0xFFFFFFFF

    def _on_edge(self, _gpio: int, level: int, tick: int) -> None:
        if level not in {1, 2}:  # 2 is pigpio watchdog timeout; ignore it for period capture.
            return
        if level == 2:
            return
        with self._lock:
            self._last_edge_ts = time.time()
            if self._last_rising_tick is not None:
                period_us = self._tick_diff(self._last_rising_tick, tick)
                self._pulse_count += 1
                self._last_period_us = float(period_us)
                self._last_frequency_hz = 1_000_000.0 / period_us if period_us > 0 else None
                if self.classifier.classify_period_us(period_us) == LevelState.UNKNOWN:
                    self._invalid_frequency_count += 1
                else:
                    self._last_valid_edge_ts = self._last_edge_ts
                self.classifier.observe_period_us(period_us)
            self._last_rising_tick = tick

    def read_state(self) -> LevelState:
        if self.simulation:
            with self._lock:
                self._pulse_count += 1
                self._last_period_us = 2520.0
                self._last_frequency_hz = 1_000_000.0 / 2520.0
                self._last_edge_ts = time.time()
                self._last_valid_edge_ts = self._last_edge_ts
                return self.classifier.observe_period_us(2520)
        with self._lock:
            return self.classifier.activity_state()

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            state = self.classifier.activity_state() if not self.simulation else self.classifier.state
            return {
                "bcm_pin": self.bcm_pin,
                "state": state.value,
                "frequency_hz": self._last_frequency_hz,
                "period_us": self._last_period_us,
                "pulse_count": self._pulse_count,
                "last_edge_ts": self._last_edge_ts,
                "last_valid_edge_ts": self._last_valid_edge_ts,
                "invalid_frequency_count": self._invalid_frequency_count,
                "measurement_method": self.classifier.MEASUREMENT_METHOD,
                "classification_windows_us": {
                    state.value: [low, high] for state, low, high in self.classifier.WINDOWS
                },
            }

    def close(self) -> None:
        callback = self._callback
        self._callback = None
        if callback is not None and hasattr(callback, "cancel"):
            callback.cancel()
        if self.pi is not None and hasattr(self.pi, "stop"):
            self.pi.stop()
        self.pi = None


class KasaHS300Client:
    """TP-Link Kasa HS300 local TCP client.

    Implements SRS 3.1.2.1 through 3.1.2.5.
    """

    def __init__(self, host: str, *, timeout: float = 3.0, retries: int = 2) -> None:
        self.host = host
        self.timeout = timeout
        self.retries = retries

    @staticmethod
    def encrypt(payload: str) -> bytes:
        key = 171
        output = bytearray()
        for char in payload.encode("utf-8"):
            cipher = key ^ char
            key = cipher
            output.append(cipher)
        return struct.pack(">I", len(output)) + bytes(output)

    @staticmethod
    def decrypt(frame: bytes) -> str:
        if len(frame) >= 4:
            frame = frame[4:]
        key = 171
        output = bytearray()
        for char in frame:
            plain = key ^ char
            key = char
            output.append(plain)
        return output.decode("utf-8")

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = json.dumps(payload, separators=(",", ":"))
        last_error: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                with socket.create_connection((self.host, 9999), timeout=self.timeout) as sock:
                    sock.settimeout(self.timeout)
                    sock.sendall(self.encrypt(message))
                    header = sock.recv(4)
                    if len(header) != 4:
                        raise TimeoutError("HS300 response header was incomplete.")
                    expected = struct.unpack(">I", header)[0]
                    body = bytearray()
                    while len(body) < expected:
                        chunk = sock.recv(expected - len(body))
                        if not chunk:
                            break
                        body.extend(chunk)
                    return json.loads(self.decrypt(header + bytes(body)))
            except Exception as exc:
                last_error = exc
                LOGGER.warning("Kasa HS300 request failed for %s: %s", self.host, exc)
                time.sleep(0.1)
        raise TimeoutError(f"HS300 request failed after retries: {last_error}") from last_error

    def set_outlet(self, outlet: int, on: bool) -> None:
        self.request({"context": {"child_ids": [str(outlet)]}, "system": {"set_relay_state": {"state": int(on)}}})

    def get_state(self) -> dict[str, Any]:
        return self.request({"system": {"get_sysinfo": {}}, "emeter": {"get_realtime": {}}})

    @staticmethod
    def extract_outlet_telemetry(payload: dict[str, Any], outlet: int) -> dict[str, float]:
        children = payload.get("system", {}).get("get_sysinfo", {}).get("children", [])
        if outlet >= len(children):
            return {}
        child = children[outlet]
        emeter = child.get("emeter", {}).get("get_realtime", {})
        return {
            "voltage": float(emeter.get("voltage_mv", emeter.get("voltage", 0)) or 0) / (1000 if "voltage_mv" in emeter else 1),
            "current": float(emeter.get("current_ma", emeter.get("current", 0)) or 0) / (1000 if "current_ma" in emeter else 1),
            "power": float(emeter.get("power_mw", emeter.get("power", 0)) or 0) / (1000 if "power_mw" in emeter else 1),
            "energy": float(emeter.get("total_wh", emeter.get("total", 0)) or 0),
        }


class MCP23017RelayBoard:
    """MCP23017 relay and enable-line driver.

    Implements SRS 3.1.1 and fixed relay/stepper enable assignments in SRS 8.1/8.3.
    """

    IODIRA = 0x00
    IODIRB = 0x01
    OLATA = 0x14
    OLATB = 0x15
    GPIOA = 0x12
    GPIOB = 0x13
    ALL_OUTPUTS = 0x00
    STEPPER_ENABLE_MASK_A = 0xF0

    def __init__(self, *, bus_id: int = 1, address: int = MCP23017_ADDRESS, simulation: bool = False) -> None:
        self.address = address
        self.simulation = simulation
        self.shadow_a = self.STEPPER_ENABLE_MASK_A
        self.shadow_b = 0
        self._lock = threading.RLock()
        self._fault: HardwareFault | None = None
        self.bus = None
        if not simulation:
            try:
                from smbus2 import SMBus  # type: ignore

                self.bus = SMBus(bus_id)
                self._initialize_outputs_safely()
            except Exception as exc:
                fault = exc if isinstance(exc, HardwareFault) else HardwareFault(f"MCP23017 initialization failed: {exc}")
                self._fault = fault
                LOGGER.critical("MCP23017 actuator hardware fault: %s", fault)
                if isinstance(exc, HardwareFault):
                    raise
                raise fault from exc

    def _initialize_outputs_safely(self) -> None:
        if not self.bus:
            return
        with self._lock:
            # SRS 3.4.4, 3.4.5, and 8.1 safety: TMC2209 ENN is active-low, so
            # GPA4-GPA7 must be preloaded HIGH before those pins become outputs.
            # Read existing latches first so warm restarts preserve AC relay state.
            self.shadow_a = self._read_register(self.OLATA, self.GPIOA) | self.STEPPER_ENABLE_MASK_A
            self.shadow_b = self._read_register(self.OLATB, self.GPIOB)
            self._write_register_verified(self.OLATA, self.shadow_a)
            self._write_register_verified(self.OLATB, self.shadow_b)
            self._write_register_verified(self.IODIRA, self.ALL_OUTPUTS)
            self._write_register_verified(self.IODIRB, self.ALL_OUTPUTS)

    def _read_register(self, preferred: int, fallback: int) -> int:
        if not self.bus:
            return 0
        last_error: Exception | None = None
        for register in (preferred, fallback):
            try:
                return int(self.bus.read_byte_data(self.address, register))
            except Exception as exc:
                last_error = exc
                continue
        raise HardwareFault(f"MCP23017 readback failed for registers {preferred:#04x}/{fallback:#04x}: {last_error}")

    def _write_register_verified(self, register: int, value: int) -> None:
        if self.simulation or not self.bus:
            return
        try:
            self.bus.write_byte_data(self.address, register, value)
            observed = int(self.bus.read_byte_data(self.address, register))
        except Exception as exc:
            self._fault = HardwareFault(f"MCP23017 write/readback failed for register {register:#04x}: {exc}")
            raise self._fault from exc
        if observed != value:
            self._fault = HardwareFault(
                f"MCP23017 register {register:#04x} readback mismatch: wrote {value:#04x}, observed {observed:#04x}"
            )
            raise self._fault

    def _assert_healthy(self) -> None:
        if self._fault is not None:
            raise HardwareFault(f"MCP23017 is faulted; actuator operations are blocked: {self._fault}")

    @staticmethod
    def _is_protected_stepper_pin(pin: str) -> bool:
        return pin.startswith("GPA") and pin[3:].isdigit() and 4 <= int(pin[3:]) <= 7

    def set_pin(self, pin: str, active: bool) -> None:
        port = pin[:3]
        bit = int(pin[3:])
        if port not in {"GPA", "GPB"} or bit < 0 or bit > 7:
            raise DiagnosticHalt(f"Invalid MCP23017 pin {pin!r}.")
        if self._is_protected_stepper_pin(pin):
            raise DiagnosticHalt(f"{pin} is a protected active-low TMC2209 ENN pin; use set_stepper_enabled().")
        with self._lock:
            self._assert_healthy()
            mask = 1 << bit
            if port == "GPA":
                value = self.shadow_a | mask if active else self.shadow_a & ~mask
                self._write_register_verified(self.OLATA, value)
                self.shadow_a = value
            else:
                value = self.shadow_b | mask if active else self.shadow_b & ~mask
                self._write_register_verified(self.OLATB, value)
                self.shadow_b = value

    def set_stepper_enabled(self, assignment: StepperAssignment, enabled: bool) -> None:
        # The TMC2209 ENN input is active-low: HIGH disables, LOW enables.
        # Encapsulate that inversion here so stepper code never relies on raw
        # GPIO polarity. When enabling one pump, first disable all other pumps.
        self._validate_stepper_assignment(assignment)
        with self._lock:
            self._assert_healthy()
            if enabled:
                disabled_value = self.shadow_a | self.STEPPER_ENABLE_MASK_A
                self._write_register_verified(self.OLATA, disabled_value)
                self.shadow_a = disabled_value
                bit = int(assignment.enable_pin[3:])
                enabled_value = disabled_value & ~(1 << bit)
                self._assert_single_stepper_low(enabled_value)
                self._write_register_verified(self.OLATA, enabled_value)
                self.shadow_a = enabled_value
                return
            self._disable_stepper_locked(assignment)

    def _disable_stepper_locked(self, assignment: StepperAssignment) -> None:
        bit = int(assignment.enable_pin[3:])
        value = self.shadow_a | (1 << bit)
        self._write_register_verified(self.OLATA, value)
        self.shadow_a = value

    def _validate_stepper_assignment(self, assignment: StepperAssignment) -> None:
        if not self._is_protected_stepper_pin(assignment.enable_pin):
            raise DiagnosticHalt(f"{assignment.enable_pin} is not one of the protected GPA4-GPA7 stepper ENN pins.")

    def _assert_single_stepper_low(self, value: int) -> None:
        low_mask = (~value) & self.STEPPER_ENABLE_MASK_A
        if low_mask and low_mask & (low_mask - 1):
            raise HardwareFault(f"Refusing GPIOA value {value:#04x}; multiple TMC2209 ENN lines would be LOW.")

    def disable_all_steppers(self) -> None:
        with self._lock:
            self._assert_healthy()
            self.shadow_a |= self.STEPPER_ENABLE_MASK_A
            self._write_register_verified(self.OLATA, self.shadow_a)

    @staticmethod
    def software_to_physical(desired_on: bool, *, normally_on: bool, relay: RelayAssignment) -> bool:
        # Implements SRS 3.1.3 configurable logic polarity and SRS 8.3 NC relay handling.
        active = desired_on
        if normally_on or relay.hardware_normally_closed:
            active = not active
        return active


class EzoPHSensor:
    """Atlas EZO pH I2C reader.

    Implements SRS 2.2 and 8.5.
    """

    def __init__(self, *, bus_id: int = 1, address: int = PH_EZO_I2C_ADDRESS, simulation: bool = False) -> None:
        self.address = address
        self.simulation = simulation
        self.bus = None
        if not simulation:
            try:
                from smbus2 import SMBus  # type: ignore

                self.bus = SMBus(bus_id)
            except Exception as exc:
                LOGGER.warning("EZO pH I2C unavailable: %s", exc)

    def read_ph(self) -> float:
        if self.simulation:
            return 8.1
        if not self.bus:
            raise HardwareUnavailable("EZO pH I2C bus is unavailable.")
        self.request_read()
        time.sleep(0.9)
        return self.read_response()

    def request_read(self) -> None:
        if self.simulation:
            return
        if not self.bus:
            raise HardwareUnavailable("EZO pH I2C bus is unavailable.")
        self.bus.write_i2c_block_data(self.address, ord("R"), [])

    def read_response(self) -> float:
        if self.simulation:
            return 8.1
        if not self.bus:
            raise HardwareUnavailable("EZO pH I2C bus is unavailable.")
        data = bytes(self.bus.read_i2c_block_data(self.address, 0, 32))
        text = data.rstrip(b"\x00").decode("ascii", errors="ignore")
        return float(text[1:] if text and not text[0].isdigit() else text)


class ADS1115AnalogReader:
    """ADS1115 analog channel reader for physical sense ports.

    Implements SRS 8.2 analog channels routed through the fixed ADS1115 map.
    """

    CONFIG_REGISTER = 0x01
    CONVERSION_REGISTER = 0x00

    def __init__(self, *, bus_id: int = 1, simulation: bool = False) -> None:
        self.simulation = simulation
        self.bus = None
        if not simulation:
            try:
                from smbus2 import SMBus  # type: ignore

                self.bus = SMBus(bus_id)
            except Exception as exc:
                LOGGER.warning("ADS1115 I2C unavailable: %s", exc)

    def read_voltage(self, address: int, channel: int) -> float:
        if channel < 0 or channel > 3:
            raise DiagnosticHalt(f"ADS1115 channel {channel} is outside AIN0-AIN3.")
        if self.simulation:
            return round(1.0 + channel * 0.25, 3)
        if not self.bus:
            raise HardwareUnavailable("ADS1115 I2C bus is unavailable.")
        mux = 0x04 + channel
        config = 0x8000 | (mux << 12) | 0x0200 | 0x0100 | 0x0080 | 0x0003
        self.bus.write_i2c_block_data(address, self.CONFIG_REGISTER, [(config >> 8) & 0xFF, config & 0xFF])
        time.sleep(0.01)
        raw = self.bus.read_i2c_block_data(address, self.CONVERSION_REGISTER, 2)
        value = (raw[0] << 8) | raw[1]
        if value & 0x8000:
            value -= 0x10000
        return round(value * 4.096 / 32768, 5)


class TemperatureReader:
    """Temperature sensor readers for 1-Wire bus, bit-banged GPIO, and host CPU.

    Implements SRS 2.1.1 through 2.1.3.
    """

    def __init__(
        self,
        *,
        simulation: bool = False,
        one_wire_root: Path | None = None,
        dtoverlay_command: str | None = None,
        sudo_command: str | None = None,
        subprocess_run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.simulation = simulation
        self._configured_kernel_pins: set[int] = set()
        self.one_wire_root = one_wire_root or Path("/sys/bus/w1/devices")
        self._dtoverlay_command = dtoverlay_command
        self._sudo_command = sudo_command
        self._subprocess_run = subprocess_run

    def configure_kernel_one_wire(self, bcm_pin: int, sensor_id: str = "") -> None:
        if self.simulation:
            return
        bcm_pin = int(bcm_pin)
        if sensor_id and self._kernel_sensor_visible(sensor_id):
            self._configured_kernel_pins.add(bcm_pin)
            return
        if bcm_pin in self._configured_kernel_pins and self._one_wire_bus_masters():
            return
        if self._overlay_loaded_for_pin(bcm_pin):
            self._configured_kernel_pins.add(bcm_pin)
            return
        self._load_kernel_overlay_privileged(bcm_pin)
        self._configured_kernel_pins.add(bcm_pin)
        self._wait_for_kernel_one_wire(bcm_pin, sensor_id=sensor_id)

    def read_one_wire_bus(self, sensor_id: str = "", *, bcm_pin: int | None = None) -> float:
        if self.simulation:
            return 78.0
        resolved_id = self._resolve_one_wire_sensor_id(sensor_id)
        path = self.one_wire_root / resolved_id / "w1_slave"
        if not path.exists():
            raise HardwareUnavailable(
                f"1-Wire sensor {resolved_id} is not present at {path}. "
                f"{self._one_wire_diagnostics(bcm_pin=bcm_pin)}"
            )
        text = path.read_text(encoding="utf-8")
        if "YES" not in text.splitlines()[0]:
            raise HardwareUnavailable(f"1-Wire sensor {resolved_id} CRC check failed.")
        marker = "t="
        if marker not in text:
            raise ValueError(f"1-Wire sensor {resolved_id} returned no temperature marker.")
        return int(text.split(marker, 1)[1].strip()) / 1000 * 9 / 5 + 32

    def _resolve_one_wire_sensor_id(self, sensor_id: str = "") -> str:
        sensors = self._one_wire_sensor_ids()
        if sensor_id:
            if sensor_id in sensors:
                return sensor_id
            raise HardwareUnavailable(
                f"Configured DS18B20 sensor ID {sensor_id!r} was not found on the kernel 1-Wire bus. "
                f"{self._one_wire_diagnostics()}"
            )
        if len(sensors) == 1:
            return sensors[0]
        if not sensors:
            raise HardwareUnavailable(f"No DS18B20 sensors were found on the kernel 1-Wire bus. {self._one_wire_diagnostics()}")
        raise HardwareUnavailable(
            "Multiple DS18B20 sensors were found; set the sense port sensor_id to the actual probe ID. "
            f"{self._one_wire_diagnostics()}"
        )

    def _one_wire_sensor_ids(self) -> list[str]:
        return sorted(path.name for path in self.one_wire_root.glob("28-*"))

    def _one_wire_bus_masters(self) -> list[Path]:
        return sorted(path for path in self.one_wire_root.glob("w1_bus_master*") if path.is_dir())

    def _kernel_sensor_visible(self, sensor_id: str) -> bool:
        return bool(sensor_id) and (self.one_wire_root / sensor_id / "w1_slave").exists()

    def _overlay_loaded_for_pin(self, bcm_pin: int) -> bool:
        dtoverlay = self._dtoverlay_path()
        if not dtoverlay:
            return False
        try:
            result = self._subprocess_run([dtoverlay, "-l"], capture_output=True, check=False, text=True)
        except Exception:
            return False
        if result.returncode != 0:
            return False
        for line in str(result.stdout).splitlines():
            if "w1-gpio" not in line:
                continue
            if f"gpiopin={bcm_pin}" in line:
                return True
            if bcm_pin == 4 and not re.search(r"gpiopin\s*=", line):
                return True
        return False

    def _load_kernel_overlay_privileged(self, bcm_pin: int) -> None:
        dtoverlay = self._dtoverlay_path()
        sudo = self._sudo_path()
        if not dtoverlay or not sudo:
            raise HardwareUnavailable(
                f"Could not enable kernel 1-Wire on BCM {bcm_pin}: dtoverlay/sudo is unavailable. "
                f"{self._one_wire_diagnostics(bcm_pin=bcm_pin)}"
            )
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise HardwareUnavailable(
                f"Refusing to enable kernel 1-Wire on BCM {bcm_pin} from a root Loggerhead process. "
                "Run Loggerhead as the unprivileged reef user and allow only the dtoverlay setup command via sudo. "
                f"{self._one_wire_diagnostics(bcm_pin=bcm_pin)}"
            )
        command = [sudo, "-n", dtoverlay, "w1-gpio", f"gpiopin={bcm_pin}"]
        try:
            result = self._subprocess_run(command, capture_output=True, check=False, text=True)
        except Exception as exc:
            raise HardwareUnavailable(
                f"Could not enable kernel 1-Wire on BCM {bcm_pin}: {exc}. "
                f"{self._one_wire_diagnostics(bcm_pin=bcm_pin)}"
            ) from exc
        if result.returncode != 0:
            stderr = str(result.stderr).strip() or str(result.stdout).strip() or f"exit code {result.returncode}"
            raise HardwareUnavailable(
                f"Could not enable kernel 1-Wire on BCM {bcm_pin} using narrow privileged setup: {stderr}. "
                f"Run the controller as an unprivileged user and allow passwordless sudo only for: "
                f"{sudo} -n {dtoverlay} w1-gpio gpiopin={bcm_pin}. "
                f"{self._one_wire_diagnostics(bcm_pin=bcm_pin)}"
            )

    def _wait_for_kernel_one_wire(self, bcm_pin: int, *, sensor_id: str = "") -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if sensor_id and self._kernel_sensor_visible(sensor_id):
                return
            if not sensor_id and self._one_wire_bus_masters():
                return
            time.sleep(0.1)
        LOGGER.warning("Kernel 1-Wire overlay on BCM %s loaded but no expected DS18B20 appeared yet.", bcm_pin)

    def _dtoverlay_path(self) -> str:
        return self._dtoverlay_command or shutil.which("dtoverlay") or "/usr/bin/dtoverlay"

    def _sudo_path(self) -> str:
        return self._sudo_command or shutil.which("sudo") or "/usr/bin/sudo"

    def _one_wire_diagnostics(self, *, bcm_pin: int | None = None) -> str:
        sensors = self._one_wire_sensor_ids()
        buses = [path.name for path in self._one_wire_bus_masters()]
        target = f"target BCM GPIO {bcm_pin}; " if bcm_pin is not None else ""
        return f"{target}visible DS18B20 IDs={sensors or 'none'}; bus masters={buses or 'none'}."

    def read_bit_banged(self, bcm_pin: int) -> float:
        if self.simulation:
            return 78.0
        try:
            import pigpio  # type: ignore

            pi = pigpio.pi()
            if not pi.connected:
                raise HardwareUnavailable("pigpiod is not connected.")
            if hasattr(pi, "set_pull_up_down") and hasattr(pigpio, "PUD_UP"):
                pi.set_pull_up_down(bcm_pin, pigpio.PUD_UP)
            scratchpad = self._read_ds18b20_scratchpad_bit_banged(pi, bcm_pin)
            return self._decode_ds18b20_scratchpad(scratchpad)
        except HardwareUnavailable:
            raise
        except Exception as exc:
            raise HardwareUnavailable(f"Bit-banged DS18B20 read failed on BCM {bcm_pin}: {exc}") from exc

    def _read_ds18b20_scratchpad_bit_banged(self, pi: Any, bcm_pin: int) -> bytes:
        self._one_wire_reset(pi, bcm_pin)
        self._one_wire_write_byte(pi, bcm_pin, 0xCC)  # Skip ROM; each sense port is expected to have one DS18B20.
        self._one_wire_write_byte(pi, bcm_pin, 0x44)  # Convert T.
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            if self._one_wire_read_bit(pi, bcm_pin):
                break
            time.sleep(0.01)
        else:
            raise HardwareUnavailable(f"DS18B20 conversion timed out on BCM {bcm_pin}.")
        self._one_wire_reset(pi, bcm_pin)
        self._one_wire_write_byte(pi, bcm_pin, 0xCC)
        self._one_wire_write_byte(pi, bcm_pin, 0xBE)  # Read scratchpad.
        scratchpad = bytes(self._one_wire_read_byte(pi, bcm_pin) for _ in range(9))
        if self._crc8_maxim(scratchpad[:8]) != scratchpad[8]:
            raise HardwareUnavailable("DS18B20 scratchpad CRC check failed.")
        return scratchpad

    def _one_wire_reset(self, pi: Any, bcm_pin: int) -> None:
        self._drive_low(pi, bcm_pin)
        self._sleep_us(480)
        self._release_line(pi, bcm_pin)
        self._sleep_us(70)
        presence = pi.read(bcm_pin) == 0
        self._sleep_us(410)
        if not presence:
            raise HardwareUnavailable(f"No DS18B20 presence pulse detected on BCM {bcm_pin}.")

    def _one_wire_write_byte(self, pi: Any, bcm_pin: int, value: int) -> None:
        for bit in range(8):
            self._one_wire_write_bit(pi, bcm_pin, bool(value & (1 << bit)))

    def _one_wire_read_byte(self, pi: Any, bcm_pin: int) -> int:
        value = 0
        for bit in range(8):
            if self._one_wire_read_bit(pi, bcm_pin):
                value |= 1 << bit
        return value

    def _one_wire_write_bit(self, pi: Any, bcm_pin: int, value: bool) -> None:
        self._drive_low(pi, bcm_pin)
        self._sleep_us(6 if value else 60)
        self._release_line(pi, bcm_pin)
        self._sleep_us(64 if value else 10)

    def _one_wire_read_bit(self, pi: Any, bcm_pin: int) -> int:
        self._drive_low(pi, bcm_pin)
        self._sleep_us(6)
        self._release_line(pi, bcm_pin)
        self._sleep_us(9)
        value = 1 if pi.read(bcm_pin) else 0
        self._sleep_us(55)
        return value

    @staticmethod
    def _drive_low(pi: Any, bcm_pin: int) -> None:
        pi.set_mode(bcm_pin, 1)
        pi.write(bcm_pin, 0)

    @staticmethod
    def _release_line(pi: Any, bcm_pin: int) -> None:
        pi.set_mode(bcm_pin, 0)

    @staticmethod
    def _sleep_us(microseconds: int) -> None:
        target = time.monotonic_ns() + microseconds * 1000
        while time.monotonic_ns() < target:
            pass

    @staticmethod
    def _decode_ds18b20_scratchpad(scratchpad: bytes) -> float:
        if len(scratchpad) < 2:
            raise HardwareUnavailable("DS18B20 scratchpad was incomplete.")
        raw = scratchpad[0] | (scratchpad[1] << 8)
        if raw & 0x8000:
            raw -= 0x10000
        celsius = raw / 16.0
        return celsius * 9 / 5 + 32

    @staticmethod
    def _crc8_maxim(data: bytes) -> int:
        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x01:
                    crc = (crc >> 1) ^ 0x8C
                else:
                    crc >>= 1
        return crc & 0xFF

    def read_host_cpu(self) -> float:
        for path in (Path("/sys/class/thermal/thermal_zone0/temp"), Path("/sys/devices/virtual/thermal/thermal_zone0/temp")):
            if path.exists():
                return int(path.read_text(encoding="utf-8").strip()) / 1000 * 9 / 5 + 32
        if self.simulation:
            return 115.0
        raise HardwareUnavailable("No Raspberry Pi CPU thermal file was found.")


class BinaryLevelSensor:
    """Binary digital level sensor.

    Implements SRS 2.3.1.
    """

    def __init__(self, bcm_pin: int, *, invert: bool = False, simulation: bool = False) -> None:
        self.bcm_pin = bcm_pin
        self.invert = invert
        self.simulation = simulation

    def read_state(self) -> LevelState:
        if self.simulation:
            raw = False
        else:
            try:
                import RPi.GPIO as GPIO  # type: ignore

                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self.bcm_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                raw = bool(GPIO.input(self.bcm_pin))
            except Exception as exc:
                raise HardwareUnavailable(f"GPIO read failed on BCM {self.bcm_pin}: {exc}") from exc
        submerged = not raw if self.invert else raw
        return LevelState.SUBMERGED if submerged else LevelState.DRY


class PigpioResourceCoordinator:
    """Process-local guard for pigpio features that may contend for waveform resources."""

    _lock = threading.RLock()
    _active_wave_owner: str | None = None
    _hardware_pwm_active = False

    @classmethod
    def begin_stepper_wave(cls, owner: str, *, stop_hardware_pwm: Callable[[], None] | None = None) -> None:
        with cls._lock:
            if cls._active_wave_owner and cls._active_wave_owner != owner:
                raise HardwareFault(f"pigpio waveform transmitter is already owned by {cls._active_wave_owner}.")
            if cls._hardware_pwm_active and stop_hardware_pwm:
                LOGGER.warning("Stopping buzzer hardware PWM before starting stepper waveform %s.", owner)
                stop_hardware_pwm()
                cls._hardware_pwm_active = False
            cls._active_wave_owner = owner

    @classmethod
    def end_stepper_wave(cls, owner: str) -> None:
        with cls._lock:
            if cls._active_wave_owner == owner:
                cls._active_wave_owner = None

    @classmethod
    def run_hardware_pwm(cls, *, active: bool, action: Callable[[], None]) -> bool:
        with cls._lock:
            if active and cls._active_wave_owner:
                LOGGER.warning(
                    "Deferring buzzer hardware PWM while stepper waveform %s is active.",
                    cls._active_wave_owner,
                )
                return False
            if not active and cls._active_wave_owner and not cls._hardware_pwm_active:
                return True
            action()
            cls._hardware_pwm_active = active
            return True

    @classmethod
    def reset_for_tests(cls) -> None:
        with cls._lock:
            cls._active_wave_owner = None
            cls._hardware_pwm_active = False


class Buzzer:
    """Hardware PWM buzzer on BCM 12.

    Implements SRS 4.6.1 through 4.6.3 and 8.6.
    """

    def __init__(self, *, bcm_pin: int = BUZZER_PWM_BCM, frequency_hz: int = 2500, simulation: bool = False) -> None:
        if bcm_pin != BUZZER_PWM_BCM:
            raise DiagnosticHalt("Buzzer output is fixed to BCM GPIO 12.")
        self.bcm_pin = bcm_pin
        self.frequency_hz = frequency_hz
        self.simulation = simulation
        self.pi = None
        if not simulation:
            try:
                import pigpio  # type: ignore

                self.pi = pigpio.pi()
                if not self.pi.connected:
                    raise HardwareUnavailable("pigpiod is not connected.")
                self.stop()
            except Exception as exc:
                LOGGER.warning("Buzzer pigpio unavailable, falling back to simulation: %s", exc)
                self.simulation = True

    def sound(self, priority: AlarmPriority = AlarmPriority.HIGH) -> None:
        duty = 192 if priority in {AlarmPriority.HIGH, AlarmPriority.CRITICAL} else 96
        if not self.simulation and self.pi:
            PigpioResourceCoordinator.run_hardware_pwm(
                active=True,
                action=lambda: self.pi.hardware_PWM(self.bcm_pin, self.frequency_hz, int(duty / 255 * 1_000_000)),
            )

    def stop(self) -> None:
        if not self.simulation and self.pi:
            PigpioResourceCoordinator.run_hardware_pwm(
                active=False,
                action=lambda: self.pi.hardware_PWM(self.bcm_pin, 0, 0),
            )


class TMC2209UART:
    """Native TMC2209 UART protocol helper.

    Implements SRS 3.4.2, 3.4.6, and 3.4.7 without third-party TMC2209 packages.
    """

    SYNC = 0x05
    MASTER = 0xFF
    REG_GCONF = 0x00
    REG_GSTAT = 0x01
    REG_IFCNT = 0x02
    REG_IHOLD_IRUN = 0x10
    REG_TPOWERDOWN = 0x11
    REG_SGTHRS = 0x40
    REG_DRV_STATUS = 0x6F
    REG_SG_RESULT = 0x41
    REG_CHOPCONF = 0x6C

    GCONF_PDN_DISABLE = 1 << 6
    GCONF_MSTEP_REG_SELECT = 1 << 7
    GSTAT_RESET = 1 << 0
    GSTAT_DRV_ERR = 1 << 1
    GSTAT_UV_CP = 1 << 2
    CHOPCONF_TOFF_MASK = 0x0F
    CHOPCONF_MRES_SHIFT = 24
    CHOPCONF_MRES_MASK = 0x0F << CHOPCONF_MRES_SHIFT
    MICROSTEP_TO_MRES = {
        256: 0,
        128: 1,
        64: 2,
        32: 3,
        16: 4,
        8: 5,
        4: 6,
        2: 7,
        1: 8,
    }

    def __init__(self, port: str = "/dev/serial0", *, baudrate: int = 115200, simulation: bool = False) -> None:
        self.simulation = simulation
        self.serial = None
        self._lock = threading.RLock()
        self._sim_registers: dict[tuple[int, int], int] = {
            (node, self.REG_CHOPCONF): 0x00000003 for node in range(4)
        }
        self._sim_ifcnt: dict[int, int] = {}
        self._current_calibration_warning_logged = False
        if not simulation:
            try:
                import serial  # type: ignore

                self.serial = serial.Serial(port, baudrate=baudrate, timeout=0.2)
            except Exception as exc:
                raise HardwareFault(f"TMC2209 UART unavailable in real hardware mode: {exc}") from exc

    @staticmethod
    def crc8(data: bytes) -> int:
        crc = 0
        for byte in data:
            current = byte
            for _ in range(8):
                if (crc >> 7) ^ (current & 0x01):
                    crc = ((crc << 1) ^ 0x07) & 0xFF
                else:
                    crc = (crc << 1) & 0xFF
                current >>= 1
        return crc

    def _validate_node(self, node: int) -> None:
        if node not in {0, 1, 2, 3}:
            raise DiagnosticHalt("TMC2209 UART nodes are fixed to addresses 0-3.")

    def _write_frame(self, node: int, register: int, value: int) -> bytes:
        self._validate_node(node)
        payload = bytes([self.SYNC, node, register | 0x80]) + (value & 0xFFFFFFFF).to_bytes(4, "big")
        return payload + bytes([self.crc8(payload)])

    def _read_frame(self, node: int, register: int) -> bytes:
        self._validate_node(node)
        payload = bytes([self.SYNC, node, register & 0x7F])
        frame = payload + bytes([self.crc8(payload)])
        return frame

    def write_register(self, node: int, register: int, value: int, *, verify: bool = True) -> None:
        self._validate_node(node)
        with self._lock:
            if self.simulation or not self.serial:
                self._sim_registers[(node, register & 0x7F)] = value & 0xFFFFFFFF
                self._sim_ifcnt[node] = (self._sim_ifcnt.get(node, 0) + 1) & 0xFF
                return
            before = self._read_register_unlocked(node, self.REG_IFCNT) if verify else None
            frame = self._write_frame(node, register, value)
            self.serial.write(frame)
            if verify:
                after = self._read_register_unlocked(node, self.REG_IFCNT)
                if before is not None and after == before:
                    raise HardwareFault(f"TMC2209 node {node} did not acknowledge write to register {register:#04x}.")

    def read_register(self, node: int, register: int) -> int:
        self._validate_node(node)
        with self._lock:
            return self._read_register_unlocked(node, register)

    def _read_register_unlocked(self, node: int, register: int) -> int:
        if self.simulation or not self.serial:
            if register == self.REG_IFCNT:
                return self._sim_ifcnt.get(node, 0)
            if register == self.REG_SG_RESULT:
                return self._sim_registers.get((node, register & 0x7F), 100)
            return self._sim_registers.get((node, register & 0x7F), 0)
        request = self._read_frame(node, register)
        self.serial.write(request)
        response = self._read_valid_response(register)
        return int.from_bytes(response[3:7], "big")

    def _read_valid_response(self, register: int) -> bytes:
        if not self.serial:
            raise HardwareFault("TMC2209 serial port is not open.")
        deadline = time.monotonic() + 0.5
        buffer = bytearray()
        while time.monotonic() < deadline:
            chunk = self.serial.read(1)
            if chunk:
                buffer.extend(chunk)
                while len(buffer) >= 8:
                    window = bytes(buffer[:8])
                    if (
                        window[0] == self.SYNC
                        and window[1] == self.MASTER
                        and window[2] == (register & 0x7F)
                        and self.crc8(window[:-1]) == window[-1]
                    ):
                        return window
                    del buffer[0]
            else:
                time.sleep(0.005)
        raise TimeoutError(f"TMC2209 UART read timed out for register {register:#04x}.")

    def set_current(self, node: int, run_current_ma: int, hold_current_ma: int) -> None:
        if not self._current_calibration_warning_logged:
            LOGGER.warning(
                "TMC2209 current scaling uses Loggerhead's configured mA values without board-specific sense "
                "resistor/VREF calibration; verify motor current on the assembled driver board."
            )
            self._current_calibration_warning_logged = True
        ihold = min(31, max(0, round(hold_current_ma / max(run_current_ma, 1) * 31)))
        irun = min(31, max(0, round(run_current_ma / 1000 * 31)))
        value = ihold | (irun << 8) | (5 << 16)
        self.write_register(node, self.REG_IHOLD_IRUN, value)

    def configure_driver(self, node: int, *, microsteps: int, stallguard_threshold: int = 0) -> None:
        self._validate_node(node)
        if microsteps not in self.MICROSTEP_TO_MRES:
            raise DiagnosticHalt(f"Unsupported TMC2209 microstep setting {microsteps}.")
        if stallguard_threshold < 0 or stallguard_threshold > 255:
            raise DiagnosticHalt("TMC2209 StallGuard threshold must be between 0 and 255.")

        with self._lock:
            gstat = self._read_register_unlocked(node, self.REG_GSTAT)
            if gstat:
                LOGGER.warning("TMC2209 node %s startup GSTAT=0x%08x.", node, gstat)
                if gstat & self.GSTAT_UV_CP:
                    raise HardwareFault(f"TMC2209 node {node} reports charge-pump undervoltage at startup.")
                self.write_register(node, self.REG_GSTAT, gstat, verify=False)

            gconf = self._read_register_unlocked(node, self.REG_GCONF)
            wanted_gconf = gconf | self.GCONF_PDN_DISABLE | self.GCONF_MSTEP_REG_SELECT
            self.write_register(node, self.REG_GCONF, wanted_gconf)

            chopconf = self._read_register_unlocked(node, self.REG_CHOPCONF)
            if chopconf & self.CHOPCONF_TOFF_MASK == 0:
                raise HardwareFault(
                    f"TMC2209 node {node} CHOPCONF.TOFF is zero; chopper is disabled and motor current "
                    "configuration cannot be trusted."
                )
            mres = self.MICROSTEP_TO_MRES[microsteps]
            wanted_chopconf = (chopconf & ~self.CHOPCONF_MRES_MASK) | (mres << self.CHOPCONF_MRES_SHIFT)
            self.write_register(node, self.REG_CHOPCONF, wanted_chopconf)
            self.write_register(node, self.REG_TPOWERDOWN, 20)
            self.write_register(node, self.REG_SGTHRS, stallguard_threshold & 0xFF)

            readback_gconf = self._read_register_unlocked(node, self.REG_GCONF)
            readback_chopconf = self._read_register_unlocked(node, self.REG_CHOPCONF)
            if readback_gconf & (self.GCONF_PDN_DISABLE | self.GCONF_MSTEP_REG_SELECT) != (
                self.GCONF_PDN_DISABLE | self.GCONF_MSTEP_REG_SELECT
            ):
                raise HardwareFault(f"TMC2209 node {node} did not retain UART configuration bits.")
            if readback_chopconf & self.CHOPCONF_MRES_MASK != (mres << self.CHOPCONF_MRES_SHIFT):
                raise HardwareFault(f"TMC2209 node {node} did not retain microstep configuration.")
            if readback_chopconf & self.CHOPCONF_TOFF_MASK == 0:
                raise HardwareFault(f"TMC2209 node {node} readback has CHOPCONF.TOFF=0.")

    def diagnostics(self, node: int) -> dict[str, Any]:
        drv = self.read_register(node, self.REG_DRV_STATUS)
        sg_result = self.read_register(node, self.REG_SG_RESULT) & 0x3FF
        return {
            "drv_status": drv,
            "sg_result": sg_result,
            "standstill": bool(drv & (1 << 31)),
            "stealthchop": bool(drv & (1 << 30)),
            "cs_actual": (drv >> 16) & 0x1F,
            "overtemp_warning": bool(drv & 0x01),
            "overtemp_shutdown": bool(drv & 0x02),
            "short_to_ground_a": bool(drv & (1 << 2)),
            "short_to_ground_b": bool(drv & (1 << 3)),
            "short_to_supply_a": bool(drv & (1 << 4)),
            "short_to_supply_b": bool(drv & (1 << 5)),
            "open_load_a": bool(drv & (1 << 6)),
            "open_load_b": bool(drv & (1 << 7)),
            "temp_120c": bool(drv & (1 << 8)),
            "temp_143c": bool(drv & (1 << 9)),
            "temp_150c": bool(drv & (1 << 10)),
            "temp_157c": bool(drv & (1 << 11)),
        }


@dataclass
class StepperMove:
    stepper_id: str
    steps: int
    steps_per_second: int


@dataclass(frozen=True)
class StepperRunResult:
    steps_sent: int
    reason: str
    requested_steps: int = 0
    possible_steps: int = 0
    completed_batches: int = 0
    interrupted_batch: bool = False
    elapsed_seconds: float = 0.0
    effective_steps_per_second: float | None = None
    max_inter_batch_gap_seconds: float = 0.0


@dataclass(frozen=True)
class PulseWindowResult:
    confirmed_steps: int
    possible_steps: int
    completed_batches: int
    interrupted_batch: bool = False
    reason: str = ""
    max_inter_batch_gap_seconds: float = 0.0


class StepperPulseEngine:
    """Hardware-timed stepper pulse engine with a software interlock.

    Implements SRS 3.4.3 through 3.4.7.
    """

    MIN_STEPS_PER_SECOND = 1
    MAX_STEPS_PER_SECOND = 5000
    STEP_HIGH_US = 8
    WAVE_BATCH_SECONDS = 0.5
    WAVE_POLL_SECONDS = 0.01
    WAVE_DIAGNOSTIC_SECONDS = 0.25
    WAVE_CHAIN_MAX_REPEATS = 65_535
    WAVE_EXTERNAL_CANCEL_TOLERANCE_SECONDS = 0.05

    def __init__(self, uart: TMC2209UART, relay_board: MCP23017RelayBoard, *, simulation: bool = False) -> None:
        self.uart = uart
        self.relay_board = relay_board
        self.simulation = simulation
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._shutdown = threading.Event()
        self._fault: Exception | None = None
        self.active_stepper: str | None = None
        self.pi = None
        self._gpio_output = 1
        self._pigpio_pulse: Callable[[int, int, int], Any] | None = None
        self._wave_ids: set[int] = set()
        self.relay_board.disable_all_steppers()
        if not simulation:
            try:
                import pigpio  # type: ignore

                self._gpio_output = getattr(pigpio, "OUTPUT", 1)
                self._pigpio_pulse = pigpio.pulse
                self.pi = pigpio.pi()
                if not self.pi.connected:
                    raise HardwareUnavailable("pigpiod is not connected.")
            except Exception as exc:
                raise HardwareFault(f"Stepper pulse hardware unavailable in real hardware mode: {exc}") from exc

    def configure_driver(
        self,
        assignment: StepperAssignment,
        *,
        microsteps: int = 16,
        stallguard_threshold: int = 0,
        direction_high: bool = True,
    ) -> None:
        self.uart.configure_driver(
            assignment.uart_address,
            microsteps=microsteps,
            stallguard_threshold=stallguard_threshold,
        )
        self._prepare_gpio(assignment, direction_high=direction_high)

    def move(
        self,
        assignment: StepperAssignment,
        *,
        steps: int,
        steps_per_second: int,
        run_current_ma: int,
        hold_current_ma: int = 0,
        microsteps: int = 16,
        stallguard_threshold: int = 0,
        direction_high: bool = True,
    ) -> None:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A stepper motor is already active.")
        primary_error: BaseException | None = None
        owner = f"stepper:{assignment.name}"
        started = time.monotonic()
        pulse_result = PulseWindowResult(0, 0, 0)
        try:
            self._raise_if_unavailable()
            self.active_stepper = assignment.name
            self._stop_event.clear()
            self.configure_driver(
                assignment,
                microsteps=microsteps,
                stallguard_threshold=stallguard_threshold,
                direction_high=direction_high,
            )
            self._raise_if_unavailable()
            self.uart.set_current(assignment.uart_address, run_current_ma, hold_current_ma)
            self._raise_if_unavailable()
            self._begin_wave_operation(owner)
            self._raise_if_stop_requested()
            self.relay_board.set_stepper_enabled(assignment, True)
            pulse_result = self._pulse_windowed(
                assignment,
                steps=steps,
                steps_per_second=steps_per_second,
                run_current_ma=run_current_ma,
                hold_current_ma=hold_current_ma,
                microsteps=microsteps,
                stallguard_threshold=stallguard_threshold,
            )
            elapsed = time.monotonic() - started
            self._log_motion_result(
                assignment,
                requested_steps=abs(steps),
                result=StepperRunResult(
                    pulse_result.confirmed_steps,
                    pulse_result.reason or "completed",
                    requested_steps=abs(steps),
                    possible_steps=pulse_result.possible_steps,
                    completed_batches=pulse_result.completed_batches,
                    interrupted_batch=pulse_result.interrupted_batch,
                    elapsed_seconds=elapsed,
                    effective_steps_per_second=pulse_result.confirmed_steps / elapsed
                    if elapsed > 0 and pulse_result.confirmed_steps
                    else None,
                    max_inter_batch_gap_seconds=pulse_result.max_inter_batch_gap_seconds,
                ),
                requested_speed=steps_per_second,
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                self.stop(assignment)
            except Exception as shutdown_error:
                self._fault = shutdown_error
                LOGGER.critical("Stepper %s failed to reach disabled state: %s", assignment.name, shutdown_error)
                if primary_error is None:
                    raise
            finally:
                PigpioResourceCoordinator.end_stepper_wave(owner)
                self.active_stepper = None
                self._lock.release()

    def run_continuous(
        self,
        assignment: StepperAssignment,
        *,
        stop_event: threading.Event,
        steps_per_second: int,
        run_current_ma: int,
        hold_current_ma: int = 0,
        microsteps: int = 16,
        stallguard_threshold: int = 0,
        direction_high: bool = True,
        max_seconds: float = 60.0,
        max_steps: int = 300_000,
    ) -> StepperRunResult:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A stepper motor is already active.")
        primary_error: BaseException | None = None
        steps_sent = 0
        possible_steps = 0
        completed_batches = 0
        interrupted_batch = False
        max_inter_batch_gap_seconds = 0.0
        requested_steps = 0
        reason = "stopped"
        started = time.monotonic()
        wave_cache: dict[tuple[int, int, int], int] = {}
        owner = f"stepper:{assignment.name}"
        try:
            self._raise_if_unavailable()
            if stop_event.is_set():
                return StepperRunResult(0, reason)
            self.active_stepper = assignment.name
            self._stop_event.clear()
            self.configure_driver(
                assignment,
                microsteps=microsteps,
                stallguard_threshold=stallguard_threshold,
                direction_high=direction_high,
            )
            self._raise_if_unavailable()
            self.uart.set_current(assignment.uart_address, run_current_ma, hold_current_ma)
            self._raise_if_unavailable()
            self._begin_wave_operation(owner)
            if stop_event.is_set() or self._stop_event.is_set():
                return StepperRunResult(0, reason)
            self.relay_board.set_stepper_enabled(assignment, True)
            while not stop_event.is_set() and not self._stop_event.is_set():
                self._raise_if_unavailable()
                elapsed = time.monotonic() - started
                if elapsed >= max_seconds:
                    reason = "max_seconds"
                    break
                remaining_steps = max_steps - steps_sent
                if remaining_steps <= 0:
                    reason = "max_steps"
                    break
                remaining_time_steps = max(1, int((max_seconds - elapsed) * max(1, steps_per_second)))
                chunk = min(remaining_steps, remaining_time_steps)
                requested_steps += chunk
                result = self._pulse_windowed(
                    assignment,
                    steps=chunk,
                    steps_per_second=steps_per_second,
                    run_current_ma=run_current_ma,
                    hold_current_ma=hold_current_ma,
                    microsteps=microsteps,
                    stallguard_threshold=stallguard_threshold,
                    wave_cache=wave_cache,
                )
                steps_sent += result.confirmed_steps
                possible_steps += result.possible_steps
                completed_batches += result.completed_batches
                interrupted_batch = interrupted_batch or result.interrupted_batch
                max_inter_batch_gap_seconds = max(max_inter_batch_gap_seconds, result.max_inter_batch_gap_seconds)
                if result.reason:
                    reason = result.reason
                if result.confirmed_steps < chunk:
                    if not result.reason:
                        reason = "stopped"
                    break
            if self._shutdown.is_set():
                reason = "shutdown"
            elapsed = time.monotonic() - started
            effective_rate = steps_sent / elapsed if elapsed > 0 and steps_sent else None
            run_result = StepperRunResult(
                steps_sent,
                reason,
                requested_steps=requested_steps,
                possible_steps=possible_steps,
                completed_batches=completed_batches,
                interrupted_batch=interrupted_batch,
                elapsed_seconds=elapsed,
                effective_steps_per_second=effective_rate,
                max_inter_batch_gap_seconds=max_inter_batch_gap_seconds,
            )
            self._log_motion_result(
                assignment,
                requested_steps=requested_steps,
                result=run_result,
                requested_speed=steps_per_second,
            )
            return run_result
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                self.stop(assignment)
            except Exception as shutdown_error:
                self._fault = shutdown_error
                LOGGER.critical("Stepper %s failed to reach disabled state: %s", assignment.name, shutdown_error)
                if primary_error is None:
                    raise
            finally:
                self._delete_cached_waves(wave_cache)
                PigpioResourceCoordinator.end_stepper_wave(owner)
                self.active_stepper = None
                self._lock.release()

    def stop(self, assignment: StepperAssignment) -> None:
        self._stop_event.set()
        errors = self._attempt_motor_safe_state(assignment)
        if errors:
            fault = HardwareFault(
                "Stepper safety shutdown incomplete: "
                + "; ".join(f"{name}: {error}" for name, error in errors)
            )
            self._fault = fault
            raise fault

    def request_stop(self) -> None:
        self._stop_event.set()
        try:
            self._cancel_wave_tx()
        except Exception as exc:
            self._fault = HardwareFault(f"Stepper waveform cancellation failed during stop request: {exc}")
            LOGGER.critical("%s", self._fault)

    def shutdown(self) -> None:
        self._shutdown.set()
        self._stop_event.set()
        errors: list[tuple[str, Exception]] = []
        try:
            self._cancel_wave_tx()
        except Exception as exc:
            errors.append(("wave cancellation", exc))
        acquired = self._lock.acquire(timeout=2.0)
        try:
            try:
                self.relay_board.disable_all_steppers()
            except Exception as exc:
                errors.append(("disable all stepper ENN lines", exc))
            for assignment in STEPPERS.values():
                try:
                    self._set_step_low(assignment)
                except Exception as exc:
                    errors.append((f"{assignment.name} STEP low", exc))
            for assignment in STEPPERS.values():
                try:
                    self.uart.set_current(assignment.uart_address, 0, 0)
                except Exception as exc:
                    errors.append((f"{assignment.name} current shutdown", exc))
        finally:
            if acquired:
                self._lock.release()
        if not acquired:
            errors.append(("stepper quiesce", HardwareFault("Stepper engine did not quiesce during shutdown.")))
        if errors:
            self._fault = HardwareFault(
                "Stepper shutdown could not verify safe state: "
                + "; ".join(f"{name}: {error}" for name, error in errors)
            )
            raise self._fault

    def _cancel_wave_tx(self) -> None:
        if self.simulation or not self.pi:
            return
        if self.pi.wave_tx_busy():
            self.pi.wave_tx_stop()

    def _attempt_motor_safe_state(self, assignment: StepperAssignment) -> list[tuple[str, Exception]]:
        errors: list[tuple[str, Exception]] = []
        actions: tuple[tuple[str, Callable[[], None]], ...] = (
            ("wave cancellation", self._cancel_wave_tx),
            ("STEP low before disable", lambda: self._set_step_low(assignment)),
            ("disable stepper ENN", lambda: self.relay_board.set_stepper_enabled(assignment, False)),
            ("TMC current shutdown", lambda: self.uart.set_current(assignment.uart_address, 0, 0)),
            ("STEP low after disable", lambda: self._set_step_low(assignment)),
        )
        for name, action in actions:
            try:
                action()
            except Exception as exc:
                errors.append((name, exc))
                LOGGER.critical("Stepper %s safety action failed during %s: %s", assignment.name, name, exc)
        return errors

    def _raise_if_unavailable(self) -> None:
        if self._shutdown.is_set():
            raise HardwareFault("Stepper engine is shut down.")
        if self._fault is not None:
            raise HardwareFault(f"Stepper engine is faulted; moves are blocked: {self._fault}") from self._fault

    def _raise_if_stop_requested(self) -> None:
        if self._stop_event.is_set():
            raise HardwareFault("Stepper motion was stopped before waveform transmission.")

    def _begin_wave_operation(self, owner: str) -> None:
        if self.simulation or not self.pi:
            return
        PigpioResourceCoordinator.begin_stepper_wave(
            owner,
            stop_hardware_pwm=lambda: self.pi.hardware_PWM(BUZZER_PWM_BCM, 0, 0),
        )

    def _prepare_gpio(self, assignment: StepperAssignment, *, direction_high: bool) -> None:
        if self.simulation or not self.pi:
            return
        self.pi.set_mode(assignment.step_bcm, self._gpio_output)
        self.pi.set_mode(assignment.direction_bcm, self._gpio_output)
        self.pi.write(assignment.step_bcm, 0)
        self.pi.write(assignment.direction_bcm, 1 if direction_high else 0)

    def _set_step_low(self, assignment: StepperAssignment) -> None:
        if self.simulation or not self.pi:
            return
        self.pi.write(assignment.step_bcm, 0)

    def _pulse_windowed(
        self,
        assignment: StepperAssignment,
        *,
        steps: int,
        steps_per_second: int,
        run_current_ma: int = 0,
        hold_current_ma: int = 0,
        microsteps: int = 16,
        stallguard_threshold: int = 0,
        wave_cache: dict[tuple[int, int, int], int] | None = None,
    ) -> PulseWindowResult:
        self._validate_step_rate(steps_per_second)
        remaining = abs(steps)
        moved = 0
        possible = 0
        completed_batches = 0
        interrupted = False
        max_inter_batch_gap_seconds = 0.0
        max_chunk = self._wave_batch_steps(steps_per_second)
        owns_wave_cache = wave_cache is None
        if wave_cache is None:
            wave_cache = {}
        try:
            last_batch_finished: float | None = None
            reason = ""
            while remaining > 0 and not self._stop_event.is_set():
                batch_started = time.monotonic()
                if last_batch_finished is not None:
                    max_inter_batch_gap_seconds = max(max_inter_batch_gap_seconds, batch_started - last_batch_finished)
                chunk = remaining if not self.simulation and self.pi else min(remaining, max_chunk)
                diagnostics = self.uart.diagnostics(assignment.uart_address)
                self._check_stepper_diagnostics(
                    assignment,
                    diagnostics,
                    phase="pre-pulse",
                    moved_steps=moved,
                    steps_per_second=steps_per_second,
                    run_current_ma=run_current_ma,
                    hold_current_ma=hold_current_ma,
                    microsteps=microsteps,
                    stallguard_threshold=stallguard_threshold,
                )
                result = self._pulse_chunk(
                    assignment,
                    chunk=chunk,
                    steps_per_second=steps_per_second,
                    run_current_ma=run_current_ma,
                    hold_current_ma=hold_current_ma,
                    microsteps=microsteps,
                    stallguard_threshold=stallguard_threshold,
                    moved_steps=moved,
                    wave_cache=wave_cache,
                )
                remaining -= result.confirmed_steps
                moved += result.confirmed_steps
                possible += result.possible_steps
                completed_batches += result.completed_batches
                interrupted = interrupted or result.interrupted_batch
                if result.reason:
                    reason = result.reason
                last_batch_finished = time.monotonic()
                if result.interrupted_batch or self._stop_event.is_set() or self._shutdown.is_set():
                    if not reason:
                        reason = "shutdown" if self._shutdown.is_set() else "stopped"
                    break
                diagnostics = self.uart.diagnostics(assignment.uart_address)
                self._check_stepper_diagnostics(
                    assignment,
                    diagnostics,
                    phase="post-pulse",
                    moved_steps=moved,
                    steps_per_second=steps_per_second,
                    run_current_ma=run_current_ma,
                    hold_current_ma=hold_current_ma,
                    microsteps=microsteps,
                    stallguard_threshold=stallguard_threshold,
                )
                if result.confirmed_steps < chunk:
                    if not reason:
                        reason = "stopped"
                    break
            return PulseWindowResult(
                moved,
                possible,
                completed_batches,
                interrupted,
                reason=reason,
                max_inter_batch_gap_seconds=max_inter_batch_gap_seconds,
            )
        finally:
            if owns_wave_cache:
                self._delete_cached_waves(wave_cache)
            self._set_step_low(assignment)

    def _pulse_chunk(
        self,
        assignment: StepperAssignment,
        *,
        chunk: int,
        steps_per_second: int,
        run_current_ma: int,
        hold_current_ma: int,
        microsteps: int,
        stallguard_threshold: int,
        moved_steps: int,
        wave_cache: dict[tuple[int, int, int], int],
    ) -> PulseWindowResult:
        if self.simulation or not self.pi:
            delay = 1.0 / max(1, steps_per_second)
            if self._stop_event.wait(chunk * delay):
                return PulseWindowResult(0, chunk, 0, interrupted_batch=True)
            return PulseWindowResult(chunk, chunk, 1)

        return self._send_wave_batch(
            assignment,
            steps=chunk,
            steps_per_second=steps_per_second,
            run_current_ma=run_current_ma,
            hold_current_ma=hold_current_ma,
            microsteps=microsteps,
            stallguard_threshold=stallguard_threshold,
            moved_steps=moved_steps,
            wave_cache=wave_cache,
        )

    def _validate_step_rate(self, steps_per_second: int) -> None:
        if steps_per_second < self.MIN_STEPS_PER_SECOND or steps_per_second > self.MAX_STEPS_PER_SECOND:
            raise DiagnosticHalt(
                f"Stepper speed must be between {self.MIN_STEPS_PER_SECOND} and {self.MAX_STEPS_PER_SECOND} step/s."
            )

    def _wave_batch_steps(self, steps_per_second: int) -> int:
        self._validate_step_rate(steps_per_second)
        return max(1, min(2500, round(steps_per_second * self.WAVE_BATCH_SECONDS)))

    def _build_step_wave_pulses(self, assignment: StepperAssignment, *, steps: int, steps_per_second: int) -> list[Any]:
        if not self._pigpio_pulse:
            raise HardwareFault("pigpio pulse factory is unavailable.")
        self._validate_step_rate(steps_per_second)
        period_base_us = 1_000_000 // steps_per_second
        period_remainder = 1_000_000 % steps_per_second
        step_mask = 1 << assignment.step_bcm
        pulses: list[Any] = []
        remainder_accumulator = 0
        for _ in range(steps):
            period_us = period_base_us
            remainder_accumulator += period_remainder
            if remainder_accumulator >= steps_per_second:
                period_us += 1
                remainder_accumulator -= steps_per_second
            high_us = min(self.STEP_HIGH_US, max(1, period_us // 2))
            low_us = max(1, period_us - high_us)
            pulses.append(self._pigpio_pulse(step_mask, 0, high_us))
            pulses.append(self._pigpio_pulse(0, step_mask, low_us))
        return pulses

    def _send_wave_batch(
        self,
        assignment: StepperAssignment,
        *,
        steps: int,
        steps_per_second: int,
        run_current_ma: int,
        hold_current_ma: int,
        microsteps: int,
        stallguard_threshold: int,
        moved_steps: int,
        wave_cache: dict[tuple[int, int, int], int],
    ) -> PulseWindowResult:
        if not self.pi:
            raise HardwareFault("pigpio is unavailable for waveform transmission.")
        batch_steps = self._wave_batch_steps(steps_per_second)
        full_repeats, tail_steps = divmod(steps, batch_steps)
        chain: list[int] = []
        if full_repeats:
            batch_wave_id = self._wave_id_for_steps(
                assignment,
                steps=batch_steps,
                steps_per_second=steps_per_second,
                wave_cache=wave_cache,
            )
            self._append_wave_repeats(chain, batch_wave_id, full_repeats)
        if tail_steps:
            tail_wave_id = self._wave_id_for_steps(
                assignment,
                steps=tail_steps,
                steps_per_second=steps_per_second,
                wave_cache=wave_cache,
            )
            chain.append(tail_wave_id)
        if not chain:
            return PulseWindowResult(0, 0, 0)

        interrupted = False
        busy_seen = False
        expected_seconds = steps / max(1, steps_per_second)
        if self._stop_event.is_set() or self._shutdown.is_set():
            return PulseWindowResult(0, steps, 0, interrupted_batch=True, reason="stopped")
        try:
            start = time.monotonic()
            send_result = self.pi.wave_chain(chain)
            if isinstance(send_result, int) and send_result < 0:
                raise HardwareFault(f"pigpio wave_chain failed with code {send_result}.")
            next_diag = time.monotonic() + self.WAVE_DIAGNOSTIC_SECONDS
            while self.pi.wave_tx_busy():
                busy_seen = True
                if self._stop_event.is_set() or self._shutdown.is_set():
                    interrupted = True
                    self.pi.wave_tx_stop()
                    break
                now = time.monotonic()
                if now >= next_diag:
                    diagnostics = self.uart.diagnostics(assignment.uart_address)
                    self._check_stepper_diagnostics(
                        assignment,
                        diagnostics,
                        phase="during-wave",
                        moved_steps=moved_steps,
                        steps_per_second=steps_per_second,
                        run_current_ma=run_current_ma,
                        hold_current_ma=hold_current_ma,
                        microsteps=microsteps,
                        stallguard_threshold=stallguard_threshold,
                    )
                    next_diag = now + self.WAVE_DIAGNOSTIC_SECONDS
                self._stop_event.wait(self.WAVE_POLL_SECONDS)
            elapsed = time.monotonic() - start
            if (
                busy_seen
                and not interrupted
                and not self._stop_event.is_set()
                and not self._shutdown.is_set()
                and elapsed + self.WAVE_EXTERNAL_CANCEL_TOLERANCE_SECONDS < expected_seconds
            ):
                raise HardwareFault(
                    f"pigpio waveform ended early after {elapsed:.3f}s; expected about {expected_seconds:.3f}s."
                )
        except BaseException:
            try:
                if self.pi.wave_tx_busy():
                    self.pi.wave_tx_stop()
            finally:
                raise
        finally:
            self._set_step_low(assignment)
        if interrupted:
            return PulseWindowResult(0, steps, 0, interrupted_batch=True, reason="shutdown" if self._shutdown.is_set() else "stopped")
        return PulseWindowResult(steps, steps, full_repeats + (1 if tail_steps else 0))

    def _wave_id_for_steps(
        self,
        assignment: StepperAssignment,
        *,
        steps: int,
        steps_per_second: int,
        wave_cache: dict[tuple[int, int, int], int],
    ) -> int:
        if not self.pi:
            raise HardwareFault("pigpio is unavailable for waveform creation.")
        cache_key = (assignment.step_bcm, steps, steps_per_second)
        wave_id = wave_cache.get(cache_key)
        if wave_id is not None:
            return wave_id
        pulses = self._build_step_wave_pulses(assignment, steps=steps, steps_per_second=steps_per_second)
        add_result = self.pi.wave_add_generic(pulses)
        if isinstance(add_result, int) and add_result < 0:
            raise HardwareFault(f"pigpio wave_add_generic failed with code {add_result}.")
        wave_id = self.pi.wave_create()
        if not isinstance(wave_id, int) or wave_id < 0:
            raise HardwareFault(f"pigpio wave_create failed with code {wave_id}.")
        wave_cache[cache_key] = wave_id
        self._wave_ids.add(wave_id)
        return wave_id

    def _append_wave_repeats(self, chain: list[int], wave_id: int, repeats: int) -> None:
        while repeats > 0:
            repeat_count = min(repeats, self.WAVE_CHAIN_MAX_REPEATS)
            if repeat_count == 1:
                chain.append(wave_id)
            else:
                chain.extend([255, 0, wave_id, 255, 1, repeat_count & 0xFF, (repeat_count >> 8) & 0xFF])
            repeats -= repeat_count

    def _delete_cached_waves(self, wave_cache: dict[tuple[int, int, int], int]) -> None:
        if self.simulation or not self.pi:
            return
        for wave_id in set(wave_cache.values()):
            try:
                self.pi.wave_delete(wave_id)
            finally:
                self._wave_ids.discard(wave_id)

    def _check_stepper_diagnostics(
        self,
        assignment: StepperAssignment,
        diagnostics: dict[str, Any],
        *,
        phase: str,
        moved_steps: int,
        steps_per_second: int,
        run_current_ma: int,
        hold_current_ma: int,
        microsteps: int,
        stallguard_threshold: int,
    ) -> None:
        context = (
            "stepper=%s phase=%s drv_status=0x%08x sg_result=%s standstill=%s stealthchop=%s "
            "cs_actual=%s open_load_a=%s open_load_b=%s speed=%s run_current_ma=%s hold_current_ma=%s "
            "microsteps=%s stallguard_threshold=%s moved_steps=%s"
        )
        context_args = (
            assignment.name,
            phase,
            diagnostics.get("drv_status", 0),
            diagnostics.get("sg_result"),
            diagnostics.get("standstill"),
            diagnostics.get("stealthchop"),
            diagnostics.get("cs_actual"),
            diagnostics.get("open_load_a"),
            diagnostics.get("open_load_b"),
            steps_per_second,
            run_current_ma,
            hold_current_ma,
            microsteps,
            stallguard_threshold,
            moved_steps,
        )
        if diagnostics.get("overtemp_shutdown") or diagnostics.get("overtemp_warning"):
            LOGGER.error("TMC2209 thermal fault: " + context, *context_args)
            raise HardwareUnavailable(f"Stepper {assignment.name} reported thermal warning.")
        short_flags = [
            key
            for key in ("short_to_ground_a", "short_to_ground_b", "short_to_supply_a", "short_to_supply_b")
            if diagnostics.get(key)
        ]
        if short_flags:
            LOGGER.error("TMC2209 short-circuit fault (%s): " + context, ",".join(short_flags), *context_args)
            raise HardwareUnavailable(f"Stepper {assignment.name} reported electrical fault: {','.join(short_flags)}.")
        if diagnostics.get("open_load_a") or diagnostics.get("open_load_b"):
            LOGGER.warning("TMC2209 open-load indicator: " + context, *context_args)

        fullstep_warmup_steps = max(1, 4 * max(1, microsteps))
        stallguard_context_valid = (
            moved_steps >= fullstep_warmup_steps
            and not diagnostics.get("standstill")
            and bool(diagnostics.get("stealthchop"))
            and stallguard_threshold > 0
        )
        if stallguard_context_valid and diagnostics.get("sg_result", 0) <= stallguard_threshold:
            LOGGER.warning(
                "TMC2209 low StallGuard4 load value below configured threshold=%s: " + context,
                stallguard_threshold,
                *context_args,
            )

    def _log_motion_result(
        self,
        assignment: StepperAssignment,
        *,
        requested_steps: int,
        result: StepperRunResult,
        requested_speed: int,
    ) -> None:
        LOGGER.info(
            "Stepper motion finished: stepper=%s requested_speed=%s requested_steps=%s confirmed_steps=%s "
            "possible_steps=%s completed_batches=%s interrupted=%s elapsed=%.3fs effective_speed=%s "
            "max_inter_batch_gap=%.6fs reason=%s",
            assignment.name,
            requested_speed,
            requested_steps,
            result.steps_sent,
            result.possible_steps,
            result.completed_batches,
            result.interrupted_batch,
            result.elapsed_seconds,
            f"{result.effective_steps_per_second:.3f}" if result.effective_steps_per_second is not None else "unknown",
            result.max_inter_batch_gap_seconds,
            result.reason,
        )

    @staticmethod
    def estimate_current_ma(run_current_ma: int, duty_cycle: float) -> float:
        return max(0.0, run_current_ma * max(0.0, min(1.0, duty_cycle)))


class HostHealthMonitor:
    """Linux host health reader.

    Implements SRS 2.5.1 through 2.5.4.
    """

    def __init__(self) -> None:
        self._last_cpu: tuple[int, int] | None = None

    def snapshot(self, root: Path = Path("/")) -> dict[str, float]:
        disk_used, disk_available = self.disk_usage(root)
        return {
            "cpu_percent": self.cpu_percent(),
            "memory_total": float(self.memory()["total"]),
            "memory_active": float(self.memory()["active"]),
            "memory_available": float(self.memory()["available"]),
            "disk_used": disk_used,
            "disk_available": disk_available,
            "uptime_seconds": self.uptime_seconds(),
        }

    @staticmethod
    def disk_usage(root: Path) -> tuple[float, float]:
        if hasattr(os, "statvfs"):
            stat = os.statvfs(root)
            return float(stat.f_blocks - stat.f_bavail), float(stat.f_bavail)
        usage = shutil.disk_usage(root)
        return float(usage.used), float(usage.free)

    def cpu_percent(self) -> float:
        try:
            fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
            values = [int(value) for value in fields]
        except Exception:
            return 0.0
        idle = values[3] + values[4]
        total = sum(values)
        if self._last_cpu is None:
            self._last_cpu = (idle, total)
            return 0.0
        last_idle, last_total = self._last_cpu
        self._last_cpu = (idle, total)
        total_delta = total - last_total
        idle_delta = idle - last_idle
        return 0.0 if total_delta <= 0 else round((1 - idle_delta / total_delta) * 100, 2)

    @staticmethod
    def memory() -> dict[str, int]:
        result: dict[str, int] = {"total": 0, "active": 0, "available": 0}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, value = line.split(":", 1)
                if key == "MemTotal":
                    result["total"] = int(value.split()[0]) * 1024
                elif key == "Active":
                    result["active"] = int(value.split()[0]) * 1024
                elif key == "MemAvailable":
                    result["available"] = int(value.split()[0]) * 1024
        except Exception:
            pass
        return result

    @staticmethod
    def uptime_seconds() -> float:
        try:
            return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        except Exception:
            return 0.0
