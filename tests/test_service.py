from __future__ import annotations

import threading
import time
from dataclasses import asdict

import pytest

from loggerhead.config import ATOProfile, OneWireMode, SensePortDevice
from loggerhead.drivers import HardwareFault, HardwareUnavailable, StepperRunResult
from loggerhead.hardware import STEPPERS, AlarmPriority, DiagnosticHalt, EquipmentKind, LevelState
from loggerhead.sensor_workers import SensorWorkerSnapshot
from loggerhead.service import LoggerheadService
from loggerhead.state import AlarmState, EquipmentState


def make_service(tmp_path) -> LoggerheadService:
    return LoggerheadService(tmp_path / "config" / "loggerhead.json", tmp_path / "data", simulation=True)


def wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_sensor_polling_isolates_failures_and_recovers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    port = service.config.sense_ports[0]
    port.device = SensePortDevice.DS18B20
    port.one_wire_mode = OneWireMode.BIT_BANG
    port.check_frequency = 1.0
    analog = service.config.sense_ports[1]
    analog.device = SensePortDevice.ANALOG
    analog.check_frequency = 1.0
    reads = {"temperature": 0, "analog": 0}

    def fail_then_recover(_bcm_pin: int) -> float:
        reads["temperature"] += 1
        if reads["temperature"] == 1:
            raise RuntimeError("probe failed")
        return 79.2

    monkeypatch.setattr(service.temperature_reader, "read_bit_banged", fail_then_recover)

    def read_voltage(_address: int, _channel: int) -> float:
        reads["analog"] += 1
        return 1.23

    monkeypatch.setattr(service.analog_reader, "read_voltage", read_voltage)

    service._poll_temperature(100.0)
    service._poll_analog(100.0)

    assert service.state.sensor_health["sense_port_1_temperature"].status == "read_error"
    assert service.state.sensor_health["sense_port_2_analog"].status == "online"
    assert service.state.readings["sense_port_2_analog"].value == 1.23

    service._poll_temperature(101.1)

    assert service.state.sensor_health["sense_port_1_temperature"].status == "online"
    assert service.state.readings["sense_port_1_temperature"].value == 79.2


def test_sensor_check_frequency_is_enforced(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    port = service.config.sense_ports[0]
    port.device = SensePortDevice.DS18B20
    port.one_wire_mode = OneWireMode.BIT_BANG
    port.check_frequency = 30.0
    calls = 0

    def read_temperature(_bcm_pin: int) -> float:
        nonlocal calls
        calls += 1
        return 78.0 + calls

    monkeypatch.setattr(service.temperature_reader, "read_bit_banged", read_temperature)

    service._poll_temperature(100.0)
    service._poll_temperature(101.0)
    service._poll_temperature(130.1)

    assert calls == 2
    assert service.state.readings["sense_port_1_temperature"].value == 80.0


def test_temperature_sensor_failure_turns_dependent_heater_off(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    port = service.config.sense_ports[0]
    port.device = SensePortDevice.DS18B20
    port.one_wire_mode = OneWireMode.BIT_BANG
    port.assigned_equipment = "ac1"
    port.equipment_type = EquipmentKind.HEATER
    service.state.equipment["ac1"] = EquipmentState("ac1", True)
    monkeypatch.setattr(service.temperature_reader, "read_bit_banged", lambda _pin: (_ for _ in ()).throw(RuntimeError("lost bus")))

    service._poll_temperature(100.0)

    assert service.state.equipment["ac1"].on is False
    assert service.state.sensor_health["sense_port_1_temperature"].status == "read_error"


def test_water_sensor_failure_sets_unknown_for_ato_failsafe(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    port = service.config.sense_ports[0]
    port.device = SensePortDevice.BINARY
    service.state.water_levels["sense_port_1_water"] = LevelState.DRY
    monkeypatch.setattr("loggerhead.service.BinaryLevelSensor.read_state", lambda _self: (_ for _ in ()).throw(RuntimeError("gpio failed")))

    service._poll_water_levels(100.0)

    assert service.state.water_levels["sense_port_1_water"] == LevelState.UNKNOWN
    assert service.state.sensor_health["sense_port_1_water"].status == "read_error"


def test_slow_sensor_worker_does_not_delay_other_sensors_or_control_loop(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    temp = service.config.sense_ports[0]
    temp.device = SensePortDevice.DS18B20
    temp.one_wire_mode = OneWireMode.BIT_BANG
    temp.check_frequency = 0.05
    analog = service.config.sense_ports[1]
    analog.device = SensePortDevice.ANALOG
    analog.check_frequency = 0.05
    service._rebuild_sensor_runtime()
    slow_started = threading.Event()
    slow_release = threading.Event()
    analog_reads = 0

    def slow_temperature(_bcm_pin: int) -> float:
        slow_started.set()
        slow_release.wait(1.0)
        return 78.0

    def read_voltage(_address: int, _channel: int) -> float:
        nonlocal analog_reads
        analog_reads += 1
        return 1.5

    monkeypatch.setattr(service.temperature_reader, "read_bit_banged", slow_temperature)
    monkeypatch.setattr(service.analog_reader, "read_voltage", read_voltage)
    monkeypatch.setattr(service, "_publish_telemetry", lambda: None)
    monkeypatch.setattr(service, "_save_state", lambda: None)
    monkeypatch.setattr(service.store, "prune_if_critical", lambda: False)
    monkeypatch.setattr(service.health, "snapshot", lambda: {})
    service._start_sensor_workers()
    try:
        assert slow_started.wait(1.0)
        wait_for(lambda: analog_reads >= 2, timeout=1.0)
        started = time.monotonic()
        service._control_loop_iteration(time.time())
        assert time.monotonic() - started < 0.5
        assert service.state.sensor_health["sense_port_2_analog"].status == "online"
    finally:
        slow_release.set()
        service._stop_sensor_workers()


def test_sensor_workers_honor_independent_check_frequencies(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    fast = service.config.sense_ports[0]
    fast.device = SensePortDevice.ANALOG
    fast.check_frequency = 0.05
    slow = service.config.sense_ports[1]
    slow.device = SensePortDevice.ANALOG
    slow.check_frequency = 0.2
    service._rebuild_sensor_runtime()
    calls = {0: 0, 1: 0}

    def read_voltage(_address: int, channel: int) -> float:
        calls[channel] += 1
        return float(channel + 1)

    monkeypatch.setattr(service.analog_reader, "read_voltage", read_voltage)
    service._start_sensor_workers()
    try:
        time.sleep(0.45)
    finally:
        service._stop_sensor_workers()

    assert calls[0] >= 5
    assert calls[1] <= 4
    assert calls[0] > calls[1]


def test_i2c_sensor_workers_serialize_shared_bus_access(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    for port in service.config.sense_ports[:3]:
        port.device = SensePortDevice.ANALOG
        port.check_frequency = 0.01
    service._rebuild_sensor_runtime()
    guard = threading.Lock()
    active = 0
    overlap_detected = False
    reads = 0

    def read_voltage(_address: int, channel: int) -> float:
        nonlocal active, overlap_detected, reads
        with guard:
            if active:
                overlap_detected = True
            active += 1
        time.sleep(0.02)
        with guard:
            active -= 1
            reads += 1
        return float(channel)

    monkeypatch.setattr(service.analog_reader, "read_voltage", read_voltage)
    service._start_sensor_workers()
    try:
        wait_for(lambda: reads >= 6, timeout=1.5)
    finally:
        service._stop_sensor_workers()

    assert overlap_detected is False


def test_worker_exception_does_not_stop_other_sensor_workers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    temp = service.config.sense_ports[0]
    temp.device = SensePortDevice.DS18B20
    temp.one_wire_mode = OneWireMode.BIT_BANG
    temp.check_frequency = 0.05
    analog = service.config.sense_ports[1]
    analog.device = SensePortDevice.ANALOG
    analog.check_frequency = 0.05
    service._rebuild_sensor_runtime()
    monkeypatch.setattr(service.temperature_reader, "read_bit_banged", lambda _pin: (_ for _ in ()).throw(RuntimeError("bad probe")))
    analog_reads = 0

    def read_voltage(_address: int, _channel: int) -> float:
        nonlocal analog_reads
        analog_reads += 1
        return 2.0

    monkeypatch.setattr(service.analog_reader, "read_voltage", read_voltage)
    service._start_sensor_workers()
    try:
        wait_for(lambda: analog_reads >= 2, timeout=1.0)
        service._control_loop_iteration(time.time())
    finally:
        service._stop_sensor_workers()

    assert service.state.sensor_health["sense_port_1_temperature"].status == "read_error"
    assert service.state.sensor_health["sense_port_2_analog"].status == "online"


def test_kernel_one_wire_worker_recovers_after_setup_failure(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    port = service.config.sense_ports[0]
    port.device = SensePortDevice.DS18B20
    port.one_wire_mode = OneWireMode.KERNEL
    port.sensor_id = "28-000000000001"
    port.check_frequency = 0.05
    service._rebuild_sensor_runtime()
    attempts = 0

    def configure(_bcm_pin: int, _sensor_id: str = "") -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HardwareUnavailable("sudo -n dtoverlay failed")

    monkeypatch.setattr(service.temperature_reader, "configure_kernel_one_wire", configure)
    monkeypatch.setattr(service.temperature_reader, "read_one_wire_bus", lambda _sensor_id, **_kwargs: 78.6)
    service._start_sensor_workers()
    try:
        wait_for(lambda: attempts >= 2, timeout=1.0)
        wait_for(lambda: service._sensor_worker_snapshots()["sense_port_1_temperature"].status == "online", timeout=1.0)
        service._control_loop_iteration(time.time())
    finally:
        service._stop_sensor_workers()

    assert service.state.sensor_health["sense_port_1_temperature"].status == "online"
    assert service.state.readings["sense_port_1_temperature"].value == 78.6


@pytest.mark.parametrize(
    ("status", "value"),
    [("read_error", LevelState.UNKNOWN.value), ("stale", LevelState.INACTIVE.value)],
)
def test_invalid_hydros_worker_snapshot_prevents_ato(tmp_path, status: str, value: str) -> None:
    service = make_service(tmp_path)
    primary = service.config.sense_ports[0]
    primary.device = SensePortDevice.HYDROS_TRIPLE
    backup = service.config.sense_ports[1]
    backup.device = SensePortDevice.HYDROS_TRIPLE
    service.config.ato.append(
        ATOProfile(
            "ato",
            "ATO",
            "sense_port_1_water",
            "sense_port_2_water",
            "mcp_relay",
            "ac1",
        )
    )
    service.state.equipment["ac1"] = EquipmentState("ac1", True)
    snapshots = {
        "sense_port_1_water": SensorWorkerSnapshot(
            sensor_id="sense_port_1_water",
            kind="water",
            worker_state="running",
            status=status,
            data_status=status,
            value=value,
            error="HYDROS invalid",
            last_attempt_ts=time.time(),
        ),
        "sense_port_2_water": SensorWorkerSnapshot(
            sensor_id="sense_port_2_water",
            kind="water",
            worker_state="running",
            status="online",
            data_status="online",
            value=LevelState.DRY.value,
            last_attempt_ts=time.time(),
            last_success_ts=time.time(),
        ),
    }
    commands: list[tuple[str, bool, str]] = []
    alarms: list[object] = []
    notifications: list[object] = []

    service._apply_sensor_worker_snapshots_locked(
        snapshots,
        time.time(),
        equipment_commands=commands,
        value_logs=[],
        events=[],
        alarms=[],
    )
    service._plan_ato_locked(time.time(), equipment_commands=commands, alarms=alarms, notifications=notifications)

    assert service.state.water_levels["sense_port_1_water"] == LevelState.UNKNOWN
    assert ("ac1", False, "ato") in commands


def test_stale_worker_snapshot_forces_heater_off(tmp_path) -> None:
    service = make_service(tmp_path)
    port = service.config.sense_ports[0]
    port.device = SensePortDevice.DS18B20
    port.assigned_equipment = "ac1"
    port.equipment_type = EquipmentKind.HEATER
    service.state.equipment["ac1"] = EquipmentState("ac1", True)
    snapshot = SensorWorkerSnapshot(
        sensor_id="sense_port_1_temperature",
        kind="temperature",
        worker_state="reading",
        status="stale",
        data_status="stale",
        error="Read in progress for 9.0s.",
        stale_after_seconds=5.0,
    )

    service._apply_sensor_worker_snapshots_locked(
        {"sense_port_1_temperature": snapshot},
        time.time(),
        equipment_commands=[],
        value_logs=[],
        events=[],
        alarms=[],
    )

    commands: list[tuple[str, bool, str]] = []
    service._apply_sensor_worker_snapshots_locked(
        {"sense_port_1_temperature": snapshot},
        time.time(),
        equipment_commands=commands,
        value_logs=[],
        events=[],
        alarms=[],
    )

    assert ("ac1", False, "sensor_stale") in commands


def test_hydros_callback_cleanup_on_repeated_config_edits(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed = 0

    class FakeHydrosReader:
        def __init__(self, *_args, **_kwargs) -> None:
            return

        def close(self) -> None:
            nonlocal closed
            closed += 1

        def read_state(self) -> LevelState:
            return LevelState.NORMAL

        def diagnostics(self) -> dict[str, object]:
            return {"pulse_count": 1}

    monkeypatch.setattr("loggerhead.service.HydrosPulseReader", FakeHydrosReader)
    service = make_service(tmp_path)
    port = service.config.sense_ports[0]
    for _ in range(3):
        port.device = SensePortDevice.HYDROS_TRIPLE
        service._rebuild_sensor_runtime()
        assert "sense_port_1_water" in service._hydros_readers
        port.device = SensePortDevice.BINARY
        service._rebuild_sensor_runtime()

    assert closed == 3


def test_sensor_worker_start_stop_releases_threads(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    port = service.config.sense_ports[0]
    port.device = SensePortDevice.ANALOG
    port.check_frequency = 0.05
    service._rebuild_sensor_runtime()
    monkeypatch.setattr(service.analog_reader, "read_voltage", lambda _address, _channel: 1.0)
    service._start_sensor_workers()
    wait_for(lambda: service._sensor_worker_snapshots()["sense_port_1_analog"].thread_alive, timeout=1.0)
    service._stop_sensor_workers()

    snapshot = service._sensor_worker_snapshots()["sense_port_1_analog"]
    assert snapshot.thread_alive is False
    assert snapshot.worker_state == "stopped"


def test_invalid_config_update_does_not_replace_active_config_or_file(tmp_path) -> None:
    service = make_service(tmp_path)
    before_file = service.config_path.read_text(encoding="utf-8")
    before_frequency = service.config.sense_ports[0].check_frequency
    payload = asdict(service.config)
    payload["sense_ports"][0]["check_frequency"] = 0

    with pytest.raises(DiagnosticHalt):
        service.update_config(payload)

    assert service.config_path.read_text(encoding="utf-8") == before_file
    assert service.config.sense_ports[0].check_frequency == before_frequency


def test_invalid_sense_port_edit_does_not_mutate_active_config(tmp_path) -> None:
    service = make_service(tmp_path)
    before_frequency = service.config.sense_ports[0].check_frequency

    with pytest.raises(DiagnosticHalt):
        service.set_sense_port(1, {"check_frequency": 0})

    assert service.config.sense_ports[0].check_frequency == before_frequency


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


def test_manual_priming_limit_chirps_without_latching_alarm(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    service.MANUAL_PRIME_LIMIT_CHIRP_SECONDS = 0.001
    sound_calls: list[AlarmPriority] = []
    stop_calls = 0
    run_count = 0

    def limit_run_continuous(_assignment, **_kwargs) -> StepperRunResult:
        nonlocal run_count
        run_count += 1
        return StepperRunResult(12, "max_seconds")

    monkeypatch.setattr(service.stepper_engine, "run_continuous", limit_run_continuous)
    monkeypatch.setattr(service.buzzer, "sound", lambda priority=AlarmPriority.HIGH: sound_calls.append(priority))

    def stop_buzzer() -> None:
        nonlocal stop_calls
        stop_calls += 1

    monkeypatch.setattr(service.buzzer, "stop", stop_buzzer)
    service.set_manual_priming("dose1", True)

    wait_for(lambda: service.state.manual_priming["dose1"] is False and "dose1" not in service._prime_threads)
    wait_for(lambda: sound_calls == [AlarmPriority.INFO])
    wait_for(lambda: stop_calls >= 1)
    assert "stepper:dose1:prime-limit" not in service.state.alarms
    assert service._active_audible_alarm_ids() == set()
    assert any("time limit" in event["message"] and "12 steps" in event["message"] for event in service.store.recent_events())

    service.set_manual_priming("dose1", True)
    wait_for(lambda: service.state.manual_priming["dose1"] is False and run_count == 2)


def test_startup_retires_persisted_manual_prime_limit_alarm(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config" / "loggerhead.json"
    data_dir = tmp_path / "data"
    service = LoggerheadService(config_path, data_dir, simulation=True)
    service.state.alarms["stepper:dose1:prime-limit"] = AlarmState(
        "stepper:dose1:prime-limit",
        "Dose 1 manual priming stopped at 12 steps due to max_seconds.",
        AlarmPriority.HIGH,
        active=True,
    )
    service.state_store.save(service.state)

    restarted = LoggerheadService(config_path, data_dir, simulation=True)
    sound_calls: list[AlarmPriority] = []
    monkeypatch.setattr(restarted.buzzer, "sound", lambda priority=AlarmPriority.HIGH: sound_calls.append(priority))

    restarted._sound_buzzer_if_needed(time.time())

    assert restarted.state.alarms["stepper:dose1:prime-limit"].active is False
    assert restarted._active_audible_alarm_ids() == set()
    assert sound_calls == []


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

