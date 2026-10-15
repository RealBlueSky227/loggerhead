from __future__ import annotations

import datetime as dt
import json
import threading
import urllib.error
import urllib.request
from dataclasses import asdict

import pytest

from loggerhead.config import AppConfig, DosingProfile, from_dict, load_config, save_config, validate_config
from loggerhead.controllers import DosingScheduler
from loggerhead.database import TelemetryStore
from loggerhead.drivers import HardwareFault, StepperRunResult
from loggerhead.hardware import DiagnosticHalt
from loggerhead.service import LoggerheadService
from loggerhead.web import DashboardServer


def make_service(tmp_path):
    return LoggerheadService(tmp_path / "config.json", tmp_path / "data", simulation=True)


def enable_schedule(service, *, pump="dose1", schedule_id="alk"):
    candidate = asdict(service.config)
    candidate["dosing"].append(asdict(DosingProfile(schedule_id, schedule_id, pump, daily_volume_ml=24,
                                                  doses_per_day=12, stepper=True, enabled=True)))
    service.update_config(candidate)
    stamp = dt.datetime(2026, 10, 15, 8).timestamp()
    service.dosing._armed_after[schedule_id] = stamp - 1
    return stamp


def successful_motion(_assignment, **kwargs):
    return StepperRunResult(kwargs["max_steps"], "max_steps", possible_steps=kwargs["max_steps"])


def test_legacy_config_does_not_enable_previously_inert_schedules(tmp_path):
    service = make_service(tmp_path)
    data = asdict(service.config)
    data["dosing"] = [{"id": "legacy", "name": "Legacy", "actuator": "dose1", "stepper": True,
                       "daily_volume_ml": 24}]
    for pump in data["steppers"]:
        pump.pop("dose_speed_steps_per_second")
    config = from_dict(AppConfig, data)
    save_config(service.config_path, config)
    reloaded = load_config(service.config_path)
    assert not reloaded.dosing[0].enabled
    assert all(pump.dose_speed_steps_per_second == 400 for pump in reloaded.steppers)


def test_scheduler_excludes_end_boundary_and_supports_overnight():
    profile = DosingProfile("alk", "Alk", "dose1", daily_volume_ml=24, doses_per_day=12)
    assert DosingScheduler.next_action(profile, dt.datetime(2026, 10, 15, 20), None).due is False
    profile.window_start, profile.window_end, profile.doses_per_day = "22:00", "06:00", 8
    slot, window_date = DosingScheduler.due_slot(profile, dt.datetime(2026, 10, 16, 1, 0, 5))
    assert slot == dt.datetime(2026, 10, 16, 1)
    assert window_date == "2026-10-15"
    assert DosingScheduler.next_slot(profile, dt.datetime(2026, 10, 16, 5, 30)) == dt.datetime(2026, 10, 16, 22)


def test_persistent_claim_deduplicates_concurrent_attempts_and_reserves_daily_budget(tmp_path):
    store = TelemetryStore(tmp_path / "history.db")
    results = []

    def claim():
        results.append(store.claim_dose(schedule_id="alk", slot_ts=100, window_date="2026-10-15",
                                       pump_id="dose1", requested_ml=2, requested_steps=1600, daily_limit=2))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert len([result for result in results if result is not None]) == 1
    assert store.claim_dose(schedule_id="alk", slot_ts=200, window_date="2026-10-15",
                            pump_id="dose1", requested_ml=1, requested_steps=800, daily_limit=2) is None
    assert len(store.recent_doses()) == 1


def test_scheduled_run_uses_calibration_and_its_own_speed_once(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    service.config.steppers[0].dose_speed_steps_per_second = 1000
    calls = []

    def run(assignment, **kwargs):
        calls.append((assignment.name, kwargs))
        assert service.store.recent_doses()[0]["status"] == "running"
        return successful_motion(assignment, **kwargs)

    monkeypatch.setattr(service.stepper_engine, "run_continuous", run)
    service.dosing.tick(stamp + 1)
    service.dosing.tick(stamp + 2)
    assert len(calls) == 1
    assert calls[0][0] == "dose1"
    assert calls[0][1]["max_steps"] == 1600
    assert calls[0][1]["steps_per_second"] == 1000
    assert calls[0][1]["run_current_ma"] == 700
    assert service.store.recent_doses()[0]["status"] == "completed"
    assert service.state.stepper_active is None


def test_simultaneous_schedules_run_serially_without_becoming_catchup(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    enable_schedule(service, pump="dose2", schedule_id="calcium")
    calls = []

    def run(assignment, **kwargs):
        assert service.dosing.active["pump_id"] == assignment.name
        calls.append(assignment.name)
        return successful_motion(assignment, **kwargs)

    monkeypatch.setattr(service.stepper_engine, "run_continuous", run)
    service.dosing.tick(stamp + 1)
    assert calls == ["dose1", "dose2"]


def test_startup_and_late_ticks_never_catch_up_old_slots(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Missed slots must not operate a motor")

    monkeypatch.setattr(service.stepper_engine, "run_continuous", unexpected)
    service.dosing.tick(stamp + 31)
    service.dosing._armed_after["alk"] = stamp + 2
    service.dosing.tick(stamp + 3)
    assert service.store.recent_doses() == []


def test_failed_dose_pauses_and_alarms_without_retry(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(True)
        raise HardwareFault("UART unavailable")

    monkeypatch.setattr(service.stepper_engine, "run_continuous", fail)
    service.dosing.tick(stamp + 1)
    service.dosing.tick(stamp + 3601)
    assert len(calls) == 1
    assert service.state.dosing_paused["alk"]
    assert service.state.dosing_faults["alk"]
    assert service.state.alarms["dosing:alk"].active
    assert service.store.recent_doses()[0]["status"] == "failed"


def test_restart_marks_uncertain_run_interrupted_and_never_resumes(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    service.store.claim_dose(schedule_id="alk", slot_ts=stamp, window_date="2026-10-15",
                             pump_id="dose1", requested_ml=2, requested_steps=1600, daily_limit=24)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Startup must not resume a dose")

    monkeypatch.setattr("loggerhead.drivers.StepperPulseEngine.run_continuous", unexpected)
    restarted = make_service(tmp_path)
    assert restarted.state.dosing_paused["alk"]
    assert restarted.state.dosing_faults["alk"]
    assert restarted.store.recent_doses()[0]["status"] == "interrupted"
    assert restarted.store.recent_doses()[0]["possible_steps"] == 1600
    assert restarted.relay_board.shadow_a & 0xF0 == 0xF0


def test_scheduled_motion_rejects_priming_and_config_edits_and_stop_pauses(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    entered, release = threading.Event(), threading.Event()

    def run(_assignment, **kwargs):
        entered.set()
        assert release.wait(3)
        assert kwargs["stop_event"].is_set()
        return StepperRunResult(0, "stopped", possible_steps=1600, interrupted_batch=True)

    monkeypatch.setattr(service.stepper_engine, "run_continuous", run)
    thread = threading.Thread(target=service.dosing.tick, args=(stamp + 1,))
    thread.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match="scheduled dose"):
            service.set_manual_priming("dose2", True)
        changed = asdict(service.config)
        changed["steppers"][0]["steps_per_ml"] = 900
        with pytest.raises(RuntimeError, match="Stop the active pump"):
            service.update_config(changed)
        service.set_manual_priming("dose1", False)
        assert service.state.dosing_paused["alk"]
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert service.store.recent_doses()[0]["status"] == "stopped"


def test_schedule_status_never_queries_database(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    enable_schedule(service)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Status must use cached dosing history")

    monkeypatch.setattr(service.store, "recent_doses", unexpected)
    for _ in range(3):
        assert service.status()["dosing"]["schedules"][0]["id"] == "alk"


def test_dosing_preview_validates_without_saving_or_arming(tmp_path):
    service = make_service(tmp_path)
    original = service.config_path.read_bytes()
    candidate = asdict(service.config)
    candidate["dosing"] = [asdict(DosingProfile("alk", "Alk", "dose1", stepper=True, daily_volume_ml=24))]
    preview = service.preview_dosing(candidate)
    assert len(preview["schedules"][0]["slots"]) == 8
    assert preview["schedules"][0]["steps_per_dose"] == 1600
    assert service.config_path.read_bytes() == original
    assert not service.config.dosing


def test_pump_only_save_keeps_sensor_workers_and_failed_save_never_arms_schedule(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workers = dict(service._sensor_workers)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Pump edits must not restart sensor workers or overlays")

    monkeypatch.setattr(service, "_rebuild_sensor_runtime", unexpected)
    candidate = asdict(service.config)
    candidate["steppers"][0]["dose_speed_steps_per_second"] = 1000
    service.update_config(candidate)
    assert service._sensor_workers == workers
    candidate["dosing"] = [asdict(DosingProfile("alk", "Alk", "dose1", stepper=True, enabled=True, daily_volume_ml=24))]

    def failed_save(*_args, **_kwargs):
        raise OSError("Filesystem read-only")

    monkeypatch.setattr("loggerhead.service.save_config", failed_save)
    with pytest.raises(OSError, match="Filesystem read-only"):
        service.update_config(candidate)
    assert not service.config.dosing
    assert service._sensor_workers == workers


def test_incomplete_scheduled_dose_requires_fault_reset_then_explicit_resume(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    monkeypatch.setattr(service.stepper_engine, "run_continuous",
                        lambda *_args, **_kwargs: StepperRunResult(100, "max_seconds", possible_steps=1600))
    service.dosing.tick(stamp + 1)
    assert service.state.dosing_faults["alk"]
    with pytest.raises(RuntimeError, match="Reset"):
        service.dosing.pause("alk", False)
    service.dosing.reset("alk")
    assert service.state.dosing_paused["alk"]
    assert not service.state.dosing_faults.get("alk")
    service.dosing.pause("alk", False)
    assert not service.state.dosing_paused["alk"]
    assert service.dosing._armed_after["alk"] > stamp


def test_calibration_cleanup_fault_never_appears_successful(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(service.stepper_engine, "run_continuous", successful_motion)

    def fail_stop(*_args):
        raise HardwareFault("ENN disable unverified")

    monkeypatch.setattr(service.stepper_engine, "stop", fail_stop)
    service.start_calibration("dose1", 800, 1000)
    thread = service._prime_threads.get("dose1")
    if thread:
        thread.join(2)
    assert service.status()["calibrations"]["dose1"]["state"] == "failed"
    assert service.state.alarms["stepper:dose1:cleanup"].active


def test_completed_calibration_is_not_a_priming_limit_warning(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(service.stepper_engine, "run_continuous", successful_motion)
    limits = []
    monkeypatch.setattr(service, "_handle_manual_prime_limit", lambda *_args: limits.append(True))
    service.start_calibration("dose1", 800, 1000)
    thread = service._prime_threads.get("dose1")
    if thread:
        thread.join(2)
    assert not limits
    assert service.status()["calibrations"]["dose1"]["state"] == "completed"


def test_faulted_schedule_must_be_acknowledged_before_removal(tmp_path):
    service = make_service(tmp_path)
    enable_schedule(service)
    service.state.dosing_faults["alk"] = "Interrupted dose"
    candidate = asdict(service.config)
    candidate["dosing"] = []
    with pytest.raises(RuntimeError, match="Reset the dosing fault"):
        service.update_config(candidate)
    assert service.config.dosing[0].id == "alk"


@pytest.fixture
def http_service(tmp_path):
    service = make_service(tmp_path)
    server = DashboardServer(service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield service, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        service.stop()
        thread.join(2)


def post(base, path, payload):
    request = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_calibration_and_preview_http_use_real_service_objects(http_service, monkeypatch):
    service, base = http_service
    monkeypatch.setattr(service.stepper_engine, "run_continuous", successful_motion)
    status, body = post(base, "/api/calibration", {"id": "dose1", "steps": 800, "speed_steps_per_second": 1000})
    assert status == 202 and body["operation_id"]
    thread = service._prime_threads.get("dose1")
    if thread:
        thread.join(2)
    assert service.status()["calibrations"]["dose1"]["state"] == "completed"
    candidate = asdict(service.config)
    candidate["dosing"] = [asdict(DosingProfile("alk", "Alk", "dose1", stepper=True, daily_volume_ml=24))]
    status, body = post(base, "/api/dosing/preview", {"config": candidate})
    assert status == 200 and body["schedules"][0]["steps_per_dose"] == 1600
    assert not service.config.dosing
    status, _body = post(base, "/api/calibration", {"id": "dose1", "steps": -1})
    assert status == 422


def test_schedule_http_pause_reset_and_invalid_config_preserve_saved_file(http_service):
    service, base = http_service
    enable_schedule(service)
    assert post(base, "/api/dosing/pause", {"id": "alk", "paused": True})[0] == 200
    assert service.state.dosing_paused["alk"]
    assert post(base, "/api/dosing/pause", {"id": "alk", "paused": False})[0] == 200
    assert not service.state.dosing_paused["alk"]
    assert post(base, "/api/dosing/pause", {"id": "alk", "paused": "false"})[0] == 400
    before = service.config_path.read_bytes()
    candidate = asdict(service.config)
    candidate["dosing"][0]["max_dose_ml"] = 0.01
    assert post(base, "/api/config", candidate)[0] == 422
    assert service.config_path.read_bytes() == before


@pytest.mark.parametrize("terminal", [False, True])
def test_journal_fault_recovery_survives_crash_before_state_save(tmp_path, terminal):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    run_id = service.store.claim_dose(schedule_id="alk", slot_ts=stamp, window_date="2026-10-15",
                                      pump_id="dose1", requested_ml=2, requested_steps=1600, daily_limit=24)
    if terminal:
        service.store.finish_dose(run_id, "failed", error="hardware fault before state save")
    else:
        service.store.recover_interrupted_doses()  # Simulate another crash immediately after recovery commit.
    assert not service.state.dosing_faults
    restarted = make_service(tmp_path)
    assert restarted.state.dosing_paused["alk"]
    assert restarted.state.dosing_faults["alk"]
    restarted.dosing.reset("alk")
    restarted.dosing.pause("alk", False)
    restarted_again = make_service(tmp_path)
    assert not restarted_again.state.dosing_faults.get("alk")
    assert not restarted_again.state.dosing_paused["alk"]


def test_coarse_calibration_cannot_round_dose_above_safety_cap(tmp_path):
    service = make_service(tmp_path)
    service.config.steppers[0].steps_per_ml = 1
    service.config.dosing = [DosingProfile("alk", "Alk", "dose1", stepper=True, enabled=True,
                                          daily_volume_ml=0.6, doses_per_day=1, max_dose_ml=0.6)]
    with pytest.raises(DiagnosticHalt, match="smaller than one"):
        validate_config(service.config)


def test_clock_is_sampled_after_configuration_lock_wait(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    clock = [stamp + 1]
    monkeypatch.setattr("loggerhead.dosing.time.time", lambda: clock[0])
    attempts = []
    monkeypatch.setattr(service.stepper_engine, "run_continuous", lambda *_args, **_kwargs: attempts.append(True))
    with service._config_write_lock:
        started = threading.Event()

        def tick():
            started.set()
            service.dosing.tick()

        thread = threading.Thread(target=tick)
        thread.start()
        assert started.wait(1)
        clock[0] = stamp + 61
    thread.join(2)
    assert not thread.is_alive()
    assert not attempts


def test_claim_delay_cannot_trigger_late_motion(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    clock = [stamp + 1]
    monkeypatch.setattr("loggerhead.dosing.time.time", lambda: clock[0])
    original = service.store.claim_dose

    def delayed_claim(**kwargs):
        result = original(**kwargs)
        clock[0] = stamp + 61
        return result

    monkeypatch.setattr(service.store, "claim_dose", delayed_claim)
    service.dosing.tick()
    assert service.store.recent_doses()[0]["status"] == "skipped"
    assert service.state.stepper_active is None


def test_supervisor_survives_database_and_alarm_reporting_failure(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    attempts = []

    def failed_tick():
        attempts.append(True)
        raise RuntimeError("Database unavailable")

    def failed_alarm(*_args, **_kwargs):
        raise OSError("Alarm database unavailable too")

    def wait(_seconds):
        if len(attempts) >= 2:
            service._stop.set()

    monkeypatch.setattr(service.dosing, "tick", failed_tick)
    monkeypatch.setattr(service, "_activate_alarm", failed_alarm)
    monkeypatch.setattr(service._stop, "wait", wait)
    service.dosing.run()
    assert len(attempts) == 2


def test_reset_persists_pause_before_acknowledging_durable_fault(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    run_id = service.store.claim_dose(schedule_id="alk", slot_ts=stamp, window_date="2026-10-15",
                                      pump_id="dose1", requested_ml=2, requested_steps=1600, daily_limit=24)
    service.store.finish_dose(run_id, "failed", error="UART failed")
    service.state.dosing_faults["alk"] = "UART failed"

    def interrupted_ack(_id):
        assert service.state_store.load().dosing_paused["alk"]
        raise OSError("crash during acknowledgment")

    monkeypatch.setattr(service.store, "acknowledge_dosing_fault", interrupted_ack)
    with pytest.raises(OSError, match="crash"):
        service.dosing.reset("alk")
    restarted = make_service(tmp_path)
    assert restarted.state.dosing_paused["alk"]
    assert restarted.state.dosing_faults["alk"]


def test_cleanup_reporting_failure_still_finalizes_and_pauses_dose(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    monkeypatch.setattr(service.stepper_engine, "run_continuous", successful_motion)

    def fail_stop(*_args):
        raise HardwareFault("ENN not verified")

    def fail_alarm(*_args, **_kwargs):
        raise OSError("alarm database failed")

    monkeypatch.setattr(service.stepper_engine, "stop", fail_stop)
    monkeypatch.setattr(service, "_activate_alarm", fail_alarm)
    with pytest.raises(OSError, match="alarm database"):
        service.dosing.tick(stamp + 1)
    assert service.dosing.active is None
    assert service.state.stepper_active is None
    assert service.state.dosing_paused["alk"]
    assert "disable" in service.state.dosing_faults["alk"]
    assert service.store.recent_doses()[0]["status"] == "failed"


def test_serially_queued_dose_cannot_start_after_its_window(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    stamp = enable_schedule(service)
    enable_schedule(service, pump="dose2", schedule_id="calcium")
    for schedule in service.config.dosing:
        schedule.window_end = "08:01"
    clock = [stamp + 1]
    monkeypatch.setattr("loggerhead.dosing.time.time", lambda: clock[0])
    calls = []

    def run(assignment, **kwargs):
        calls.append(assignment.name)
        clock[0] = stamp + 61
        return successful_motion(assignment, **kwargs)

    monkeypatch.setattr(service.stepper_engine, "run_continuous", run)
    service.dosing.tick()
    assert calls == ["dose1"]


@pytest.mark.parametrize("change", [
    {"daily_volume_ml": float("nan")}, {"window_start": "25:00"}, {"doses_per_day": 0},
    {"doses_per_day": 1.5}, {"max_dose_ml": 0.1}, {"max_run_seconds": 1},
    {"enabled": "false"}, {"max_run_seconds": 601},
])
def test_schedule_validation_blocks_unsafe_values(tmp_path, change):
    service = make_service(tmp_path)
    schedule = DosingProfile("alk", "Alk", "dose1", stepper=True, enabled=True, daily_volume_ml=24)
    for key, value in change.items():
        setattr(schedule, key, value)
    service.config.dosing = [schedule]
    with pytest.raises(DiagnosticHalt):
        validate_config(service.config)


def test_duplicate_enabled_pump_schedules_are_rejected(tmp_path):
    service = make_service(tmp_path)
    service.config.dosing = [DosingProfile(id_, id_, "dose1", stepper=True, enabled=True, daily_volume_ml=12)
                              for id_ in ("alk", "calcium")]
    with pytest.raises(DiagnosticHalt, match="one enabled"):
        validate_config(service.config)


@pytest.mark.parametrize("complete", [True, False])
def test_calibration_requires_fully_confirmed_fixed_steps(tmp_path, monkeypatch, complete):
    service = make_service(tmp_path)
    before = asdict(service.config.steppers[0])

    def run(_assignment, **kwargs):
        steps = kwargs["max_steps"]
        return StepperRunResult(steps if complete else 0, "max_steps" if complete else "stopped",
                                possible_steps=steps, interrupted_batch=not complete)

    monkeypatch.setattr(service.stepper_engine, "run_continuous", run)
    result = service.start_calibration("dose1", 800, 1000)
    thread = service._prime_threads.get("dose1")
    if thread:
        thread.join(2)
        assert not thread.is_alive()
    record = service.status()["calibrations"]["dose1"]
    assert record["operation_id"] == result["operation_id"]
    assert record["state"] == ("completed" if complete else "incomplete")
    assert record["confirmed_steps"] == (800 if complete else 0)
    assert asdict(service.config.steppers[0]) == before
