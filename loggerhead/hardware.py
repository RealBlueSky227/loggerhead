from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DiagnosticHalt(RuntimeError):
    """Raised when a configuration violates the fixed hardware map.

    Implements SRS 8.7 Software Pin Enforcement Interlock.
    """


class EquipmentDriver(StrEnum):
    MCP23017_RELAY = "mcp23017_relay"
    KASA_HS300 = "kasa_hs300"


class TemperatureDriver(StrEnum):
    ONE_WIRE_BUS = "one_wire_bus"
    BIT_BANGED_ONE_WIRE = "bit_banged_one_wire"
    HOST_CPU = "host_cpu"


class WaterLevelDriver(StrEnum):
    BINARY = "binary"
    HYDROS_TRIPLE = "hydros_triple"


class EquipmentKind(StrEnum):
    HEATER = "heater"
    CHILLER = "chiller"
    FAN = "fan"
    GENERIC = "generic"


class LevelState(StrEnum):
    DRY = "dry"
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    SUBMERGED = "submerged"
    WET = "wet"
    INACTIVE = "inactive_faulted"
    UNKNOWN = "unknown"


class AlarmPriority(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class StepperAssignment:
    name: str
    uart_address: int
    enable_pin: str
    step_bcm: int
    direction_bcm: int


@dataclass(frozen=True)
class SensePort:
    number: int
    ads1115_address: int
    analog_channel: int
    digital_bcm: int


@dataclass(frozen=True)
class RelayAssignment:
    name: str
    mcp_pin: str
    hardware_normally_closed: bool


# Implements SRS 8.1 Stepper Dosing Pump Interface Assignments.
STEPPERS: dict[str, StepperAssignment] = {
    "dose1": StepperAssignment("dose1", 0, "GPA4", 16, 20),
    "dose2": StepperAssignment("dose2", 1, "GPA5", 8, 7),
    "dose3": StepperAssignment("dose3", 2, "GPA6", 24, 25),
    "dose4": StepperAssignment("dose4", 3, "GPA7", 18, 23),
}

# Implements SRS 8.2 Sensing Port Assignments.
SENSE_PORTS: dict[int, SensePort] = {
    1: SensePort(1, 0x48, 0, 4),
    2: SensePort(2, 0x48, 1, 17),
    3: SensePort(3, 0x48, 2, 11),
    4: SensePort(4, 0x48, 3, 5),
    5: SensePort(5, 0x49, 0, 27),
    6: SensePort(6, 0x49, 1, 22),
    7: SensePort(7, 0x49, 2, 6),
    8: SensePort(8, 0x49, 3, 26),
    9: SensePort(9, 0x4A, 0, 10),
    10: SensePort(10, 0x4A, 1, 9),
}

# Implements SRS 8.3 Physical Onboard AC Relay Outlets.
RELAYS: dict[str, RelayAssignment] = {
    "AC1": RelayAssignment("AC1", "GPB0", False),
    "AC2": RelayAssignment("AC2", "GPB1", False),
    "AC3": RelayAssignment("AC3", "GPB2", False),
    "AC4": RelayAssignment("AC4", "GPB3", False),
    "AC5": RelayAssignment("AC5", "GPA0", False),
    "AC6": RelayAssignment("AC6", "GPA1", False),
    "AC7": RelayAssignment("AC7", "GPA2", True),
    "AC8": RelayAssignment("AC8", "GPA3", True),
}

MCP23017_ADDRESS = 0x20
PH_EZO_I2C_ADDRESS = 0x63  # Implements SRS 8.5.
BUZZER_PWM_BCM = 12  # Implements SRS 8.6.
STEPPER_UART_RX_BCM = 15

ALLOWED_DIGITAL_BCM = {port.digital_bcm for port in SENSE_PORTS.values()}
ALLOWED_STEPPER_BCM = {pin for stepper in STEPPERS.values() for pin in (stepper.step_bcm, stepper.direction_bcm)}
ALLOWED_BCM = ALLOWED_DIGITAL_BCM | ALLOWED_STEPPER_BCM | {BUZZER_PWM_BCM, STEPPER_UART_RX_BCM}


def require_relay(name: str) -> RelayAssignment:
    try:
        return RELAYS[name]
    except KeyError as exc:
        raise DiagnosticHalt(f"{name!r} is not a permitted onboard relay. Allowed relays: {sorted(RELAYS)}") from exc


def require_sense_port(number: int) -> SensePort:
    try:
        return SENSE_PORTS[number]
    except KeyError as exc:
        raise DiagnosticHalt(f"Sense port {number!r} is outside the fixed Sensor 1-10 map.") from exc


def require_stepper(name: str) -> StepperAssignment:
    try:
        return STEPPERS[name]
    except KeyError as exc:
        raise DiagnosticHalt(f"{name!r} is not one of the four fixed TMC2209 dosing pumps.") from exc


def require_bcm(pin: int) -> None:
    if pin not in ALLOWED_BCM:
        raise DiagnosticHalt(f"BCM GPIO {pin} is not permitted by the Loggerhead hardware map.")
