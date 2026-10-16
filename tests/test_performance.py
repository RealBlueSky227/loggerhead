from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from loggerhead.drivers import StepperRunResult
from loggerhead.performance import PerformanceMonitor
from loggerhead.service import LoggerheadService
from loggerhead.web import DashboardServer


def test_timings_are_bounded_and_include_active_thread_progress():
    monitor = PerformanceMonitor()
    with monitor.measure('outer'):
        snapshot = monitor.snapshot(stacks=True)
        current = next(row for row in snapshot['threads'] if row['ident'] == threading.get_ident())
        assert current['active_operations'][0]['operation'] == 'outer'
        assert current['stack']
        with pytest.raises(RuntimeError), monitor.measure('failure'):
            raise RuntimeError('expected failure')
    for index in range(180):
        with monitor.measure(f'operation-{index}'):
            pass
    snapshot = monitor.snapshot()
    assert snapshot['operations']['failure']['errors'] == 1
    assert snapshot['operations']['outer']['count'] == 1
    assert len(snapshot['operations']) <= 129
    assert all(not row['active_operations'] for row in snapshot['threads'])


def test_status_and_stop_remain_available_during_pending_calibration_persistence(tmp_path, monkeypatch):
    service = LoggerheadService(tmp_path / 'config.json', tmp_path / 'data', simulation=True)
    entered, release = threading.Event(), threading.Event()
    original = service._save_state
    motions, results, errors = [], [], []

    def delayed_persist():
        if threading.current_thread().name == 'pending-calibration':
            entered.set()
            assert release.wait(4)
        original()

    def start():
        try:
            results.append(service.start_calibration('dose1', 800, 1000))
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(service, '_save_state', delayed_persist)
    monkeypatch.setattr(service.stepper_engine, 'run_continuous', lambda *_args, **_kwargs: motions.append(True))
    thread = threading.Thread(target=start, name='pending-calibration')
    thread.start()
    try:
        assert entered.wait(1)
        before = time.monotonic()
        snapshot = service.status()
        assert time.monotonic()-before < .5
        assert snapshot['calibrations']['dose1']['state'] == 'starting'
        assert snapshot['stepper_active'] == 'dose1'
        service.set_manual_priming('dose1', False)
        assert service._prime_stop['dose1'].is_set()
        assert service._prime_pending == {'dose1'}
        assert not motions
    finally:
        release.set()
        thread.join(4)
    assert not thread.is_alive() and not errors
    assert results[0]['state'] == 'cancelled'
    assert not motions
    assert not service._prime_pending and not service._prime_threads
    assert not service.state.manual_priming['dose1']


def test_failed_start_persistence_never_runs_motor_or_leaves_reservation(tmp_path, monkeypatch):
    service = LoggerheadService(tmp_path / 'config.json', tmp_path / 'data', simulation=True)
    motions = []

    def failed_save():
        raise OSError('SD card unavailable')

    monkeypatch.setattr(service, '_save_state', failed_save)
    monkeypatch.setattr(service.stepper_engine, 'run_continuous', lambda *_args, **_kwargs: motions.append(True))
    with pytest.raises(OSError, match='SD card'):
        service.start_calibration('dose1', 800, 1000)
    assert not motions and not service._prime_pending and not service._prime_threads
    assert not service.state.manual_priming['dose1']
    assert service._prime_results['dose1']['state'] == 'failed'


def test_blocked_state_writer_allows_status_and_stop_before_shutdown(tmp_path, monkeypatch):
    service = LoggerheadService(tmp_path / 'config.json', tmp_path / 'data', simulation=True)
    attempted, stop_requested = threading.Event(), threading.Event()
    original = service._save_state
    motions, errors = [], []

    def save():
        attempted.set()
        original()

    def request_stop():
        stop_requested.set()

    def command(enabled):
        try:
            service.set_manual_priming('dose1', enabled)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(service, '_save_state', save)
    monkeypatch.setattr(service.stepper_engine, 'request_stop', request_stop)
    monkeypatch.setattr(service.stepper_engine, 'run_continuous', lambda *_a, **_kw: motions.append(True))
    start = threading.Thread(target=command, args=(True,))
    stop = threading.Thread(target=command, args=(False,))
    with service.state_store._write_lock:
        start.start()
        assert attempted.wait(1)
        assert service.status()['stepper_active'] == 'dose1'
        stop.start()
        assert stop_requested.wait(1)
        assert service._prime_stop['dose1'].is_set()
        assert service.status()['stepper_active'] is None
        service._stop.set()
    start.join(4)
    stop.join(4)
    assert not start.is_alive() and not stop.is_alive()
    assert not errors and not motions
    assert service._prime_results['dose1']['state'] == 'cancelled'
    assert not service._prime_threads and not service._prime_pending


def test_started_calibration_retains_stop_event_until_motor_exits(tmp_path, monkeypatch):
    service = LoggerheadService(tmp_path / 'config.json', tmp_path / 'data', simulation=True)
    entered = threading.Event()

    def run(_assignment, **kwargs):
        entered.set()
        assert kwargs['stop_event'].wait(3)
        return StepperRunResult(0, 'stopped', possible_steps=0)

    monkeypatch.setattr(service.stepper_engine, 'run_continuous', run)
    service.start_calibration('dose1', 800, 1000)
    assert entered.wait(1)
    thread = service._prime_threads['dose1']
    event = service._prime_stop['dose1']
    assert not event.is_set()
    service.set_manual_priming('dose1', False)
    assert event.is_set()
    thread.join(4)
    assert not thread.is_alive()
    assert not service._prime_stop and not service._prime_threads


def test_calibration_completion_persistence_does_not_block_status(tmp_path, monkeypatch):
    service = LoggerheadService(tmp_path / 'config.json', tmp_path / 'data', simulation=True)
    entered, release = threading.Event(), threading.Event()
    original = service._save_state

    def delayed_finish():
        if threading.current_thread().name == 'prime-dose1':
            entered.set()
            assert release.wait(4)
        original()

    def run(_assignment, **kwargs):
        return StepperRunResult(kwargs['max_steps'], 'max_steps', possible_steps=kwargs['max_steps'])

    monkeypatch.setattr(service, '_save_state', delayed_finish)
    monkeypatch.setattr(service.stepper_engine, 'run_continuous', run)
    service.start_calibration('dose1', 800, 1000)
    try:
        assert entered.wait(1)
        before = time.monotonic()
        snapshot = service.status()
        assert time.monotonic()-before < .5
        assert snapshot['calibrations']['dose1']['state'] == 'completed'
        assert snapshot['stepper_active'] is None
    finally:
        release.set()
        for thread in service._prime_threads_snapshot().values():
            thread.join(4)


def test_performance_http_does_not_require_controller_locks(tmp_path):
    service = LoggerheadService(tmp_path / 'config.json', tmp_path / 'data', simulation=True)
    server = DashboardServer(service, host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with service._state_lock, service._prime_lock:
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/performance?stacks=1', timeout=1) as response:
                snapshot = json.load(response)
            assert snapshot['threads']
            assert all('stack' in row for row in snapshot['threads'])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        service.stop()
