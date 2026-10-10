from __future__ import annotations

import datetime as dt

from loggerhead.config import ATOProfile, DosingProfile, TemperatureSensorProfile
from loggerhead.controllers import ATOController, DosingScheduler, ThermalController
from loggerhead.hardware import EquipmentKind, LevelState, TemperatureDriver
from loggerhead.state import RuntimeState


def test_heater_turns_on_below_hysteresis_and_off_at_target() -> None:
    sensor = TemperatureSensorProfile("t", "Temp", TemperatureDriver.ONE_WIRE_BUS, equipment_type=EquipmentKind.HEATER)
    assert ThermalController.evaluate(sensor, 77.0, False) is True
    assert ThermalController.evaluate(sensor, 78.0, True) is False


def test_chiller_turns_on_above_hysteresis_and_off_at_target() -> None:
    sensor = TemperatureSensorProfile("t", "Temp", TemperatureDriver.ONE_WIRE_BUS, equipment_type=EquipmentKind.CHILLER)
    assert ThermalController.evaluate(sensor, 79.0, False) is True
    assert ThermalController.evaluate(sensor, 78.0, True) is False


def test_dosing_scheduler_divides_daily_volume() -> None:
    profile = DosingProfile("alk", "Alk", "dose1", daily_volume_ml=24, calibration_ml_per_minute=12, doses_per_day=12)
    action = DosingScheduler.next_action(profile, dt.datetime(2026, 10, 6, 8, 0), None)
    assert action.due is True
    assert action.volume_ml == 2
    assert action.run_seconds == 10


def test_ato_locks_out_after_max_runtime() -> None:
    profile = ATOProfile("ato", "ATO", "primary", "backup", "mcp_relay", "pump", max_run_time=10)
    state = RuntimeState()
    state.water_levels["primary"] = LevelState.DRY
    state.water_levels["backup"] = LevelState.DRY
    should_run, ato_state, alarm = ATOController.evaluate(profile, state, now=100)
    assert should_run is True
    ato_state.started_at = 50
    should_run, ato_state, alarm = ATOController.evaluate(profile, state, now=100)
    assert should_run is False
    assert ato_state.faulted is True
    assert alarm is not None


def test_ato_fails_safe_when_any_level_sensor_unknown() -> None:
    profile = ATOProfile("ato", "ATO", "primary", "backup", "mcp_relay", "pump", max_run_time=10)
    state = RuntimeState()
    state.water_levels["primary"] = LevelState.DRY
    state.water_levels["backup"] = LevelState.UNKNOWN

    should_run, ato_state, alarm = ATOController.evaluate(profile, state, now=100)

    assert should_run is False
    assert ato_state.running is False
    assert alarm is None
