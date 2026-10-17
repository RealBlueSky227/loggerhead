from __future__ import annotations

import json
import logging
import math
import shutil
import sqlite3
import time
from contextlib import nullcontext
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .drivers import HardwareFault
from .hardware import DiagnosticHalt

LOGGER = logging.getLogger(__name__)
STATIC_ROOT = Path(__file__).with_name("static")
STATIC_ASSETS = {
    "/static/plots.mjs": ("plots.mjs", "text/javascript; charset=utf-8"),
    "/static/dosing.mjs": ("dosing.mjs", "text/javascript; charset=utf-8"),
    "/static/ato.mjs": ("ato.mjs", "text/javascript; charset=utf-8"),
    "/static/vendor/plotly-basic-4.1.2.min.js": ("vendor/plotly-basic-4.1.2.min.js", "text/javascript; charset=utf-8"),
}


class DashboardServer(ThreadingHTTPServer):
    """Local-network dashboard server.

    Implements SRS 5.1 through 5.3 and 5.5 with a dependency-light web UI.
    """

    def __init__(self, service, *, host: str, port: int) -> None:
        self.service = service
        super().__init__((host, port), DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        monitor = getattr(self.server.service, "performance", None)
        path = urlparse(self.path).path
        label = path if path in {"/", "/api/status", "/api/performance", "/api/history", "/api/history/streams"} else "asset_or_other"
        with monitor.measure(f"http.GET {label}") if monitor else nullcontext():
            self._handle_GET()

    def _handle_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send_html(INDEX_HTML)
            elif parsed.path in STATIC_ASSETS:
                self._send_asset(parsed.path)
            elif parsed.path == "/api/status":
                self._send_json(self.server.service.status())
            elif parsed.path == "/api/performance":
                self._send_json(self.server.service.performance_snapshot(stacks=parse_qs(parsed.query).get("stacks") == ["1"]))
            elif parsed.path == "/api/history/streams":
                self._send_json({"streams": self.server.service.history_streams()})
            elif parsed.path == "/api/history":
                query = parse_qs(parsed.query)
                streams = query.get("stream", [])
                lookback = float(query.get("lookback", ["86400"])[0])
                if not math.isfinite(lookback) or not 1 <= lookback <= 604800:
                    raise ValueError("History lookback must be between 1 second and 7 days.")
                if len(streams) > 12 or any(not stream or len(stream) > 200 for stream in streams):
                    raise ValueError("Select at most 12 valid history stream IDs.")
                self._send_json(self.server.service.history(streams, time.time() - lookback))
            else:
                self.send_error(404)
        except (KeyError, ValueError) as exc:
            self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, status=400)
        except sqlite3.OperationalError:
            LOGGER.exception("Telemetry query unavailable while handling %s.", parsed.path)
            self._send_json(
                {
                    "ok": False,
                    "error": "telemetry_unavailable",
                    "message": "Telemetry query busy or timed out; retry with fewer streams or a shorter range.",
                },
                status=503,
            )
        except Exception as exc:
            LOGGER.exception("Unexpected dashboard API error while handling %s.", parsed.path)
            self._send_json({"ok": False, "error": "internal_error", "message": str(exc)}, status=500)

    def do_POST(self) -> None:
        monitor = getattr(self.server.service, "performance", None)
        with monitor.measure("http.POST") if monitor else nullcontext():
            self._handle_POST()

    def _handle_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/equipment":
                self.server.service.set_equipment(payload["id"], bool(payload["on"]))
                self._send_json({"ok": True})
            elif parsed.path == "/api/config":
                config = self.server.service.update_config(payload)
                self._send_json({"ok": True, "config": asdict(config), "saved_at": time.time()})
            elif parsed.path == "/api/sense-port":
                self.server.service.set_sense_port(int(payload["number"]), payload)
                self._send_json({"ok": True})
            elif parsed.path == "/api/ato/reset":
                self.server.service.reset_ato(payload["id"])
                self._send_json({"ok": True})
            elif parsed.path == "/api/buzzer/silence":
                result = self.server.service.silence_buzzer()
                self._send_json({"ok": True, **result})
            elif parsed.path == "/api/alarm/enabled":
                self.server.service.set_alarm_enabled(bool(payload["enabled"]))
                self._send_json({"ok": True})
            elif parsed.path == "/api/prime":
                result = self.server.service.set_manual_priming(
                    payload["id"], bool(payload["on"]),
                    speed_steps_per_second=payload.get("speed_steps_per_second"),
                    max_seconds=payload.get("max_seconds"),
                )
                self._send_json({"ok": True, **result}, status=202)
            elif parsed.path == "/api/calibration":
                result = self.server.service.start_calibration(payload["id"], payload["steps"], payload.get("speed_steps_per_second"))
                self._send_json({"ok": True, **result}, status=202)
            elif parsed.path == "/api/dosing/preview":
                self._send_json({"ok": True, **self.server.service.preview_dosing(payload["config"])})
            elif parsed.path == "/api/dosing/pause":
                self.server.service.dosing.pause(payload["id"], payload["paused"])
                self._send_json({"ok": True})
            elif parsed.path == "/api/dosing/reset":
                self.server.service.dosing.reset(payload["id"])
                self._send_json({"ok": True})
            else:
                self.send_error(404)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, status=400)
        except DiagnosticHalt as exc:
            self._send_json({"ok": False, "error": "configuration", "message": str(exc)}, status=422)
        except HardwareFault as exc:
            LOGGER.exception("Hardware fault while handling %s.", parsed.path)
            self._send_json({"ok": False, "error": "hardware_fault", "message": str(exc)}, status=503)
        except RuntimeError as exc:
            self._send_json({"ok": False, "error": "conflict", "message": str(exc)}, status=409)
        except Exception as exc:
            LOGGER.exception("Unexpected dashboard API error while handling %s.", parsed.path)
            self._send_json({"ok": False, "error": "internal_error", "message": str(exc)}, status=500)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_asset(self, url: str) -> None:
        relative, content_type = STATIC_ASSETS[url]
        path = STATIC_ROOT / relative
        compressed = path.with_suffix(path.suffix + ".gz")
        use_gzip = "gzip" in self.headers.get("Accept-Encoding", "") and compressed.is_file()
        if use_gzip:
            path = compressed
        stat = path.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        unchanged = self.headers.get("If-None-Match") == etag
        self.send_response(304 if unchanged else 200)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if "/vendor/" in url else "no-cache")
        self.send_header("Vary", "Accept-Encoding")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        if not unchanged:
            self.send_header("Content-Length", str(stat.st_size))
        self.end_headers()
        if not unchanged:
            with path.open("rb") as asset:
                shutil.copyfileobj(asset, self.wfile)

    def _send_json(self, payload: Any, *, status: int = 200) -> None:
        body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            LOGGER.debug("Dashboard client disconnected while receiving %s.", self.path)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Loggerhead</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #070a0d;
      --panel: #10171c;
      --panel2: #141d23;
      --line: #26343c;
      --text: #e9f2f2;
      --muted: #88a1a1;
      --ok: #35d07f;
      --hot: #ff4b5f;
      --cold: #39a7ff;
      --warn: #ffc857;
      --off: #4b5d63;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); }
    header { display: flex; align-items: center; justify-content: space-between; padding: 18px 24px; border-bottom: 1px solid var(--line); background: #0b1115; position: sticky; top: 0; z-index: 2; }
    h1 { font-size: 20px; margin: 0; letter-spacing: 0; }
    nav { display: flex; gap: 8px; }
    button, select, input { background: #0d1419; color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 9px 11px; font: inherit; }
    input[type="checkbox"] { width: 18px; height: 18px; }
    button { cursor: pointer; min-width: 40px; }
    button.active, .filled { background: var(--ok); color: #031008; border-color: var(--ok); font-weight: 700; }
    button.danger { background: var(--hot); color: #fff; border-color: var(--hot); font-weight: 700; }
    button.hollow { background: transparent; color: var(--muted); }
    main { padding: 20px; display: grid; gap: 18px; }
    .toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .clock { font-size: 30px; font-weight: 800; color: var(--ok); font-variant-numeric: tabular-nums; }
    .heartbeat { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); }
    .dot { width: 11px; height: 11px; border-radius: 50%; background: var(--off); display: inline-block; }
    .dot.ok { background: var(--ok); box-shadow: 0 0 14px rgba(53, 208, 127, .45); }
    .dot.bad { background: var(--hot); box-shadow: 0 0 14px rgba(255, 75, 95, .45); }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--widget-width, 230px), 1fr)); gap: 12px; }
    .card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; min-height: 112px; }
    .label { color: var(--muted); font-size: 13px; margin-bottom: 8px; }
    .value { font-size: 28px; font-weight: 800; color: var(--ok); overflow-wrap: anywhere; }
    .value.hot { color: var(--hot); }
    .value.cold { color: var(--cold); }
    .value.off { color: var(--muted); }
    .row { display: flex; align-items: center; justify-content: space-between; gap: 10px; border-bottom: 1px solid var(--line); padding: 10px 0; }
    .row:last-child { border-bottom: 0; }
    .tabs { display: flex; gap: 8px; flex-wrap: wrap; }
    section[hidden] { display: none; }
    #plot { width: 100%; height: 520px; min-width: 0; }
    #plotStream { max-width: 100%; min-width: 220px; }
    @media (max-width: 640px) { #plot { height: 560px; } #plotStream { width: 100%; } }
    textarea { width: 100%; min-height: 360px; background: #080d11; color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 12px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    .alarm { border-color: var(--warn); }
    .diag { border-color: #31505a; }
    .field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-top: 10px; }
    .field-grid label { display: grid; min-width: 0; gap: 4px; color: var(--muted); font-size: 12px; }
    .field-grid input, .field-grid select { min-width: 0; max-width: 100%; }
    .field-grid label span { color: var(--muted); }
    .message { min-height: 24px; color: var(--muted); }
    .message.error { color: var(--hot); }
    .message.ok { color: var(--ok); }
    .status-line { color: var(--muted); font-size: 12px; margin-top: 8px; }
    .ato-profile { border-top: 1px solid var(--line); padding: 16px 0; }
    .ato-profile .toolbar { margin-top: 12px; }
    .ato-profile input:not([type="checkbox"]), .ato-profile select { width: 100%; }
    .ato-table { overflow: auto; max-width: 100%; }
    .ato-table table { border-collapse: collapse; font-size: 12px; width: 100%; }
    .ato-table th, .ato-table td { padding: 8px; text-align: left; border-bottom: 1px solid var(--line); }
    #atoOverview .value { font-size: 20px; }
    .value.warn { color: var(--warn); }
    @media (max-width: 640px) { header { align-items: flex-start; flex-direction: column; gap: 12px; } .clock { font-size: 24px; } main { padding: 12px; } .grid { grid-template-columns: minmax(0, 1fr); } main > section { min-width: 0; } }
  </style>
</head>
<body>
  <header>
    <h1>Loggerhead</h1>
    <div class="tabs">
      <button data-tab="dash" class="active">Dashboard</button>
      <button data-tab="plots">Plots</button>
      <button data-tab="diagnostics">Diagnostics</button>
      <button data-tab="config">Config</button>
    </div>
  </header>
  <main>
    <section id="dash">
      <div class="toolbar">
        <div class="clock" id="clock"></div>
        <div class="heartbeat"><span class="dot bad" id="heartbeatDot"></span><span id="heartbeatText">Connecting</span></div>
        <label>Widget width <input id="scale" type="range" min="180" max="420" value="230"></label>
        <button id="silence">Silence</button>
        <button id="alarmToggle">Alarm Enabled</button>
        <span id="alarmMessage" class="message"></span>
      </div>
      <h2>Life Support</h2>
      <div id="readings" class="grid"></div>
      <h2>Equipment</h2>
      <div id="equipment" class="grid"></div>
      <h2>Dosing Pumps</h2>
      <span id="pumpMessage" class="message" role="status"></span>
      <div id="pumps" class="grid"></div>
      <div id="dosingOverview" class="grid"></div>
      <h2>Automatic Top Off</h2>
      <span id="atoMessage" class="message" role="status"></span>
      <div id="atoOverview" class="grid"></div>
      <h2>Alarms</h2>
      <div id="alarms" class="grid"></div>
    </section>
    <section id="plots" hidden>
      <div class="toolbar">
        <select id="plotStream" multiple size="6" aria-label="Historical streams"></select>
        <select id="lookback" aria-label="Time range">
          <option value="3600">1 hour</option>
          <option value="21600">6 hours</option>
          <option value="86400" selected>24 hours</option>
          <option value="604800">7 days</option>
        </select>
        <button id="loadPlot">Refresh</button>
        <span id="plotMessage" class="message" role="status" aria-live="polite"></span>
      </div>
      <div id="plot" aria-label="Telemetry history chart"></div>
    </section>
    <section id="diagnostics" hidden>
      <div id="healthGrid" class="grid"></div>
      <h2>Hardware</h2>
      <div id="diagnosticGrid" class="grid"></div>
      <h2>Events</h2>
      <div id="events" class="grid"></div>
    </section>
    <section id="config" hidden>
      <div class="toolbar">
        <button id="saveConfig">Save Config</button>
        <button id="cancelConfig" class="hollow">Cancel</button>
        <span id="configMessage" class="message" role="status" aria-live="polite"></span>
      </div>
      <h2>Dosing Pump Configuration</h2>
      <div id="pumpConfig" class="grid"></div>
      <h2>Dosing Scheduler</h2>
      <div class="toolbar"><button id="addDoseSchedule">Add Schedule</button></div>
      <div id="dosingSchedules"></div>
      <h3>Dosing History</h3>
      <div id="dosingHistory"></div>
      <h2>Automatic Top Off</h2>
      <div class="toolbar"><button id="addATO">Add ATO</button><span id="atoConfigMessage" class="message" role="status"></span></div>
      <p class="status-line">SensePort Desired state controls alerts only. ATO uses its own start and stop thresholds.</p>
      <div id="atoConfig"></div>
      <h3>Refill History</h3>
      <div id="atoHistory"></div>
      <h2>Sense Ports</h2>
      <div class="toolbar">
        <button id="cancelSensePorts" class="hollow">Cancel</button>
        <span id="sensePortMessage" class="message"></span>
      </div>
      <div id="sensePorts" class="grid"></div>
      <details><summary>Raw Config</summary><textarea id="configText" aria-label="Raw configuration"></textarea></details>
    </section>
  </main>
  <script>
    let status = {};
    let lastStatusAt = 0;
    let refreshInFlight = false;
    let rawConfigDirty = false;
    let sensorEditorDirty = false;
    let sensorDrafts = [];
    let lastSensePortHash = "";
    const $ = (id) => document.getElementById(id);
    const clone = (value) => JSON.parse(JSON.stringify(value));
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
    const DEVICE_OPTIONS = [
      ["empty", "Empty"],
      ["hydros_triple", "HYDROS triple optical PWM"],
      ["binary", "Binary float/switch"],
      ["ds18b20", "DS18B20 temperature"],
      ["analog", "Analog voltage"],
    ];
    const LEVEL_OPTIONS = [["dry", "Dry"], ["low", "Low"], ["normal", "Normal"], ["high", "High"], ["submerged", "Submerged"], ["wet", "Wet"]];
    const EQUIPMENT_TYPES = [["heater", "Heater"], ["chiller", "Chiller"], ["fan", "Fan"], ["generic", "Generic"]];
    document.querySelectorAll("[data-tab]").forEach(btn => btn.onclick = () => {
      document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("active", b === btn));
      ["dash","plots","diagnostics","config"].forEach(id => $(id).hidden = id !== btn.dataset.tab);
      if (btn.dataset.tab === "plots") {
        document.dispatchEvent(new Event("loggerhead:plots-open"));
      }
    });
    $("scale").oninput = e => document.documentElement.style.setProperty("--widget-width", `${e.target.value}px`);
    $("silence").onclick = async () => {
      try {
        const result = await apiPost("/api/buzzer/silence", {});
        status.buzzer_muted_until = result.muted_until;
        status.buzzer_muted_remaining_seconds = Math.max(0, result.muted_until - Date.now() / 1000);
        updateSilenceControl();
        setMessage("alarmMessage", `Buzzer silenced until ${new Date(result.muted_until * 1000).toLocaleTimeString()}.`, "ok");
      } catch (error) {
        setMessage("alarmMessage", error.message, "error");
      }
    };
    $("alarmToggle").onclick = async () => {
      await fetch("/api/alarm/enabled", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({enabled: !status.config.buzzer.alarm_enabled})});
      await refresh();
    };
    $("configText").oninput = () => { rawConfigDirty = true; setMessage("configMessage", "Unsaved raw config changes."); };
    $("saveConfig").onclick = async () => {
      try {
        const saveButton = $("saveConfig");
        saveButton.disabled = true;
        saveButton.textContent = "Saving...";
        let payload = rawConfigDirty ? JSON.parse($("configText").value) : clone(status.config);
        if (sensorEditorDirty) payload.sense_ports = sensorDrafts.map(portForSave);
        if (window.loggerheadDosing) payload = window.loggerheadDosing.mergeConfig(payload);
        if (window.loggerheadATO) payload = window.loggerheadATO.mergeConfig(payload);
        const result = await apiPost("/api/config", payload);
        status.config = result.config;
        status.sense_ports = result.config.sense_ports;
        status.steppers = result.config.steppers;
        document.dispatchEvent(new CustomEvent("loggerhead:config-saved", {detail: result.config}));
        rawConfigDirty = false;
        sensorEditorDirty = false;
        $("configText").value = JSON.stringify(result.config, null, 2);
        lastSensePortHash = "";
        loadSensePortDrafts(true);
        setMessage("configMessage", `Config saved at ${new Date((result.saved_at || Date.now() / 1000) * 1000).toLocaleTimeString()}.`, "ok");
        setMessage("sensePortMessage", "Sensor changes saved with config.", "ok");
        await refresh();
      } catch (error) {
        setMessage("configMessage", error.message, "error");
      } finally {
        $("saveConfig").disabled = false;
        $("saveConfig").textContent = "Save Config";
      }
    };
    $("cancelConfig").onclick = () => {
      rawConfigDirty = false;
      sensorEditorDirty = false;
      loadSensePortDrafts(true);
      document.dispatchEvent(new Event("loggerhead:config-canceled"));
      $("configText").value = JSON.stringify(status.config, null, 2);
      setMessage("configMessage", "Unsaved config changes canceled.");
    };
    $("cancelSensePorts").onclick = () => {
      sensorEditorDirty = false;
      loadSensePortDrafts(true);
      setMessage("sensePortMessage", "Sensor changes canceled.");
    };
    async function apiPost(path, payload) {
      const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
      const body = await response.json().catch(() => ({}));
      if (!response.ok || body.ok === false) throw new Error(body.message || `${response.status} ${response.statusText}`);
      return body;
    }
    function setMessage(id, text, cls="") {
      const el = $(id);
      el.textContent = text || "";
      el.className = `message ${cls}`;
    }
    function card(label, value, cls="", detail="") {
      return `<div class="card ${cls}"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div>${detail ? `<div class="status-line">${esc(detail)}</div>` : ""}</div>`;
    }
    function healthFor(id) {
      return status.sensor_health?.[id] || {status: "initializing", last_error: ""};
    }
    function statusLabel(health) {
      return {
        initializing: "Initializing",
        online: "Online",
        read_error: "Read Error",
        disconnected: "Disconnected",
        stale: "Stale",
      }[health.status] || "Initializing";
    }
    function statusClass(health) {
      if (health.status === "online") return "";
      if (health.status === "initializing") return "off";
      if (health.status === "stale") return "warn";
      return "hot";
    }
    function readingFor(sensor) {
      const health = healthFor(sensor.id);
      const detail = health.last_error || statusLabel(health);
      if (sensor.kind === "water") {
        if (health.status !== "online") return card(sensor.name, statusLabel(health), statusClass(health), detail);
        const value = status.water_levels[sensor.id] || "initializing";
        const cls = value === sensor.desired ? "" : "hot";
        return card(sensor.name, value.replaceAll("_", " "), cls);
      }
      const reading = status.readings[sensor.id];
      if (!reading || health.status !== "online") return card(sensor.name, statusLabel(health), statusClass(health), detail);
      let cls = "";
      if (sensor.kind === "temperature" && typeof reading.value === "number") {
        if (reading.value > 82 && sensor.main) cls = "hot";
        if (reading.value < 74 && sensor.main) cls = "cold";
      }
      return card(sensor.name, `${reading.value} ${reading.unit || ""}`, cls);
    }
    function setHeartbeat(ok, text) {
      $("heartbeatDot").className = `dot ${ok ? "ok" : "bad"}`;
      $("heartbeatText").textContent = text;
    }
    async function refresh() {
      if (refreshInFlight) return;
      refreshInFlight = true;
      const controller = new AbortController();
      const deadline = setTimeout(() => controller.abort(), 8000);
      try {
        await refreshStatus(controller.signal);
      } catch {
        setHeartbeat(false, lastStatusAt ? "Backend stale" : "Backend offline");
      } finally {
        clearTimeout(deadline);
        refreshInFlight = false;
      }
    }
    async function refreshStatus(signal) {
      const response = await fetch("/api/status", {signal});
      if (!response.ok) throw new Error(`Status unavailable (${response.status}).`);
      const next = await response.json();
      if (!next.config || !next.heartbeat?.ok) throw new Error("Invalid backend status.");
      status = next;
      lastStatusAt = Date.now();
      setHeartbeat(true, "Backend online");
      $("clock").textContent = new Date(status.time * 1000).toLocaleString();
      if (!rawConfigDirty && document.activeElement !== $("configText")) $("configText").value = JSON.stringify(status.config, null, 2);
      loadSensePortDrafts(false);
      $("alarmToggle").textContent = status.config.buzzer.alarm_enabled ? "Alarm Enabled" : "Alarm Disabled";
      $("alarmToggle").className = status.config.buzzer.alarm_enabled ? "filled" : "danger";
      updateSilenceControl();
      $("readings").innerHTML = status.sensor_catalog.filter(s => s.main).map(readingFor).join("");
      $("equipment").innerHTML = Object.values(status.equipment).map(e => {
        const klass = e.on ? "filled" : "hollow";
        const reserved = status.config.ato?.some(p => p.assigned_actuator === e.id && (p.enabled || status.ato?.[p.id]?.faulted));
        return `<div class="card"><div class="label">${e.id}</div><button class="${klass}" ${reserved && !e.on ? 'disabled title="Reserved by Automatic Top Off"' : ""} onclick="toggleEquipment('${e.id}', ${!e.on})">${e.on ? "ON" : "OFF"}</button></div>`;
      }).join("");
      $("pumps").innerHTML = status.steppers.map(p => {
        const on = !!status.manual_priming[p.id] || status.stepper_active === p.id;
        const occupied = status.stepper_active && !on;
        const reserved = !on && status.config.ato?.some(a => a.assigned_actuator === p.id && (a.enabled || status.ato?.[a.id]?.faulted));
        return `<div class="card"><div class="label">${esc(p.name)}</div><button class="${on ? "filled" : "hollow"}" ${occupied || reserved ? "disabled" : ""} onclick="togglePrime('${esc(p.id)}', ${!on})">${on ? "STOP" : "PRIME"}</button></div>`;
      }).join("");
      $("dosingOverview").innerHTML = (status.config.dosing || []).filter(p => p.enabled).map(profile => {
        const runtime = status.dosing?.schedules.find(s => s.id === profile.id) || {};
        const active = status.dosing?.active?.schedule_id === profile.id;
        const state = runtime.fault ? "Fault" : runtime.paused ? "Paused" : active ? "Dosing" : "Scheduled";
        const next = runtime.next_ts ? new Date(runtime.next_ts * 1000).toLocaleString() : "No upcoming dose";
        return `<div class="card"><div class="label">${esc(profile.name)}</div><div class="value ${runtime.fault ? "hot" : ""}">${state}</div><div class="status-line">${esc(runtime.paused || runtime.fault ? runtime.fault || "Paused" : `Next: ${next}`)}</div><button data-dose-id="${esc(profile.id)}" data-dose-action="${runtime.fault ? "reset" : runtime.paused ? "resume" : "pause"}">${runtime.fault ? "Reset Fault" : runtime.paused ? "Resume" : active ? "Stop / Pause" : "Pause"}</button></div>`;
      }).join("");
      $("alarms").innerHTML = Object.values(status.alarms).filter(a => a.active).map(a => card(a.priority, a.message, "alarm")).join("") || card("ok", "No active alarms");
      $("healthGrid").innerHTML = Object.values(status.readings).filter(r => r.id.startsWith("health_")).map(r => card(r.id.replace("health_", "").replaceAll("_", " "), r.value)).join("");
      $("diagnosticGrid").innerHTML = Object.entries(status.diagnostics).map(([k, v]) => card(k.replaceAll("_", " "), typeof v === "object" ? JSON.stringify(v) : v, "diag")).join("");
      $("events").innerHTML = status.events.map(e => card(e.category, e.message, "diag")).join("");
      document.dispatchEvent(new CustomEvent("loggerhead:status", {detail: status}));
    }
    async function toggleEquipment(id, on) {
      await fetch("/api/equipment", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id, on})});
      await refresh();
    }
    async function togglePrime(id, on) {
      try {
        await apiPost("/api/prime", {id, on});
        setMessage("pumpMessage", on ? "Priming started." : "Stop requested.", "ok");
        await refresh();
      } catch (error) { setMessage("pumpMessage", error.message, "error"); }
    }
    $("dosingOverview").onclick = async event => {
      const button = event.target.closest("[data-dose-action]");
      if (!button) return;
      try {
        const action = button.dataset.doseAction;
        await apiPost(action === "reset" ? "/api/dosing/reset" : "/api/dosing/pause", {id: button.dataset.doseId, paused: action === "pause"});
        await refresh();
      } catch (error) { setMessage("pumpMessage", error.message, "error"); }
    };
    function loadSensePortDrafts(force) {
      const hash = JSON.stringify(status.sense_ports || []);
      const focused = $("sensePorts").contains(document.activeElement);
      if (!force && (sensorEditorDirty || focused || hash === lastSensePortHash)) return;
      sensorDrafts = clone(status.sense_ports || []);
      lastSensePortHash = hash;
      renderSensePorts();
    }
    function renderSensePorts() {
      $("sensePorts").innerHTML = sensorDrafts.map(renderSensePort).join("");
    }
    function optionList(options, current) {
      return options.map(([value, label]) => `<option value="${value}" ${current === value ? "selected" : ""}>${esc(label)}</option>`).join("");
    }
    function equipmentOptions(current) {
      const items = [["", "None"], ...(status.config?.equipment || []).map(item => [item.id, item.name || item.id])];
      return optionList(items, current || "");
    }
    function textField(port, key, label, type="text") {
      return `<label><span>${esc(label)}</span><input type="${type}" value="${esc(port[key] ?? "")}" oninput="updatePortDraft(${port.number}, '${key}', this.value)"></label>`;
    }
    function selectField(port, key, label, options) {
      return `<label><span>${esc(label)}</span><select onchange="updatePortDraft(${port.number}, '${key}', this.value)">${optionList(options, port[key])}</select></label>`;
    }
    function checkboxField(port, key, label) {
      return `<label><span>${esc(label)}</span><input type="checkbox" ${port[key] ? "checked" : ""} onchange="updatePortDraft(${port.number}, '${key}', this.checked)"></label>`;
    }
    function renderSensePort(port) {
      const fields = [
        textField(port, "name", "Name"),
        selectField(port, "device", "Device", DEVICE_OPTIONS),
        textField(port, "check_frequency", "Check seconds", "number"),
      ];
      if (port.device === "ds18b20") {
        fields.push(
          textField(port, "target_temp", "Target F", "number"),
          textField(port, "hysteresis", "Hysteresis F", "number"),
          textField(port, "alert_below", "Alert below F", "number"),
          textField(port, "alert_above", "Alert above F", "number"),
          textField(port, "emergency_below", "Emergency below F", "number"),
          textField(port, "emergency_above", "Emergency above F", "number"),
          `<label><span>Assigned equipment</span><select onchange="updatePortDraft(${port.number}, 'assigned_equipment', this.value)">${equipmentOptions(port.assigned_equipment)}</select></label>`,
          selectField(port, "equipment_type", "Equipment type", EQUIPMENT_TYPES),
        );
      }
      if (port.device === "hydros_triple" || port.device === "binary") {
        fields.push(
          selectField(port, "desired_state", "Desired state", LEVEL_OPTIONS),
          textField(port, "alert_wait", "Alert delay seconds", "number"),
          textField(port, "alert_frequency", "Alert repeat seconds", "number"),
        );
      }
      if (port.device === "hydros_triple") {
        fields.push(
          textField(port, "activity_timeout", "Activity timeout seconds", "number"),
          textField(port, "debounce_samples", "Debounce samples", "number"),
        );
      }
      if (port.device === "binary") fields.push(checkboxField(port, "invert_binary", "Invert input"));
      if (port.device === "analog") {
        fields.push(
          textField(port, "analog_unit", "Unit"),
          textField(port, "analog_scale", "Scale", "number"),
          textField(port, "analog_offset", "Offset", "number"),
        );
      }
      const mode = port.device === "ds18b20" ? "DS18B20 kernel 1-Wire" : port.device.replaceAll("_", " ");
      return `<div class="card"><div class="label">Sense Port ${port.number}</div><div class="status-line">${esc(mode)}</div><div class="field-grid">${fields.join("")}</div></div>`;
    }
    function updatePortDraft(number, key, value) {
      const port = sensorDrafts.find(item => item.number === number);
      if (!port) return;
      port[key] = value;
      sensorEditorDirty = true;
      setMessage("sensePortMessage", "Unsaved sensor changes.");
      if (key === "device") renderSensePorts();
    }
    function portForSave(port) {
      const next = clone(port);
      const numeric = ["check_frequency","alert_wait","alert_frequency","activity_timeout","target_temp","hysteresis","alert_above","alert_below","emergency_above","emergency_below","analog_scale","analog_offset"];
      numeric.forEach(key => { if (key in next) next[key] = Number(next[key]); });
      if ("debounce_samples" in next) next.debounce_samples = Number.parseInt(next.debounce_samples, 10);
      next.invert_binary = !!next.invert_binary;
      return next;
    }
    function updateSilenceControl() {
      const remaining = Math.max(0, Math.ceil((status.buzzer_muted_until || 0) - Date.now() / 1000));
      $("silence").className = remaining > 0 ? "filled" : "hollow";
      $("silence").textContent = remaining > 0 ? `Silenced ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}` : "Silence";
    }
    window.loggerheadConfigBridge = {getStatus: () => status};
    document.addEventListener("loggerhead:refresh-request", () => refresh());
    refresh(); setInterval(refresh, 2000);
  </script>
  <script type="module" src="/static/plots.mjs"></script>
  <script type="module" src="/static/dosing.mjs"></script>
  <script type="module" src="/static/ato.mjs"></script>
</body>
</html>
"""
