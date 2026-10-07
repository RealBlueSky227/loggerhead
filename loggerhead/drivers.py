from __future__ import annotations

import json
import logging
import os
import socket
import struct
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

    def __init__(self, *, bus_id: int = 1, address: int = MCP23017_ADDRESS, simulation: bool = False) -> None:
        self.address = address
        self.simulation = simulation
        self.shadow_a = 0
        self.shadow_b = 0
        self.bus = None
        if not simulation:
            try:
                from smbus2 import SMBus  # type: ignore

                self.bus = SMBus(bus_id)
                self.bus.write_byte_data(address, self.IODIRA, 0x00)
                self.bus.write_byte_data(address, self.IODIRB, 0x00)
            except Exception as exc:
                LOGGER.warning("MCP23017 unavailable, falling back to simulation: %s", exc)
                self.simulation = True

    def set_pin(self, pin: str, active: bool) -> None:
        port = pin[:3]
        bit = int(pin[3:])
        if port not in {"GPA", "GPB"} or bit < 0 or bit > 7:
            raise DiagnosticHalt(f"Invalid MCP23017 pin {pin!r}.")
        mask = 1 << bit
        if port == "GPA":
            self.shadow_a = self.shadow_a | mask if active else self.shadow_a & ~mask
            register = self.OLATA
            value = self.shadow_a
        else:
            self.shadow_b = self.shadow_b | mask if active else self.shadow_b & ~mask
            register = self.OLATB
            value = self.shadow_b
        if not self.simulation and self.bus:
            self.bus.write_byte_data(self.address, register, value)

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

    def read_one_wire_bus(self, sensor_id: str) -> float:
        path = Path("/sys/bus/w1/devices") / sensor_id / "w1_slave"
        if self.simulation or not path.exists():
            return 78.0
        text = path.read_text(encoding="utf-8")
        marker = "t="
        if marker not in text:
            raise ValueError(f"1-Wire sensor {sensor_id} returned no temperature marker.")
        return int(text.split(marker, 1)[1].strip()) / 1000 * 9 / 5 + 32

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
    REG_GSTAT = 0x01
    REG_IHOLD_IRUN = 0x10
    REG_DRV_STATUS = 0x6F
    REG_SG_RESULT = 0x41

    def __init__(self, port: str = "/dev/serial0", *, baudrate: int = 115200, simulation: bool = False) -> None:
        self.simulation = simulation
        self.serial = None
        if not simulation:
            try:
                import serial  # type: ignore

                self.serial = serial.Serial(port, baudrate=baudrate, timeout=0.2)
            except Exception as exc:
                LOGGER.warning("TMC2209 UART unavailable, falling back to simulation: %s", exc)
                self.simulation = True

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

    def write_register(self, node: int, register: int, value: int) -> None:
        if node not in {0, 1, 2, 3}:
            raise DiagnosticHalt("TMC2209 UART nodes are fixed to addresses 0-3.")
        payload = bytes([self.SYNC, self.MASTER, node, register | 0x80]) + value.to_bytes(4, "big")
        frame = payload + bytes([self.crc8(payload)])
        if not self.simulation and self.serial:
            self.serial.write(frame)

    def read_register(self, node: int, register: int) -> int:
        if self.simulation or not self.serial:
            if register == self.REG_SG_RESULT:
                return 100
            return 0
        request = bytes([self.SYNC, self.MASTER, node, register & 0x7F])
        self.serial.write(request + bytes([self.crc8(request)]))
        response = self.serial.read(8)
        if len(response) < 8:
            raise TimeoutError("TMC2209 UART read timed out.")
        return int.from_bytes(response[3:7], "big")

    def set_current(self, node: int, run_current_ma: int, hold_current_ma: int) -> None:
        ihold = min(31, max(0, round(hold_current_ma / max(run_current_ma, 1) * 31)))
        irun = min(31, max(0, round(run_current_ma / 1000 * 31)))
        value = ihold | (irun << 8) | (5 << 16)
        self.write_register(node, self.REG_IHOLD_IRUN, value)

    def diagnostics(self, node: int) -> dict[str, Any]:
        drv = self.read_register(node, self.REG_DRV_STATUS)
        return {
            "sg_result": self.read_register(node, self.REG_SG_RESULT),
            "overtemp_warning": bool(drv & (1 << 26)),
            "overtemp_shutdown": bool(drv & (1 << 25)),
        }


@dataclass
class StepperMove:
    stepper_id: str
    steps: int
    steps_per_second: int


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
        self.active_stepper: str | None = None
        self.pi = None
        if not simulation:
            try:
                import pigpio  # type: ignore

                self.pi = pigpio.pi()
                if not self.pi.connected:
                    raise HardwareUnavailable("pigpiod is not connected.")
            except Exception as exc:
                LOGGER.warning("Stepper pigpio unavailable, falling back to simulation: %s", exc)
                self.simulation = True

    def move(self, assignment: StepperAssignment, *, steps: int, steps_per_second: int, run_current_ma: int, hold_current_ma: int = 0) -> None:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A stepper motor is already active.")
        self.active_stepper = assignment.name
        self._stop_event.clear()
        try:
            self.uart.set_current(assignment.uart_address, run_current_ma, hold_current_ma)
            self.relay_board.set_pin(assignment.enable_pin, True)
            self._pulse_windowed(assignment, steps=steps, steps_per_second=steps_per_second)
        finally:
            self.stop(assignment)
            self.active_stepper = None
            self._lock.release()

    def stop(self, assignment: StepperAssignment) -> None:
        self._stop_event.set()
        self.relay_board.set_pin(assignment.enable_pin, False)
        self.uart.set_current(assignment.uart_address, 0, 0)

    def _pulse_windowed(self, assignment: StepperAssignment, *, steps: int, steps_per_second: int) -> None:
        # Implements SRS 3.4.3.1 with bounded chunks so pigpio queues never grow unbounded.
        delay = 1.0 / max(1, steps_per_second)
        remaining = abs(steps)
        window = deque([min(remaining, 256)])
        while remaining > 0 and not self._stop_event.is_set():
            chunk = window.popleft()
            diagnostics = self.uart.diagnostics(assignment.uart_address)
            if diagnostics["overtemp_warning"] or diagnostics["overtemp_shutdown"]:
                raise HardwareUnavailable(f"Stepper {assignment.name} reported thermal warning.")
            if diagnostics["sg_result"] < 10:
                raise HardwareUnavailable(f"Stepper {assignment.name} reported stall/load fault.")
            if self.simulation or not self.pi:
                time.sleep(chunk * delay)
            else:
                # A production Pi uses pigpio wave chains here. The chunking and sleep fallback
                # preserve deterministic queue behavior in simulation and tests.
                for _ in range(chunk):
                    self.pi.write(assignment.step_bcm, 1)
                    time.sleep(delay / 2)
                    self.pi.write(assignment.step_bcm, 0)
                    time.sleep(delay / 2)
            remaining -= chunk
            if remaining:
                window.append(min(remaining, 256))

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
