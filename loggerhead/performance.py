"""Bounded, read-only runtime diagnostics; never takes controller/device locks."""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


class PerformanceMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._operations = {}
        self._active = {}

    @contextmanager
    def measure(self, name):
        start, cpu = time.monotonic(), time.thread_time()
        ident = threading.get_ident()
        with self._lock:
            if name not in self._operations and len(self._operations) >= 128:
                name = 'other'
            self._active.setdefault(ident, []).append((name, start))
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            elapsed, used = time.monotonic() - start, time.thread_time() - cpu
            with self._lock:
                stack = self._active.get(ident, [])
                if stack:
                    stack.pop()
                if not stack:
                    self._active.pop(ident, None)
                row = self._operations.setdefault(name, {
                    'count': 0, 'errors': 0, 'wall_seconds': 0., 'cpu_seconds': 0.,
                    'max_seconds': 0., 'last_finished_ts': 0., 'recent': deque(maxlen=128),
                })
                row['count'] += 1
                row['errors'] += int(failed)
                row['wall_seconds'] += elapsed
                row['cpu_seconds'] += used
                row['max_seconds'] = max(row['max_seconds'], elapsed)
                row['last_finished_ts'] = time.time()
                row['recent'].append(elapsed)

    def snapshot(self, *, stacks=False):
        now = time.monotonic()
        with self._lock:
            active = {ident: [{'operation': name, 'elapsed_seconds': now - start} for name, start in stack]
                      for ident, stack in self._active.items()}
            operations = {}
            for name, row in self._operations.items():
                samples = sorted(row['recent'])
                operations[name] = {key: value for key, value in row.items() if key != 'recent'}
                operations[name]['recent_p95_seconds'] = samples[min(len(samples)-1, int(len(samples)*.95))] if samples else 0
        frames = sys._current_frames() if stacks else {}
        threads = []
        for thread in threading.enumerate()[:128]:
            row = {'name': thread.name, 'native_id': thread.native_id, 'ident': thread.ident,
                   'daemon': thread.daemon, 'type': f'{type(thread).__module__}.{type(thread).__name__}',
                   'active_operations': active.get(thread.ident, [])}
            if stacks:
                frame, trace = frames.get(thread.ident), []
                while frame is not None and len(trace) < 16:
                    trace.append({'file': Path(frame.f_code.co_filename).name,
                                  'function': frame.f_code.co_name, 'line': frame.f_lineno})
                    frame = frame.f_back
                row['stack'] = trace
            threads.append(row)
        del frames
        return {'pid': os.getpid(), 'time': time.time(), 'threads': threads, 'operations': operations,
                'note': 'Inclusive operation timings overlap; do not sum nested scopes as total CPU.'}


def measured(name):
    def decorate(function):
        @wraps(function)
        def invoke(self, *args, **kwargs):
            with self.performance.measure(name):
                return function(self, *args, **kwargs)
        return invoke
    return decorate
