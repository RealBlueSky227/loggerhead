from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .hardware import AlarmPriority, LevelState

LOGGER = logging.getLogger(__name__)


@dataclass
class EquipmentState:
    id: str
    on: bool
    source: str = "startup"
    verified_on: bool | None = None
    voltage: float | None = None
    current: float | None = None
    power: float | None = None
    energy: float | None = None


@dataclass
class SensorReading:
    id: str
    value: float | str
    unit: str
    ok: bool = True
    ts: float = 0.0


@dataclass
class SensorHealth:
    id: str
    status: str = "initializing"
    worker_state: str = "starting"
    data_status: str = "initializing"
    last_success_ts: float = 0.0
    last_attempt_ts: float = 0.0
    read_duration_seconds: float = 0.0
    next_scheduled_ts: float = 0.0
    last_error: str = ""
    consecutive_failures: int = 0
    stale_after_seconds: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class AlarmState:
    id: str
    message: str
    priority: AlarmPriority
    active: bool = True
    first_seen: float = 0.0
    last_notified: float = 0.0


@dataclass
class ATOState:
    id: str
    running: bool = False
    faulted: bool = False
    started_at: float | None = None


@dataclass
class RuntimeState:
    """Persistent runtime snapshot.

    Implements SRS 6.3 State Persistence and Reboot Recovery.
    """

    equipment: dict[str, EquipmentState] = field(default_factory=dict)
    readings: dict[str, SensorReading] = field(default_factory=dict)
    sensor_health: dict[str, SensorHealth] = field(default_factory=dict)
    water_levels: dict[str, LevelState] = field(default_factory=dict)
    alarms: dict[str, AlarmState] = field(default_factory=dict)
    ato: dict[str, ATOState] = field(default_factory=dict)
    buzzer_muted_until: float = 0.0
    stepper_active: str | None = None
    manual_priming: dict[str, bool] = field(default_factory=dict)


class StateStore:
    def __init__(self, path: Path, *, state_lock: threading.RLock | None = None) -> None:
        self.path = path
        self._state_lock = state_lock or threading.RLock()
        self._write_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> RuntimeState:
        if not self.path.exists():
            return RuntimeState()
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        state = RuntimeState()
        state.equipment = {
            key: EquipmentState(**value) for key, value in data.get("equipment", {}).items()
        }
        state.readings = {
            key: SensorReading(**value) for key, value in data.get("readings", {}).items()
        }
        state.sensor_health = {
            key: SensorHealth(**value) for key, value in data.get("sensor_health", {}).items()
        }
        state.water_levels = {
            key: LevelState(value) for key, value in data.get("water_levels", {}).items()
        }
        state.alarms = {
            key: AlarmState(priority=AlarmPriority(value["priority"]), **{k: v for k, v in value.items() if k != "priority"})
            for key, value in data.get("alarms", {}).items()
        }
        state.ato = {key: ATOState(**value) for key, value in data.get("ato", {}).items()}
        state.buzzer_muted_until = data.get("buzzer_muted_until", 0.0)
        state.stepper_active = data.get("stepper_active")
        state.manual_priming = data.get("manual_priming", {})
        return state

    def save(self, state: RuntimeState) -> None:
        # Lock order is state snapshot first, then file write. Callers may already
        # hold _state_lock; _write_lock is private to StateStore to avoid deadlocks.
        with self._state_lock:
            payload = _state_dict(state)
        with self._write_lock:
            self._cleanup_abandoned_temps()
            tmp_name = ""
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f"{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    tmp_name = handle.name
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self.path)
                tmp_name = ""
                _fsync_directory(self.path.parent)
            except Exception:
                LOGGER.exception("Failed to persist Loggerhead runtime state to %s.", self.path)
                if tmp_name:
                    try:
                        Path(tmp_name).unlink(missing_ok=True)
                    except Exception:
                        LOGGER.warning("Could not remove abandoned state temp file %s.", tmp_name, exc_info=True)
                raise

    def _cleanup_abandoned_temps(self) -> None:
        for path in self.path.parent.glob(f"{self.path.name}.*.tmp"):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                LOGGER.warning("Could not remove abandoned state temp file %s.", path, exc_info=True)


def _state_dict(state: RuntimeState) -> dict[str, Any]:
    last_error: RuntimeError | None = None
    for _ in range(10):
        try:
            data = asdict(state)
            data["water_levels"] = {key: value.value for key, value in state.water_levels.items()}
            data["alarms"] = {
                key: {**asdict(value), "priority": value.priority.value} for key, value in state.alarms.items()
            }
            return data
        except RuntimeError as exc:
            if "dictionary changed size during iteration" not in str(exc):
                raise
            last_error = exc
            time.sleep(0.001)
    raise last_error or RuntimeError("Runtime state changed while it was being serialized.")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
