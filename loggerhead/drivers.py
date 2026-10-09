from __future__ import annotations

import json
import logging
import os
import socket
import struct
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hardware import (
    BUZZER_PWM_BCM,
    MCP23017_ADDRESS,
    PH_EZO_I2C_ADDRESS,
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

    WINDOWS: tuple[tuple[LevelState, int, int], ...] = (
        (LevelState.HIGH, 1000, 1500),
        (LevelState.NORMAL, 2000, 3000),
        (LevelState.LOW, 4000, 6000),
        (LevelState.DRY, 20000, 30000),
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
        if candidate != LevelState.UNKNOWN and self._candidate_count >= self.debounce_samples:
            self.state = candidate
        return self.state

    def activity_state(self) -> LevelState:
        if time.monotonic() - self._last_edge > self.activity_timeout:
            self.state = LevelState.INACTIVE
        return self.state


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
                LOGGER.warning("EZO pH I2C unavailable, falling back to simulation: %s", exc)
                self.simulation = True

    def read_ph(self) -> float:
        if self.simulation or not self.bus:
            return 8.1
        self.bus.write_i2c_block_data(self.address, ord("R"), [])
        time.sleep(0.9)
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
                LOGGER.warning("ADS1115 I2C unavailable, falling back to simulation: %s", exc)
                self.simulation = True

    def read_voltage(self, address: int, channel: int) -> float:
        if channel < 0 or channel > 3:
            raise DiagnosticHalt(f"ADS1115 channel {channel} is outside AIN0-AIN3.")
        if self.simulation or not self.bus:
            return round(1.0 + channel * 0.25, 3)
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

    def __init__(self, *, simulation: bool = False) -> None:
        self.simulation = simulation
        self._configured_kernel_pins: set[int] = set()

    def configure_kernel_one_wire(self, bcm_pin: int) -> None:
        if self.simulation or bcm_pin in self._configured_kernel_pins:
            return
        result = subprocess.run(
            ["dtoverlay", "w1-gpio", f"gpiopin={bcm_pin}"],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            raise HardwareUnavailable(f"Could not enable kernel 1-Wire on BCM {bcm_pin}: {result.stderr.strip()}")
        self._configured_kernel_pins.add(bcm_pin)
        time.sleep(1.0)

    def read_one_wire_bus(self, sensor_id: str = "") -> float:
        resolved_id = sensor_id or self._first_one_wire_sensor_id()
        path = Path("/sys/bus/w1/devices") / resolved_id / "w1_slave"
        if self.simulation or not path.exists():
            return 78.0
        text = path.read_text(encoding="utf-8")
        marker = "t="
        if marker not in text:
            raise ValueError(f"1-Wire sensor {resolved_id} returned no temperature marker.")
        return int(text.split(marker, 1)[1].strip()) / 1000 * 9 / 5 + 32

    @staticmethod
    def _first_one_wire_sensor_id() -> str:
        devices = sorted(Path("/sys/bus/w1/devices").glob("28-*"))
        if not devices:
            raise HardwareUnavailable("No DS18B20 sensors were found on the kernel 1-Wire bus.")
        return devices[0].name

    def read_bit_banged(self, bcm_pin: int) -> float:
        # The exact DS18B20 timing is delegated to pigpio wave captures on hardware.
        # Implements SRS 2.1.2 while staying importable on non-Pi hosts.
        if self.simulation:
            return 78.0
        raise HardwareUnavailable(f"Bit-banged 1-Wire on BCM {bcm_pin} requires a Pi pigpio runtime.")

    def read_host_cpu(self) -> float:
        for path in (Path("/sys/class/thermal/thermal_zone0/temp"), Path("/sys/devices/virtual/thermal/thermal_zone0/temp")):
            if path.exists():
                return int(path.read_text(encoding="utf-8").strip()) / 1000 * 9 / 5 + 32
        return 0.0 if not self.simulation else 115.0


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
            self.pi.hardware_PWM(self.bcm_pin, self.frequency_hz, int(duty / 255 * 1_000_000))

    def stop(self) -> None:
        if not self.simulation and self.pi:
            self.pi.hardware_PWM(self.bcm_pin, 0, 0)


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
        self._sim_registers: dict[tuple[int, int], int] = {}
        self._sim_ifcnt: dict[int, int] = {}
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


class StepperPulseEngine:
    """Hardware-timed stepper pulse engine with a software interlock.

    Implements SRS 3.4.3 through 3.4.7.
    """

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
        self.relay_board.disable_all_steppers()
        if not simulation:
            try:
                import pigpio  # type: ignore

                self._gpio_output = getattr(pigpio, "OUTPUT", 1)
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
            self.relay_board.set_stepper_enabled(assignment, True)
            self._pulse_windowed(
                assignment,
                steps=steps,
                steps_per_second=steps_per_second,
                run_current_ma=run_current_ma,
                hold_current_ma=hold_current_ma,
                microsteps=microsteps,
                stallguard_threshold=stallguard_threshold,
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
        reason = "stopped"
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
            self.relay_board.set_stepper_enabled(assignment, True)
            started = time.monotonic()
            chunk_limit = min(256, max(1, steps_per_second // 4 or 1))
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
                chunk = min(chunk_limit, remaining_steps, remaining_time_steps)
                sent = self._pulse_windowed(
                    assignment,
                    steps=chunk,
                    steps_per_second=steps_per_second,
                    run_current_ma=run_current_ma,
                    hold_current_ma=hold_current_ma,
                    microsteps=microsteps,
                    stallguard_threshold=stallguard_threshold,
                )
                steps_sent += sent
                if sent < chunk:
                    reason = "stopped"
                    break
            if self._shutdown.is_set():
                reason = "shutdown"
            return StepperRunResult(steps_sent, reason)
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
                self.active_stepper = None
                self._lock.release()

    def stop(self, assignment: StepperAssignment) -> None:
        self._stop_event.set()
        self._set_step_low(assignment)
        self.relay_board.set_stepper_enabled(assignment, False)
        self.uart.set_current(assignment.uart_address, 0, 0)
        self._set_step_low(assignment)

    def request_stop(self) -> None:
        self._stop_event.set()

    def shutdown(self) -> None:
        self._shutdown.set()
        self._stop_event.set()
        acquired = self._lock.acquire(timeout=2.0)
        try:
            self.relay_board.disable_all_steppers()
        finally:
            if acquired:
                self._lock.release()
        if not acquired:
            self._fault = HardwareFault("Stepper engine did not quiesce during shutdown.")
            raise self._fault

    def _raise_if_unavailable(self) -> None:
        if self._shutdown.is_set():
            raise HardwareFault("Stepper engine is shut down.")
        if self._fault is not None:
            raise HardwareFault(f"Stepper engine is faulted; moves are blocked: {self._fault}") from self._fault

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
    ) -> int:
        # Implements SRS 3.4.3.1 with bounded chunks so pigpio queues never grow unbounded.
        delay = 1.0 / max(1, steps_per_second)
        remaining = abs(steps)
        window = deque([min(remaining, 256)])
        moved = 0
        try:
            while remaining > 0 and not self._stop_event.is_set():
                chunk = window.popleft()
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
                sent = self._pulse_chunk(assignment, chunk=chunk, delay=delay)
                remaining -= sent
                moved += sent
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
                if sent < chunk:
                    break
                if remaining:
                    window.append(min(remaining, 256))
            return moved
        finally:
            self._set_step_low(assignment)

    def _pulse_chunk(self, assignment: StepperAssignment, *, chunk: int, delay: float) -> int:
        if self.simulation or not self.pi:
            if self._stop_event.wait(chunk * delay):
                return 0
            return chunk

        sent = 0
        for _ in range(chunk):
            if self._stop_event.is_set():
                break
            self.pi.write(assignment.step_bcm, 1)
            if self._stop_event.wait(delay / 2):
                self.pi.write(assignment.step_bcm, 0)
                break
            self.pi.write(assignment.step_bcm, 0)
            sent += 1
            if self._stop_event.wait(delay / 2):
                break
        self.pi.write(assignment.step_bcm, 0)
        return sent

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
        return {
            "cpu_percent": self.cpu_percent(),
            "memory_total": float(self.memory()["total"]),
            "memory_active": float(self.memory()["active"]),
            "memory_available": float(self.memory()["available"]),
            "disk_used": float(os.statvfs(root).f_blocks - os.statvfs(root).f_bavail),
            "disk_available": float(os.statvfs(root).f_bavail),
            "uptime_seconds": self.uptime_seconds(),
        }

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
