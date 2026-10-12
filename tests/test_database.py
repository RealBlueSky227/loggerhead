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


def test_stream_catalog_discovers_numeric_and_categorical_history(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "loggerhead.sqlite3")
    store.log_value("temperature.sense_port_1_temperature", 78.2, unit="F", ts=100.0)
    store.log_value("ph.probe", 8.1, unit="pH", ts=101.0)
    store.log_value("analog.sense_port_3_analog", 1.25, unit="V", ts=102.0)
    store.log_value("health.cpu_temperature", 110.0, unit="F", ts=103.0)
    store.log_value("equipment.ac1", 1, ts=104.0)
    store.log_value("water.sense_port_2_water", "dry", ts=105.0)

    catalog = {item["stream"]: item for item in store.stream_catalog()}

    assert catalog["temperature.sense_port_1_temperature"]["kind"] == "numeric"
    assert catalog["temperature.sense_port_1_temperature"]["unit"] == "F"
    assert catalog["ph.probe"]["kind"] == "numeric"
    assert catalog["analog.sense_port_3_analog"]["kind"] == "numeric"
    assert catalog["health.cpu_temperature"]["kind"] == "numeric"
    assert catalog["equipment.ac1"]["kind"] == "numeric"
    assert catalog["water.sense_port_2_water"]["kind"] == "categorical"
    assert catalog["water.sense_port_2_water"]["count"] == 1


def test_history_returns_categorical_values(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "loggerhead.sqlite3")
    store.log_value("water.sense_port_2_water", "dry", ts=100.0)
    store.log_value("water.sense_port_2_water", "normal", ts=101.0)

    history = store.history(["water.sense_port_2_water"], start_ts=99.0, end_ts=102.0)

    assert [point["value"] for point in history["water.sense_port_2_water"]] == ["dry", "normal"]


def test_event_logging(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "loggerhead.sqlite3")
    store.log_event("equipment", "Pump on", {"id": "pump"})
    events = store.recent_events()
    assert events[0]["category"] == "equipment"
