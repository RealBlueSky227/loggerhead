from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .drivers import HardwareFault
from .hardware import DiagnosticHalt

LOGGER = logging.getLogger(__name__)


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
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(INDEX_HTML)
        elif parsed.path == "/api/status":
            self._send_json(self.server.service.status())
        elif parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            streams = query.get("stream", [])
            lookback = float(query.get("lookback", ["86400"])[0])
            self._send_json(self.server.service.history(streams, time.time() - lookback))
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/equipment":
                self.server.service.set_equipment(payload["id"], bool(payload["on"]))
                self._send_json({"ok": True})
            elif parsed.path == "/api/config":
                config = self.server.service.update_config(payload)
                self._send_json({"ok": True, "config": asdict(config)})
            elif parsed.path == "/api/sense-port":
                self.server.service.set_sense_port(int(payload["number"]), payload)
                self._send_json({"ok": True})
            elif parsed.path == "/api/ato/reset":
                self.server.service.reset_ato(payload["id"])
                self._send_json({"ok": True})
            elif parsed.path == "/api/buzzer/silence":
                self.server.service.silence_buzzer()
                self._send_json({"ok": True})
            elif parsed.path == "/api/alarm/enabled":
                self.server.service.set_alarm_enabled(bool(payload["enabled"]))
                self._send_json({"ok": True})
            elif parsed.path == "/api/prime":
                result = self.server.service.set_manual_priming(payload["id"], bool(payload["on"]))
                self._send_json({"ok": True, **result}, status=202)
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

    def _send_json(self, payload: Any, *, status: int = 200) -> None:
        body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    .tabs { display: flex; gap: 8px; }
    section[hidden] { display: none; }
    canvas { width: 100%; height: 320px; background: var(--panel2); border: 1px solid var(--line); border-radius: 8px; }
    textarea { width: 100%; min-height: 360px; background: #080d11; color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 12px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    .alarm { border-color: var(--warn); }
    .diag { border-color: #31505a; }
    .field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-top: 10px; }
    .field-grid label { display: grid; gap: 4px; color: var(--muted); font-size: 12px; }
    .field-grid label span { color: var(--muted); }
    .message { min-height: 24px; color: var(--muted); }
    .message.error { color: var(--hot); }
    .message.ok { color: var(--ok); }
    .status-line { color: var(--muted); font-size: 12px; margin-top: 8px; }
    .value.warn { color: var(--warn); }
    @media (max-width: 640px) { header { align-items: flex-start; flex-direction: column; gap: 12px; } .clock { font-size: 24px; } main { padding: 12px; } }
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
      </div>
      <h2>Life Support</h2>
      <div id="readings" class="grid"></div>
      <h2>Equipment</h2>
      <div id="equipment" class="grid"></div>
      <h2>Dosing Pumps</h2>
      <div id="pumps" class="grid"></div>
      <h2>Alarms</h2>
      <div id="alarms" class="grid"></div>
    </section>
    <section id="plots" hidden>
      <div class="toolbar">
        <select id="plotStream" multiple size="6"></select>
        <select id="lookback">
          <option value="3600">1 hour</option>
          <option value="86400" selected>24 hours</option>
          <option value="604800">7 days</option>
        </select>
        <button id="loadPlot">Refresh</button>
      </div>
      <canvas id="plot" width="1200" height="360"></canvas>
    </section>
    <section id="diagnostics" hidden>
      <div id="healthGrid" class="grid"></div>
      <h2>Hardware</h2>
      <div id="diagnosticGrid" class="grid"></div>
      <h2>Events</h2>
      <div id="events" class="grid"></div>
    </section>
    <section id="config" hidden>
      <h2>Sense Ports</h2>
      <div class="toolbar">
        <button id="saveSensePorts">Save Sensors</button>
        <button id="cancelSensePorts" class="hollow">Cancel</button>
        <span id="sensePortMessage" class="message"></span>
      </div>
      <div id="sensePorts" class="grid"></div>
      <h2>Raw Config</h2>
      <textarea id="configText"></textarea>
      <div class="toolbar">
        <button id="saveConfig">Save Config</button>
        <button id="cancelConfig" class="hollow">Cancel</button>
        <span id="configMessage" class="message"></span>
      </div>
    </section>
  </main>
  <script>
    let status = {};
    let lastStatusAt = 0;
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
    const WIRE_OPTIONS = [["bit_bang", "GPIO bit-bang"], ["kernel", "Kernel 1-Wire"]];
    const LEVEL_OPTIONS = [["dry", "Dry"], ["low", "Low"], ["normal", "Normal"], ["high", "High"], ["submerged", "Submerged"], ["wet", "Wet"]];
    const EQUIPMENT_TYPES = [["heater", "Heater"], ["chiller", "Chiller"], ["fan", "Fan"], ["generic", "Generic"]];
    document.querySelectorAll("[data-tab]").forEach(btn => btn.onclick = () => {
      document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("active", b === btn));
      ["dash","plots","diagnostics","config"].forEach(id => $(id).hidden = id !== btn.dataset.tab);
    });
    $("scale").oninput = e => document.documentElement.style.setProperty("--widget-width", `${e.target.value}px`);
    $("silence").onclick = () => fetch("/api/buzzer/silence", {method:"POST", body:"{}"});
    $("alarmToggle").onclick = async () => {
      await fetch("/api/alarm/enabled", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({enabled: !status.config.buzzer.alarm_enabled})});
      await refresh();
    };
    $("configText").oninput = () => { rawConfigDirty = true; setMessage("configMessage", "Unsaved raw config changes."); };
    $("saveConfig").onclick = async () => {
      try {
        const payload = JSON.parse($("configText").value);
        const result = await apiPost("/api/config", payload);
        status.config = result.config;
        rawConfigDirty = false;
        sensorEditorDirty = false;
        setMessage("configMessage", "Config saved.", "ok");
        await refresh();
      } catch (error) {
        setMessage("configMessage", error.message, "error");
      }
    };
    $("cancelConfig").onclick = () => {
      rawConfigDirty = false;
      $("configText").value = JSON.stringify(status.config, null, 2);
      setMessage("configMessage", "Raw config changes canceled.");
    };
    $("saveSensePorts").onclick = saveSensePorts;
    $("cancelSensePorts").onclick = () => {
      sensorEditorDirty = false;
      loadSensePortDrafts(true);
      setMessage("sensePortMessage", "Sensor changes canceled.");
    };
    $("loadPlot").onclick = loadPlot;
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
      try {
        status = await fetch("/api/status").then(r => r.json());
        lastStatusAt = Date.now();
        setHeartbeat(true, "Backend online");
      } catch {
        setHeartbeat(false, lastStatusAt ? "Backend stale" : "Backend offline");
        return;
      }
      $("clock").textContent = new Date(status.time * 1000).toLocaleString();
      if (!rawConfigDirty && document.activeElement !== $("configText")) $("configText").value = JSON.stringify(status.config, null, 2);
      loadSensePortDrafts(false);
      $("alarmToggle").textContent = status.config.buzzer.alarm_enabled ? "Alarm Enabled" : "Alarm Disabled";
      $("alarmToggle").className = status.config.buzzer.alarm_enabled ? "filled" : "danger";
      $("readings").innerHTML = status.sensor_catalog.filter(s => s.main).map(readingFor).join("");
      $("equipment").innerHTML = Object.values(status.equipment).map(e => {
        const klass = e.on ? "filled" : "hollow";
        return `<div class="card"><div class="label">${e.id}</div><button class="${klass}" onclick="toggleEquipment('${e.id}', ${!e.on})">${e.on ? "ON" : "OFF"}</button></div>`;
      }).join("");
      $("pumps").innerHTML = status.steppers.map(p => {
        const on = !!status.manual_priming[p.id];
        return `<div class="card"><div class="label">${p.name}</div><button class="${on ? "filled" : "hollow"}" onclick="togglePrime('${p.id}', ${!on})">${on ? "PRIMING" : "PRIME"}</button></div>`;
      }).join("");
      $("alarms").innerHTML = Object.values(status.alarms).filter(a => a.active).map(a => card(a.priority, a.message, "alarm")).join("") || card("ok", "No active alarms");
      $("healthGrid").innerHTML = Object.values(status.readings).filter(r => r.id.startsWith("health_")).map(r => card(r.id.replace("health_", "").replaceAll("_", " "), r.value)).join("");
      $("diagnosticGrid").innerHTML = Object.entries(status.diagnostics).map(([k, v]) => card(k.replaceAll("_", " "), typeof v === "object" ? JSON.stringify(v) : v, "diag")).join("");
      $("events").innerHTML = status.events.map(e => card(e.category, e.message, "diag")).join("");
      const streams = Object.keys(status.readings).map(id => id.startsWith("health_") ? `health.${id.replace("health_", "")}` : id.startsWith("ph") ? `ph.${id}` : id.includes("temp") ? `temperature.${id}` : id.startsWith("analog") ? `analog.${id}` : `snapshot.${id}`);
      $("plotStream").innerHTML = [...new Set(streams)].sort().map(s => `<option value="${s}">${s}</option>`).join("");
    }
    async function toggleEquipment(id, on) {
      await fetch("/api/equipment", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id, on})});
      await refresh();
    }
    async function togglePrime(id, on) {
      await fetch("/api/prime", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id, on})});
      await refresh();
    }
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
          selectField(port, "one_wire_mode", "DS18B20 backend", WIRE_OPTIONS),
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
      const mode = port.device === "ds18b20" ? `DS18B20 ${port.one_wire_mode.replaceAll("_", " ")}` : port.device.replaceAll("_", " ");
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
    async function saveSensePorts() {
      try {
        const next = clone(status.config);
        next.sense_ports = sensorDrafts.map(portForSave);
        const result = await apiPost("/api/config", next);
        status.config = result.config;
        status.sense_ports = result.config.sense_ports;
        sensorEditorDirty = false;
        setMessage("sensePortMessage", "Sensors saved.", "ok");
        loadSensePortDrafts(true);
        await refresh();
      } catch (error) {
        setMessage("sensePortMessage", error.message, "error");
      }
    }
    async function loadPlot() {
      const selected = [...$("plotStream").selectedOptions].map(o => `stream=${encodeURIComponent(o.value)}`).join("&");
      const lookback = $("lookback").value;
      const data = await fetch(`/api/history?${selected}&lookback=${lookback}`).then(r => r.json());
      drawPlot(data);
    }
    function drawPlot(series) {
      const canvas = $("plot"), ctx = canvas.getContext("2d");
      ctx.clearRect(0,0,canvas.width,canvas.height);
      const colors = ["#35d07f", "#39a7ff", "#ff4b5f", "#ffc857", "#b68cff"];
      const all = Object.values(series).flat().filter(p => typeof p.value === "number");
      if (!all.length) return;
      const minT = Math.min(...all.map(p => p.ts)), maxT = Math.max(...all.map(p => p.ts));
      const minV = Math.min(...all.map(p => p.value)), maxV = Math.max(...all.map(p => p.value));
      Object.entries(series).forEach(([name, points], i) => {
        ctx.strokeStyle = colors[i % colors.length]; ctx.lineWidth = 3; ctx.beginPath();
        points.filter(p => typeof p.value === "number").forEach((p, index) => {
          const x = (p.ts - minT) / Math.max(1, maxT - minT) * canvas.width;
          const y = canvas.height - ((p.value - minV) / Math.max(1, maxV - minV) * (canvas.height - 30) + 15);
          if (index === 0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
        });
        ctx.stroke(); ctx.fillStyle = ctx.strokeStyle; ctx.fillText(name, 12, 18 + i * 18);
      });
    }
    refresh(); setInterval(refresh, 2000);
  </script>
</body>
</html>
"""
