from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

import pytest

from loggerhead.config import default_config
from loggerhead.database import TelemetryStore
from loggerhead.web import INDEX_HTML, DashboardServer


class FakeService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.config = default_config()
        self.history_requests: list[tuple[list[str], float]] = []

    def set_manual_priming(self, stepper_id: str, enabled: bool) -> dict[str, object]:
        if self.fail:
            raise RuntimeError("busy")
        return {"id": stepper_id, "priming": enabled, "state": "started"}

    def update_config(self, payload: dict[str, object]):
        self.last_config_payload = payload
        return self.config

    def status(self) -> dict[str, object]:
        return {
            "readings": {},
            "config": asdict(self.config),
        }

    def history_streams(self) -> list[dict[str, object]]:
        return [
            {
                "stream": "temperature.sense_port_1_temperature",
                "kind": "numeric",
                "unit": "F",
                "last_ts": 100.0,
                "count": 2,
            },
            {"stream": "water.sense_port_2_water", "kind": "categorical", "unit": "", "last_ts": 100.0, "count": 2},
            {"stream": "equipment.ac1", "kind": "numeric", "unit": "", "last_ts": 100.0, "count": 1},
        ]

    def history(self, streams: list[str], start_ts: float):
        self.history_requests.append((streams, start_ts))
        return {
            "temperature.sense_port_1_temperature": [
                {"ts": 100.0, "value": 78.0, "unit": "F"},
                {"ts": 101.0, "value": 79.0, "unit": "F"},
            ],
            "water.sense_port_2_water": [
                {"ts": 100.0, "value": "dry", "unit": ""},
                {"ts": 101.0, "value": "normal", "unit": ""},
            ],
        }


def post_json(server: DashboardServer, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    host, port = server.server_address
    request = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get_json(server: DashboardServer, path: str) -> tuple[int, dict[str, object]]:
    host, port = server.server_address
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_prime_api_returns_accepted_json() -> None:
    server = DashboardServer(FakeService(), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = post_json(server, "/api/prime", {"id": "dose1", "on": True})
        assert status == 202
        assert body["ok"] is True
        assert body["id"] == "dose1"
    finally:
        server.shutdown()
        thread.join(timeout=1)


def test_prime_api_returns_structured_conflict() -> None:
    server = DashboardServer(FakeService(fail=True), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = post_json(server, "/api/prime", {"id": "dose2", "on": True})
        assert status == 409
        assert body == {"ok": False, "error": "conflict", "message": "busy"}
    finally:
        server.shutdown()
        thread.join(timeout=1)


def test_config_api_returns_structured_config() -> None:
    server = DashboardServer(FakeService(), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = post_json(server, "/api/config", asdict(default_config()))
        assert status == 200
        assert body["ok"] is True
        assert isinstance(body["config"], dict)
        assert isinstance(body["config"]["sense_ports"], list)
    finally:
        server.shutdown()
        thread.join(timeout=1)


def test_history_streams_api_returns_sqlite_catalog() -> None:
    server = DashboardServer(FakeService(), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = get_json(server, "/api/history/streams")
        assert status == 200
        assert [item["stream"] for item in body["streams"]] == [
            "temperature.sense_port_1_temperature",
            "water.sense_port_2_water",
            "equipment.ac1",
        ]
    finally:
        server.shutdown()
        thread.join(timeout=1)


def test_history_api_returns_numeric_and_categorical_series() -> None:
    service = FakeService()
    server = DashboardServer(service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = get_json(
            server,
            "/api/history?stream=temperature.sense_port_1_temperature&stream=water.sense_port_2_water&lookback=60",
        )
        assert status == 200
        assert service.history_requests[0][0] == ["temperature.sense_port_1_temperature", "water.sense_port_2_water"]
        assert body["temperature.sense_port_1_temperature"][0]["value"] == 78.0
        assert body["water.sense_port_2_water"][0]["value"] == "dry"
    finally:
        server.shutdown()
        thread.join(timeout=1)


def test_dashboard_sensor_editor_preserves_drafts_between_refreshes() -> None:
    assert "sensorEditorDirty" in INDEX_HTML
    assert "loadSensePortDrafts(false)" in INDEX_HTML
    assert "if (!force && (sensorEditorDirty || focused" in INDEX_HTML
    assert "saveSensePorts" not in INDEX_HTML
    assert "one_wire_mode" not in INDEX_HTML
    assert "DS18B20 ID" not in INDEX_HTML


def test_dashboard_plot_selector_preserves_selection_between_refreshes() -> None:
    assert "/static/plots.mjs" in INDEX_HTML
    assert "status.history_streams" not in INDEX_HTML
    assert '<canvas id="plot"' not in INDEX_HTML


def test_dashboard_plots_autoload_and_show_history_states() -> None:
    assert "loggerhead:plots-open" in INDEX_HTML
    assert 'role="status" aria-live="polite"' in INDEX_HTML
    assert '<option value="21600">6 hours</option>' in INDEX_HTML


@pytest.mark.parametrize("lookback", ["nan", "inf", "0", "-1", "604801", "bad"])
def test_history_api_rejects_invalid_ranges(lookback) -> None:
    service = FakeService()
    server = DashboardServer(service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = get_json(server, f"/api/history?stream=temperature.water&lookback={lookback}")
        assert status == 400
        assert body["error"] == "bad_request"
        assert service.history_requests == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_static_plot_assets_are_local_cacheable_and_restricted() -> None:
    server = DashboardServer(FakeService(), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        for path in ["/static/plots.mjs", "/static/vendor/plotly-basic-4.1.2.min.js"]:
            with urllib.request.urlopen(base + path, timeout=2) as response:
                assert response.status == 200
                assert response.headers["Content-Type"].startswith("text/javascript")
                assert response.headers["ETag"]
                etag = response.headers["ETag"]
                assert response.read()
            request = urllib.request.Request(base + path, headers={"If-None-Match": etag})
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=2)
            assert error.value.code == 304
        request = urllib.request.Request(
            base + "/static/vendor/plotly-basic-4.1.2.min.js", headers={"Accept-Encoding": "gzip"}
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            import gzip

            assert response.headers["Content-Encoding"] == "gzip"
            assert b"plotly.js (basic - minified) v4.1.2" in gzip.decompress(response.read())[:200]
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/static/../config/loggerhead.json", timeout=2)
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_plotly_bundle_license_and_package_data() -> None:
    static = Path(__file__).parents[1] / "loggerhead" / "static"
    assert b"plotly.js (basic - minified) v4.1.2" in (static / "vendor/plotly-basic-4.1.2.min.js").read_bytes()[:200]
    assert "MIT" in (static / "vendor/PLOTLY-LICENSE").read_text()


def test_history_endpoints_with_real_sqlite_and_missing_stream(tmp_path) -> None:
    service = FakeService()
    store = TelemetryStore(tmp_path / "telemetry.db", max_points_default=5)
    for index in range(20):
        store.log_value("temperature.retired", 78, unit="F", ts=time.time() - 30 + index)
    store.log_value("water.level", "unknown")
    service.history_streams = store.stream_catalog
    service.history = lambda streams, start: store.history(streams, start_ts=start)
    server = DashboardServer(service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = get_json(server, "/api/history/streams")
        assert status == 200
        assert {item["stream"] for item in body["streams"]} == {"temperature.retired", "water.level"}
        status, body = get_json(server, "/api/history?stream=temperature.retired&stream=water.level&stream=missing&lookback=3600")
        assert status == 200
        assert len(body["temperature.retired"]) <= 5
        assert body["temperature.retired"][0]["unit"] == "F"
        assert body["water.level"][0]["value"] == "unknown"
        assert body["missing"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_history_busy_returns_actionable_error(monkeypatch) -> None:
    service = FakeService()

    def busy(*_args):
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(service, "history", busy)
    server = DashboardServer(service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = get_json(server, "/api/history?stream=temperature.water")
        assert status == 503
        assert body["error"] == "telemetry_unavailable"
        assert "shorter range" in body["message"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_dashboard_save_config_merges_sensor_drafts_and_shows_acknowledgement() -> None:
    assert "payload.sense_ports = sensorDrafts.map(portForSave)" in INDEX_HTML
    assert "Config saved at" in INDEX_HTML
    assert "Sensor changes saved with config." in INDEX_HTML


def test_dashboard_silence_button_shows_muted_state() -> None:
    assert "buzzer_muted_remaining_seconds" in INDEX_HTML
    assert "Silenced ${Math.floor" in INDEX_HTML
    assert "Buzzer silenced until" in INDEX_HTML
