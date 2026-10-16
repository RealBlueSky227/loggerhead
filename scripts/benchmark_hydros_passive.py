"""Observe GPIO17 only: no pin setup, service restart, config edits, or equipment IO."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.request
from pathlib import Path

from loggerhead.drivers import HydrosPulseReader, HydrosTripleClassifier
from loggerhead.hardware import SENSE_PORTS


def get_json(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def ticks(pid, tid=None):
    path = Path(f'/proc/{pid}/task/{tid}/stat' if tid else f'/proc/{pid}/stat')
    fields = path.read_text().rsplit(')', 1)[1].split()
    return int(fields[11]) + int(fields[12])


def system_cpu():
    values = [int(value) for value in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    return sum(values), values[3]+values[4]


def counters(pid, hydros_tid, observer_tid=None):
    total, idle = system_cpu()
    return dict(at=time.monotonic(), controller=ticks(pid), hydros=ticks(pid, hydros_tid),
                observer=time.process_time(), notification=ticks(os.getpid(), observer_tid) if observer_tid else 0,
                system_total=total, system_idle=idle)


def difference(before, after):
    elapsed, hz = after['at']-before['at'], os.sysconf('SC_CLK_TCK')
    return dict(elapsed_seconds=elapsed,
                controller_cpu_percent=100*(after['controller']-before['controller'])/hz/elapsed,
                production_hydros_cpu_percent=100*(after['hydros']-before['hydros'])/hz/elapsed,
                observer_process_cpu_percent=100*(after['observer']-before['observer'])/elapsed,
                observer_notification_cpu_percent=100*(after['notification']-before['notification'])/hz/elapsed,
                system_busy_percent=100*(1-(after['system_idle']-before['system_idle'])/
                                         max(1, after['system_total']-before['system_total'])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--passive-gpio17', action='store_true', required=True)
    parser.add_argument('--url', default='http://127.0.0.1:8080')
    parser.add_argument('--seconds', type=float, default=24)
    parser.add_argument('--interval', type=float, default=4)
    args = parser.parse_args()
    if SENSE_PORTS[2].digital_bcm != 17:
        raise RuntimeError('SensePort map no longer associates port 2 with GPIO17: review this fixed-target benchmark.')
    if not 8 <= args.seconds <= 120 or not 1 <= args.interval <= 10:
        parser.error('Use 8-120 seconds and a 1-10 second interval.')
    base = args.url.rstrip('/')
    status, performance = get_json(base+'/api/status'), get_json(base+'/api/performance')
    port = next(row for row in status['config']['sense_ports'] if row['number'] == 2)
    if port['device'] != 'hydros_triple':
        raise RuntimeError('Refusing measurement: SensePort 2 is not configured as HYDROS.')
    pid = performance['pid']
    tid = next(row['native_id'] for row in performance['threads']
               if 'HYDROS sense_port_2_water pigpio notifications' in row.get('roles', []))
    before = counters(pid, tid)
    time.sleep(args.seconds)
    baseline = difference(before, counters(pid, tid))
    reader = HydrosPulseReader(17, HydrosTripleClassifier(), configure_gpio=False)
    if reader.pi.get_mode(17) != 0:
        reader.close()
        raise RuntimeError('Refusing measurement: GPIO17 is not already an input.')
    observer_tid = reader.pi._notify.native_id
    cycles, idle_cpu, latencies = [], 0., []
    try:
        before = counters(pid, tid, observer_tid)
        next_due, finish = time.monotonic(), time.monotonic()+args.seconds
        while time.monotonic() < finish:
            state = reader.read_state()
            diagnostics = reader.diagnostics()
            if reader.pi._notify.monitor != 0 or diagnostics['notifications_subscribed']:
                raise RuntimeError('Notifications are still subscribed between measurements.')
            latencies.append(diagnostics['last_acquisition_seconds'])
            cycles.append({**diagnostics, 'state': state.value})
            next_due += args.interval
            idle_start = ticks(os.getpid(), observer_tid)
            time.sleep(max(0, min(next_due, finish)-time.monotonic()))
            idle_cpu += (ticks(os.getpid(), observer_tid)-idle_start)/os.sysconf('SC_CLK_TCK')
        measured = difference(before, counters(pid, tid, observer_tid))
        end = get_json(base+'/api/status')
        end_pid = get_json(base+'/api/performance')['pid']
        if end_pid != pid or next(row for row in end['config']['sense_ports'] if row['number'] == 2) != port:
            raise RuntimeError('Production restarted or target settings changed: discard comparison.')
        print(json.dumps(dict(baseline_production=baseline, production_plus_passive_reader=measured,
                              cycles=cycles, idle_notification_cpu_seconds=idle_cpu,
                              latency_median_ms=statistics.median(latencies)*1000,
                              latency_max_ms=max(latencies)*1000,
                              production_sensor_after=end['sensor_health']['sense_port_2_water'],
                              note='Production was never changed. Its continuous reader remains running in both phases. '
                                   'Observer CPU/latency is measured, not a deployed total-system improvement; '
                                   'no ATO equipment test was performed.'), indent=2))
    finally:
        reader.close()


if __name__ == '__main__':
    main()
