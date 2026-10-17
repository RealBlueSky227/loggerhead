from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from .hardware import (
    BUZZER_PWM_BCM,
    PH_EZO_I2C_ADDRESS,
    DiagnosticHalt,
    EquipmentDriver,
    EquipmentKind,
    LevelState,
    TemperatureDriver,
    WaterLevelDriver,
    require_relay,
    require_sense_port,
    require_stepper,
)

MIN_MANUAL_PRIME_STEPS_PER_SECOND = 1
MAX_MANUAL_PRIME_STEPS_PER_SECOND = 5000
DEFAULT_MANUAL_PRIME_STEPS_PER_SECOND = 400
DEFAULT_MANUAL_PRIME_MAX_SECONDS = 60.0
DEFAULT_MANUAL_PRIME_MAX_STEPS = 300_000
SUPPORTED_MICROSTEPS = {1, 2, 4, 8, 16, 32, 64, 128, 256}


class SensePortDevice(StrEnum):
    EMPTY = "empty"
    HYDROS_TRIPLE = "hydros_triple"
    BINARY = "binary"
    DS18B20 = "ds18b20"
    ANALOG = "analog"


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class HomeAssistantNotifyConfig:
    enabled: bool = False
    base_url: str = "http://homeassistant.local:8123"
    token: str = ""
    notify_service: str = "persistent_notification.create"


@dataclass
class MQTTConfig:
    enabled: bool = False
    host: str = "homeassistant.local"
    port: int = 1883
    username: str = ""
    password: str = ""
    base_topic: str = "loggerhead"


@dataclass
class BuzzerConfig:
    enabled: bool = True
    alarm_enabled: bool = True
    bcm_pin: int = BUZZER_PWM_BCM
    frequency_hz: int = 2500
    rearm_seconds: int = 900


@dataclass
class EquipmentProfile:
    # Implements SRS 5.4.2 Equipment Parameters.
    id: str
    name: str
    driver: EquipmentDriver
    pin_or_outlet: str
    default_on: bool = False
    normally_on: bool = False
    kasa_host: str = ""
    verify_seconds: int = 30


@dataclass
class PHSensorProfile:
    # Implements SRS 5.4.3 pH Sensor Parameters.
    id: str
    name: str
    check_frequency: float = 30.0
    i2c_address: int = PH_EZO_I2C_ADDRESS


@dataclass
class AnalogSensorProfile:
    id: str
    name: str
    sense_port: int
    check_frequency: float = 10.0
    unit: str = "V"
    scale: float = 1.0
    offset: float = 0.0


@dataclass
class SensePortProfile:
    number: int
    device: SensePortDevice = SensePortDevice.EMPTY
    name: str = ""
    check_frequency: float = 10.0
    sensor_id: str = ""
    desired_state: LevelState = LevelState.NORMAL
    alert_wait: float = 60.0
    alert_frequency: float = 300.0
    activity_timeout: float = 2.0
    debounce_samples: int = 3
    invert_binary: bool = False
    target_temp: float = 78.0
    hysteresis: float = 0.5
    alert_above: float = 82.0
    alert_below: float = 74.0
    emergency_above: float = 84.0
    emergency_below: float = 72.0
    assigned_equipment: str = ""
    equipment_type: EquipmentKind = EquipmentKind.HEATER
    analog_unit: str = "V"
    analog_scale: float = 1.0
    analog_offset: float = 0.0

    @property
    def sensor_id_or_default(self) -> str:
        return self.sensor_id or f"sense_port_{self.number}"

    @property
    def display_name(self) -> str:
        return self.name or f"Sense Port {self.number}"


@dataclass
class TemperatureSensorProfile:
    # Implements SRS 5.4.4 Temperature Sensor Parameters.
    id: str
    name: str
    driver: TemperatureDriver
    target_temp: float = 78.0
    hysteresis: float = 0.5
    check_frequency: float = 10.0
    alert_above: float = 82.0
    alert_below: float = 74.0
    emergency_above: float = 84.0
    emergency_below: float = 72.0
    assigned_equipment: str = ""
    equipment_type: EquipmentKind = EquipmentKind.HEATER
    sensor_id: str = ""
    sense_port: int | None = None


@dataclass
class WaterLevelSensorProfile:
    # Implements SRS 5.4.5 Water Level Sensor Parameters.
    id: str
    name: str
    driver: WaterLevelDriver
    sense_port: int
    desired_state: LevelState = LevelState.NORMAL
    current_state: LevelState = LevelState.UNKNOWN
    alert_wait: float = 60.0
    alert_frequency: float = 300.0
    check_frequency: float = 1.0
    activity_timeout: float = 2.0
    debounce_samples: int = 3
    invert_binary: bool = False


@dataclass
class DosingProfile:
    # Implements SRS 3.3 and 3.4 dosing configuration.
    id: str
    name: str
    actuator: str
    daily_volume_ml: float = 0.0
    calibration_ml_per_minute: float = 30.0
    window_start: str = "08:00"
    window_end: str = "20:00"
    doses_per_day: int = 12
    stepper: bool = False
    enabled: bool = False
    max_dose_ml: float = 10.0
    max_run_seconds: float = 120.0


@dataclass
class StepperProfile:
    # Implements SRS 3.4 Advanced Stepper Dosing Pumps.
    id: str
    name: str
    assignment: str
    run_current_ma: int = 700
    hold_current_ma: int = 0
    microsteps: int = 16
    steps_per_ml: float = 800.0
    stallguard_threshold: int = 50
    manual_speed_steps_per_second: int = DEFAULT_MANUAL_PRIME_STEPS_PER_SECOND
    dose_speed_steps_per_second: int = DEFAULT_MANUAL_PRIME_STEPS_PER_SECOND
    manual_max_seconds: float = DEFAULT_MANUAL_PRIME_MAX_SECONDS
    manual_max_steps: int = DEFAULT_MANUAL_PRIME_MAX_STEPS
    direction_high: bool = True


@dataclass
class ATOProfile:
    # Implements SRS 5.4.6 Auto Top Off Parameters.
    id: str
    name: str
    primary_level_sensor: str
    backup_failsafe_sensor: str
    actuator_device_type: str
    assigned_actuator: str
    max_run_time: float = 60.0
    enabled: bool = True
    start_level: LevelState = LevelState.DRY
    stop_level: LevelState | None = None  # Legacy: first non-dry state stops.
    max_refill_ml: float = 0.0
    max_daily_ml: float = 0.0


@dataclass
class AppConfig:
    # Implements SRS 5.4 Configuration Management and SRS 5.4.7 default safe config.
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    home_assistant_notify: HomeAssistantNotifyConfig = field(default_factory=HomeAssistantNotifyConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    buzzer: BuzzerConfig = field(default_factory=BuzzerConfig)
    equipment: list[EquipmentProfile] = field(default_factory=list)
    sense_ports: list[SensePortProfile] = field(default_factory=list)
    ph_sensors: list[PHSensorProfile] = field(default_factory=list)
    analog_sensors: list[AnalogSensorProfile] = field(default_factory=list)
    temperature_sensors: list[TemperatureSensorProfile] = field(default_factory=list)
    water_level_sensors: list[WaterLevelSensorProfile] = field(default_factory=list)
    dosing: list[DosingProfile] = field(default_factory=list)
    steppers: list[StepperProfile] = field(default_factory=list)
    ato: list[ATOProfile] = field(default_factory=list)
    database_poll_seconds: int = 60
    plot_max_points: int = 800


def default_config() -> AppConfig:
    return AppConfig(
        equipment=[
            EquipmentProfile(f"ac{index}", f"AC{index}", EquipmentDriver.MCP23017_RELAY, f"AC{index}")
            for index in range(1, 9)
        ],
        sense_ports=[SensePortProfile(index) for index in range(1, 11)],
        ph_sensors=[],
        analog_sensors=[],
        temperature_sensors=[
            TemperatureSensorProfile(
                "cpu_temp",
                "Raspberry Pi CPU",
                TemperatureDriver.HOST_CPU,
                target_temp=140.0,
                hysteresis=10.0,
                alert_above=176.0,
                alert_below=-40.0,
                emergency_above=185.0,
                emergency_below=-40.0,
                equipment_type=EquipmentKind.GENERIC,
            )
        ],
        water_level_sensors=[],
        steppers=[
            StepperProfile("dose1", "Stepper Dose 1", "dose1"),
            StepperProfile("dose2", "Stepper Dose 2", "dose2"),
            StepperProfile("dose3", "Stepper Dose 3", "dose3"),
            StepperProfile("dose4", "Stepper Dose 4", "dose4"),
        ],
        ato=[],
    )


def _coerce_value(value: Any, typ: Any) -> Any:
    origin = get_origin(typ)
    if origin is list:
        (inner,) = get_args(typ)
        return [_coerce_value(item, inner) for item in value]
    if origin is dict:
        key_type, value_type = get_args(typ)
        return {_coerce_value(k, key_type): _coerce_value(v, value_type) for k, v in value.items()}
    if origin is not None and type(None) in get_args(typ):
        if value is None:
            return None
        non_none = next(arg for arg in get_args(typ) if arg is not type(None))
        return _coerce_value(value, non_none)
    if isinstance(typ, type) and issubclass(typ, str) and hasattr(typ, "__members__"):
        return typ(value)
    if isinstance(typ, type) and is_dataclass(typ):
        return from_dict(typ, value)
    return value


def from_dict[T](cls: type[T], data: dict[str, Any]) -> T:
    values = {}
    type_hints = get_type_hints(cls)
    for field_info in fields(cls):
        if field_info.name in data:
            values[field_info.name] = _coerce_value(data[field_info.name], type_hints[field_info.name])
    return cls(**values)


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        config = default_config()
        save_config(path, config)
        return config
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    _migrate_raw_config(raw)
    config = from_dict(AppConfig, raw)
    apply_config_migrations(config)
    validate_config(config)
    return config


def _migrate_raw_config(raw: dict[str, Any]) -> None:
    """Move legacy fields before dataclass coercion can drop them."""
    for ato in raw.get("ato", []):
        if isinstance(ato, dict) and ato.get("actuator_device_type") == "stepper" and (
            "max_refill_ml" not in ato or "max_daily_ml" not in ato
        ):
            # Old stepper ATO settings were accepted but never actuated. Do
            # not turn them into live pumps merely by upgrading the software.
            ato["enabled"] = False
    for port in raw.get("sense_ports", []):
        if isinstance(port, dict):
            port.pop("one_wire_mode", None)
    for sensor in raw.get("temperature_sensors", []):
        if isinstance(sensor, dict) and sensor.get("driver") == "bit_banged_one_wire":
            sensor["driver"] = TemperatureDriver.ONE_WIRE_BUS.value
    dosing = raw.get("dosing", [])
    steppers = raw.get("steppers", [])
    if not isinstance(dosing, list) or not isinstance(steppers, list):
        return
    legacy_speeds = {
        item.get("actuator"): item.get("manual_speed_steps_per_second")
        for item in dosing
        if isinstance(item, dict) and item.get("stepper") and "manual_speed_steps_per_second" in item
    }
    for stepper in steppers:
        if not isinstance(stepper, dict):
            continue
        stepper_id = stepper.get("id")
        current_speed = stepper.get("manual_speed_steps_per_second", DEFAULT_MANUAL_PRIME_STEPS_PER_SECOND)
        try:
            current_speed_is_default = int(current_speed) == DEFAULT_MANUAL_PRIME_STEPS_PER_SECOND
        except (TypeError, ValueError):
            current_speed_is_default = False
        if stepper_id in legacy_speeds and current_speed_is_default:
            stepper["manual_speed_steps_per_second"] = legacy_speeds[stepper_id]


def save_config(path: Path, config: AppConfig) -> None:
    apply_config_migrations(config)
    validate_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(asdict(config), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = ""
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def apply_config_migrations(config: AppConfig) -> None:
    if not config.sense_ports:
        config.sense_ports = [SensePortProfile(index) for index in range(1, 11)]
    for port in config.sense_ports:
        if port.device == SensePortDevice.DS18B20:
            port.sensor_id = ""
    for sensor in config.temperature_sensors:
        if sensor.driver == TemperatureDriver.ONE_WIRE_BUS:
            sensor.sensor_id = ""
        if sensor.driver == TemperatureDriver.HOST_CPU and sensor.alert_above <= 100:
            sensor.target_temp = 140.0
            sensor.hysteresis = 10.0
            sensor.alert_above = 176.0
            sensor.alert_below = -40.0
            sensor.emergency_above = 185.0
            sensor.emergency_below = -40.0
            sensor.assigned_equipment = ""
            sensor.equipment_type = EquipmentKind.GENERIC


def validate_config(config: AppConfig) -> None:
    # Implements SRS 7.4 and 8.7 by rejecting anything outside the BCM/I2C map.
    if config.buzzer.bcm_pin != BUZZER_PWM_BCM:
        raise DiagnosticHalt("The buzzer must use BCM GPIO 12.")
    equipment_ids = {item.id for item in config.equipment}
    materialized_water = materialized_water_level_sensors(config)
    materialized_temperature = materialized_temperature_sensors(config)
    materialized_analog = materialized_analog_sensors(config)
    water_ids = {item.id for item in materialized_water}
    stepper_ids = {item.id for item in config.steppers}
    stepper_assignments = [item.assignment for item in config.steppers]
    if len(equipment_ids) != len(config.equipment):
        raise DiagnosticHalt("Equipment profile IDs must be unique.")
    if len(stepper_ids) != len(config.steppers):
        raise DiagnosticHalt("Stepper profile IDs must be unique.")
    if len(set(stepper_assignments)) != len(stepper_assignments):
        raise DiagnosticHalt("Stepper assignments must be unique.")
    for item in config.equipment:
        if item.driver == EquipmentDriver.MCP23017_RELAY:
            require_relay(item.pin_or_outlet)
        elif item.driver == EquipmentDriver.KASA_HS300:
            outlet = int(item.pin_or_outlet)
            if outlet < 0 or outlet > 5:
                raise DiagnosticHalt("Kasa HS300 outlet indexes must be 0 through 5.")
            if not item.kasa_host:
                raise DiagnosticHalt("Kasa HS300 equipment requires a static LAN host/IP.")
    for item in config.ph_sensors:
        if item.i2c_address != PH_EZO_I2C_ADDRESS:
            raise DiagnosticHalt("The pH interface is fixed to EZO address 0x63.")
        _validate_positive_frequency(item.check_frequency, f"pH sensor {item.id}")
    port_numbers = {item.number for item in config.sense_ports}
    if len(port_numbers) != len(config.sense_ports):
        raise DiagnosticHalt("Sense port entries must be unique.")
    for item in config.sense_ports:
        require_sense_port(item.number)
        _validate_sense_port_profile(item, equipment_ids)
    for item in materialized_analog:
        require_sense_port(item.sense_port)
        _validate_positive_frequency(item.check_frequency, f"Analog sensor {item.id}")
    for item in materialized_temperature:
        if item.sense_port is not None:
            require_sense_port(item.sense_port)
        _validate_temperature_profile(item, equipment_ids)
        if item.assigned_equipment and item.assigned_equipment not in equipment_ids:
            raise DiagnosticHalt(f"Temperature sensor {item.id} references unknown equipment {item.assigned_equipment}.")
    for item in materialized_water:
        require_sense_port(item.sense_port)
        _validate_water_profile(item)
    _validate_digital_sensor_ownership(materialized_temperature, materialized_water)
    for item in config.steppers:
        require_stepper(item.assignment)
        if item.hold_current_ma < 0:
            raise DiagnosticHalt("Stepper holding current cannot be negative.")
        if item.run_current_ma <= 0:
            raise DiagnosticHalt("Stepper run current must be greater than zero.")
        if item.hold_current_ma > item.run_current_ma:
            raise DiagnosticHalt("Stepper holding current cannot exceed running current.")
        if int(item.microsteps) not in SUPPORTED_MICROSTEPS:
            raise DiagnosticHalt(f"Stepper microsteps must be one of {sorted(SUPPORTED_MICROSTEPS)}.")
        item.microsteps = int(item.microsteps)
        if not math.isfinite(float(item.steps_per_ml)) or item.steps_per_ml <= 0:
            raise DiagnosticHalt("Stepper steps_per_ml must be greater than zero.")
        if item.stallguard_threshold < 0 or item.stallguard_threshold > 255:
            raise DiagnosticHalt("Stepper StallGuard threshold must be between 0 and 255.")
        item.stallguard_threshold = int(item.stallguard_threshold)
        try:
            manual_speed = int(item.manual_speed_steps_per_second)
        except (TypeError, ValueError) as exc:
            raise DiagnosticHalt(f"Stepper {item.id} manual priming speed must be an integer.") from exc
        if manual_speed < MIN_MANUAL_PRIME_STEPS_PER_SECOND:
            raise DiagnosticHalt(
                f"Stepper manual priming speed must be at least {MIN_MANUAL_PRIME_STEPS_PER_SECOND} step/s."
            )
        if manual_speed > MAX_MANUAL_PRIME_STEPS_PER_SECOND:
            raise DiagnosticHalt(
                f"Stepper manual priming speed must be no more than {MAX_MANUAL_PRIME_STEPS_PER_SECOND} step/s."
            )
        item.manual_speed_steps_per_second = manual_speed
        dose_speed = item.dose_speed_steps_per_second
        if isinstance(dose_speed, bool) or not isinstance(dose_speed, int) or not 1 <= dose_speed <= 5000:
            raise DiagnosticHalt("Scheduled dosing speed must be an integer from 1 to 5000 step/s.")
        item.manual_max_seconds = float(item.manual_max_seconds)
        if not math.isfinite(item.manual_max_seconds) or item.manual_max_seconds <= 0:
            raise DiagnosticHalt("Stepper manual priming max seconds must be greater than zero.")
        item.manual_max_steps = int(item.manual_max_steps)
        if item.manual_max_steps <= 0:
            raise DiagnosticHalt("Stepper manual priming max steps must be greater than zero.")
        item.direction_high = bool(item.direction_high)
    if len({item.id for item in config.dosing}) != len(config.dosing):
        raise DiagnosticHalt("Dosing schedule IDs must be unique.")
    enabled_pumps: set[str] = set()
    for item in config.dosing:
        if item.stepper and item.actuator not in stepper_ids:
            raise DiagnosticHalt(f"Dosing profile {item.id} references unknown stepper {item.actuator}.")
        if not item.stepper and item.actuator not in equipment_ids:
            raise DiagnosticHalt(f"Dosing profile {item.id} references unknown equipment {item.actuator}.")
        if type(item.enabled) is not bool:
            raise DiagnosticHalt("Dosing enabled must be true or false.")
        if item.enabled and not item.stepper:
            raise DiagnosticHalt("Automatic dosing currently requires an onboard stepper pump.")
        for value, name, allow_zero in (
            (item.daily_volume_ml, "daily volume", True),
            (item.max_dose_ml, "maximum dose volume", False),
            (item.max_run_seconds, "maximum dose runtime", False),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
                raise DiagnosticHalt(f"Dosing {name} must be finite and {'nonnegative' if allow_zero else 'positive'}.")
        if type(item.doses_per_day) is not int or not 0 <= item.doses_per_day <= 96 or (item.enabled and item.doses_per_day == 0):
            raise DiagnosticHalt("Dosing schedules require 1 to 96 doses per day.")
        for value in (item.window_start, item.window_end):
            try:
                hour, minute = value.split(":")
                valid = len(hour) == 2 and len(minute) == 2 and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59
            except (AttributeError, TypeError, ValueError):
                valid = False
            if not valid:
                raise DiagnosticHalt("Dosing window times must use HH:MM in 24-hour time.")
        if item.max_run_seconds > 600:
            raise DiagnosticHalt("A scheduled dose runtime cannot exceed 600 seconds.")
        if item.enabled:
            if item.daily_volume_ml <= 0:
                raise DiagnosticHalt("An enabled dosing schedule requires a positive daily volume.")
            if item.actuator in enabled_pumps:
                raise DiagnosticHalt("Only one enabled dosing schedule is allowed per pump.")
            enabled_pumps.add(item.actuator)
            if item.daily_volume_ml / item.doses_per_day > item.max_dose_ml:
                raise DiagnosticHalt("Scheduled dose volume exceeds the per-dose safety limit.")
            pump = next(pump for pump in config.steppers if pump.id == item.actuator)
            steps = math.floor(item.daily_volume_ml / item.doses_per_day * pump.steps_per_ml)
            if steps < 1:
                raise DiagnosticHalt("Scheduled dose is smaller than one calibrated step.")
            if steps / pump.dose_speed_steps_per_second + 2 > item.max_run_seconds:
                raise DiagnosticHalt("Scheduled dose cannot finish within its runtime limit (including startup margin).")
    if len({item.id for item in config.ato}) != len(config.ato):
        raise DiagnosticHalt("ATO profile IDs must be unique.")
    ato_devices: set[str] = set()
    water_by_id = {sensor.id: sensor for sensor in materialized_water}
    if config.ato and len(water_by_id) != len(materialized_water):
        raise DiagnosticHalt("ATO sensor references require unique water sensor IDs.")
    for item in config.ato:
        if not item.id or not item.name.strip() or type(item.enabled) is not bool:
            raise DiagnosticHalt("ATO requires a name, stable ID, and boolean enabled setting.")
        if item.primary_level_sensor not in water_ids:
            raise DiagnosticHalt(f"ATO {item.id} references unknown primary sensor {item.primary_level_sensor}.")
        if item.backup_failsafe_sensor not in water_ids:
            raise DiagnosticHalt(f"ATO {item.id} references unknown backup sensor {item.backup_failsafe_sensor}.")
        if item.assigned_actuator not in equipment_ids and item.assigned_actuator not in stepper_ids:
            raise DiagnosticHalt(f"ATO {item.id} references unknown actuator {item.assigned_actuator}.")
        primary, backup = water_by_id[item.primary_level_sensor], water_by_id[item.backup_failsafe_sensor]
        if primary.sense_port == backup.sense_port:
            raise DiagnosticHalt("ATO primary and backup must use different physical SensePorts.")
        if item.actuator_device_type in {"mcp_relay", "mcp23017_relay", "kasa_hs300"}:
            item.actuator_device_type = "relay"
        if item.actuator_device_type not in {"relay", "stepper"}:
            raise DiagnosticHalt("ATO refill type must be relay or stepper.")
        if item.assigned_actuator not in (stepper_ids if item.actuator_device_type == "stepper" else equipment_ids):
            raise DiagnosticHalt("ATO refill device does not match its selected type.")
        levels = ato_levels(primary)
        stop = item.stop_level or levels[1]
        if item.start_level not in levels or stop not in levels or levels.index(item.start_level) >= levels.index(stop):
            raise DiagnosticHalt("ATO start level must be below its stop level and supported by the primary sensor.")
        for key in ("max_run_time", "max_refill_ml", "max_daily_ml"):
            value = getattr(item, key)
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise DiagnosticHalt(f"ATO {key} must be a finite nonnegative number.")
        if not 0 < item.max_run_time <= 3600:
            raise DiagnosticHalt("ATO runtime must be positive and no greater than 3600 seconds.")
        if item.actuator_device_type == "stepper":
            pump = next(p for p in config.steppers if p.id == item.assigned_actuator)
            legacy_disabled = not item.enabled and item.max_refill_ml == item.max_daily_ml == 0
            if not legacy_disabled and (item.max_refill_ml <= 0 or item.max_daily_ml < item.max_refill_ml):
                raise DiagnosticHalt("Stepper ATO requires positive refill/daily volume limits; daily must be at least refill.")
            if not legacy_disabled and math.floor(item.max_refill_ml * pump.steps_per_ml) < 1:
                raise DiagnosticHalt("ATO refill volume must permit at least one calibrated step.")
            if item.enabled and item.assigned_actuator in enabled_pumps:
                raise DiagnosticHalt("An enabled ATO and chemical dosing schedule cannot share a pump.")
            device_key = f"stepper:{pump.assignment}"
        else:
            equipment = next(p for p in config.equipment if p.id == item.assigned_actuator)
            if item.max_refill_ml or item.max_daily_ml:
                raise DiagnosticHalt("Relay ATO cannot enforce volume limits without calibrated step feedback.")
            device_key = f"{equipment.driver}:{equipment.kasa_host}:{equipment.pin_or_outlet}"
            if any(other.id != equipment.id and (other.driver, other.kasa_host, other.pin_or_outlet) ==
                   (equipment.driver, equipment.kasa_host, equipment.pin_or_outlet) for other in config.equipment):
                raise DiagnosticHalt("ATO relay must not have another equipment ID aliasing its physical output.")
            if item.enabled and any(p.assigned_equipment == equipment.id for p in materialized_temperature):
                raise DiagnosticHalt("ATO cannot share temperature-controlled equipment.")
        if item.enabled:
            if device_key in ato_devices:
                raise DiagnosticHalt("Enabled ATO profiles cannot share an actuator.")
            ato_devices.add(device_key)


def ato_levels(sensor: WaterLevelSensorProfile) -> tuple[LevelState, ...]:
    if sensor.driver == WaterLevelDriver.BINARY:
        return (LevelState.DRY, LevelState.SUBMERGED)
    return (LevelState.DRY, LevelState.LOW, LevelState.NORMAL, LevelState.HIGH)


def _validate_positive_frequency(value: float, name: str) -> None:
    try:
        frequency = float(value)
    except (TypeError, ValueError) as exc:
        raise DiagnosticHalt(f"{name} check frequency must be numeric.") from exc
    if frequency <= 0:
        raise DiagnosticHalt(f"{name} check frequency must be greater than zero seconds.")


def _validate_sense_port_profile(item: SensePortProfile, equipment_ids: set[str]) -> None:
    _validate_positive_frequency(item.check_frequency, f"Sense Port {item.number}")
    item.alert_wait = float(item.alert_wait)
    item.alert_frequency = float(item.alert_frequency)
    item.activity_timeout = float(item.activity_timeout)
    item.debounce_samples = int(item.debounce_samples)
    item.analog_scale = float(item.analog_scale)
    item.analog_offset = float(item.analog_offset)
    if item.alert_wait < 0 or item.alert_frequency <= 0:
        raise DiagnosticHalt("Sense port alert delay must be non-negative and alert frequency must be positive.")
    if item.activity_timeout <= 0:
        raise DiagnosticHalt("HYDROS activity timeout must be greater than zero.")
    if item.debounce_samples <= 0:
        raise DiagnosticHalt("HYDROS debounce samples must be greater than zero.")
    if item.emergency_below > item.alert_below or item.alert_below >= item.alert_above or item.alert_above >= item.emergency_above:
        raise DiagnosticHalt("Temperature alert thresholds must be ordered emergency_low < alert_low < alert_high < emergency_high.")
    if item.assigned_equipment and item.assigned_equipment not in equipment_ids:
        raise DiagnosticHalt(f"Sense Port {item.number} references unknown equipment {item.assigned_equipment}.")


def _validate_temperature_profile(item: TemperatureSensorProfile, equipment_ids: set[str]) -> None:
    _validate_positive_frequency(item.check_frequency, f"Temperature sensor {item.id}")
    if item.driver == TemperatureDriver.ONE_WIRE_BUS:
        if item.sensor_id:
            raise DiagnosticHalt(
                f"Temperature sensor {item.id} must not configure a DS18B20 ROM ID; assign it to a fixed sense port GPIO."
            )
        if item.sense_port is None:
            raise DiagnosticHalt(f"Temperature sensor {item.id} must be assigned to a fixed sense port GPIO.")
    if item.emergency_below > item.alert_below or item.alert_below >= item.alert_above or item.alert_above >= item.emergency_above:
        raise DiagnosticHalt(f"Temperature sensor {item.id} thresholds must be ordered.")
    if item.assigned_equipment and item.assigned_equipment not in equipment_ids:
        raise DiagnosticHalt(f"Temperature sensor {item.id} references unknown equipment {item.assigned_equipment}.")


def _validate_water_profile(item: WaterLevelSensorProfile) -> None:
    _validate_positive_frequency(item.check_frequency, f"Water level sensor {item.id}")
    item.alert_wait = float(item.alert_wait)
    item.alert_frequency = float(item.alert_frequency)
    item.activity_timeout = float(item.activity_timeout)
    item.debounce_samples = int(item.debounce_samples)
    if item.alert_wait < 0 or item.alert_frequency <= 0:
        raise DiagnosticHalt(f"Water level sensor {item.id} alert delay must be non-negative and alert frequency must be positive.")
    if item.activity_timeout <= 0:
        raise DiagnosticHalt(f"Water level sensor {item.id} activity timeout must be greater than zero.")
    if item.debounce_samples <= 0:
        raise DiagnosticHalt(f"Water level sensor {item.id} debounce samples must be greater than zero.")


def _validate_digital_sensor_ownership(
    temperature_sensors: list[TemperatureSensorProfile],
    water_sensors: list[WaterLevelSensorProfile],
) -> None:
    owners: dict[int, str] = {}
    for item in temperature_sensors:
        if item.sense_port is None:
            continue
        owner = f"{item.id} ({item.driver.value})"
        previous = owners.setdefault(item.sense_port, owner)
        if previous != owner:
            raise DiagnosticHalt(
                f"Sense Port {item.sense_port} digital GPIO is shared by multiple sensor backends: {previous}, {owner}."
            )
    for item in water_sensors:
        owner = f"{item.id} ({item.driver.value})"
        previous = owners.setdefault(item.sense_port, owner)
        if previous != owner:
            raise DiagnosticHalt(
                f"Sense Port {item.sense_port} digital GPIO is shared by multiple sensor backends: {previous}, {owner}."
            )


def materialized_temperature_sensors(config: AppConfig) -> list[TemperatureSensorProfile]:
    sensors = list(config.temperature_sensors)
    for port in config.sense_ports:
        if port.device != SensePortDevice.DS18B20:
            continue
        sensors.append(
            TemperatureSensorProfile(
                f"sense_port_{port.number}_temperature",
                port.display_name,
                TemperatureDriver.ONE_WIRE_BUS,
                target_temp=port.target_temp,
                hysteresis=port.hysteresis,
                check_frequency=port.check_frequency,
                alert_above=port.alert_above,
                alert_below=port.alert_below,
                emergency_above=port.emergency_above,
                emergency_below=port.emergency_below,
                assigned_equipment=port.assigned_equipment,
                equipment_type=port.equipment_type,
                sense_port=port.number,
            )
        )
    return sensors


def materialized_water_level_sensors(config: AppConfig) -> list[WaterLevelSensorProfile]:
    sensors = list(config.water_level_sensors)
    for port in config.sense_ports:
        if port.device not in {SensePortDevice.HYDROS_TRIPLE, SensePortDevice.BINARY}:
            continue
        sensors.append(
            WaterLevelSensorProfile(
                f"sense_port_{port.number}_water",
                port.display_name,
                WaterLevelDriver.HYDROS_TRIPLE if port.device == SensePortDevice.HYDROS_TRIPLE else WaterLevelDriver.BINARY,
                port.number,
                desired_state=port.desired_state,
                alert_wait=port.alert_wait,
                alert_frequency=port.alert_frequency,
                check_frequency=port.check_frequency,
                activity_timeout=port.activity_timeout,
                debounce_samples=port.debounce_samples,
                invert_binary=port.invert_binary,
            )
        )
    return sensors


def materialized_analog_sensors(config: AppConfig) -> list[AnalogSensorProfile]:
    sensors = list(config.analog_sensors)
    for port in config.sense_ports:
        if port.device != SensePortDevice.ANALOG:
            continue
        sensors.append(
            AnalogSensorProfile(
                f"sense_port_{port.number}_analog",
                port.display_name,
                port.number,
                check_frequency=port.check_frequency,
                unit=port.analog_unit,
                scale=port.analog_scale,
                offset=port.analog_offset,
            )
        )
    return sensors
