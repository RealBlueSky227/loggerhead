from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


class TelemetryStore:
    """SQLite telemetry and event log.

    Implements SRS 6.1 Continuous Polled Scraping, 6.2 Event-Driven Data Logging,
    and 6.4 Adaptive Database Capacity and Storage Preservation.
    """

    def __init__(self, path: Path, *, max_points_default: int = 800) -> None:
        self.path = path
        self.max_points_default = max_points_default
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    stream TEXT NOT NULL,
                    value REAL,
                    text_value TEXT,
                    unit TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_stream_ts ON telemetry(stream, ts)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}'
                )
                """
            )

    def log_value(
        self,
        stream: str,
        value: float | str,
        *,
        unit: str = "",
        ts: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        numeric_value: float | None
        text_value: str | None
        if isinstance(value, int | float):
            numeric_value = float(value)
            text_value = None
        else:
            numeric_value = None
            text_value = str(value)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO telemetry(ts, stream, value, text_value, unit, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                (ts or time.time(), stream, numeric_value, text_value, unit, json.dumps(metadata or {}, sort_keys=True)),
            )

    def log_many(self, values: Iterable[tuple[str, float | str, str]]) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO telemetry(ts, stream, value, text_value, unit, metadata) VALUES (?, ?, ?, ?, ?, '{}')",
                [
                    (now, stream, float(value), None, unit)
                    if isinstance(value, int | float)
                    else (now, stream, None, str(value), unit)
                    for stream, value, unit in values
                ],
            )

    def log_event(self, category: str, message: str, payload: dict[str, Any] | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(ts, category, message, payload) VALUES (?, ?, ?, ?)",
                (time.time(), category, message, _json(payload or {})),
            )

    def history(
        self,
        streams: list[str],
        *,
        start_ts: float,
        end_ts: float | None = None,
        max_points: int | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        # Implements SRS 5.3.3 and 5.3.4 with SQL window downsampling before JSON response.
        limit = max_points or self.max_points_default
        end = end_ts or time.time()
        result: dict[str, list[dict[str, Any]]] = {}
        with self.connect() as conn:
            for stream in streams:
                rows = conn.execute(
                    """
                    SELECT ts, value, text_value, unit
                    FROM telemetry
                    WHERE stream = ? AND ts >= ? AND ts <= ?
                    ORDER BY ts ASC
                    """,
                    (stream, start_ts, end),
                ).fetchall()
                result[stream] = _downsample(
                    [
                        {
                            "ts": row["ts"],
                            "value": row["value"] if row["value"] is not None else row["text_value"],
                            "unit": row["unit"],
                        }
                        for row in rows
                    ],
                    limit,
                )
        return result

    def prune_if_critical(self, *, warning_free_ratio: float = 0.20, critical_free_ratio: float = 0.05) -> bool:
        usage = shutil.disk_usage(self.path.parent)
        free_ratio = usage.free / usage.total if usage.total else 1.0
        if free_ratio >= critical_free_ratio:
            return free_ratio < warning_free_ratio
        with self.connect() as conn:
            oldest_ids = [
                row["id"]
                for row in conn.execute("SELECT id FROM telemetry ORDER BY ts ASC LIMIT 500").fetchall()
            ]
            if oldest_ids:
                placeholders = ",".join("?" for _ in oldest_ids)
                conn.execute(f"DELETE FROM telemetry WHERE id IN ({placeholders})", oldest_ids)
                self.log_event("storage_prune", "Pruned oldest telemetry rows after critical free-space threshold.")
        return True

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT ts, category, message, payload FROM events ORDER BY ts DESC LIMIT ?", (limit,))
            return [dict(row) for row in rows]


def _json(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, sort_keys=True, default=str)


def _downsample(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if max_points <= 0 or len(points) <= max_points:
        return points
    stride = len(points) / max_points
    return [points[int(index * stride)] for index in range(max_points)]
