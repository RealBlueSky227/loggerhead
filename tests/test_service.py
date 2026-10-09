from __future__ import annotations

import threading
import time

import pytest

from loggerhead.drivers import HardwareFault, StepperRunResult
from loggerhead.hardware import STEPPERS, AlarmPriority, DiagnosticHalt
from loggerhead.service import LoggerheadService
from loggerhead.state import AlarmState


def make_service(tmp_path) -> LoggerheadService:
    return LoggerheadService(tmp_path / "config" / "loggerhead.json", tmp_path / "data", simulation=True)


def wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_manual_priming_starts_all_pumps_with_configured_speed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    calls: list[tuple[str, int, float, int, bool]] = []

    def fake_run_continuous(assignment, *, stop_event: threading.Event, steps_per_second: int, **kwargs) -> StepperRunResult:
        calls.append(
            (
                assignment.name,
                steps_per_second,
                kwargs["max_seconds"],
                kwargs["max_steps"],
                kwargs["direction_high"],
            )
        )
        stop_event.wait(0.2)
        return StepperRunResult(0, "stopped")

    monkeypatch.setattr(service.stepper_engine, "run_continuous", fake_run_continuous)
    for index, profile in enumerate(service.config.steppers, start=1):
        expected = (
            profile.assignment,
            100 + index,
            profile.manual_max_seconds,
            profile.manual_max_steps,
            profile.direction_high,
        )
        profile.manual_speed_steps_per_second = 100 + index
        result = service.set_manual_priming(profile.id, True)
        assert result["speed_steps_per_second"] == 100 + index
        wait_for(lambda expected=expected: bool(calls) and calls[-1] == expected)
        service.set_manual_priming(profile.id, False)
        thread = service._prime_threads.get(profile.id)
        if thread:
            thread.join(timeout=1.0)
        assert service.state.manual_priming[profile.id] is False
        assert service.state.stepper_active is None
    assert [name for name, _speed, _max_seconds, _max_steps, _direction in calls] == list(STEPPERS)


def test_manual_priming_prime_stop_prime_again(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    started = threading.Event()

    def fake_run_continuous(_assignment, *, stop_event: threading.Event, **_kwargs) -> StepperRunResult:
        started.set()
        stop_event.wait(0.2)
        return StepperRunResult(0, "stopped")

    monkeypatch.setattr(service.stepper_engine, "run_continuous", fake_run_continuous)
    service.set_manual_priming("dose1", True)
    assert started.wait(1.0)
    service.set_manual_priming("dose1", False)
    wait_for(lambda: service.state.manual_priming["dose1"] is False and "dose1" not in service._prime_threads)
    started.clear()
    service.set_manual_priming("dose1", True)
    assert started.wait(1.0)
    service.set_manual_priming("dose1", False)
    wait_for(lambda: service.state.manual_priming["dose1"] is False)


def test_manual_priming_rejects_invalid_speed_before_worker(tmp_path) -> None:
    service = make_service(tmp_path)
    service.config.steppers[0].manual_speed_steps_per_second = 0
    with pytest.raises(DiagnosticHalt):
        service.set_manual_priming("dose1", True)
    assert service._prime_threads == {}
    assert all(value is False for value in service.state.manual_priming.values())


def test_manual_priming_rejects_competing_pumps(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    started = threading.Event()

    def fake_run_continuous(_assignment, *, stop_event: threading.Event, **_kwargs) -> StepperRunResult:
        started.set()
        stop_event.wait(0.2)
        return StepperRunResult(0, "stopped")

    monkeypatch.setattr(service.stepper_engine, "run_continuous", fake_run_continuous)
    service.set_manual_priming("dose1", True)
    assert started.wait(1.0)
    with pytest.raises(RuntimeError, match="already active"):
        service.set_manual_priming("dose2", True)
    service.set_manual_priming("dose1", False)


def test_manual_priming_worker_error_clears_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)

    def fail_run_continuous(_assignment, **_kwargs) -> StepperRunResult:
        raise HardwareFault("move failed")

    monkeypatch.setattr(service.stepper_engine, "run_continuous", fail_run_continuous)
    service.set_manual_priming("dose1", True)
    wait_for(lambda: service.state.manual_priming["dose1"] is False)
    assert service.state.stepper_active is None
    alarm = service.state.alarms["stepper:dose1"]
    assert alarm.active is True
    assert alarm.priority == AlarmPriority.HIGH
    assert "manual priming fault" in alarm.message


def test_manual_priming_limit_alarm_clears_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)

    def limit_run_continuous(_assignment, **_kwargs) -> StepperRunResult:
        return StepperRunResult(12, "max_steps")

    monkeypatch.setattr(service.stepper_engine, "run_continuous", limit_run_continuous)
    service.set_manual_priming("dose1", True)

    wait_for(lambda: service.state.manual_priming["dose1"] is False)
    alarm = service.state.alarms["stepper:dose1:prime-limit"]
    assert alarm.active is True
    assert alarm.priority == AlarmPriority.HIGH
    assert "12 steps" in alarm.message


def test_shutdown_blocks_new_priming_and_clears_active_worker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    started = threading.Event()

    def fake_run_continuous(_assignment, *, stop_event: threading.Event, **_kwargs) -> StepperRunResult:
        started.set()
        stop_event.wait(0.5)
        return StepperRunResult(0, "stopped")

    monkeypatch.setattr(service.stepper_engine, "run_continuous", fake_run_continuous)
    service.set_manual_priming("dose1", True)
    assert started.wait(1.0)
    service.stop()
    assert all(value is False for value in service.state.manual_priming.values())
    with pytest.raises(RuntimeError, match="stopping"):
        service.set_manual_priming("dose1", True)


def test_startup_does_not_resume_persisted_manual_priming(tmp_path) -> None:
    config_path = tmp_path / "config" / "loggerhead.json"
    data_dir = tmp_path / "data"
    service = LoggerheadService(config_path, data_dir, simulation=True)
    service.state.manual_priming["dose1"] = True
    service.state.stepper_active = "dose1"
    service.state_store.save(service.state)

    restarted = LoggerheadService(config_path, data_dir, simulation=True)
    assert restarted.state.stepper_active is None
    assert all(value is False for value in restarted.state.manual_priming.values())
    assert restarted._prime_threads == {}


def test_restart_suppresses_audible_buzzer_for_persisted_alarm(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config" / "loggerhead.json"
    data_dir = tmp_path / "data"
    service = LoggerheadService(config_path, data_dir, simulation=True)
    service.state.alarms["water:sump"] = AlarmState("water:sump", "Sump level high.", AlarmPriority.HIGH, active=True)
    service.state_store.save(service.state)

    restarted = LoggerheadService(config_path, data_dir, simulation=True)
    sound_calls: list[AlarmPriority] = []
    monkeypatch.setattr(restarted.buzzer, "sound", lambda priority=AlarmPriority.HIGH: sound_calls.append(priority))

    restarted._sound_buzzer_if_needed(time.time())

    assert sound_calls == []
    assert "water:sump" in restarted._restart_suppressed_alarm_ids


def test_new_high_alarm_after_restart_can_still_sound(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    sound_calls: list[AlarmPriority] = []
    monkeypatch.setattr(service.buzzer, "sound", lambda priority=AlarmPriority.HIGH: sound_calls.append(priority))

    service.state.alarms["stepper:dose1"] = AlarmState("stepper:dose1", "Dose fault.", AlarmPriority.HIGH, active=True)
    service._sound_buzzer_if_needed(time.time())

    assert sound_calls == [AlarmPriority.HIGH]

