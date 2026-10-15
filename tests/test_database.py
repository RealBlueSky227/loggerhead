from __future__ import annotations

import sqlite3
import time

import pytest

from loggerhead.database import TelemetryStore


def test_batched_samples_preserve_timestamps_units_types_and_catalog(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.stream_catalog()
    samples = [("temperature.water", 78.2, "F", 10.5), ("water.sump", "normal", "", 11.5)]
    store.log_samples(iter(samples))
    history = store.history([s[0] for s in samples], start_ts=0, end_ts=20)
    assert history["temperature.water"][0] == {"ts": 10.5, "value": 78.2, "unit": "F"}
    assert history["water.sump"][0] == {"ts": 11.5, "value": "normal", "unit": ""}
    assert {item["stream"] for item in store.stream_catalog()} == {s[0] for s in samples}


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


def test_history_is_bounded_and_keeps_endpoints(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "telemetry.db")
    with store.connect() as conn:
        conn.executemany(
            "INSERT INTO telemetry(ts, stream, value) VALUES (?, 'temperature.water', ?)",
            ((index, index % 100) for index in range(10000)),
        )
    result = store.history(["temperature.water"], start_ts=0, end_ts=9999, max_points=80)["temperature.water"]
    assert len(result) <= 80
    assert result[0]["ts"] == 0
    assert result[-1]["ts"] == 9999
    assert result == sorted(result, key=lambda point: point["ts"])
    assert store.stream_catalog()[0]["count"] == 10000


def test_history_single_equal_and_duplicate_times(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.log_value("single", 42, ts=0)
    for _ in range(100):
        store.log_value("equal", 42, ts=1)
    result = store.history(["single", "equal"], start_ts=0, end_ts=1, max_points=10)
    assert result["single"] == [{"ts": 0, "value": 42, "unit": ""}]
    assert 1 <= len(result["equal"]) <= 10
    assert all(point["value"] == 42 for point in result["equal"])


def test_catalog_cache_and_shape_invalidation(tmp_path, monkeypatch) -> None:
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.log_value("temperature.water", 78, unit="F", ts=1)
    original = store.connect
    calls = []

    def connect():
        calls.append(True)
        return original()

    monkeypatch.setattr(store, "connect", connect)
    first = store.stream_catalog()
    first[0]["stream"] = "changed by client"
    assert store.stream_catalog()[0]["stream"] == "temperature.water"
    assert len(calls) == 1
    store.log_value("temperature.water", 79, unit="F", ts=2)
    before = len(calls)
    store.stream_catalog()
    assert len(calls) == before  # Regular telemetry does not defeat the TTL.
    store.log_many([("water.level", "dry", "")])
    assert {item["stream"] for item in store.stream_catalog()} == {"temperature.water", "water.level"}
    store.log_value("temperature.water", "error", unit="F")
    assert store.stream_catalog()[0]["kind"] == "mixed"
    store.log_value("temperature.water", 25, unit="C")
    assert store.stream_catalog()[0]["unit"] == "C"
    store._catalog_expires = 0
    before = len(calls)
    store.stream_catalog()
    assert len(calls) == before + 1


def test_legacy_database_catalog_backfill_and_retention(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    store = TelemetryStore(path)
    store.log_value("temperature.water", 78, unit="F", ts=1)
    store.log_value("temperature.water", 25, unit="C", ts=2)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "DROP TRIGGER telemetry_catalog_insert; DROP TRIGGER telemetry_catalog_delete; DROP TABLE telemetry_streams;"
        )
    migrated = TelemetryStore(path)
    assert migrated.stream_catalog()[0]["unit"] == "C"
    assert migrated.stream_catalog()[0]["count"] == 2
    with migrated.connect() as conn:
        conn.execute("DELETE FROM telemetry WHERE ts = 2")
    migrated._catalog_expires = 0
    assert migrated.stream_catalog()[0]["unit"] == "F"
    assert migrated.stream_catalog()[0]["count"] == 1
    with migrated.connect() as conn:
        conn.execute("DELETE FROM telemetry")
    migrated._catalog_expires = 0
    assert migrated.stream_catalog() == []


def test_history_limits_and_invalid_ranges(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "telemetry.db")
    with pytest.raises(ValueError, match="at most"):
        store.history([str(index) for index in range(13)], start_ts=0)
    for start, end in [(float("nan"), 1), (0, float("inf")), (2, 1)]:
        with pytest.raises(ValueError, match="finite"):
            store.history(["temperature.water"], start_ts=start, end_ts=end)


def test_database_connections_close_on_success_and_error(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "telemetry.db")
    with store.connect() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    with pytest.raises(RuntimeError), store.connect() as failed:
        raise RuntimeError("test rollback")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed.execute("SELECT 1")
