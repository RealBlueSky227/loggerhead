from __future__ import annotations

import copy
import datetime as dt
import json
import threading
import time
from dataclasses import asdict

import pytest

from loggerhead.ato import ATOJournal
from loggerhead.config import ATOProfile, DosingProfile, SensePortDevice, load_config, validate_config
from loggerhead.controllers import ATOController
from loggerhead.drivers import HardwareFault, PulseWindowResult, StepperRunResult
from loggerhead.hardware import DiagnosticHalt, LevelState, require_stepper
from loggerhead.service import LoggerheadService
from loggerhead.state import RuntimeState, SensorHealth


def wait_for(predicate, timeout=3):
    deadline = time.monotonic()+timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail('Timed out waiting for mocked ATO worker')
        time.sleep(0.01)


@pytest.fixture
def controller(tmp_path, monkeypatch):
    service = LoggerheadService(tmp_path/'config.json', tmp_path/'data', simulation=True)
    candidate = asdict(service.config)
    for port in candidate['sense_ports'][:2]:
        port['device'] = SensePortDevice.HYDROS_TRIPLE
    candidate['ato'] = [asdict(ATOProfile('topoff', 'Fresh water', 'sense_port_1_water', 'sense_port_2_water', 'relay', 'ac1',
                                        start_level=LevelState.LOW, stop_level=LevelState.NORMAL))]
    service.update_config(candidate)
    monkeypatch.setattr(service, '_sensor_worker_snapshots', lambda: {})
    set_levels(service)
    yield service
    service.stop()


def set_levels(service, primary=LevelState.DRY, backup=LevelState.DRY):
    with service._state_lock:
        for key, level in [('sense_port_1_water', primary), ('sense_port_2_water', backup)]:
            service.state.water_levels[key] = level
            service.state.sensor_health[key] = SensorHealth(key, status='online', data_status='online',
                                                           last_success_ts=time.time(), stale_after_seconds=2)


def stepper_config(service):
    candidate = asdict(service.config)
    candidate['ato'][0].update(actuator_device_type='stepper', assigned_actuator='dose1', max_refill_ml=2, max_daily_ml=4)
    service.update_config(candidate)
    set_levels(service)


@pytest.mark.parametrize(('start', 'stop'), [('dry', 'low'), ('dry', 'normal'), ('dry', 'high'),
                                          ('low', 'normal'), ('low', 'high'), ('normal', 'high')])
def test_hydros_hysteresis_all_valid_thresholds(start, stop):
    profile = ATOProfile('ato', 'ATO', 'p', 'b', 'relay', 'ac1', start_level=LevelState(start), stop_level=LevelState(stop))
    state = RuntimeState(water_levels={'p': LevelState.HIGH, 'b': LevelState.DRY})
    assert not ATOController.evaluate(profile, state, 100)[0]
    state.water_levels['p'] = LevelState(start)
    assert ATOController.evaluate(profile, state, 101)[0]
    levels = list((LevelState.DRY, LevelState.LOW, LevelState.NORMAL, LevelState.HIGH))
    for level in levels[levels.index(LevelState(start)):levels.index(LevelState(stop))]:
        state.water_levels['p'] = level
        assert ATOController.evaluate(profile, state, 102)[0]
    state.water_levels['p'] = LevelState(stop)
    assert not ATOController.evaluate(profile, state, 103)[0]
    if levels.index(LevelState(stop))-levels.index(LevelState(start)) > 1:
        state.water_levels['p'] = levels[levels.index(LevelState(stop))-1]
        assert not ATOController.evaluate(profile, state, 104)[0]


def test_relay_refill_and_stop_threshold(controller):
    service = controller
    service.ato.tick()
    wait_for(lambda: service.state.equipment['ac1'].on)
    set_levels(service, LevelState.LOW)
    time.sleep(0.12)
    assert service.state.equipment['ac1'].on
    set_levels(service, LevelState.NORMAL)
    wait_for(lambda: not service.ato.jobs)
    assert not service.state.equipment['ac1'].on
    assert not service.state.ato['topoff'].faulted
    assert 'Stop threshold' in service.ato.history[0]['reason']


@pytest.mark.parametrize('failure', ['unknown', 'inactive', 'stale', 'disconnected', 'backup', 'heartbeat', 'runtime'])
def test_relay_faults_stop_and_latch(controller, failure):
    service = controller
    service.ato.tick()
    wait_for(lambda: service.state.equipment['ac1'].on)
    job = service.ato.jobs['topoff']
    if failure == 'unknown':
        set_levels(service, LevelState.UNKNOWN)
    elif failure == 'inactive':
        set_levels(service, LevelState.INACTIVE)
    elif failure == 'stale':
        service.state.sensor_health['sense_port_1_water'].last_success_ts -= 10
    elif failure == 'disconnected':
        service.state.sensor_health['sense_port_2_water'].data_status = 'disconnected'
    elif failure == 'backup':
        set_levels(service, backup=LevelState.HIGH)
    elif failure == 'heartbeat':
        service.ato._control_seen -= 10
    else:
        job.started -= 100
    wait_for(lambda: not service.ato.jobs)
    assert not service.state.equipment['ac1'].on
    assert service.state.ato['topoff'].faulted
    service.ato.tick()
    assert not service.ato.jobs
    assert service.ato.history[0]['status'] == 'fault'


def test_reset_requires_restored_level_and_fresh_backup(controller):
    service = controller
    service.ato._latch('topoff', 'Test fault')
    with pytest.raises(RuntimeError, match='Restore primary'):
        service.reset_ato('topoff')
    set_levels(service, LevelState.NORMAL, LevelState.HIGH)
    with pytest.raises(RuntimeError, match='still active'):
        service.reset_ato('topoff')
    set_levels(service, LevelState.NORMAL)
    service.state.sensor_health['sense_port_2_water'].last_success_ts -= 20
    with pytest.raises(RuntimeError, match='fresh'):
        service.reset_ato('topoff')
    set_levels(service, LevelState.NORMAL)
    service.reset_ato('topoff')
    assert not service.state.ato['topoff'].faulted
    service.ato.tick()
    assert not service.ato.jobs


def test_stepper_refill_uses_existing_motion_and_exclusive_ownership(controller, monkeypatch):
    service = controller
    stepper_config(service)
    started = threading.Event()
    calls = []

    def motion(assignment, **kwargs):
        calls.append(kwargs)
        assert assignment.name == 'dose1'
        assert service._motion_owner == ('ato:topoff', 'dose1')
        assert service.ato.journal.recent()[0]['status'] == 'running'
        started.set()
        kwargs['stop_event'].wait(2)
        return StepperRunResult(100, 'stopped', possible_steps=150, interrupted_batch=True)

    monkeypatch.setattr(service.stepper_engine, 'run_continuous', motion)
    service.ato.start()
    service.ato.tick()
    assert started.wait(1)
    with pytest.raises(RuntimeError, match='already active'):
        service.set_manual_priming('dose2', True)
    with pytest.raises(RuntimeError, match='Disable ATO'):
        service.start_calibration('dose1', 100)
    set_levels(service, LevelState.NORMAL)
    wait_for(lambda: not service.ato.jobs)
    assert service._motion_owner is None
    assert calls[0]['steps_per_second'] == 400
    assert calls[0]['run_current_ma'] == 700
    assert calls[0]['pulse_window_seconds'] == .25
    assert calls[0]['max_steps'] == 1600
    assert service.ato.history[0]['charged_ml'] == 150/800
    assert service.ato.history[0]['confirmed_steps'] == 100
    assert not service.state.ato['topoff'].faulted


@pytest.mark.parametrize('outcome', ['limit', 'uart', 'external_cancel', 'cleanup', 'journal'])
def test_stepper_faults_and_uncertain_accounting(controller, monkeypatch, outcome):
    service = controller
    stepper_config(service)

    def motion(_assignment, **kwargs):
        if outcome == 'uart':
            raise HardwareFault('UART read failed')
        if outcome == 'limit':
            return StepperRunResult(kwargs['max_steps'], 'max_steps', possible_steps=kwargs['max_steps'])
        if outcome in {'cleanup', 'journal'}:
            service.ato.cancel(service.ato.jobs['topoff'], 'Manual cancellation', True)
        return StepperRunResult(0, 'stopped', possible_steps=100, interrupted_batch=True)

    monkeypatch.setattr(service.stepper_engine, 'run_continuous', motion)
    if outcome == 'cleanup':
        monkeypatch.setattr(service.stepper_engine, 'stop', lambda _a: (_ for _ in ()).throw(HardwareFault('ENN failed')))
    if outcome == 'journal':
        monkeypatch.setattr(service.ato.journal, 'finish', lambda *_a, **_kw: (_ for _ in ()).throw(OSError('disk failed')))
    service.ato.tick()
    wait_for(lambda: not service.ato.jobs)
    assert service.state.ato['topoff'].faulted
    with pytest.raises(RuntimeError):
        service.set_manual_priming('dose1', True)
    if outcome == 'uart':
        assert service.ato.history[0]['confirmed_steps'] == 0
        assert service.ato.history[0]['charged_ml'] == 2
    if outcome == 'cleanup':
        assert service.stepper_engine._fault


def test_busy_motion_is_not_queued_and_rechecks_water(controller):
    service = controller
    stepper_config(service)
    with service._prime_lock:
        service._claim_motion('test:other', 'dose2')
    service.ato.tick()
    assert not service.ato.jobs
    assert 'already active' in service.state.ato['topoff'].waiting_reason
    set_levels(service, LevelState.NORMAL)
    with service._prime_lock:
        service._release_motion('test:other')
    service.ato.tick()
    assert not service.ato.jobs


def test_daily_volume_claims_survive_reset_and_new_profile_id(controller):
    service = controller
    stepper_config(service)
    profile = service.config.ato[0]
    journal = service.ato.journal
    first, amount = journal.claim(profile, 800, 2)
    journal.finish(first, fault=True, confirmed=0, possible=-1, duration=1, reason='uncertain')
    journal.acknowledge(profile.id)
    renamed = copy.deepcopy(profile)
    renamed.id = 'new-profile'
    second, amount = journal.claim(renamed, 800, 2)
    assert amount == 2
    journal.finish(second, fault=True, confirmed=1600, possible=1600, duration=1, reason='limit')
    journal.acknowledge(renamed.id)
    with pytest.raises(HardwareFault, match='24-hour'):
        journal.claim(renamed, 800, 2)


def test_restart_marks_running_refill_faulted_and_never_replays(controller):
    service = controller
    stepper_config(service)
    profile = service.config.ato[0]
    service.ato.journal.claim(profile, 800, 2)
    recovered = LoggerheadService(service.config_path, service.data_dir, simulation=True)
    try:
        assert recovered.state.ato['topoff'].faulted
        assert not recovered.state.ato['topoff'].running
        assert not recovered.ato.jobs
        assert recovered.state.alarms['ato:topoff'].active
        assert recovered.ato.history[0]['charged_ml'] == 2
    finally:
        recovered.stop()


def test_configuration_disable_stops_active_refill_and_preserves_lockout(controller):
    service = controller
    service.ato.tick()
    wait_for(lambda: service.state.equipment['ac1'].on)
    candidate = asdict(service.config)
    candidate['ato'][0]['enabled'] = False
    service.update_config(candidate)
    assert not service.ato.jobs
    assert not service.state.equipment['ac1'].on
    assert service.state.ato['topoff'].faulted
    assert not service.config.ato[0].enabled
    assert not load_config(service.config_path).ato[0].enabled


def test_startup_never_restores_ato_relay_on(controller):
    service = controller
    service._write_equipment_output('ac1', True, source='test')
    service._save_state()
    restarted = LoggerheadService(service.config_path, service.data_dir, simulation=True)
    try:
        assert not restarted.state.equipment['ac1'].on
    finally:
        restarted.stop()


@pytest.mark.parametrize('edit', ['same_sensor', 'missing', 'reversed', 'nan', 'zero', 'wrong_type', 'volume', 'daily', 'dosing'])
def test_configuration_validation(controller, edit):
    service = controller
    stepper_config(service)
    candidate = copy.deepcopy(service.config)
    profile = candidate.ato[0]
    if edit == 'same_sensor':
        profile.backup_failsafe_sensor = profile.primary_level_sensor
    elif edit == 'missing':
        profile.backup_failsafe_sensor = 'missing'
    elif edit == 'reversed':
        profile.start_level, profile.stop_level = LevelState.HIGH, LevelState.NORMAL
    elif edit == 'nan':
        profile.max_run_time = float('nan')
    elif edit == 'zero':
        profile.max_run_time = 0
    elif edit == 'wrong_type':
        profile.actuator_device_type = 'relay'
    elif edit == 'volume':
        profile.max_refill_ml = 0
    elif edit == 'daily':
        profile.max_daily_ml = 1
    else:
        candidate.dosing.append(DosingProfile('dose', 'Chemical', 'dose1', daily_volume_ml=2, doses_per_day=1, stepper=True, enabled=True))
    with pytest.raises(DiagnosticHalt):
        validate_config(candidate)


def test_legacy_relay_and_binary_configuration(controller):
    config = copy.deepcopy(controller.config)
    config.ato[0].actuator_device_type = 'mcp_relay'
    config.ato[0].start_level, config.ato[0].stop_level = LevelState.DRY, None
    config.sense_ports[0].device = SensePortDevice.BINARY
    validate_config(config)
    assert config.ato[0].actuator_device_type == 'relay'
    state = RuntimeState(water_levels={'sense_port_1_water': LevelState.DRY, 'sense_port_2_water': LevelState.DRY})
    assert ATOController.evaluate(config.ato[0], state, 1)[0]
    state.water_levels['sense_port_1_water'] = LevelState.SUBMERGED
    assert not ATOController.evaluate(config.ato[0], state, 2)[0]


def test_manual_relay_control_cannot_compete(controller):
    with pytest.raises(RuntimeError, match='reserved by ATO'):
        controller.set_equipment('ac1', True)


def test_watchdog_stops_relay_even_if_worker_logging_is_blocked(controller, monkeypatch):
    service = controller
    service.ato.tick()
    wait_for(lambda: service.state.equipment['ac1'].on)
    monkeypatch.setattr(service.store, 'log_event', lambda *_a, **_kw: (_ for _ in ()).throw(OSError('logging failed')))
    service.ato._control_seen -= 10
    service.ato.watch_once()
    assert not service.state.equipment['ac1'].on
    wait_for(lambda: not service.ato.jobs)


def test_journal_has_atomic_single_actuator_claim(controller):
    journal = ATOJournal(controller.store)
    profile = controller.config.ato[0]
    results = []
    def claim():
        try:
            results.append(journal.claim(profile, 0, 0)[0])
        except HardwareFault:
            pass
    threads = [threading.Thread(target=claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert len(results) == 1


def test_legacy_inert_stepper_ato_is_not_activated_by_upgrade(controller):
    candidate = asdict(controller.config)
    candidate['ato'][0].update(actuator_device_type='stepper', assigned_actuator='dose1')
    for key in ('max_refill_ml', 'max_daily_ml', 'start_level', 'stop_level'):
        candidate['ato'][0].pop(key)
    controller.config_path.write_text(json.dumps(candidate))
    loaded = load_config(controller.config_path)
    assert not loaded.ato[0].enabled
    assert loaded.ato[0].max_refill_ml == 0


def test_dosing_owns_motion_before_ato_and_does_not_start_obsolete_refill(controller, monkeypatch):
    service = controller
    stepper_config(service)
    candidate = asdict(service.config)
    candidate['dosing'] = [asdict(DosingProfile('alk', 'Alk', 'dose2', daily_volume_ml=2, doses_per_day=1, stepper=True, enabled=True))]
    service.update_config(candidate)
    set_levels(service)
    stamp = dt.datetime.now().replace(hour=8, minute=0, second=0, microsecond=0).timestamp()
    service.dosing._armed_after['alk'] = stamp-1

    def motion(_assignment, **kwargs):
        service.ato.tick()
        assert not service.ato.jobs
        assert 'already active' in service.state.ato['topoff'].waiting_reason
        set_levels(service, LevelState.NORMAL)
        return StepperRunResult(kwargs['max_steps'], 'max_steps', possible_steps=kwargs['max_steps'])

    monkeypatch.setattr(service.stepper_engine, 'run_continuous', motion)
    service.dosing.tick(stamp+1)
    service.ato.tick()
    assert not service.ato.jobs
    assert service._motion_owner is None


def test_ato_owns_motion_before_dosing(controller, monkeypatch):
    service = controller
    stepper_config(service)
    candidate = asdict(service.config)
    candidate['dosing'] = [asdict(DosingProfile('alk', 'Alk', 'dose2', daily_volume_ml=2, doses_per_day=1, stepper=True, enabled=True))]
    service.update_config(candidate)
    set_levels(service)
    started = threading.Event()
    calls = []

    def motion(_assignment, **kwargs):
        calls.append(_assignment.name)
        started.set()
        kwargs['stop_event'].wait(2)
        return StepperRunResult(0, 'stopped', possible_steps=10, interrupted_batch=True)

    monkeypatch.setattr(service.stepper_engine, 'run_continuous', motion)
    service.ato.tick()
    assert started.wait(1)
    service.dosing.tick()
    assert calls == ['dose1'] and service.dosing.active is None
    service.ato.cancel(service.ato.jobs['topoff'], 'Test interrupted refill', True)
    wait_for(lambda: not service.ato.jobs)


def test_new_ato_assignment_deenergizes_existing_manual_relay(controller):
    service = controller
    service.set_equipment('ac2', True)
    candidate = asdict(service.config)
    candidate['ato'][0]['assigned_actuator'] = 'ac2'
    service.update_config(candidate)
    assert not service.state.equipment['ac2'].on


def test_relay_alias_cannot_bypass_ato_ownership(controller):
    candidate = copy.deepcopy(controller.config)
    alias = copy.deepcopy(candidate.equipment[0])
    alias.id = 'other-id'
    candidate.equipment.append(alias)
    with pytest.raises(DiagnosticHalt, match='aliasing'):
        validate_config(candidate)


def test_engine_ato_windows_are_bounded_and_cancellable(controller, monkeypatch):
    service = controller
    stop = threading.Event()
    windows = []
    def pulse(_assignment, **kwargs):
        windows.append(kwargs['steps'])
        return PulseWindowResult(kwargs['steps'], kwargs['steps'], 1)
    def progress(confirmed, possible):
        assert confirmed == possible == 100
        stop.set()
    monkeypatch.setattr(service.stepper_engine, '_pulse_windowed', pulse)
    result = service.stepper_engine.run_continuous(require_stepper('dose1'), stop_event=stop,
                                                  steps_per_second=400, run_current_ma=700, max_steps=10000,
                                                  pulse_window_seconds=.25, progress=progress)
    assert windows == [100]
    assert result.steps_sent == result.possible_steps == 100
    assert result.reason == 'stopped'


def test_manual_relay_stop_is_immediate_and_requires_reset(controller):
    controller.ato.tick()
    wait_for(lambda: controller.state.equipment['ac1'].on)
    controller.set_equipment('ac1', False)
    assert not controller.state.equipment['ac1'].on
    assert controller.state.ato['topoff'].faulted
    wait_for(lambda: not controller.ato.jobs)


def test_worker_start_failure_releases_reservation_and_latches_alarm(controller, monkeypatch):
    service = controller
    stepper_config(service)
    def failed_start(_thread):
        raise RuntimeError('thread resource exhausted')
    monkeypatch.setattr(threading.Thread, 'start', failed_start)
    with pytest.raises(RuntimeError, match='resource exhausted'):
        service.ato.tick()
    assert not service.ato.jobs and service._motion_owner is None
    assert service.state.ato['topoff'].faulted
    assert service.state.alarms['ato:topoff'].active


def test_config_change_blocks_on_unstopped_worker(controller, monkeypatch):
    service = controller
    service.ato.tick()
    wait_for(lambda: service.state.equipment['ac1'].on)
    job = service.ato.jobs['topoff']
    real_thread = job.thread
    class Stuck:
        def join(self, _timeout):
            pass
        def is_alive(self):
            return True
    job.thread = Stuck()
    candidate = asdict(service.config)
    candidate['ato'][0]['enabled'] = False
    try:
        with pytest.raises(HardwareFault, match='transition blocked'):
            service.update_config(candidate)
        assert service.config.ato[0].enabled
        assert not service.state.equipment['ac1'].on
    finally:
        job.thread = real_thread
        wait_for(lambda: not service.ato.jobs)


def test_status_api_and_reset_surface_real_ato_states(controller):
    import urllib.error
    import urllib.request

    from loggerhead.web import DashboardServer
    service = controller
    service.ato._latch('topoff', 'Test reset fault')
    server = DashboardServer(service, host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_address[1]}'
        with urllib.request.urlopen(base+'/api/status') as response:
            body = json.load(response)
        assert body['ato_runtime']['profiles']['topoff']['status'] == 'FAULT'
        request = urllib.request.Request(base+'/api/ato/reset', data=b'{"id":"topoff"}', headers={'Content-Type': 'application/json'})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        assert caught.value.code == 409
        set_levels(service, LevelState.NORMAL)
        with urllib.request.urlopen(request) as response:
            assert json.load(response)['ok']
        assert not service.state.ato['topoff'].faulted
    finally:
        server.shutdown()
        thread.join(2)
