const PLOTLY_URL = "/static/vendor/plotly-basic-4.1.2.min.js";
const STORAGE_KEY = "loggerhead.plot.streams.v1";
const CATALOG_TTL_MS = 30000;
let plotlyPromise;

export function loadPlotly() {
  if (!plotlyPromise) {
    plotlyPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = PLOTLY_URL;
      script.onload = () => window.Plotly ? resolve(window.Plotly) : reject(new Error("Plotly did not initialize"));
      script.onerror = () => { script.remove(); reject(new Error("Could not load the local Plotly asset")); };
      document.head.append(script);
    }).catch(error => { plotlyPromise = undefined; throw error; });
  }
  return plotlyPromise;
}

export function defaultSelection(catalog) {
  const first = catalog.find(item => item.stream.startsWith("temperature.")) ||
    catalog.find(item => item.kind === "numeric") || catalog[0];
  return first ? [first.stream] : [];
}

const LEVEL_ORDER = ["unknown", "inactive", "dry", "low", "normal", "high", "submerged", "wet"];

const localZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

// Plotly date coordinates are UTC. Localize labels, not coordinates: shifting
// timestamps would distort elapsed time or collapse the repeated DST hour.
export function plotDateMs(value) {
  if (typeof value === "number") return value;
  let text = String(value).replace(" ", "T");
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) text += "T00:00:00";
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) text += "Z";
  return Date.parse(text);
}

function hoverTimeFormatter(timeZone) {
  return new Intl.DateTimeFormat(undefined, {timeZone, year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZoneName: "short"});
}

export function localTimeTicks(range, timeZone = localZone(), count = 6) {
  let [start, end] = range.map(plotDateMs);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return {tickvals: [], ticktext: []};
  if (end < start) [start, end] = [end, start];
  if (end === start) { start -= 30000; end += 30000; }
  const intervals = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000,
    60000, 120000, 300000, 600000, 900000, 1800000, 3600000, 7200000, 10800000, 21600000,
    43200000, 86400000, 172800000, 604800000, 2592000000, 31536000000];
  const target = (end-start)/Math.max(1, count-1);
  const step = intervals.find(value => value >= target) || intervals.at(-1);
  const time = new Intl.DateTimeFormat(undefined, {timeZone, hour: "2-digit", minute: "2-digit",
    ...(step < 60000 ? {second: "2-digit"} : {}), ...(step < 1000 ? {fractionalSecondDigits: 3} : {}),
    hourCycle: "h23", timeZoneName: "short"});
  const date = new Intl.DateTimeFormat(undefined, {timeZone, month: "short", day: "numeric"});
  const tickvals = [], ticktext = [];
  for (let value = Math.ceil(start/step)*step; value <= end && tickvals.length < 12; value += step) {
    tickvals.push(new Date(value).toISOString());
    ticktext.push(`${time.format(value)}<br>${date.format(value)}`);
  }
  return {tickvals, ticktext};
}

export function buildFigure(series, catalog, lookback, timeZone = localZone()) {
  const metadata = new Map(catalog.map(item => [item.stream, item]));
  const groups = new Map();
  const traces = [];
  const hoverTime = hoverTimeFormatter(timeZone);
  let start = Infinity, end = -Infinity;
  for (const [stream, raw] of Object.entries(series)) {
    const points = raw.filter(point => Number.isFinite(point.ts) && point.value !== null &&
      (typeof point.value === "string" || Number.isFinite(point.value)));
    if (!points.length) continue;
    for (const point of points) { start = Math.min(start, point.ts*1000); end = Math.max(end, point.ts*1000); }
    const equipment = stream.startsWith("equipment.");
    const categorical = equipment || metadata.get(stream)?.kind === "categorical" ||
      points.some(point => typeof point.value === "string");
    const unit = metadata.get(stream)?.unit || points.find(point => point.unit)?.unit ||
      (stream.startsWith("ph.") ? "pH" : "Value");
    const key = equipment ? "Equipment" : categorical ? stream : unit;
    if (!groups.has(key)) groups.set(key, {key, categorical, labels: equipment ? ["OFF", "ON"] : []});
    const group = groups.get(key);
    const labels = points.map(point => equipment ? (Number(point.value) === 1 ? "ON" : "OFF") : String(point.value));
    if (categorical && !equipment) {
      group.labels = [...new Set([...group.labels, ...labels])].sort((a, b) => {
        const rank = value => LEVEL_ORDER.includes(value) ? LEVEL_ORDER.indexOf(value) : LEVEL_ORDER.length;
        return rank(a) - rank(b) || a.localeCompare(b);
      });
    }
    const axis = [...groups.keys()].indexOf(key) + 1;
    traces.push({
      type: "scatter", name: stream, uid: stream,
      x: points.map(point => new Date(point.ts * 1000).toISOString()),
      customdata: points.map(point => hoverTime.format(point.ts * 1000)),
      y: categorical ? labels : points.map(point => point.value),
      yaxis: axis === 1 ? "y" : `y${axis}`,
      mode: points.length === 1 ? "markers" : "lines",
      marker: {size: 7}, line: {shape: categorical ? "hv" : "linear", width: 2},
      connectgaps: false,
      hovertemplate: categorical ? "%{customdata}<br>%{y}<extra>%{fullData.name}</extra>" :
        `%{customdata}<br>%{y:.3f} ${unit}<extra>%{fullData.name}</extra>`,
    });
  }
  const height = Math.max(520, groups.size * 155 + 150);
  const layout = {
    autosize: true, height, margin: {l: 75, r: 20, t: 25, b: 75},
    paper_bgcolor: "#10171c", plot_bgcolor: "#10171c",
    font: {family: "system-ui, sans-serif", color: "#e9f2f2", size: 12},
    colorway: ["#35d07f", "#39a7ff", "#ffc857", "#ff4b5f", "#78e0dc", "#f78fb3"],
    uirevision: `loggerhead:${lookback}:${timeZone}`, hovermode: "closest", dragmode: "zoom",
    meta: {time_zone: timeZone},
    legend: {orientation: "h", y: 1.04, yanchor: "bottom", x: 0, xanchor: "left"},
    xaxis: {type: "date", anchor: groups.size > 1 ? `y${groups.size}` : "y",
      title: {text: "Local Time"}, tickmode: "array", ...localTimeTicks([start, end], timeZone),
      rangeslider: {visible: true, thickness: 0.10}, gridcolor: "#26343c", automargin: true},
  };
  [...groups.values()].forEach((group, index) => {
    const axis = index === 0 ? "yaxis" : `yaxis${index + 1}`;
    const gap = groups.size > 1 ? 0.035 : 0;
    layout[axis] = {
      title: {text: group.categorical && group.key !== "Equipment" ? "Level" : group.key},
      automargin: true, gridcolor: "#26343c",
      domain: [(groups.size - index - 1) / groups.size + gap, (groups.size - index) / groups.size],
      anchor: "x", uirevision: group.key,
      ...(group.categorical ? {type: "category", categoryorder: "array", categoryarray: group.labels} : {}),
    };
  });
  return {traces, layout, height, timeZone, points: traces.reduce((sum, trace) => sum + trace.x.length, 0)};
}

export function createPlotsController({
  document: doc, fetch: request, storage, plotlyLoader = loadPlotly, now = Date.now,
}) {
  const select = doc.getElementById("plotStream");
  const range = doc.getElementById("lookback");
  const chart = doc.getElementById("plot");
  const message = doc.getElementById("plotMessage");
  let catalog = [], catalogHash = "", catalogExpires = 0;
  let selected = null, pending, generation = 0, renderQueue = Promise.resolve();
  let attached = false, disposed = false, activePlotly, activeTimeZone;
  try {
    const saved = JSON.parse(storage?.getItem(STORAGE_KEY) ?? "null");
    if (Array.isArray(saved) && saved.every(item => typeof item === "string")) selected = saved;
  } catch { /* Browser storage may be disabled. */ }

  function state(kind, text) {
    message.textContent = text;
    message.className = `message ${kind === "error" ? "error" : kind === "ready" ? "ok" : ""}`;
    chart.dataset.state = kind;
    chart.setAttribute("aria-busy", String(kind === "loading"));
  }
  function saveSelection() {
    selected = [...select.selectedOptions].map(option => option.value);
    try { storage?.setItem(STORAGE_KEY, JSON.stringify(selected)); } catch { /* Optional persistence. */ }
  }
  async function updateTimeTicks() {
    const axis = chart._fullLayout?.xaxis;
    if (disposed || !axis?.range || !activePlotly?.relayout) return;
    const width = chart._fullLayout.width || 900;
    const ticks = localTimeTicks(axis.range, activeTimeZone, width < 600 ? 3 : 6);
    if (JSON.stringify(chart.layout.xaxis.tickvals) === JSON.stringify(ticks.tickvals) &&
        JSON.stringify(chart.layout.xaxis.ticktext) === JSON.stringify(ticks.ticktext)) return;
    await activePlotly.relayout(chart, {"xaxis.tickvals": ticks.tickvals, "xaxis.ticktext": ticks.ticktext});
  }
  const onRelayout = event => {
    if (disposed || !Object.keys(event).some(key => key.startsWith("xaxis.range") ||
        key === "xaxis.autorange" || key === "autosize" || key === "width")) return;
    renderQueue = renderQueue.catch(() => {}).then(updateTimeTicks).catch(error => {
      if (!disposed) state("error", `Time axis error: ${error.message}`);
    });
  };
  async function json(url, signal) {
    const response = await request(url, {signal});
    const body = await response.json();
    if (!response.ok || body.ok === false) throw new Error(body.message || `HTTP ${response.status}`);
    return body;
  }
  async function streams(force, signal) {
    if (!force && now() < catalogExpires) return;
    const body = await json("/api/history/streams", signal);
    if (signal.aborted) return;
    const next = [...body.streams].sort((a, b) => a.stream.localeCompare(b.stream));
    const hash = JSON.stringify(next.map(item => [item.stream, item.kind, item.unit]));
    catalog = next;
    catalogExpires = now() + CATALOG_TTL_MS;
    if (hash === catalogHash) return;
    const wanted = selected === null || (selected.length && !catalog.some(item => selected.includes(item.stream))) ?
      defaultSelection(catalog) : selected;
    const options = catalog.map(item => {
      const option = doc.createElement("option");
      option.value = item.stream;
      option.textContent = `${item.stream}${item.unit ? ` (${item.unit})` : ""}`;
      option.selected = wanted.includes(item.stream);
      return option;
    });
    select.replaceChildren(...options);
    catalogHash = hash;
    if (catalog.length || selected !== null) saveSelection();
  }
  async function load({refreshCatalog = false} = {}) {
    const id = ++generation;
    pending?.abort();
    pending = new AbortController();
    const signal = pending.signal;
    state("loading", "Loading history...");
    try {
      await streams(refreshCatalog, signal);
      if (id !== generation || signal.aborted) return;
      if (catalog.length || selected !== null) saveSelection();
      const selection = selected || [];
      if (selection.length > 12) throw new Error("Select at most 12 streams.");
      const query = new URLSearchParams({lookback: range.value});
      selection.forEach(stream => query.append("stream", stream));
      const series = selection.length ? await json(`/api/history?${query}`, signal) : {};
      const figure = buildFigure(series, catalog, range.value);
      const Plotly = await plotlyLoader();
      renderQueue = renderQueue.catch(() => {}).then(async () => {
        if (id !== generation || signal.aborted) return;
        activePlotly = Plotly;
        activeTimeZone = figure.timeZone;
        chart.style.height = `${figure.height}px`;
        await Plotly.react(chart, figure.traces, figure.layout, {
          responsive: true, scrollZoom: true, displaylogo: false,
          modeBarButtonsToRemove: ["sendChartToCloud"],
          toImageButtonOptions: {format: "png", filename: "loggerhead-history", scale: 2},
        });
        if (!attached && typeof chart.on === "function") {
          chart.on("plotly_relayout", onRelayout);
          attached = true;
        }
        await updateTimeTicks();
        if (id !== generation || signal.aborted) return;
        const empty = !selection.length ? (catalog.length ? "Select one or more streams." : "No historical streams recorded yet.") :
          "No data points found in this time range.";
        state(figure.points ? "ready" : "empty", figure.points ? `${figure.points} points loaded.` : empty);
      });
      await renderQueue;
    } catch (error) {
      if (id === generation && !signal.aborted) state("error", `History error: ${error.message}`);
    }
  }
  const onSelection = () => { saveSelection(); void load(); };
  const onRange = () => void load();
  const onRefresh = () => void load({refreshCatalog: true});
  const onConfig = () => { catalogExpires = 0; };
  const refresh = doc.getElementById("loadPlot");
  select.addEventListener("change", onSelection);
  range.addEventListener("change", onRange);
  refresh.addEventListener("click", onRefresh);
  doc.addEventListener("loggerhead:config-saved", onConfig);
  return {
    open: () => load(),
    dispose() {
      disposed = true;
      ++generation;
      pending?.abort();
      select.removeEventListener("change", onSelection);
      range.removeEventListener("change", onRange);
      refresh.removeEventListener("click", onRefresh);
      doc.removeEventListener("loggerhead:config-saved", onConfig);
      if (attached) chart.removeListener?.("plotly_relayout", onRelayout);
    },
  };
}

if (typeof document !== "undefined") {
  let storage;
  try { storage = window.localStorage; } catch { /* Optional persistence. */ }
  const controller = createPlotsController({document, fetch: window.fetch.bind(window), storage});
  document.addEventListener("loggerhead:plots-open", () => void controller.open());
}
