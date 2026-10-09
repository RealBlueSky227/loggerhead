from __future__ import annotations

import threading
import time

import pytest

from loggerhead.drivers import HardwareFault
from loggerhead.hardware import STEPPERS, AlarmPriority, DiagnosticHalt
from loggerhead.service import LoggerheadService


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
    calls: list[tuple[str, int, int]] = []

    def fake_move(assignment, *, steps: int, steps_per_second: int, **_kwargs) -> None:
        service.stepper_engine._stop_event.clear()
        calls.append((assignment.name, steps, steps_per_second))
        service.stepper_engine._stop_event.wait(0.2)

    monkeypatch.setattr(service.stepper_engine, "move", fake_move)
    for index, profile in enumerate(service.config.steppers, start=1):
        expected = (profile.assignment, 100 + index, 100 + index)
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
    assert [name for name, _steps, _speed in calls] == list(STEPPERS)


def test_manual_priming_prime_stop_prime_again(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    started = threading.Event()

    def fake_move(_assignment, **_kwargs) -> None:
        service.stepper_engine._stop_event.clear()
        started.set()
        service.stepper_engine._stop_event.wait(0.2)

    monkeypatch.setattr(service.stepper_engine, "move", fake_move)
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

    def fake_move(_assignment, **_kwargs) -> None:
        service.stepper_engine._stop_event.clear()
        started.set()
        service.stepper_engine._stop_event.wait(0.2)

    monkeypatch.setattr(service.stepper_engine, "move", fake_move)
    service.set_manual_priming("dose1", True)
    assert started.wait(1.0)
    with pytest.raises(RuntimeError, match="already active"):
        service.set_manual_priming("dose2", True)
    service.set_manual_priming("dose1", False)


def test_manual_priming_worker_error_clears_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)

    def fail_move(_assignment, **_kwargs) -> None:
        raise HardwareFault("move failed")

    monkeypatch.setattr(service.stepper_engine, "move", fail_move)
    service.set_manual_priming("dose1", True)
    wait_for(lambda: service.state.manual_priming["dose1"] is False)
    assert service.state.stepper_active is None
    alarm = service.state.alarms["stepper:dose1"]
    assert alarm.active is True
    assert alarm.priority == AlarmPriority.HIGH
    assert "manual priming fault" in alarm.message


def test_shutdown_blocks_new_priming_and_clears_active_worker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    started = threading.Event()

    def fake_move(_assignment, **_kwargs) -> None:
        service.stepper_engine._stop_event.clear()
        started.set()
        service.stepper_engine._stop_event.wait(0.5)

    monkeypatch.setattr(service.stepper_engine, "move", fake_move)
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

