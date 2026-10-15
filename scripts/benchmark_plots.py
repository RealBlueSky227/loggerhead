"""Isolated local API benchmark: simulation only, temporary telemetry, no GPIO access."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from loggerhead.service import LoggerheadService
from loggerhead.web import DashboardServer

STREAMS = [
    ("temperature.water", "F"),
    ("ph.probe", "pH"),
    ("analog.voltage", "V"),
    ("water.level", ""),
    ("equipment.ac1", ""),
    ("temperature.cpu_temp", "F"),
]


def seed(service: LoggerheadService, rows: int) -> None:
    end = time.time()

    def values():
        for index in range(rows):
            stream, unit = STREAMS[index % len(STREAMS)]
            ts = end - 604800 + 604800 * index / max(1, rows - 1)
            fraction = index % 100 / 100
            value, text = 77.0 + fraction * 2, None
            if stream == "water.level":
                value, text = None, ["dry", "low", "normal", "high"][(index // 60) % 4]
            elif stream.startswith("equipment."):
                value = (index // 60) % 2
            elif stream == "ph.probe":
                value = 8.0 + fraction * 0.2
            elif stream == "analog.voltage":
                value = 0.5 + fraction * 2.5
            elif stream == "temperature.cpu_temp":
                value = 100.0 + fraction * 15
            yield ts, stream, value, text, unit

    with service.store.connect() as conn:
        conn.executemany(
            "INSERT INTO telemetry(ts, stream, value, text_value, unit) VALUES (?, ?, ?, ?, ?)",
            values(),
        )


def measure(base: str, path: str, iterations: int) -> dict:
    latencies, sizes = [], []
    cpu_start = time.process_time()
    for _ in range(iterations):
        start = time.perf_counter()
        with urllib.request.urlopen(base + path, timeout=15) as response:
            body = response.read()
        json.loads(body)
        latencies.append((time.perf_counter() - start) * 1000)
        sizes.append(len(body))
    return {
        "median_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))], 3),
        "cpu_ms_per_request": round((time.process_time() - cpu_start) * 1000 / iterations, 3),
        "response_bytes": max(sizes),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=300000)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument(
        "--serve", action="store_true", help="Serve the isolated browser-test fixture until terminated."
    )
    args = parser.parse_args()
    if args.rows < 1 or args.iterations < 1:
        parser.error("rows and iterations must be positive")
    with tempfile.TemporaryDirectory(prefix="loggerhead-plots-") as directory:
        root = Path(directory)
        service = LoggerheadService(root / "config.json", root / "data", simulation=True)
        seed(service, args.rows)
        server = DashboardServer(service, host="127.0.0.1", port=0)
        if args.serve:
            def shutdown_on_input() -> None:
                for line in sys.stdin:
                    if line.strip() == "shutdown":
                        server.shutdown()
                        return

            threading.Thread(target=shutdown_on_input, daemon=True).start()
            print(json.dumps({"port": server.server_port}), flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
                service.stop()
            return
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        history = "/api/history?lookback=604800&" + "&".join(f"stream={stream}" for stream, _ in STREAMS)
        try:
            cold_catalog = measure(base, "/api/history/streams", 1)
            result = {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "rows": args.rows,
                "streams": len(STREAMS),
                "iterations": args.iterations,
                "catalog_cold": cold_catalog,
                "catalog_cached": measure(base, "/api/history/streams", args.iterations),
                "status": measure(base, "/api/status", args.iterations),
                "history_7d_six_streams": measure(base, history, args.iterations),
            }
            with ThreadPoolExecutor(max_workers=2) as pool:
                history_load = pool.submit(measure, base, history, args.iterations)
                result["status_during_history"] = measure(base, "/api/status", args.iterations)
                history_load.result()
            result["status_projected_cpu_percent_at_2s"] = round(result["status"]["cpu_ms_per_request"] / 20, 3)
            result["note"] = (
                "Combined local HTTP client/server CPU; synthetic data, simulation only. Not real sensor/control workload."
            )
            print(json.dumps(result, indent=2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            service.stop()


if __name__ == "__main__":
    main()
