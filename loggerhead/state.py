from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .hardware import AlarmPriority, LevelState


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
    water_levels: dict[str, LevelState] = field(default_factory=dict)
    alarms: dict[str, AlarmState] = field(default_factory=dict)
    ato: dict[str, ATOState] = field(default_factory=dict)
    buzzer_muted_until: float = 0.0
    stepper_active: str | None = None


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
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
        return state

    def save(self, state: RuntimeState) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(_state_dict(state), handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(self.path)


def _state_dict(state: RuntimeState) -> dict[str, Any]:
    data = asdict(state)
    data["water_levels"] = {key: value.value for key, value in state.water_levels.items()}
    data["alarms"] = {
        key: {**asdict(value), "priority": value.priority.value} for key, value in state.alarms.items()
    }
    return data
