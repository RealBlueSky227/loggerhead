from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

LOGGER = logging.getLogger(__name__)


@dataclass
class SensorReadResult:
    sensor_id: str
    kind: str
    value: Any
    unit: str = ""
    data_status: str = "online"
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SensorWorkerSnapshot:
    sensor_id: str
    kind: str
    worker_state: str = "starting"
    status: str = "initializing"
    data_status: str = "initializing"
    value: Any = None
    unit: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    last_attempt_ts: float = 0.0
    last_attempt_monotonic: float = 0.0
    last_success_ts: float = 0.0
    last_success_monotonic: float = 0.0
    read_started_ts: float = 0.0
    read_started_monotonic: float = 0.0
    read_duration_seconds: float = 0.0
    next_scheduled_ts: float = 0.0
    next_scheduled_monotonic: float = 0.0
    consecutive_failures: int = 0
    stale_after_seconds: float = 0.0
    check_frequency: float = 0.0
    thread_alive: bool = False


@dataclass(frozen=True)
class SensorWorkerSpec:
    sensor_id: str
    kind: str
    check_frequency: float
    stale_after_seconds: float
    read: Callable[[threading.Event], SensorReadResult]
    close: Callable[[], None] | None = None
    read_timeout_seconds: float | None = None


class SensorWorker:
    """Persistent periodic worker for one blocking sensor acquisition path."""

    def __init__(self, spec: SensorWorkerSpec, *, stop_event: threading.Event | None = None) -> None:
        self.spec = spec
        self._stop = stop_event or threading.Event()
        self._lock = threading.RLock()
        self._snapshot = SensorWorkerSnapshot(
            sensor_id=spec.sensor_id,
            kind=spec.kind,
            stale_after_seconds=spec.stale_after_seconds,
            check_frequency=spec.check_frequency,
        )
        self._thread: threading.Thread | None = None

    @property
    def sensor_id(self) -> str:
        return self.spec.sensor_id

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name=f"sensor-{self.spec.sensor_id}", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        try:
            if self.spec.close is not None:
                self.spec.close()
        except Exception:
            LOGGER.warning("Sensor %s cleanup failed.", self.spec.sensor_id, exc_info=True)
        alive = bool(thread and thread.is_alive())
        with self._lock:
            self._snapshot.worker_state = "failed" if alive else "stopped"
            self._snapshot.thread_alive = alive
            if alive:
                self._snapshot.error = "Worker did not stop before timeout."
                self._snapshot.data_status = "stale"
                self._snapshot.status = "stale"
        return not alive

    def snapshot(self) -> SensorWorkerSnapshot:
        now = time.monotonic()
        with self._lock:
            copy = SensorWorkerSnapshot(**self._snapshot.__dict__)
            copy.metadata = dict(self._snapshot.metadata)
        thread = self._thread
        copy.thread_alive = bool(thread and thread.is_alive())
        if copy.last_success_monotonic and now - copy.last_success_monotonic > copy.stale_after_seconds:
            copy.data_status = "stale"
            copy.status = "stale"
            copy.error = copy.error or f"No successful reading for {now - copy.last_success_monotonic:.1f}s."
        timeout = self.spec.read_timeout_seconds or copy.stale_after_seconds
        if copy.worker_state == "reading" and copy.read_started_monotonic and now - copy.read_started_monotonic > timeout:
            copy.data_status = "stale"
            copy.status = "stale"
            copy.error = f"Read in progress for {now - copy.read_started_monotonic:.1f}s."
        return copy

    def _run(self) -> None:
        next_due = time.monotonic()
        with self._lock:
            self._snapshot.worker_state = "running"
        try:
            while not self._stop.is_set():
                wait_for = max(0.0, next_due - time.monotonic())
                if self._stop.wait(wait_for):
                    break
                attempt_wall = time.time()
                attempt_mono = time.monotonic()
                with self._lock:
                    self._snapshot.worker_state = "reading"
                    self._snapshot.last_attempt_ts = attempt_wall
                    self._snapshot.last_attempt_monotonic = attempt_mono
                    self._snapshot.read_started_ts = attempt_wall
                    self._snapshot.read_started_monotonic = attempt_mono
                    self._snapshot.next_scheduled_monotonic = attempt_mono + max(0.1, self.spec.check_frequency)
                    self._snapshot.next_scheduled_ts = attempt_wall + max(0.1, self.spec.check_frequency)
                try:
                    result = self.spec.read(self._stop)
                    self._record_success(result, attempt_mono)
                except Exception as exc:
                    self._record_failure(exc, attempt_mono)
                next_due = attempt_mono + max(0.1, self.spec.check_frequency)
                with self._lock:
                    if self._snapshot.worker_state != "failed":
                        self._snapshot.worker_state = "running"
        except Exception as exc:
            LOGGER.exception("Sensor worker %s crashed: %s", self.spec.sensor_id, exc)
            with self._lock:
                self._snapshot.worker_state = "failed"
                self._snapshot.status = "failed"
                self._snapshot.data_status = "stale"
                self._snapshot.error = str(exc)
        finally:
            with self._lock:
                if self._snapshot.worker_state != "failed":
                    self._snapshot.worker_state = "stopped"

    def _record_success(self, result: SensorReadResult, started_mono: float) -> None:
        finished_mono = time.monotonic()
        finished_wall = time.time()
        status = result.data_status or "online"
        with self._lock:
            self._snapshot.value = result.value
            self._snapshot.unit = result.unit
            self._snapshot.metadata = dict(result.metadata)
            self._snapshot.data_status = status
            self._snapshot.status = status
            self._snapshot.error = result.error
            self._snapshot.last_success_ts = finished_wall if status == "online" else self._snapshot.last_success_ts
            self._snapshot.last_success_monotonic = finished_mono if status == "online" else self._snapshot.last_success_monotonic
            self._snapshot.read_duration_seconds = max(0.0, finished_mono - started_mono)
            self._snapshot.consecutive_failures = 0 if status == "online" else self._snapshot.consecutive_failures + 1
            self._snapshot.read_started_ts = 0.0
            self._snapshot.read_started_monotonic = 0.0

    def _record_failure(self, exc: Exception, started_mono: float) -> None:
        finished_mono = time.monotonic()
        with self._lock:
            status = "disconnected" if exc.__class__.__name__ == "HardwareUnavailable" else "read_error"
            self._snapshot.status = status
            self._snapshot.data_status = status
            self._snapshot.error = str(exc)
            self._snapshot.read_duration_seconds = max(0.0, finished_mono - started_mono)
            self._snapshot.consecutive_failures += 1
            self._snapshot.read_started_ts = 0.0
            self._snapshot.read_started_monotonic = 0.0
        LOGGER.warning("Sensor %s read failed: %s", self.spec.sensor_id, exc)
