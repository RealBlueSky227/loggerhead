from __future__ import annotations

import json
import math
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


class TelemetryStore:
    """SQLite telemetry and event log.

    Implements SRS 6.1 Continuous Polled Scraping, 6.2 Event-Driven Data Logging,
    and 6.4 Adaptive Database Capacity and Storage Preservation.
    """

    CATALOG_TTL_SECONDS = 30.0
    MAX_HISTORY_STREAMS = 12
    MAX_HISTORY_POINTS = 2000
    MAX_HISTORY_TOTAL_POINTS = 8000

    def __init__(self, path: Path, *, max_points_default: int = 800) -> None:
        self.path = path
        self.max_points_default = max_points_default
        self._catalog_lock = threading.Lock()
        self._catalog_cache: list[dict[str, Any]] | None = None
        self._catalog_expires = 0.0
        self._catalog_shapes: set[tuple[str, str, bool]] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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
            self._init_stream_catalog(conn)
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC)")

    @staticmethod
    def _init_stream_catalog(conn: sqlite3.Connection) -> None:
        # Backfill once; subsequent catalog requests never scan telemetry history.
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'telemetry_streams'").fetchone()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry_streams (
                stream TEXT PRIMARY KEY, unit TEXT NOT NULL, last_ts REAL NOT NULL,
                count INTEGER NOT NULL, numeric_count INTEGER NOT NULL, text_count INTEGER NOT NULL
            )
        """)
        if not exists:
            conn.execute("""
                INSERT INTO telemetry_streams
                SELECT stream, '', MAX(ts), COUNT(*), COUNT(value), COUNT(text_value)
                FROM telemetry GROUP BY stream
            """)
            conn.execute("""
                UPDATE telemetry_streams SET unit = (
                    SELECT unit FROM telemetry WHERE stream = telemetry_streams.stream
                    ORDER BY ts DESC, id DESC LIMIT 1
                )
            """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS telemetry_catalog_insert AFTER INSERT ON telemetry BEGIN
                INSERT INTO telemetry_streams VALUES (
                    NEW.stream, NEW.unit, NEW.ts, 1, NEW.value IS NOT NULL, NEW.text_value IS NOT NULL
                ) ON CONFLICT(stream) DO UPDATE SET
                    unit = CASE WHEN NEW.ts >= last_ts THEN NEW.unit ELSE unit END,
                    last_ts = MAX(last_ts, NEW.ts), count = count + 1,
                    numeric_count = numeric_count + (NEW.value IS NOT NULL),
                    text_count = text_count + (NEW.text_value IS NOT NULL);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS telemetry_catalog_delete AFTER DELETE ON telemetry BEGIN
                UPDATE telemetry_streams SET count = count - 1,
                    numeric_count = numeric_count - (OLD.value IS NOT NULL),
                    text_count = text_count - (OLD.text_value IS NOT NULL)
                WHERE stream = OLD.stream;
                DELETE FROM telemetry_streams WHERE stream = OLD.stream AND count = 0;
                UPDATE telemetry_streams SET
                    last_ts = (SELECT ts FROM telemetry WHERE stream = OLD.stream ORDER BY ts DESC, id DESC LIMIT 1),
                    unit = (SELECT unit FROM telemetry WHERE stream = OLD.stream ORDER BY ts DESC, id DESC LIMIT 1)
                WHERE stream = OLD.stream AND last_ts <= OLD.ts;
            END
        """)

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
                (
                    time.time() if ts is None else ts,
                    stream,
                    numeric_value,
                    text_value,
                    unit,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
        self._invalidate_catalog_if_new([(stream, value, unit)])

    def log_many(self, values: Iterable[tuple[str, float | str, str]]) -> None:
        now = time.time()
        values = list(values)
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
        self._invalidate_catalog_if_new(values)

    def _invalidate_catalog_if_new(self, values: Iterable[tuple[str, float | str, str]]) -> None:
        with self._catalog_lock:
            if any(
                (stream, unit, isinstance(value, int | float)) not in self._catalog_shapes
                for stream, value, unit in values
            ):
                self._catalog_cache = None

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
        streams = list(dict.fromkeys(streams))
        if len(streams) > self.MAX_HISTORY_STREAMS:
            raise ValueError(f"Select at most {self.MAX_HISTORY_STREAMS} history streams.")
        limit = max_points if max_points is not None else self.max_points_default
        limit = max(2, min(int(limit), self.MAX_HISTORY_POINTS, self.MAX_HISTORY_TOTAL_POINTS // max(1, len(streams))))
        end = time.time() if end_ts is None else end_ts
        if not math.isfinite(start_ts) or not math.isfinite(end) or start_ts > end:
            raise ValueError("History requires a finite, ordered time range.")
        result: dict[str, list[dict[str, Any]]] = {}
        with self.connect() as conn:
            # Readers use WAL and a short lock timeout; long queries cannot monopolize the Pi.
            conn.execute("PRAGMA busy_timeout=1000")
            deadline = time.monotonic() + 5.0
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            for stream in streams:
                rows = conn.execute(
                    """
                    SELECT ts, value, text_value, unit
                    FROM telemetry
                    WHERE stream = ? AND ts >= ? AND ts <= ?
                    ORDER BY ts ASC, id ASC LIMIT ?
                    """,
                    (stream, start_ts, end, limit + 1),
                ).fetchall()
                if len(rows) > limit:
                    last = conn.execute(
                        "SELECT ts FROM telemetry WHERE stream = ? AND ts >= ? AND ts <= ? ORDER BY ts DESC, id DESC LIMIT 1",
                        (stream, start_ts, end),
                    ).fetchone()["ts"]
                    first = rows[0]["ts"]
                    # Bounded index seeks instead of scanning/ranking every historical row.
                    # Exact sparse series; dense series retain endpoints with time-uniform samples.
                    rows = conn.execute(
                        """
                        WITH RECURSIVE targets(n, ts) AS (
                            SELECT 0, ?
                            UNION ALL SELECT n + 1, ? + (? - ?) * (n + 1) / (? - 1)
                            FROM targets WHERE n < ? - 2
                        ), picked(id) AS (
                            SELECT (SELECT id FROM telemetry
                                    WHERE stream = ? AND ts >= targets.ts AND ts <= ?
                                    ORDER BY ts ASC, id ASC LIMIT 1) FROM targets
                            UNION SELECT id FROM (
                                SELECT id FROM telemetry WHERE stream = ? AND ts >= ? AND ts <= ?
                                ORDER BY ts DESC, id DESC LIMIT 1
                            )
                        )
                        SELECT t.ts, t.value, t.text_value, t.unit FROM telemetry t
                        JOIN picked ON t.id = picked.id ORDER BY t.ts ASC, t.id ASC
                    """,
                        (first, first, last, first, limit, limit, stream, last, stream, first, last),
                    ).fetchall()
                result[stream] = [
                    {
                        "ts": row["ts"],
                        "value": row["value"] if row["value"] is not None else row["text_value"],
                        "unit": row["unit"],
                    }
                    for row in rows
                ]
        return result

    def stream_catalog(self) -> list[dict[str, Any]]:
        """Cached SQLite catalog; new streams/units/kinds invalidate immediately."""
        with self._catalog_lock:
            if self._catalog_cache is not None and time.monotonic() < self._catalog_expires:
                return [dict(item) for item in self._catalog_cache]
            with self.connect() as conn:
                rows = conn.execute("SELECT * FROM telemetry_streams ORDER BY stream").fetchall()
            catalog: list[dict[str, Any]] = []
            for row in rows:
                numeric_count = int(row["numeric_count"] or 0)
                text_count = int(row["text_count"] or 0)
                if numeric_count and text_count:
                    kind = "mixed"
                elif text_count:
                    kind = "categorical"
                else:
                    kind = "numeric"
                catalog.append(
                    {
                        "stream": row["stream"],
                        "unit": row["unit"],
                        "kind": kind,
                        "last_ts": row["last_ts"],
                        "count": row["count"],
                    }
                )
            self._catalog_cache = catalog
            self._catalog_expires = time.monotonic() + self.CATALOG_TTL_SECONDS
            self._catalog_shapes = {
                (item["stream"], item["unit"], numeric)
                for item in catalog
                for numeric in (True, False)
                if item["kind"] == "mixed" or numeric == (item["kind"] == "numeric")
            }
            return [dict(item) for item in catalog]

    def prune_if_critical(self, *, warning_free_ratio: float = 0.20, critical_free_ratio: float = 0.05) -> bool:
        usage = shutil.disk_usage(self.path.parent)
        free_ratio = usage.free / usage.total if usage.total else 1.0
        if free_ratio >= critical_free_ratio:
            return free_ratio < warning_free_ratio
        with self.connect() as conn:
            oldest_ids = [
                row["id"] for row in conn.execute("SELECT id FROM telemetry ORDER BY ts ASC LIMIT 500").fetchall()
            ]
            if oldest_ids:
                placeholders = ",".join("?" for _ in oldest_ids)
                conn.execute(f"DELETE FROM telemetry WHERE id IN ({placeholders})", oldest_ids)
        if oldest_ids:
            with self._catalog_lock:
                self._catalog_cache = None
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
