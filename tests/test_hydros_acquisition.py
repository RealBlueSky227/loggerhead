from __future__ import annotations

import threading
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from loggerhead.config import ATOProfile, SensePortDevice
from loggerhead.drivers import HardwareFault, HardwareUnavailable, HydrosPulseReader, HydrosTripleClassifier
from loggerhead.hardware import LevelState
from loggerhead.sensor_workers import SensorReadResult, SensorWorker, SensorWorkerSnapshot, SensorWorkerSpec
from loggerhead.service import LoggerheadService
from loggerhead.state import EquipmentState


class PulsePi:
    connected = True

    def __init__(self):
        self._notify = SimpleNamespace(handle=7)
        self.tick = 1000
        self.callbacks = []
        self.active = None
        self.subscribed = False
        self.transport_edges = 0
        self.stops = 0
        self.settings = []
        self.pause_error = self.cancel_error = self.arm_error = False
        self.pause_calls = 0
        self.on_arm = None

    def set_mode(self, *args):
        self.settings.append(args)

    def set_pull_up_down(self, *args):
        self.settings.append(args)

    def notify_pause(self, handle):
        assert handle == 7
        self.pause_calls += 1
        if self.pause_error:
            return -1
        self.subscribed = False
        return 0

    def get_current_tick(self):
        return self.tick

    def callback(self, gpio, edge, func):
        assert gpio == 17 and edge == 0
        self.active = func
        self.callbacks.append(func)
        self.subscribed = True
        if self.arm_error:
            raise OSError('arm failed after subscription')
        if self.on_arm:
            self.on_arm(self)

        def cancel():
            if self.cancel_error:
                raise OSError('cancel failed')
            self.active = None
            self.subscribed = False

        return SimpleNamespace(cancel=cancel)

    def emit(self, periods, *, callback=None):
        callback = callback or self.active
        if callback is None:
            return
        callback(17, 1, self.tick)
        for period in periods:
            self.tick = (self.tick + period) & 0xFFFFFFFF
            if self.subscribed:
                self.transport_edges += 1
            callback(17, 1, self.tick)

    def stop(self):
        self.stops += 1
        self.active = None
        self.subscribed = False


def reader_for(pi=None, **kwargs):
    pi = pi or PulsePi()
    module = SimpleNamespace(INPUT=0, PUD_UP=2, RISING_EDGE=0, pi=lambda: pi)
    return HydrosPulseReader(17, HydrosTripleClassifier(**kwargs), pigpio_module=module), pi


@pytest.mark.parametrize(('period', 'state'), [(2520, LevelState.HIGH), (5040, LevelState.NORMAL),
                                             (10080, LevelState.LOW), (50394, LevelState.DRY)])
def test_all_levels_require_eight_full_periods_and_pause_real_subscription(period, state):
    reader, pi = reader_for()
    pi.on_arm = lambda pi: pi.emit([period]*8)
    assert not pi.subscribed and not pi.callbacks
    assert reader.read_state() == state
    data = reader.diagnostics()
    assert data['pulse_count'] == 8 and data['rising_edges'] == 9
    assert data['period_us'] == period
    assert not pi.subscribed and pi.active is None
    assert pi.pause_calls == 2
    reader.close()
    reader.close()
    assert pi.stops == 1


def test_no_old_value_or_old_debounce_is_reused_and_paused_activity_is_not_disconnect():
    reader, pi = reader_for(activity_timeout=.01)
    pi.on_arm = lambda pi: pi.emit([2520]*8)
    assert reader.read_state() == LevelState.HIGH
    measured = reader.diagnostics()['last_measurement_ts']
    time.sleep(.02)
    assert reader.diagnostics()['state'] == 'high'
    pi.on_arm = lambda pi: pi.emit([5040]*2)
    assert reader.read_state() == LevelState.UNKNOWN
    assert reader.diagnostics()['last_measurement_ts'] == measured
    pi.on_arm = lambda pi: pi.emit([50394]*8)
    assert reader.read_state() == LevelState.DRY
    reader.close()


@pytest.mark.parametrize('periods', [[2520, 5040]*16, [10]*32, [18000]*32, [0]*32])
def test_noise_or_invalid_periods_never_qualify(periods):
    reader, pi = reader_for()
    pi.on_arm = lambda pi: pi.emit(periods)
    assert reader.read_state() == LevelState.UNKNOWN
    assert reader.diagnostics()['last_acquisition_reason'] == 'inconsistent_periods'
    assert not pi.subscribed
    reader.close()


def test_noisy_transition_resets_streak_then_requires_eight_consistent_new_periods():
    reader, pi = reader_for()
    pi.on_arm = lambda pi: pi.emit([2520]*7 + [10, 5040] + [10080]*8)
    assert reader.read_state() == LevelState.LOW
    assert reader.diagnostics()['invalid_frequency_count'] == 1
    reader.close()


def test_configured_debounce_larger_than_eight_is_honored():
    reader, pi = reader_for(debounce_samples=10, activity_timeout=.01)
    pi.on_arm = lambda pi: pi.emit([5040]*9)
    assert reader.read_state() == LevelState.UNKNOWN
    pi.on_arm = lambda pi: pi.emit([5040]*10)
    assert reader.read_state() == LevelState.NORMAL
    reader.close()


def test_tick_rollover_and_queued_pre_arm_packets():
    reader, pi = reader_for()
    pi.tick = 0xFFFFFF00

    def pulses(pi):
        pi.active(17, 1, (pi.tick-10000) & 0xFFFFFFFF)
        pi.emit([2520]*8)

    pi.on_arm = pulses
    assert reader.read_state() == LevelState.HIGH
    assert reader.diagnostics()['rising_edges'] == 9
    reader.close()


def test_missing_pulses_and_reconnection_do_not_return_a_previous_valid_level():
    reader, pi = reader_for(activity_timeout=.02)
    pi.on_arm = lambda pi: pi.emit([2520]*8)
    assert reader.read_state() == LevelState.HIGH
    pi.on_arm = None
    before = time.monotonic()
    assert reader.read_state() == LevelState.INACTIVE
    assert time.monotonic()-before < .2
    assert not pi.subscribed
    pi.on_arm = lambda pi: pi.emit([5040]*8)
    assert reader.read_state() == LevelState.NORMAL
    reader.close()


def test_deadline_bounds_never_stable_capture(monkeypatch):
    reader, pi = reader_for(activity_timeout=10)
    monkeypatch.setattr(HydrosPulseReader, 'acquisition_timeout', property(lambda self: .02))
    pi.on_arm = lambda pi: pi.emit([2520, 5040])
    assert reader.read_state() == LevelState.UNKNOWN
    assert reader.diagnostics()['last_acquisition_reason'] == 'timeout'
    assert not pi.subscribed
    reader.close()


def test_late_callbacks_cannot_publish_into_new_capture():
    reader, pi = reader_for()
    pi.on_arm = lambda pi: pi.emit([2520]*8)
    assert reader.read_state() == LevelState.HIGH
    old = pi.callbacks[-1]

    def next_capture(pi):
        before = reader.diagnostics()['pulse_count']
        pi.emit([2520]*20, callback=old)
        assert reader.diagnostics()['pulse_count'] == before
        pi.emit([50394]*8)

    pi.on_arm = next_capture
    assert reader.read_state() == LevelState.DRY
    old(17, 1, pi.tick+2520)
    assert reader.diagnostics()['state'] == 'dry'
    reader.close()


@pytest.mark.parametrize('close', [False, True])
def test_active_capture_is_interruptible_and_cleanup_is_complete(close):
    reader, pi = reader_for()
    armed, stop = threading.Event(), threading.Event()
    results = []
    pi.on_arm = lambda pi: armed.set()
    thread = threading.Thread(target=lambda: results.append(reader.read_state(stop_event=stop)))
    thread.start()
    assert armed.wait(1)
    if close:
        reader.close()
    else:
        stop.set()
    thread.join(1)
    assert not thread.is_alive()
    assert results[0] in {LevelState.UNKNOWN, LevelState.INACTIVE}
    assert reader.diagnostics()['last_acquisition_reason'] == 'cancelled'
    assert not pi.subscribed
    reader.close()


@pytest.mark.parametrize('failure', ['pause_error', 'cancel_error', 'arm_error'])
def test_subscription_failures_close_connection_and_latch_invalid_state(failure):
    reader, pi = reader_for()

    def arm(pi):
        setattr(pi, failure, True)
        pi.emit([2520]*8)

    if failure == 'arm_error':
        pi.arm_error = True
    pi.on_arm = arm
    with pytest.raises(HardwareUnavailable):
        reader.read_state()
    assert not pi.subscribed and pi.stops >= 1
    assert reader.diagnostics()['state'] == 'unknown'
    with pytest.raises(HardwareUnavailable):
        reader.read_state()
    reader.close()


def test_repeated_reads_reuse_one_connection_and_idle_has_no_notification_traffic():
    reader, pi = reader_for()
    pi.on_arm = lambda pi: pi.emit([2520]*8)
    thread_count = threading.active_count()
    for _ in range(100):
        assert reader.read_state() == LevelState.HIGH
        before = pi.transport_edges
        for _ in range(1600):
            if pi.subscribed:
                pi.transport_edges += 1
        assert pi.transport_edges == before
        assert not pi.subscribed and pi.active is None
    assert threading.active_count() == thread_count
    assert pi.stops == 0
    reader.close()
    assert pi.stops == 1


def test_passive_benchmark_does_not_configure_gpio():
    pi = PulsePi()
    module = SimpleNamespace(RISING_EDGE=0, pi=lambda: pi)
    reader = HydrosPulseReader(17, HydrosTripleClassifier(), pigpio_module=module, configure_gpio=False)
    assert not pi.settings
    reader.close()


def test_ato_inputs_use_faster_schedule_and_hung_read_becomes_stale(tmp_path):
    service = LoggerheadService(tmp_path/'config.json', tmp_path/'data', simulation=True)
    port = service.config.sense_ports[0]
    port.device = SensePortDevice.HYDROS_TRIPLE
    port.check_frequency = 4
    service.config.ato.append(ATOProfile('ato', 'ATO', 'sense_port_1_water', 'sense_port_2_water', 'relay', 'ac1'))
    specs = service._build_sensor_worker_specs()
    spec = next(spec for spec in specs if spec.sensor_id == 'sense_port_1_water')
    assert spec.check_frequency == 1 and spec.stale_after_seconds == 2
    service.config.ato[0].enabled = False
    spec = next(spec for spec in service._build_sensor_worker_specs() if spec.sensor_id == 'sense_port_1_water')
    assert spec.check_frequency == 4


def test_completed_water_read_is_not_lost_when_control_runs_mid_capture(tmp_path):
    service = LoggerheadService(tmp_path/'config.json', tmp_path/'data', simulation=True)
    service.config.sense_ports[0].device = SensePortDevice.HYDROS_TRIPLE
    snapshot = SensorWorkerSnapshot('sense_port_1_water', 'water', worker_state='reading', data_status='online',
                                    status='online', value='dry', last_attempt_ts=time.time(), last_success_ts=time.time()-1)
    service.state.water_levels[snapshot.sensor_id] = LevelState.DRY
    kwargs = dict(equipment_commands=[], value_logs=[], events=[], alarms=[])
    service._apply_sensor_worker_snapshots_locked({snapshot.sensor_id: snapshot}, time.time(), **kwargs)
    snapshot.worker_state, snapshot.value = 'running', 'high'
    service._apply_sensor_worker_snapshots_locked({snapshot.sensor_id: snapshot}, time.time(), **kwargs)
    assert service.state.water_levels[snapshot.sensor_id] == LevelState.HIGH


def test_hung_ato_input_is_invalidated_using_monotonic_success_timestamp(tmp_path):
    service = LoggerheadService(tmp_path/'config.json', tmp_path/'data', simulation=True)
    service.config.sense_ports[0].device = SensePortDevice.HYDROS_TRIPLE
    service.config.sense_ports[1].device = SensePortDevice.HYDROS_TRIPLE
    service.config.ato.append(ATOProfile('ato', 'ATO', 'sense_port_1_water', 'sense_port_2_water', 'relay', 'ac1'))
    service.state.equipment['ac1'] = EquipmentState('ac1', True)
    worker = SensorWorker(SensorWorkerSpec('sense_port_1_water', 'water', 1, 2, lambda stop: None))
    worker._record_success(SensorReadResult('sense_port_1_water', 'water', 'dry'), time.monotonic())
    worker._snapshot.last_success_monotonic -= 3
    worker._snapshot.worker_state = 'reading'
    backup = SensorWorkerSnapshot('sense_port_2_water', 'water', worker_state='running', data_status='online',
                                   status='online', value='dry', last_attempt_ts=time.time(), last_success_ts=time.time())
    commands = []
    service._apply_sensor_worker_snapshots_locked({worker.sensor_id: worker.snapshot(), backup.sensor_id: backup}, time.time(),
                                                 equipment_commands=commands, value_logs=[], events=[], alarms=[])
    service._plan_ato_locked(time.time(), equipment_commands=commands, alarms=[], notifications=[])
    assert ('ac1', False, 'ato') in commands


def test_failed_reader_cleanup_blocks_gpio_handoff(tmp_path):
    service = LoggerheadService(tmp_path/'config.json', tmp_path/'data', simulation=True)

    def fail():
        raise HardwareUnavailable('notification thread stuck')

    reader = SimpleNamespace(close=fail)
    service._hydros_readers['sense_port_1_water'] = reader
    with pytest.raises(HardwareFault, match='handoff blocked'):
        service._close_sensor_readers()
    assert service._hydros_readers['sense_port_1_water'] is reader


def test_already_cancelled_read_never_subscribes():
    reader, pi = reader_for()
    stop = threading.Event()
    stop.set()
    with pytest.raises(HardwareUnavailable, match='cancelled'):
        reader.read_state(stop_event=stop)
    assert not pi.callbacks and not pi.subscribed
    reader.close()


def test_concurrent_acquisition_is_rejected_without_disturbing_current_read():
    reader, pi = reader_for()
    armed, stop = threading.Event(), threading.Event()
    pi.on_arm = lambda pi: armed.set()
    thread = threading.Thread(target=lambda: reader.read_state(stop_event=stop))
    thread.start()
    try:
        assert armed.wait(1)
        with pytest.raises(HardwareUnavailable, match='already in progress'):
            reader.read_state()
        assert len(pi.callbacks) == 1 and pi.subscribed
    finally:
        stop.set()
        thread.join(1)
        reader.close()
    assert not thread.is_alive()


def test_stuck_notification_thread_prevents_cleanup_until_exit(monkeypatch):
    reader, pi = reader_for()
    release = threading.Event()
    notify = threading.Thread(target=lambda: release.wait(3))
    notify.handle = 7
    pi._notify = notify
    notify.start()
    monkeypatch.setattr(reader, 'COMMAND_TIMEOUT_SECONDS', .01)
    try:
        with pytest.raises(HardwareUnavailable, match='handoff is blocked'):
            reader.close()
        assert reader.pi is pi and reader._retiring_notify is notify
    finally:
        release.set()
        notify.join(1)
        reader.close()
    assert reader.pi is None


def test_config_transition_interrupts_previous_capture_before_replacing_backend(tmp_path):
    service = LoggerheadService(tmp_path/'config.json', tmp_path/'data', simulation=True)
    reader, pi = reader_for()
    armed = threading.Event()
    pi.on_arm = lambda pi: armed.set()
    service.config.sense_ports[1].device = SensePortDevice.HYDROS_TRIPLE
    service._hydros_readers['sense_port_2_water'] = reader
    service._install_sensor_workers(service._build_sensor_worker_specs())
    service._start_sensor_workers()
    old_worker = service._sensor_workers['sense_port_2_water']
    try:
        assert armed.wait(1)
        service.set_sense_port(2, {'device': 'empty'})
        assert not old_worker._thread.is_alive()
        assert not pi.subscribed and pi.stops == 1
        assert 'sense_port_2_water' not in service._sensor_workers
    finally:
        service.stop()


@pytest.mark.parametrize('backup_input', [False, True])
def test_new_level_stops_ato_on_next_control_evaluation(tmp_path, backup_input):
    service = LoggerheadService(tmp_path/'config.json', tmp_path/'data', simulation=True)
    for port in service.config.sense_ports[:2]:
        port.device = SensePortDevice.HYDROS_TRIPLE
    service.config.ato.append(ATOProfile('ato', 'ATO', 'sense_port_1_water', 'sense_port_2_water', 'relay', 'ac1'))
    service.state.water_levels.update(sense_port_1_water=LevelState.DRY, sense_port_2_water=LevelState.DRY)
    commands = []
    service._plan_ato_locked(time.time(), equipment_commands=commands, alarms=[], notifications=[])
    assert ('ac1', True, 'ato') in commands
    service.state.equipment['ac1'] = EquipmentState('ac1', True)
    sensor_id = 'sense_port_2_water' if backup_input else 'sense_port_1_water'
    snapshot = SensorWorkerSnapshot(sensor_id, 'water', worker_state='running', data_status='online', status='online',
                                    value='high', last_attempt_ts=time.time(), last_success_ts=time.time())
    service._apply_sensor_worker_snapshots_locked({sensor_id: snapshot}, time.time(), equipment_commands=[], value_logs=[],
                                                 events=[], alarms=[])
    commands.clear()
    service._plan_ato_locked(time.time(), equipment_commands=commands, alarms=[], notifications=[])
    assert ('ac1', False, 'ato') in commands


def test_enabled_ato_max_runtime_is_unchanged_by_capture_policy(tmp_path):
    service = LoggerheadService(tmp_path/'config.json', tmp_path/'data', simulation=True)
    service.config.ato.append(ATOProfile('ato', 'ATO', 'primary', 'backup', 'relay', 'ac1', max_run_time=2))
    service.state.water_levels.update(primary=LevelState.DRY, backup=LevelState.DRY)
    service._plan_ato_locked(100., equipment_commands=[], alarms=[], notifications=[])
    service.state.equipment['ac1'] = EquipmentState('ac1', True)
    commands, alarms = [], []
    service._plan_ato_locked(103., equipment_commands=commands, alarms=alarms, notifications=[])
    assert ('ac1', False, 'ato') in commands
    assert service.state.ato['ato'].faulted and alarms


def test_overrunning_acquisition_publishes_completion_even_during_next_read(tmp_path):
    service = LoggerheadService(tmp_path/'config.json', tmp_path/'data', simulation=True)
    service.config.sense_ports[0].device = SensePortDevice.HYDROS_TRIPLE
    worker = SensorWorker(SensorWorkerSpec('sense_port_1_water', 'water', .1, 2, lambda stop: None))
    worker._record_success(SensorReadResult(worker.sensor_id, 'water', 'dry'), time.monotonic())
    worker._snapshot.worker_state = 'reading'
    kwargs = dict(equipment_commands=[], value_logs=[], events=[], alarms=[])
    service._apply_sensor_worker_snapshots_locked({worker.sensor_id: worker.snapshot()}, time.time(), **kwargs)
    assert service.state.water_levels[worker.sensor_id] == LevelState.DRY
    worker._record_success(SensorReadResult(worker.sensor_id, 'water', 'high'), time.monotonic())
    worker._snapshot.last_attempt_ts = time.time() + 1  # Next read already begun.
    service._apply_sensor_worker_snapshots_locked({worker.sensor_id: worker.snapshot()}, time.time(), **kwargs)
    assert service.state.water_levels[worker.sensor_id] == LevelState.HIGH
    assert len(kwargs['value_logs']) == 2
    service._apply_sensor_worker_snapshots_locked({worker.sensor_id: worker.snapshot()}, time.time(), **kwargs)
    assert len(kwargs['value_logs']) == 2


def test_enabling_ato_alone_rebuilds_effective_sensor_schedule(tmp_path):
    service = LoggerheadService(tmp_path/'config.json', tmp_path/'data', simulation=True)
    service.config.sense_ports[0].device = SensePortDevice.HYDROS_TRIPLE
    service.config.sense_ports[0].check_frequency = 4
    service.config.sense_ports[1].device = SensePortDevice.BINARY
    service.config.ato.append(ATOProfile('ato', 'ATO', 'sense_port_1_water', 'sense_port_2_water', 'relay', 'ac1', enabled=False))
    service._rebuild_sensor_runtime()
    assert service._sensor_workers['sense_port_1_water'].spec.check_frequency == 4
    payload = asdict(service.config)
    payload['ato'][0]['enabled'] = True
    service.update_config(payload)
    assert service._sensor_workers['sense_port_1_water'].spec.check_frequency == 1
    assert service.config.sense_ports[0].check_frequency == 4


def test_missing_pause_acknowledgment_invalidates_capture():
    reader, pi = reader_for()
    pi.on_arm = lambda pi: pi.emit([2520]*8)
    pi.notify_pause = lambda handle: None
    with pytest.raises(HardwareUnavailable, match='acknowledge'):
        reader.read_state()
    assert reader.diagnostics()['state'] == 'unknown' and not pi.subscribed
    reader.close()


@pytest.mark.parametrize('tick', [None, -1, 0x100000000])
def test_invalid_start_ticks_fail_before_subscribing(tick):
    reader, pi = reader_for()
    pi.tick = tick
    with pytest.raises(HardwareUnavailable):
        reader.read_state()
    assert not pi.callbacks and pi.stops == 1
    reader.close()
