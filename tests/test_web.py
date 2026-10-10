from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import asdict

from loggerhead.config import default_config
from loggerhead.web import INDEX_HTML, DashboardServer


class FakeService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.config = default_config()

    def set_manual_priming(self, stepper_id: str, enabled: bool) -> dict[str, object]:
        if self.fail:
            raise RuntimeError("busy")
        return {"id": stepper_id, "priming": enabled, "state": "started"}

    def update_config(self, payload: dict[str, object]):
        self.last_config_payload = payload
        return self.config


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


def test_dashboard_sensor_editor_preserves_drafts_between_refreshes() -> None:
    assert "sensorEditorDirty" in INDEX_HTML
    assert "loadSensePortDrafts(false)" in INDEX_HTML
    assert "if (!force && (sensorEditorDirty || focused" in INDEX_HTML
    assert "saveSensePorts" in INDEX_HTML
    assert "one_wire_mode" in INDEX_HTML

