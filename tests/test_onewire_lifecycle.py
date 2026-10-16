from __future__ import annotations

import json
import shutil
import threading
import types
from pathlib import Path

import pytest

from loggerhead.drivers import HardwareUnavailable, TemperatureReader
from loggerhead.onewire import HelperOneWireOverlayClient, OneWireOverlayResult


def make_bus(root, name, gpio, *, linked=True):
    master = root / name
    master.mkdir(parents=True)
    if linked:
        node = master / 'device/of_node'
        node.mkdir(parents=True)
        node.joinpath('compatible').write_bytes(b'w1-gpio\0')
        node.joinpath('gpios').write_bytes(b''.join(n.to_bytes(4, 'big') for n in (7, gpio, 0)))
    master.joinpath('w1_master_slaves').write_text('28-00000ff27476\n')
    slave = master / '28-00000ff27476'
    slave.mkdir()
    slave.joinpath('w1_slave').write_text('aa YES\nbb t=24750\n')
    return master


class OverlaySpy:
    def __init__(self):
        self.calls = []
        self.token = 1
        self.mappings = {}

    def verification_token(self):
        return (self.token,)

    def status(self):
        self.calls.append(('status', None))
        return OneWireOverlayResult('status', -1, owned_bus_masters=dict(self.mappings))

    def activate(self, gpio):
        self.calls.append(('activate', gpio))
        raise AssertionError('Read/recovery must never activate an overlay')

    def release(self, gpio):
        self.calls.append(('release', gpio))
        return OneWireOverlayResult('release', gpio, owned=True, safe_input=True)


def fixture_reader(tmp_path, *, linked=True):
    root, platforms = tmp_path / 'w1', tmp_path / 'platform'
    root.mkdir()
    platforms.mkdir()
    master = make_bus(root, 'w1_bus_master10', 17, linked=linked)
    if not linked:
        node = platforms / 'onewire@11/of_node'
        node.mkdir(parents=True)
        node.joinpath('compatible').write_bytes(b'w1-gpio\0')
        node.joinpath('gpios').write_bytes(b''.join(n.to_bytes(4, 'big') for n in (7, 17, 0)))
    overlay = OverlaySpy()
    overlay.mappings[17] = master.name
    reader = TemperatureReader(one_wire_root=root, platform_root=platforms, overlay_client=overlay)
    return reader, overlay, master


@pytest.mark.parametrize('linked', [True, False])
def test_verified_polling_never_calls_helper_or_gpioinfo_after_startup(tmp_path, monkeypatch, linked):
    reader, overlay, _master = fixture_reader(tmp_path, linked=linked)
    gpioinfo_calls = []
    monkeypatch.setattr(reader, '_gpioinfo_consumer_for_gpio', lambda gpio: gpioinfo_calls.append(gpio) or 'onewire@11')
    reader.configure_kernel_one_wire(17)
    calls = list(overlay.calls), len(gpioinfo_calls)
    clock = [100.0]
    monkeypatch.setattr('loggerhead.drivers.time.monotonic', lambda: clock[0])
    for now in (100, 140, 1000, 86400):
        clock[0] = now
        reading = reader.read_one_wire_gpio_detailed(17)
        assert reading.celsius == 24.75 and reading.crc_valid
    assert (overlay.calls, len(gpioinfo_calls)) == calls


def test_ownership_change_invalidates_before_bounded_read_only_recovery(tmp_path, monkeypatch):
    reader, overlay, _master = fixture_reader(tmp_path)
    clock = [100.0]
    monkeypatch.setattr('loggerhead.drivers.time.monotonic', lambda: clock[0])
    reader.configure_kernel_one_wire(17)
    overlay.token += 1
    with pytest.raises(HardwareUnavailable, match='ownership/topology changed'):
        reader.read_one_wire_gpio(17)
    assert overlay.calls == [('status', None)]
    with pytest.raises(HardwareUnavailable, match='bounded recovery'):
        reader.read_one_wire_gpio(17)
    clock[0] += 31
    assert reader.read_one_wire_gpio(17) == pytest.approx(76.55)
    assert overlay.calls == [('status', None), ('status', None)]
    clock[0] += 3600
    reader.read_one_wire_gpio(17)
    assert len(overlay.calls) == 2


def test_detached_master_recreated_with_same_name_is_not_trusted(tmp_path, monkeypatch):
    reader, overlay, master = fixture_reader(tmp_path, linked=False)
    reader.configure_kernel_one_wire(17)
    master.rename(tmp_path / 'old-master')
    make_bus(reader.one_wire_root, master.name, 17, linked=False)
    with pytest.raises(HardwareUnavailable, match='ownership/topology changed'):
        reader.read_one_wire_gpio(17)
    reader._kernel_recovery_after[17] = 0
    with pytest.raises(HardwareUnavailable, match='No verified kernel 1-Wire bus'):
        reader.read_one_wire_gpio(17)
    assert (17, master.name) in reader._rejected_runtime_masters
    assert not any(call[0] == 'activate' for call in overlay.calls)


def test_missing_bus_never_reuses_probe_from_another_gpio(tmp_path):
    reader, overlay, master = fixture_reader(tmp_path)
    reader.configure_kernel_one_wire(17)
    master.rename(tmp_path / 'old-master')
    make_bus(reader.one_wire_root, 'w1_bus_master3', 4)
    with pytest.raises(HardwareUnavailable, match='ownership/topology changed'):
        reader.read_one_wire_gpio(17)
    reader._kernel_recovery_after[17] = 0
    with pytest.raises(HardwareUnavailable, match='No verified kernel 1-Wire bus'):
        reader.read_one_wire_gpio(17)
    assert not any(call[0] == 'activate' for call in overlay.calls)


def test_changed_gpio_evidence_during_conversion_discards_result(tmp_path, monkeypatch):
    reader, _overlay, master = fixture_reader(tmp_path)
    reader.configure_kernel_one_wire(17)
    original = Path.read_text

    def changing_read(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.name == 'w1_slave':
            master.joinpath('device/of_node/gpios').write_bytes(b''.join(n.to_bytes(4, 'big') for n in (7, 4, 0)))
        return result

    monkeypatch.setattr(Path, 'read_text', changing_read)
    with pytest.raises(HardwareUnavailable, match='ownership/topology changed'):
        reader.read_one_wire_gpio(17)
    assert not reader._last_valid_kernel_celsius


def test_crc_faults_do_not_reconfigure_or_invoke_helper(tmp_path):
    reader, overlay, master = fixture_reader(tmp_path)
    reader.configure_kernel_one_wire(17)
    master.joinpath('28-00000ff27476/w1_slave').write_text('aa NO\nbb t=24750\n')
    for _ in range(3):
        with pytest.raises(HardwareUnavailable, match='CRC'):
            reader.read_one_wire_gpio(17)
    assert overlay.calls == [('status', None)]
    master.joinpath('28-00000ff27476/w1_slave').write_text('aa YES\nbb t=25000\n')
    assert reader.read_one_wire_gpio(17) == 77.0
    assert overlay.calls == [('status', None)]


def test_cancelled_conversion_cannot_publish_a_fresh_reading(tmp_path, monkeypatch):
    reader, _overlay, _master = fixture_reader(tmp_path)
    reader.configure_kernel_one_wire(17)
    stop = threading.Event()
    original = Path.read_text

    def cancelled_read(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.name == 'w1_slave':
            stop.set()
        return result

    monkeypatch.setattr(Path, 'read_text', cancelled_read)
    with pytest.raises(HardwareUnavailable, match='cancelled'):
        reader.read_one_wire_gpio_detailed(17, stop_event=stop)
    assert not reader._last_valid_kernel_celsius


def test_slow_conversion_does_not_hold_lifecycle_lock_or_delay_other_port(tmp_path, monkeypatch):
    reader, overlay, master = fixture_reader(tmp_path)
    other = make_bus(reader.one_wire_root, 'w1_bus_master2', 4)
    reader.configure_kernel_one_wire(17)
    reader.configure_kernel_one_wire(4)
    reader.complete_kernel_one_wire_configuration()
    entered, release = threading.Event(), threading.Event()
    original = Path.read_text
    results, errors = [], []

    def blocking_read(path, *args, **kwargs):
        if path == master / '28-00000ff27476/w1_slave':
            entered.set()
            assert release.wait(3)
        return original(path, *args, **kwargs)

    def slow_read():
        try:
            results.append(reader.read_one_wire_gpio(17))
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(Path, 'read_text', blocking_read)
    thread = threading.Thread(target=slow_read)
    thread.start()
    try:
        assert entered.wait(1)
        assert other.exists()
        assert reader.read_one_wire_gpio(4) == pytest.approx(76.55)
    finally:
        release.set()
        thread.join(3)
    assert results and not errors and not thread.is_alive()
    assert overlay.calls == [('status', None), ('status', None)]


def test_sensor_replacement_resets_power_up_default_validation(tmp_path, monkeypatch):
    reader, overlay, master = fixture_reader(tmp_path)
    reader.configure_kernel_one_wire(17)
    reader.read_one_wire_gpio(17)
    shutil.rmtree(master / '28-00000ff27476')
    master.joinpath('w1_master_slaves').write_text('28-000000000002\n')
    replacement = master / '28-000000000002'
    replacement.mkdir()
    replacement.joinpath('w1_slave').write_text('aa YES\nbb t=85000\n')
    monkeypatch.setattr('loggerhead.drivers.time.sleep', lambda _seconds: None)
    with pytest.raises(HardwareUnavailable, match='power-up default'):
        reader.read_one_wire_gpio(17)
    replacement.joinpath('w1_slave').write_text('aa YES\nbb t=25000\n')
    assert reader.read_one_wire_gpio(17) == 77.0
    assert overlay.calls == [('status', None)]


def test_captured_mapping_cannot_override_direct_physical_gpio_evidence(tmp_path):
    reader, overlay, master = fixture_reader(tmp_path, linked=False)
    shutil.rmtree(master)
    make_bus(reader.one_wire_root, master.name, 4)
    reader._runtime_kernel_bus_names[17] = master.name
    with pytest.raises(HardwareUnavailable, match='No verified kernel 1-Wire bus'):
        reader.read_one_wire_gpio(17)
    assert not overlay.calls


def test_runtime_mapping_cannot_assign_one_master_to_two_ports(tmp_path):
    reader, _overlay, _master = fixture_reader(tmp_path, linked=False)
    reader._runtime_kernel_bus_names = {17: 'w1_bus_master10', 4: 'w1_bus_master10'}
    with pytest.raises(HardwareUnavailable, match='No verified kernel 1-Wire bus'):
        reader.read_one_wire_gpio(17)


def test_recovery_helper_failure_is_bounded_and_never_falls_back_to_old_mapping(tmp_path, monkeypatch):
    reader, overlay, _master = fixture_reader(tmp_path)
    clock = [100.0]
    monkeypatch.setattr('loggerhead.drivers.time.monotonic', lambda: clock[0])
    reader.configure_kernel_one_wire(17)
    overlay.token += 1
    with pytest.raises(HardwareUnavailable):
        reader.read_one_wire_gpio(17)
    calls = []

    def fail_status():
        calls.append(True)
        raise TimeoutError('helper timeout')

    monkeypatch.setattr(overlay, 'status', fail_status)
    clock[0] = 131
    with pytest.raises(HardwareUnavailable, match='reverify'):
        reader.read_one_wire_gpio(17)
    for _ in range(3):
        with pytest.raises(HardwareUnavailable, match='bounded recovery'):
            reader.read_one_wire_gpio(17)
    assert calls == [True]
    assert not reader._verified_kernel_buses


def test_bus_membership_change_during_read_is_not_published(tmp_path, monkeypatch):
    reader, _overlay, master = fixture_reader(tmp_path)
    reader.configure_kernel_one_wire(17)
    original = Path.read_text

    def read_with_disconnect(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        if path.name == 'w1_slave':
            master.joinpath('w1_master_slaves').write_text('')
        return text

    monkeypatch.setattr(Path, 'read_text', read_with_disconnect)
    with pytest.raises(HardwareUnavailable, match='membership changed'):
        reader.read_one_wire_gpio(17)
    assert not reader._last_valid_kernel_celsius


def test_real_helper_client_has_zero_privileged_calls_in_steady_state(tmp_path, monkeypatch):
    reader, _overlay, _master = fixture_reader(tmp_path)
    state, overlays = tmp_path / 'owned.json', tmp_path / 'overlays'
    state.write_text('{}')
    overlays.mkdir()
    monkeypatch.setattr('loggerhead.onewire.os.geteuid', lambda: 1000, raising=False)
    calls = []

    def run(command, **_kwargs):
        calls.append(command[-1])
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'action': 'status'}), stderr='')

    reader._overlay_client = HelperOneWireOverlayClient(
        subprocess_run=run, state_path=state, overlays_path=overlays, masters_path=reader.one_wire_root,
    )
    clock = [100.0]
    monkeypatch.setattr('loggerhead.onewire.time.monotonic', lambda: clock[0])
    reader.configure_kernel_one_wire(17)
    for now in (100, 140, 3600, 86400):
        clock[0] = now
        assert reader.read_one_wire_gpio(17) == pytest.approx(76.55)
    assert calls == ['status']
    state.write_text('{"changed": true}')
    with pytest.raises(HardwareUnavailable, match='ownership/topology changed'):
        reader.read_one_wire_gpio(17)
    reader.read_one_wire_gpio(17)
    assert calls == ['status', 'status']


def test_release_invalidates_an_in_progress_conversion_before_handoff(tmp_path, monkeypatch):
    reader, overlay, _master = fixture_reader(tmp_path)
    reader.configure_kernel_one_wire(17)
    original = Path.read_text

    def read_while_released(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        if path.name == 'w1_slave':
            reader.release_kernel_one_wire(17)
        return text

    monkeypatch.setattr(Path, 'read_text', read_while_released)
    with pytest.raises(HardwareUnavailable, match='ownership/topology changed'):
        reader.read_one_wire_gpio(17)
    assert ('release', 17) in overlay.calls
    assert not reader._last_valid_kernel_celsius


def test_new_reader_verifies_startup_instead_of_reusing_process_mapping(tmp_path):
    reader, overlay, _master = fixture_reader(tmp_path, linked=False)
    reader.configure_kernel_one_wire(17)
    restarted = TemperatureReader(one_wire_root=reader.one_wire_root, platform_root=reader.platform_root, overlay_client=overlay)
    assert not restarted._verified_kernel_buses
    restarted.configure_kernel_one_wire(17)
    assert restarted.read_one_wire_gpio(17) == pytest.approx(76.55)
    assert overlay.calls == [('status', None), ('status', None)]


def test_detached_instance_change_between_discovery_and_snapshot_fails_closed(tmp_path):
    reader, _overlay, master = fixture_reader(tmp_path, linked=False)
    reader.configure_kernel_one_wire(17)
    verified = reader._verified_kernel_buses.pop(17)
    master.rename(tmp_path / 'old-master')
    make_bus(reader.one_wire_root, master.name, 17, linked=False)
    with pytest.raises(HardwareUnavailable, match='instance'):
        reader._remember_verified_kernel_bus(verified.bus)
    assert not reader._verified_kernel_buses
