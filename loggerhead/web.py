from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


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
        payload = self._read_json()
        if parsed.path == "/api/equipment":
            self.server.service.set_equipment(payload["id"], bool(payload["on"]))
            self._send_json({"ok": True})
        elif parsed.path == "/api/config":
            config = self.server.service.update_config(payload)
            self._send_json({"ok": True, "config": config})
        elif parsed.path == "/api/ato/reset":
            self.server.service.reset_ato(payload["id"])
            self._send_json({"ok": True})
        elif parsed.path == "/api/buzzer/silence":
            self.server.service.silence_buzzer()
            self._send_json({"ok": True})
        else:
            self.send_error(404)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_json(self, payload: Any) -> None:
        body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        self.send_response(200)
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
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); }
    header { display: flex; align-items: center; justify-content: space-between; padding: 18px 24px; border-bottom: 1px solid var(--line); background: #0b1115; position: sticky; top: 0; z-index: 2; }
    h1 { font-size: 20px; margin: 0; letter-spacing: 0; }
    nav { display: flex; gap: 8px; }
    button, select, input { background: #0d1419; color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 9px 11px; font: inherit; }
    button { cursor: pointer; min-width: 40px; }
    button.active, .filled { background: var(--ok); color: #031008; border-color: var(--ok); font-weight: 700; }
    button.hollow { background: transparent; color: var(--muted); }
    main { padding: 20px; display: grid; gap: 18px; }
    .toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .clock { font-size: 30px; font-weight: 800; color: var(--ok); font-variant-numeric: tabular-nums; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--widget-width, 230px), 1fr)); gap: 12px; }
    .card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; min-height: 112px; }
    .label { color: var(--muted); font-size: 13px; margin-bottom: 8px; }
    .value { font-size: 28px; font-weight: 800; color: var(--ok); overflow-wrap: anywhere; }
    .value.hot { color: var(--hot); }
    .value.cold { color: var(--cold); }
    .row { display: flex; align-items: center; justify-content: space-between; gap: 10px; border-bottom: 1px solid var(--line); padding: 10px 0; }
    .row:last-child { border-bottom: 0; }
    .tabs { display: flex; gap: 8px; }
    section[hidden] { display: none; }
    canvas { width: 100%; height: 320px; background: var(--panel2); border: 1px solid var(--line); border-radius: 8px; }
    textarea { width: 100%; min-height: 360px; background: #080d11; color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 12px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    .alarm { border-color: var(--warn); }
    @media (max-width: 640px) { header { align-items: flex-start; flex-direction: column; gap: 12px; } .clock { font-size: 24px; } main { padding: 12px; } }
  </style>
</head>
<body>
  <header>
    <h1>Loggerhead</h1>
    <div class="tabs">
      <button data-tab="dash" class="active">Dashboard</button>
      <button data-tab="plots">Plots</button>
      <button data-tab="health">System Health</button>
      <button data-tab="config">Config</button>
    </div>
  </header>
  <main>
    <section id="dash">
      <div class="toolbar">
        <div class="clock" id="clock"></div>
        <label>Widget width <input id="scale" type="range" min="180" max="420" value="230"></label>
        <button id="silence">Silence</button>
      </div>
      <h2>Readouts</h2>
      <div id="readings" class="grid"></div>
      <h2>Equipment</h2>
      <div id="equipment" class="grid"></div>
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
    <section id="health" hidden>
      <div id="healthGrid" class="grid"></div>
    </section>
    <section id="config" hidden>
      <textarea id="configText"></textarea>
      <div class="toolbar"><button id="saveConfig">Save Config</button></div>
    </section>
  </main>
  <script>
    let status = {};
    const $ = (id) => document.getElementById(id);
    document.querySelectorAll("[data-tab]").forEach(btn => btn.onclick = () => {
      document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("active", b === btn));
      ["dash","plots","health","config"].forEach(id => $(id).hidden = id !== btn.dataset.tab);
    });
    $("scale").oninput = e => document.documentElement.style.setProperty("--widget-width", `${e.target.value}px`);
    $("silence").onclick = () => fetch("/api/buzzer/silence", {method:"POST", body:"{}"});
    $("saveConfig").onclick = async () => {
      await fetch("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:$("configText").value});
      await refresh();
    };
    $("loadPlot").onclick = loadPlot;
    function card(label, value, cls="") { return `<div class="card ${cls}"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`; }
    async function refresh() {
      status = await fetch("/api/status").then(r => r.json());
      $("clock").textContent = new Date(status.time * 1000).toLocaleString();
      $("configText").value = JSON.stringify(status.config, null, 2);
      $("readings").innerHTML = Object.values(status.readings).map(r => {
        let cls = "";
        if (r.id.includes("temp") && typeof r.value === "number") {
          if (r.value > 82) cls = "hot";
          if (r.value < 74) cls = "cold";
        }
        return card(r.id.replaceAll("_", " "), `${r.value} ${r.unit || ""}`, cls);
      }).join("");
      $("equipment").innerHTML = Object.values(status.equipment).map(e => {
        const klass = e.on ? "filled" : "hollow";
        return `<div class="card"><div class="label">${e.id}</div><button class="${klass}" onclick="toggleEquipment('${e.id}', ${!e.on})">${e.on ? "ON" : "OFF"}</button></div>`;
      }).join("");
      $("alarms").innerHTML = Object.values(status.alarms).filter(a => a.active).map(a => card(a.priority, a.message, "alarm")).join("") || card("ok", "No active alarms");
      $("healthGrid").innerHTML = Object.values(status.readings).filter(r => r.id.startsWith("health_")).map(r => card(r.id.replace("health_", "").replaceAll("_", " "), r.value)).join("");
      const streams = Object.keys(status.readings).map(id => id.startsWith("health_") ? `health.${id.replace("health_", "")}` : id.startsWith("ph") ? `ph.${id}` : id.includes("temp") ? `temperature.${id}` : `snapshot.${id}`);
      $("plotStream").innerHTML = [...new Set(streams)].sort().map(s => `<option value="${s}">${s}</option>`).join("");
    }
    async function toggleEquipment(id, on) {
      await fetch("/api/equipment", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id, on})});
      await refresh();
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
