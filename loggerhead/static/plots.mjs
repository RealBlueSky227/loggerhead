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

export function buildFigure(series, catalog, lookback) {
  const metadata = new Map(catalog.map(item => [item.stream, item]));
  const groups = new Map();
  const traces = [];
  for (const [stream, raw] of Object.entries(series)) {
    const points = raw.filter(point => Number.isFinite(point.ts) && point.value !== null &&
      (typeof point.value === "string" || Number.isFinite(point.value)));
    if (!points.length) continue;
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
      y: categorical ? labels : points.map(point => point.value),
      yaxis: axis === 1 ? "y" : `y${axis}`,
      mode: points.length === 1 ? "markers" : "lines",
      marker: {size: 7}, line: {shape: categorical ? "hv" : "linear", width: 2},
      connectgaps: false,
      hovertemplate: categorical ? "%{x}<br>%{y}<extra>%{fullData.name}</extra>" :
        `%{x}<br>%{y:.3f} ${unit}<extra>%{fullData.name}</extra>`,
    });
  }
  const height = Math.max(520, groups.size * 155 + 150);
  const layout = {
    autosize: true, height, margin: {l: 75, r: 20, t: 25, b: 50},
    paper_bgcolor: "#10171c", plot_bgcolor: "#10171c",
    font: {family: "system-ui, sans-serif", color: "#e9f2f2", size: 12},
    colorway: ["#35d07f", "#39a7ff", "#ffc857", "#ff4b5f", "#78e0dc", "#f78fb3"],
    uirevision: `loggerhead:${lookback}`, hovermode: "closest", dragmode: "zoom",
    legend: {orientation: "h", y: 1.04, yanchor: "bottom", x: 0, xanchor: "left"},
    xaxis: {type: "date", anchor: groups.size > 1 ? `y${groups.size}` : "y",
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
  return {traces, layout, height, points: traces.reduce((sum, trace) => sum + trace.x.length, 0)};
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
        chart.style.height = `${figure.height}px`;
        await Plotly.react(chart, figure.traces, figure.layout, {
          responsive: true, scrollZoom: true, displaylogo: false,
          modeBarButtonsToRemove: ["sendChartToCloud"],
          toImageButtonOptions: {format: "png", filename: "loggerhead-history", scale: 2},
        });
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
      ++generation;
      pending?.abort();
      select.removeEventListener("change", onSelection);
      range.removeEventListener("change", onRange);
      refresh.removeEventListener("click", onRefresh);
      doc.removeEventListener("loggerhead:config-saved", onConfig);
    },
  };
}

if (typeof document !== "undefined") {
  let storage;
  try { storage = window.localStorage; } catch { /* Optional persistence. */ }
  const controller = createPlotsController({document, fetch: window.fetch.bind(window), storage});
  document.addEventListener("loggerhead:plots-open", () => void controller.open());
}
