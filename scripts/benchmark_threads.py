"""Read-only /proc thread CPU sampler and named-operation observer for Linux."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path


def read_threads(pid):
    result = {}
    for path in (Path('/proc') / str(pid) / 'task').iterdir():
        try:
            text = path.joinpath('stat').read_text()
            start, end = text.index('('), text.rindex(')')
            fields = text[end+2:].split()
            switches = {line.split(':')[0]: int(line.split(':')[1]) for line in path.joinpath('status').read_text().splitlines()
                        if line.startswith(('voluntary_ctxt_switches:', 'nonvoluntary_ctxt_switches:'))}
            result[int(path.name)] = {'comm': text[start+1:end], 'state': fields[0], 'ticks': int(fields[11])+int(fields[12]),
                                      'start': int(fields[19]), 'switches': sum(switches.values()),
                                      'wait': path.joinpath('wchan').read_text().strip()}
        except (OSError, ValueError, IndexError):
            continue
    return result


def get_json(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--url', default='http://127.0.0.1:8080')
    args = parser.parse_args()
    start, hz = time.monotonic(), os.sysconf('SC_CLK_TCK')
    previous, totals, names, errors, snapshots = read_threads(args.pid), {}, {}, [], []
    try:
        initial = get_json(args.url.rstrip('/')+'/api/performance')
    except Exception as exc:
        initial = {}
        errors.append(str(exc))
    main_start = previous.get(args.pid, {}).get('start')
    max_threads = len(previous)
    while time.monotonic() - start < args.seconds:
        time.sleep(min(1, max(0, args.seconds - (time.monotonic()-start))))
        current = read_threads(args.pid)
        if current.get(args.pid, {}).get('start') != main_start:
            raise RuntimeError('Controller restarted during sampling; discard the measurement.')
        max_threads = max(max_threads, len(current))
        for tid, row in current.items():
            before = previous.get(tid)
            entry = totals.setdefault((tid, row['start']), {'tid': tid, 'cpu_seconds': 0., 'switches': 0, 'samples': 0, 'wait_states': {}})
            if before and before['start'] == row['start']:
                entry['cpu_seconds'] += (row['ticks'] - before['ticks'])/hz
                entry['switches'] += max(0, row['switches'] - before['switches'])
            entry['samples'] += 1
            entry['wait_states'][row['wait']] = entry['wait_states'].get(row['wait'], 0) + 1
        previous = current
        if len(snapshots) < 1 or time.monotonic() - snapshots[-1]['observed_mono'] >= 10:
            try:
                snapshot = get_json(args.url.rstrip('/')+'/api/performance?stacks=1')
                names.update({row['native_id']: row for row in snapshot['threads']})
                snapshots.append({'observed_mono': time.monotonic(), **snapshot})
            except Exception as exc:
                errors.append(str(exc))
                snapshots.append({'observed_mono': time.monotonic(), 'unavailable': str(exc)})
    elapsed = time.monotonic()-start
    try:
        final = get_json(args.url.rstrip('/')+'/api/performance?stacks=1')
        names.update({row['native_id']: row for row in final['threads']})
    except Exception as exc:
        final = snapshots[-1] if snapshots else {}
        errors.append(str(exc))
    operations = {}
    for name, row in final.get('operations', {}).items():
        before = initial.get('operations', {}).get(name, {})
        operations[name] = {key: row[key]-before.get(key, 0) for key in ('count', 'errors', 'wall_seconds', 'cpu_seconds')}
        operations[name]['recent_p95_seconds'] = row['recent_p95_seconds']
    for row in totals.values():
        row['cpu_percent_one_core'] = round(row['cpu_seconds']/elapsed*100, 3)
        row['cpu_seconds'] = round(row['cpu_seconds'], 4)
        row.update({key: names.get(row['tid'], {}).get(key) for key in ('name', 'type', 'roles', 'stack', 'active_operations')})
    print(json.dumps({'elapsed_seconds': round(elapsed,3), 'pid': args.pid, 'max_os_threads': max_threads,
                      'threads': sorted(totals.values(), key=lambda row: -row['cpu_seconds']),
                      'operations_delta': operations, 'errors': errors,
                      'note': 'Exited short-lived request threads can be missed by /proc sampling; HTTP CPU is recorded separately. Operation deltas cover this interval; p95 uses the latest 128 completions.'}, indent=2))


if __name__ == '__main__':
    main()
