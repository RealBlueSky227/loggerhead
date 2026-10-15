from __future__ import annotations

import copy
import threading
import time
from dataclasses import asdict

import pytest

from loggerhead.config import ATOProfile, SensePortDevice
from loggerhead.drivers import HardwareFault, HardwareUnavailable, StepperRunResult
from loggerhead.hardware import STEPPERS, AlarmPriority, DiagnosticHalt, EquipmentKind, LevelState
from loggerhead.onewire import OneWireOverlayResult, OneWireTemperatureReading
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


def kernel_temperature_reading(value_f: float, bcm_pin: int = 4) -> OneWireTemperatureReading:
    return OneWireTemperatureReading(
        bcm_pin=bcm_pin,
        sensor_id="28-test",
        bus_master="simulation",
        celsius=(value_f - 32) * 5 / 9,
        fahrenheit=value_f,
        crc_valid=True,
    )


def test_status_excludes_catalog_and_reads_events_outside_state_lock(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)

    def unexpected_catalog():
        raise AssertionError("Status polling must not query history streams")

    def events(_limit):
        acquired = threading.Event()

        def check_lock():
            with service._state_lock:
                acquired.set()

        worker = threading.Thread(target=check_lock)
        worker.start()
        worker.join(timeout=1)
        assert acquired.is_set(), "Telemetry reads held the equipment state lock"
        return []

    monkeypatch.setattr(service, "history_streams", unexpected_catalog)
    monkeypatch.setattr(service.store, "recent_events", events)
    for _ in range(3):
        snapshot = service.status()
        assert "history_streams" not in snapshot
        assert snapshot["events"] == []


def test_slow_diagnostics_do_not_block_status_or_equipment_state(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def slow_uart(_node):
        entered.set()
        assert release.wait(2)
        return {"drv_status": 0}

    monkeypatch.setattr(service.uart, "diagnostics", slow_uart)
    worker = threading.Thread(target=service._refresh_stepper_diagnostics)
    worker.start()
    try:
        assert entered.wait(1)
        snapshot = service.status()
        assert snapshot["diagnostics"]["steppers"]["dose1"]["status"] == "initializing"
        with service._state_lock:
            service.state.equipment["ac1"] = EquipmentState("ac1", False)
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    snapshot = service.status()
    assert snapshot["diagnostics"]["steppers"]["dose1"]["drv_status"] == 0
    assert snapshot["diagnostics"]["steppers"]["dose1"]["sampled_at"] > 0


def test_disabled_mqtt_does_not_build_status_and_diagnostics_skip_active_motor(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)

    def unexpected(*_args):
        raise AssertionError("Unexpected status serialization or UART traffic")

    monkeypatch.setattr(service, "status", unexpected)
    service._publish_telemetry()
    monkeypatch.setattr(service.uart, "diagnostics", unexpected)
    service.state.stepper_active = "dose1"
    service._refresh_stepper_diagnostics()
    assert service._stepper_diagnostics == {}


def test_control_health_reads_and_telemetry_writes_release_state_lock(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    samples = []

    def assert_unlocked():
        acquired = threading.Event()

        def probe():
            with service._state_lock:
                acquired.set()

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join(1)
        assert acquired.is_set(), "I/O blocked the shared equipment state lock"

    def health_snapshot():
        assert_unlocked()
        return {"cpu_percent": 25}

    def log_samples(values):
        assert_unlocked()
        samples.extend(values)

    monkeypatch.setattr(service.health, "snapshot", health_snapshot)
    monkeypatch.setattr(service.store, "log_samples", log_samples)
    service._control_loop_iteration(100)
    assert ("health.cpu_percent", 25, "", 100) in samples


def test_sensor_type_change_retires_only_obsolete_alarms_and_live_readings(tmp_path) -> None:
    service = make_service(tmp_path)
    service.set_sense_port(1, {"device": "ds18b20"})
    alarms = ["sensor:sense_port_1_temperature", "temperature:sense_port_1_temperature",
              "sensor:worker-stop", "sensor:gpio:4:handoff", "stepper:dose1", "water:sense_port_2_water"]
    for alarm_id in alarms:
        service.state.alarms[alarm_id] = AlarmState(alarm_id, "test fault", AlarmPriority.HIGH)
    service.set_sense_port(2, {"device": "hydros_triple"})
    service._poll_temperature(time.time())
    service.set_sense_port(1, {"device": "analog"})
    for alarm_id in alarms[:3]:
        assert not service.state.alarms[alarm_id].active
    for alarm_id in alarms[3:]:
        assert service.state.alarms[alarm_id].active
    assert "sense_port_1_temperature" not in service.status()["readings"]
    assert "water:sense_port_2_water" in service._active_audible_alarm_ids()


def test_restart_reconciles_removed_sensor_alarms_without_losing_history(tmp_path) -> None:
    service = make_service(tmp_path)
    alarm_id = "temperature:sense_port_2_temperature"
    service.state.alarms[alarm_id] = AlarmState(alarm_id, "old temperature fault", AlarmPriority.CRITICAL)
    service.store.log_value("temperature.sense_port_2_temperature", 32, unit="F")
    service._save_state()
    restarted = make_service(tmp_path)
    assert not restarted.state.alarms[alarm_id].active
    assert alarm_id not in restarted._active_audible_alarm_ids()
    assert any(item["stream"] == "temperature.sense_port_2_temperature" for item in restarted.history_streams())


def test_worker_stop_alarm_remains_until_successful_rebuild(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    original = service._stop_sensor_workers
    monkeypatch.setattr(service, "_stop_sensor_workers", lambda: ["sense_port_1_temperature"])
    with pytest.raises(HardwareFault, match="workers did not stop"):
        service._rebuild_sensor_runtime()
    assert service.state.alarms["sensor:worker-stop"].active
    monkeypatch.setattr(service, "_stop_sensor_workers", original)
    service._rebuild_sensor_runtime()
    assert not service.state.alarms["sensor:worker-stop"].active


def test_water_level_alarm_resolves_when_desired_state_returns(tmp_path) -> None:
    service = make_service(tmp_path)
    service.set_sense_port(1, {"device": "hydros_triple"})
    sensor_id = "sense_port_1_water"
    alarm_id = f"water:{sensor_id}"
    service.state.alarms[alarm_id] = AlarmState(alarm_id, "dry", AlarmPriority.HIGH)
    snapshot = SensorWorkerSnapshot(sensor_id, "water", status="online", data_status="online",
                                    value="normal", last_attempt_ts=100, last_success_ts=100)
    service._apply_sensor_worker_snapshots_locked(
        {sensor_id: snapshot}, 100, equipment_commands=[], value_logs=[], events=[], alarms=[]
    )
    assert not service.state.alarms[alarm_id].active


def test_sensor_polling_isolates_failures_and_recovers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    port = service.config.sense_ports[0]
    port.device = SensePortDevice.DS18B20
    port.check_frequency = 1.0
    analog = service.config.sense_ports[1]
    analog.device = SensePortDevice.ANALOG
    analog.check_frequency = 1.0
    reads = {"temperature": 0, "analog": 0}

    def fail_then_recover(_bcm_pin: int, **_kwargs) -> OneWireTemperatureReading:
        reads["temperature"] += 1
        if reads["temperature"] == 1:
            raise RuntimeError("probe failed")
        return kernel_temperature_reading(79.2)

    monkeypatch.setattr(service.temperature_reader, "read_one_wire_gpio_detailed", fail_then_recover)

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
    port.check_frequency = 30.0
    calls = 0

    def read_temperature(_bcm_pin: int, **_kwargs) -> OneWireTemperatureReading:
        nonlocal calls
        calls += 1
        return kernel_temperature_reading(78.0 + calls)

    monkeypatch.setattr(service.temperature_reader, "read_one_wire_gpio_detailed", read_temperature)

    service._poll_temperature(100.0)
    service._poll_temperature(101.0)
    service._poll_temperature(130.1)

    assert calls == 2
    assert service.state.readings["sense_port_1_temperature"].value == 80.0


def test_temperature_sensor_failure_turns_dependent_heater_off(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    port = service.config.sense_ports[0]
    port.device = SensePortDevice.DS18B20
    port.assigned_equipment = "ac1"
    port.equipment_type = EquipmentKind.HEATER
    service.state.equipment["ac1"] = EquipmentState("ac1", True)
    monkeypatch.setattr(
        service.temperature_reader,
        "read_one_wire_gpio_detailed",
        lambda _pin, **_kwargs: (_ for _ in ()).throw(RuntimeError("lost bus")),
    )

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
    temp.check_frequency = 0.05
    analog = service.config.sense_ports[1]
    analog.device = SensePortDevice.ANALOG
    analog.check_frequency = 0.05
    service._rebuild_sensor_runtime()
    slow_started = threading.Event()
    slow_release = threading.Event()
    analog_reads = 0

    def slow_temperature(_bcm_pin: int, **_kwargs) -> OneWireTemperatureReading:
        slow_started.set()
        slow_release.wait(1.0)
        return kernel_temperature_reading(78.0)

    def read_voltage(_address: int, _channel: int) -> float:
        nonlocal analog_reads
        analog_reads += 1
        return 1.5

    monkeypatch.setattr(service.temperature_reader, "read_one_wire_gpio_detailed", slow_temperature)
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
    temp.check_frequency = 0.05
    analog = service.config.sense_ports[1]
    analog.device = SensePortDevice.ANALOG
    analog.check_frequency = 0.05
    service._rebuild_sensor_runtime()
    monkeypatch.setattr(
        service.temperature_reader,
        "read_one_wire_gpio_detailed",
        lambda _pin, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad probe")),
    )
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
    port.check_frequency = 0.05
    service._rebuild_sensor_runtime()
    attempts = 0

    def configure(_bcm_pin: int, **_kwargs) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HardwareUnavailable("one-wire helper activation failed")

    monkeypatch.setattr(service.temperature_reader, "configure_kernel_one_wire", configure)
    monkeypatch.setattr(
        service.temperature_reader,
        "read_one_wire_gpio_detailed",
        lambda _bcm_pin, **_kwargs: OneWireTemperatureReading(
            bcm_pin=4,
            sensor_id="28-000000000001",
            bus_master="w1_bus_master1",
            celsius=25.889,
            fahrenheit=78.6,
            crc_valid=True,
        ),
    )
    service._start_sensor_workers()
    try:
        wait_for(lambda: attempts >= 2, timeout=1.0)
        wait_for(lambda: service._sensor_worker_snapshots()["sense_port_1_temperature"].status == "online", timeout=1.0)
        service._control_loop_iteration(time.time())
    finally:
        service._stop_sensor_workers()

    assert service.state.sensor_health["sense_port_1_temperature"].status == "online"
    assert service.state.readings["sense_port_1_temperature"].value == 78.6


def test_kernel_one_wire_worker_stop_cancels_blocking_setup(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    port = service.config.sense_ports[0]
    port.device = SensePortDevice.DS18B20
    port.check_frequency = 0.05
    service._rebuild_sensor_runtime()
    setup_started = threading.Event()
    setup_cancelled = threading.Event()

    def configure(_bcm_pin: int, *, stop_event: threading.Event | None = None) -> None:
        assert stop_event is not None
        setup_started.set()
        if stop_event.wait(5.0):
            setup_cancelled.set()
            raise HardwareUnavailable("setup cancelled")
        raise AssertionError("test setup did not receive worker cancellation")

    monkeypatch.setattr(service.temperature_reader, "configure_kernel_one_wire", configure)

    service._start_sensor_workers()
    assert setup_started.wait(1.0)
    started = time.monotonic()
    stuck = service._stop_sensor_workers()

    assert stuck == []
    assert setup_cancelled.is_set()
    assert time.monotonic() - started < 1.0


def test_sensor_worker_stop_deadline_covers_one_wire_helper_timeout() -> None:
    assert LoggerheadService.SENSOR_WORKER_STOP_TIMEOUT_SECONDS >= 10.0


def test_sensor_runtime_releases_removed_kernel_one_wire_pin_and_marks_affected_stack(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    service.config.sense_ports[0].device = SensePortDevice.DS18B20
    service.config.sense_ports[1].device = SensePortDevice.ANALOG
    service._kernel_one_wire_pins = {4, 17}
    released: list[int] = []
    configured: list[int] = []

    def release(gpio: int) -> OneWireOverlayResult:
        released.append(gpio)
        return OneWireOverlayResult("release", gpio, owned=True, affected_gpios=(17,), safe_input=True)

    def configure(gpio: int) -> None:
        configured.append(gpio)

    monkeypatch.setattr(service.temperature_reader, "release_kernel_one_wire", release)
    monkeypatch.setattr(service.temperature_reader, "configure_kernel_one_wire", configure)

    service._rebuild_sensor_runtime()

    assert released == [17]
    assert configured == [4]
    assert service._kernel_one_wire_pins == {4}
    assert service.state.sensor_health["sense_port_2_analog"].status == "stale"
    assert "overlay stack" in service.state.sensor_health["sense_port_2_analog"].last_error


def test_sensor_runtime_blocks_config_change_when_kernel_release_fails(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    previous = service.config
    service.config.sense_ports[0].device = SensePortDevice.EMPTY
    service._kernel_one_wire_pins = {4}
    monkeypatch.setattr(
        service.temperature_reader,
        "release_kernel_one_wire",
        lambda _gpio: (_ for _ in ()).throw(HardwareUnavailable("active overlay is not owned")),
    )

    with pytest.raises(HardwareFault, match="Could not release kernel 1-Wire backend"):
        service._rebuild_sensor_runtime(previous_config=previous)


def test_sensor_runtime_blocks_config_change_when_gpio_handoff_is_unsafe(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    previous = copy.deepcopy(service.config)
    previous.sense_ports[0].device = SensePortDevice.DS18B20
    service.config.sense_ports[0].device = SensePortDevice.ANALOG
    service._kernel_one_wire_pins = {4}
    monkeypatch.setattr(
        service.temperature_reader,
        "release_kernel_one_wire",
        lambda gpio: OneWireOverlayResult("release", gpio, owned=True, safe_input=False),
    )

    with pytest.raises(HardwareFault, match="handoff could not be verified"):
        service._rebuild_sensor_runtime(previous_config=previous)


def test_startup_restore_forces_temperature_controlled_equipment_off(tmp_path) -> None:
    service = make_service(tmp_path)
    service.config.sense_ports[0].device = SensePortDevice.DS18B20
    service.config.sense_ports[0].assigned_equipment = "ac1"
    service.config.sense_ports[0].equipment_type = EquipmentKind.HEATER
    service.state.equipment["ac1"] = EquipmentState("ac1", True, "restore")

    service._invalidate_configured_sensor_readings_on_startup()
    service._restore_equipment_defaults()

    assert service.state.equipment["ac1"].on is False
    assert service.state.equipment["ac1"].source == "startup_sensor_lockout"


def test_startup_releases_helper_owned_overlay_not_in_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    service.config.sense_ports[0].device = SensePortDevice.EMPTY
    released: list[int] = []
    monkeypatch.setattr(service.temperature_reader, "owned_runtime_one_wire_pins", lambda: {4})
    monkeypatch.setattr(
        service.temperature_reader,
        "release_kernel_one_wire",
        lambda gpio: released.append(gpio) or OneWireOverlayResult("release", gpio, owned=True, safe_input=True),
    )

    service._rebuild_sensor_runtime()

    assert released == [4]


def test_update_config_migrates_legacy_bit_bang_payload(tmp_path) -> None:
    service = make_service(tmp_path)
    payload = asdict(service.config)
    payload["sense_ports"][0]["device"] = "ds18b20"
    payload["sense_ports"][0]["one_wire_mode"] = "bit_bang"

    updated = service.update_config(payload)

    assert updated.sense_ports[0].device == SensePortDevice.DS18B20
    assert "one_wire_mode" not in service.config_path.read_text(encoding="utf-8")


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


def test_manual_prime_overrides_are_transient_and_keep_motor_settings(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    original = asdict(service.config.steppers[0])
    config_bytes = service.config_path.read_bytes()
    calls = []

    def run(_assignment, **kwargs):
        calls.append(kwargs)
        return StepperRunResult(0, "stopped")

    monkeypatch.setattr(service.stepper_engine, "run_continuous", run)
    service.set_manual_priming("dose1", True, speed_steps_per_second=1000, max_seconds=3)
    wait_for(lambda: bool(calls) and "dose1" not in service._prime_threads)
    assert calls[0]["steps_per_second"] == 1000
    assert calls[0]["max_seconds"] == 3
    for key in ("run_current_ma", "hold_current_ma", "microsteps", "stallguard_threshold", "direction_high"):
        assert calls[0][key] == original[key]
    assert asdict(service.config.steppers[0]) == original
    assert service.config_path.read_bytes() == config_bytes


@pytest.mark.parametrize("overrides", [
    {"speed_steps_per_second": 5001}, {"speed_steps_per_second": True},
    {"speed_steps_per_second": 1000.5}, {"max_seconds": 61}, {"max_seconds": 0},
    {"max_seconds": float("nan")}, {"max_seconds": "3"},
])
def test_invalid_prime_override_cannot_start_a_motor(tmp_path, overrides) -> None:
    service = make_service(tmp_path)
    with pytest.raises(DiagnosticHalt):
        service.set_manual_priming("dose1", True, **overrides)
    assert service._prime_threads == {}


def test_stop_ignores_invalid_speed_and_duration(tmp_path) -> None:
    service = make_service(tmp_path)
    service.config.steppers[0].manual_speed_steps_per_second = 0
    result = service.set_manual_priming("dose1", False, speed_steps_per_second=-1, max_seconds=-1)
    assert result["state"] == "stopped"


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
    service.set_sense_port(1, {"device": "hydros_triple"})
    alarm_id = "water:sense_port_1_water"
    service.state.alarms[alarm_id] = AlarmState(alarm_id, "Sump level high.", AlarmPriority.HIGH, active=True)
    service.state_store.save(service.state)

    restarted = LoggerheadService(config_path, data_dir, simulation=True)
    sound_calls: list[AlarmPriority] = []
    monkeypatch.setattr(restarted.buzzer, "sound", lambda priority=AlarmPriority.HIGH: sound_calls.append(priority))

    restarted._sound_buzzer_if_needed(time.time())

    assert sound_calls == []
    assert alarm_id in restarted._restart_suppressed_alarm_ids


def test_new_high_alarm_after_restart_can_still_sound(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(tmp_path)
    sound_calls: list[AlarmPriority] = []
    monkeypatch.setattr(service.buzzer, "sound", lambda priority=AlarmPriority.HIGH: sound_calls.append(priority))

    service.state.alarms["stepper:dose1"] = AlarmState("stepper:dose1", "Dose fault.", AlarmPriority.HIGH, active=True)
    service._sound_buzzer_if_needed(time.time())

    assert sound_calls == [AlarmPriority.HIGH]

