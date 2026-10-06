from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass

from .config import ATOProfile, DosingProfile, EquipmentProfile, TemperatureSensorProfile, WaterLevelSensorProfile
from .hardware import AlarmPriority, EquipmentKind, LevelState
from .state import AlarmState, ATOState, RuntimeState


def desired_physical_state(profile: EquipmentProfile, software_on: bool) -> bool:
    """Map software state to physical relay intent.

    Implements SRS 3.1.3.1 and 3.1.3.2.
    """

    return not software_on if profile.normally_on else software_on


class ThermalController:
    """Automated heater/chiller/fan control.

    Implements SRS 3.2.1 Heater Logic and 3.2.2 Chiller/Fan Logic.
    """

    @staticmethod
    def evaluate(sensor: TemperatureSensorProfile, reading: float, currently_on: bool) -> bool:
        lower = sensor.target_temp - sensor.hysteresis
        upper = sensor.target_temp + sensor.hysteresis
        if sensor.equipment_type == EquipmentKind.HEATER:
            if reading < lower:
                return True
            if reading >= sensor.target_temp:
                return False
            return currently_on
        if sensor.equipment_type in {EquipmentKind.CHILLER, EquipmentKind.FAN}:
            if reading > upper:
                return True
            if reading <= sensor.target_temp:
                return False
            return currently_on
        return currently_on


@dataclass(frozen=True)
class DoseAction:
    due: bool
    volume_ml: float
    run_seconds: float


class DosingScheduler:
    """Daily dosing plan generator.

    Implements SRS 3.3.1 equal dosing distribution.
    """

    @staticmethod
    def next_action(profile: DosingProfile, now: dt.datetime, last_dose_ts: float | None) -> DoseAction:
        if profile.daily_volume_ml <= 0 or profile.doses_per_day <= 0:
            return DoseAction(False, 0.0, 0.0)
        start = _time_today(now, profile.window_start)
        end = _time_today(now, profile.window_end)
        if end <= start:
            end += dt.timedelta(days=1)
        if not start <= now <= end:
            return DoseAction(False, 0.0, 0.0)
        span = (end - start).total_seconds()
        interval = span / profile.doses_per_day
        slot = int((now - start).total_seconds() // interval)
        slot_time = start + dt.timedelta(seconds=slot * interval)
        if last_dose_ts and last_dose_ts >= slot_time.timestamp():
            return DoseAction(False, 0.0, 0.0)
        volume = profile.daily_volume_ml / profile.doses_per_day
        run_seconds = volume / max(profile.calibration_ml_per_minute, 0.001) * 60
        return DoseAction(True, volume, run_seconds)


class ATOController:
    """Auto top off state machine.

    Implements SRS 3.5.1 through 3.5.5.3.
    """

    @staticmethod
    def evaluate(profile: ATOProfile, state: RuntimeState, now: float | None = None) -> tuple[bool, ATOState, AlarmState | None]:
        now = now or time.time()
        ato_state = state.ato.setdefault(profile.id, ATOState(profile.id))
        if not profile.enabled or ato_state.faulted:
            return False, ato_state, None

        primary = state.water_levels.get(profile.primary_level_sensor, LevelState.UNKNOWN)
        backup = state.water_levels.get(profile.backup_failsafe_sensor, LevelState.UNKNOWN)
        backup_wet = backup in {LevelState.WET, LevelState.SUBMERGED, LevelState.NORMAL, LevelState.HIGH}

        if backup_wet:
            ato_state.running = False
            ato_state.started_at = None
            return False, ato_state, None

        should_run = primary == LevelState.DRY
        if should_run and not ato_state.running:
            ato_state.running = True
            ato_state.started_at = now
        elif not should_run:
            ato_state.running = False
            ato_state.started_at = None

        if ato_state.running and ato_state.started_at is not None and now - ato_state.started_at > profile.max_run_time:
            ato_state.running = False
            ato_state.faulted = True
            alarm = AlarmState(
                f"ato:{profile.id}",
                f"ATO {profile.name} exceeded max run time and was locked out.",
                AlarmPriority.HIGH,
                first_seen=now,
            )
            return False, ato_state, alarm
        return ato_state.running, ato_state, None

    @staticmethod
    def reset(profile: ATOProfile, state: RuntimeState) -> None:
        state.ato[profile.id] = ATOState(profile.id)


class AlertEvaluator:
    """Evaluates temperature and water-level alarms.

    Implements SRS 4.3, 4.4, 4.4.1, 4.6.2, and 4.7.
    """

    @staticmethod
    def temperature_alarm(sensor: TemperatureSensorProfile, value: float, now: float | None = None) -> AlarmState | None:
        now = now or time.time()
        if value >= sensor.emergency_above or value <= sensor.emergency_below:
            return AlarmState(f"temperature:{sensor.id}", f"{sensor.name} is at emergency temperature {value:.2f}.", AlarmPriority.CRITICAL, first_seen=now)
        if value >= sensor.alert_above:
            return AlarmState(f"temperature:{sensor.id}", f"{sensor.name} is too hot at {value:.2f}.", AlarmPriority.WARNING, first_seen=now)
        if value <= sensor.alert_below:
            return AlarmState(f"temperature:{sensor.id}", f"{sensor.name} is too cold at {value:.2f}.", AlarmPriority.WARNING, first_seen=now)
        return None

    @staticmethod
    def water_alarm(profile: WaterLevelSensorProfile, observed_since: float, now: float | None = None) -> AlarmState | None:
        now = now or time.time()
        if profile.current_state == profile.desired_state:
            return None
        if now - observed_since < profile.alert_wait:
            return None
        return AlarmState(
            f"water:{profile.id}",
            f"{profile.name} has remained {profile.current_state.value} instead of {profile.desired_state.value}.",
            AlarmPriority.HIGH,
            first_seen=observed_since,
        )


def _time_today(now: dt.datetime, value: str) -> dt.datetime:
    hour, minute = [int(part) for part in value.split(":", 1)]
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
