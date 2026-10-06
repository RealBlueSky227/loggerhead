from __future__ import annotations

import time

from loggerhead.database import TelemetryStore


def test_history_downsamples(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "loggerhead.sqlite3")
    start = time.time() - 10
    for index in range(100):
        store.log_value("temperature.water", index, ts=start + index)
    history = store.history(["temperature.water"], start_ts=start, max_points=10)
    assert len(history["temperature.water"]) == 10


def test_event_logging(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "loggerhead.sqlite3")
    store.log_event("equipment", "Pump on", {"id": "pump"})
    events = store.recent_events()
    assert events[0]["category"] == "equipment"
