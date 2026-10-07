from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
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
    manual_speed_steps_per_second: int = 400


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


@dataclass
class AppConfig:
    # Implements SRS 5.4 Configuration Management and SRS 5.4.7 default safe config.
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    home_assistant_notify: HomeAssistantNotifyConfig = field(default_factory=HomeAssistantNotifyConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    buzzer: BuzzerConfig = field(default_factory=BuzzerConfig)
    equipment: list[EquipmentProfile] = field(default_factory=list)
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
    water_level_sensors = [
        WaterLevelSensorProfile(f"level_sensor_{port}", f"Sensor {port} Triple Optical", WaterLevelDriver.HYDROS_TRIPLE, port)
        for port in (1, 4, 7, 10)
    ]
    temperature_sensors = [
        TemperatureSensorProfile(
            f"ds18b20_sensor_{port}",
            f"Sensor {port} DS18B20",
            TemperatureDriver.BIT_BANGED_ONE_WIRE,
            assigned_equipment="heater" if port == 2 else "",
            equipment_type=EquipmentKind.HEATER,
            sensor_id=f"sensor-{port}",
            sense_port=port,
        )
        for port in (2, 5, 8)
    ]
    temperature_sensors.append(
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
    )
    return AppConfig(
        equipment=[
            EquipmentProfile("heater", "Main Heater", EquipmentDriver.MCP23017_RELAY, "AC1"),
            EquipmentProfile("fan", "Cooling Fan", EquipmentDriver.MCP23017_RELAY, "AC2"),
            EquipmentProfile("ato_pump", "ATO Pump", EquipmentDriver.MCP23017_RELAY, "AC3"),
        ],
        ph_sensors=[PHSensorProfile("ph", "EZO pH")],
        analog_sensors=[
            AnalogSensorProfile(f"analog_sensor_{port}", f"Sensor {port} Analog", port)
            for port in (3, 6, 9)
        ],
        temperature_sensors=temperature_sensors,
        water_level_sensors=water_level_sensors,
        steppers=[
            StepperProfile("dose1", "Stepper Dose 1", "dose1"),
            StepperProfile("dose2", "Stepper Dose 2", "dose2"),
            StepperProfile("dose3", "Stepper Dose 3", "dose3"),
            StepperProfile("dose4", "Stepper Dose 4", "dose4"),
        ],
        ato=[
            ATOProfile("main_ato", "Main ATO", "level_sensor_1", "level_sensor_4", "mcp_relay", "ato_pump"),
        ],
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
    config = from_dict(AppConfig, raw)
    apply_config_migrations(config)
    validate_config(config)
    return config


def save_config(path: Path, config: AppConfig) -> None:
    apply_config_migrations(config)
    validate_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(asdict(config), handle, indent=2, sort_keys=True)
        handle.write("\n")


def apply_config_migrations(config: AppConfig) -> None:
    for sensor in config.temperature_sensors:
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
    water_ids = {item.id for item in config.water_level_sensors}
    stepper_ids = {item.id for item in config.steppers}
    if len(equipment_ids) != len(config.equipment):
        raise DiagnosticHalt("Equipment profile IDs must be unique.")
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
    for item in config.analog_sensors:
        require_sense_port(item.sense_port)
    for item in config.temperature_sensors:
        if item.sense_port is not None:
            require_sense_port(item.sense_port)
        if item.assigned_equipment and item.assigned_equipment not in equipment_ids:
            raise DiagnosticHalt(f"Temperature sensor {item.id} references unknown equipment {item.assigned_equipment}.")
    for item in config.water_level_sensors:
        require_sense_port(item.sense_port)
    for item in config.steppers:
        require_stepper(item.assignment)
        if item.hold_current_ma < 0:
            raise DiagnosticHalt("Stepper holding current cannot be negative.")
    for item in config.dosing:
        if item.stepper and item.actuator not in stepper_ids:
            raise DiagnosticHalt(f"Dosing profile {item.id} references unknown stepper {item.actuator}.")
        if not item.stepper and item.actuator not in equipment_ids:
            raise DiagnosticHalt(f"Dosing profile {item.id} references unknown equipment {item.actuator}.")
    for item in config.ato:
        if item.primary_level_sensor not in water_ids:
            raise DiagnosticHalt(f"ATO {item.id} references unknown primary sensor {item.primary_level_sensor}.")
        if item.backup_failsafe_sensor not in water_ids:
            raise DiagnosticHalt(f"ATO {item.id} references unknown backup sensor {item.backup_failsafe_sensor}.")
        if item.assigned_actuator not in equipment_ids and item.assigned_actuator not in stepper_ids:
            raise DiagnosticHalt(f"ATO {item.id} references unknown actuator {item.assigned_actuator}.")
