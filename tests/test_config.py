from __future__ import annotations

import json

import pytest

from loggerhead.config import (
    MAX_MANUAL_PRIME_STEPS_PER_SECOND,
    EquipmentProfile,
    SensePortDevice,
    TemperatureSensorProfile,
    WaterLevelSensorProfile,
    default_config,
    load_config,
    materialized_temperature_sensors,
    save_config,
    validate_config,
)
from loggerhead.hardware import DiagnosticHalt, EquipmentDriver, TemperatureDriver, WaterLevelDriver


def test_default_config_is_valid() -> None:
    validate_config(default_config())


def test_config_round_trips_enums(tmp_path) -> None:
    path = tmp_path / "loggerhead.json"
    save_config(path, default_config())
    loaded = load_config(path)
    assert loaded.equipment[0].driver == EquipmentDriver.MCP23017_RELAY


def test_rejects_unmapped_relay() -> None:
    config = default_config()
    config.equipment.append(EquipmentProfile("bad", "Bad Relay", EquipmentDriver.MCP23017_RELAY, "AC99"))
    with pytest.raises(DiagnosticHalt):
        validate_config(config)


def test_rejects_kasa_outlet_outside_hs300_range() -> None:
    config = default_config()
    config.equipment.append(
        EquipmentProfile("bad", "Bad Outlet", EquipmentDriver.KASA_HS300, "6", kasa_host="192.0.2.10")
    )
    with pytest.raises(DiagnosticHalt):
        validate_config(config)


def test_migrates_legacy_dosing_manual_prime_speed_to_stepper(tmp_path) -> None:
    path = tmp_path / "loggerhead.json"
    config = default_config()
    raw = json.loads(json.dumps(config, default=lambda value: value.value if hasattr(value, "value") else value.__dict__))
    raw["dosing"] = [
        {
            "id": "dose1_schedule",
            "name": "Dose 1 Schedule",
            "actuator": "dose1",
            "stepper": True,
            "manual_speed_steps_per_second": 777,
        }
    ]
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_config(path)
    assert next(item for item in loaded.steppers if item.id == "dose1").manual_speed_steps_per_second == 777


def test_rejects_invalid_stepper_manual_prime_speed() -> None:
    config = default_config()
    config.steppers[0].manual_speed_steps_per_second = MAX_MANUAL_PRIME_STEPS_PER_SECOND + 1
    with pytest.raises(DiagnosticHalt):
        validate_config(config)


def test_stepper_safety_fields_keep_backward_compatible_defaults(tmp_path) -> None:
    path = tmp_path / "loggerhead.json"
    config = default_config()
    raw = json.loads(json.dumps(config, default=lambda value: value.value if hasattr(value, "value") else value.__dict__))
    for stepper in raw["steppers"]:
        stepper.pop("manual_max_seconds", None)
        stepper.pop("manual_max_steps", None)
        stepper.pop("direction_high", None)
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = load_config(path)

    assert loaded.steppers[0].manual_max_seconds == 60.0
    assert loaded.steppers[0].manual_max_steps == 300_000
    assert loaded.steppers[0].direction_high is True


def test_migrates_legacy_ds18b20_sense_ports_away_from_rom_ids(tmp_path) -> None:
    path = tmp_path / "loggerhead.json"
    config = default_config()
    config.sense_ports[0].device = SensePortDevice.DS18B20
    config.sense_ports[0].sensor_id = "28-000000000001"
    config.temperature_sensors.append(
        TemperatureSensorProfile(
            "legacy_kernel",
            "Legacy Kernel DS18B20",
            TemperatureDriver.ONE_WIRE_BUS,
            sensor_id="28-000000000002",
            sense_port=2,
        )
    )
    save_config(path, config)

    loaded = load_config(path)
    materialized = [sensor for sensor in materialized_temperature_sensors(loaded) if sensor.sense_port == 1]
    legacy_kernel = next(sensor for sensor in loaded.temperature_sensors if sensor.id == "legacy_kernel")

    assert loaded.sense_ports[0].sensor_id == ""
    assert legacy_kernel.sensor_id == ""
    assert len(materialized) == 1
    assert materialized[0].sensor_id == ""


def test_rejects_ds18b20_temperature_without_fixed_sense_port() -> None:
    config = default_config()
    config.temperature_sensors.append(TemperatureSensorProfile("tank", "Tank", TemperatureDriver.ONE_WIRE_BUS))

    with pytest.raises(DiagnosticHalt, match="fixed sense port GPIO"):
        validate_config(config)


def test_rejects_ds18b20_rom_ids_without_migration() -> None:
    config = default_config()
    config.temperature_sensors.append(
        TemperatureSensorProfile(
            "tank",
            "Tank",
            TemperatureDriver.ONE_WIRE_BUS,
            sensor_id="28-000000000001",
            sense_port=1,
        )
    )

    with pytest.raises(DiagnosticHalt, match="must not configure a DS18B20 ROM ID"):
        validate_config(config)


def test_rejects_invalid_stepper_microsteps() -> None:
    config = default_config()
    config.steppers[0].microsteps = 3
    with pytest.raises(DiagnosticHalt):
        validate_config(config)


def test_rejects_duplicate_stepper_assignments() -> None:
    config = default_config()
    config.steppers[1].assignment = config.steppers[0].assignment
    with pytest.raises(DiagnosticHalt):
        validate_config(config)


def test_rejects_invalid_sense_port_polling_and_thresholds() -> None:
    config = default_config()
    config.sense_ports[0].device = SensePortDevice.DS18B20
    config.sense_ports[0].check_frequency = 0
    with pytest.raises(DiagnosticHalt, match="check frequency"):
        validate_config(config)

    config = default_config()
    config.sense_ports[0].device = SensePortDevice.DS18B20
    config.sense_ports[0].alert_below = 90
    with pytest.raises(DiagnosticHalt, match="threshold"):
        validate_config(config)


def test_rejects_conflicting_digital_sensor_backends_on_same_sense_port() -> None:
    config = default_config()
    config.sense_ports[0].device = SensePortDevice.DS18B20
    config.water_level_sensors.append(
        WaterLevelSensorProfile("backup_level", "Backup Level", WaterLevelDriver.BINARY, sense_port=1)
    )

    with pytest.raises(DiagnosticHalt, match="shared by multiple sensor backends"):
        validate_config(config)
