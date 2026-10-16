"""Read-only Linux service CPU/helper benchmark; never commands equipment."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, timeout=10).strip()


def usage(cgroup: str) -> int:
    fields = dict(line.split() for line in (Path('/sys/fs/cgroup') / cgroup.lstrip('/') / 'cpu.stat').read_text().splitlines())
    return int(fields['usage_usec'])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=90)
    parser.add_argument('--url', default='http://127.0.0.1:8080/api/status')
    args = parser.parse_args()
    pid = command('systemctl', 'show', 'loggerhead', '-p', 'MainPID', '--value')
    cgroup = command('systemctl', 'show', 'loggerhead', '-p', 'ControlGroup', '--value')
    start_wall, start, cpu = time.time(), time.monotonic(), usage(cgroup)
    samples = []
    while time.monotonic() - start < args.seconds:
        before = time.monotonic()
        with urllib.request.urlopen(args.url, timeout=10) as response:
            status = json.load(response)
        samples.append({'status_ms': round((time.monotonic()-before)*1000, 2),
                        'sensor_health': {key: {'status': row['status'], 'last_success_ts': row['last_success_ts']}
                                          for key, row in status.get('sensor_health', {}).items()},
                        'active_pump': status.get('stepper_active')})
        remaining = args.seconds - (time.monotonic() - start)
        if remaining > 0:
            time.sleep(min(10, remaining))
    elapsed, used, end_wall = time.monotonic()-start, usage(cgroup)-cpu, time.time()
    if command('systemctl', 'show', 'loggerhead', '-p', 'MainPID', '--value') != pid:
        raise RuntimeError('Service restarted during measurement; discard this sample.')
    logs = command('journalctl', '-u', 'loggerhead', '--since', f'@{start_wall:.6f}', '--until', f'@{end_wall:.6f}', '-o', 'json', '--no-pager')
    invocations = []
    for line in logs.splitlines():
        row = json.loads(line)
        match = re.search(r'COMMAND=.*loggerhead-onewire-helper (status|activate|release)(?:\s|$)', row.get('MESSAGE', ''))
        if match:
            invocations.append({'operation': match[1], 'ts': int(row['__REALTIME_TIMESTAMP'])/1e6})
    print(json.dumps({'pid': int(pid), 'elapsed_seconds': round(elapsed, 3), 'cpu_seconds': round(used/1e6, 4),
                      'service_cpu_percent_one_core': round(used/1e6/elapsed*100, 3),
                      'helper_invocations': invocations, 'samples': samples}, indent=2))


if __name__ == '__main__':
    main()
