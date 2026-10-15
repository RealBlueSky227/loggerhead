import assert from "node:assert/strict";
import test from "node:test";
import {buildFigure, createPlotsController, defaultSelection} from "../loggerhead/static/plots.mjs";

const catalog = [
  {stream: "temperature.water", kind: "numeric", unit: "F"},
  {stream: "ph.probe", kind: "numeric", unit: "pH"},
  {stream: "analog.voltage", kind: "numeric", unit: "V"},
  {stream: "water.level", kind: "categorical", unit: ""},
  {stream: "equipment.ac1", kind: "numeric", unit: ""},
];
const point = (value, ts = 100) => ({value, ts, unit: ""});

class Element extends EventTarget {
  options = []; value = "86400"; dataset = {}; style = {}; attributes = {}; replacements = 0;
  get selectedOptions() { return this.options.filter(option => option.selected); }
  replaceChildren(...options) { this.options = options; ++this.replacements; }
  setAttribute(name, value) { this.attributes[name] = value; }
}
class Document extends EventTarget {
  elements = new Map();
  getElementById(id) {
    if (!this.elements.has(id)) this.elements.set(id, new Element());
    return this.elements.get(id);
  }
  createElement() { return new Element(); }
}
function fixture(saved) {
  const doc = new Document(), requests = [], renders = [], savedValues = new Map();
  if (saved) savedValues.set("loggerhead.plot.streams.v1", JSON.stringify(saved));
  const state = {clock: 100000, catalog: structuredClone(catalog), error: false, empty: false};
  const request = async (url, {signal}) => {
    requests.push({url, signal});
    if (state.error) return {ok: false, status: 503, json: async () => ({message: "database busy"})};
    return {ok: true, json: async () => url === "/api/history/streams" ? {streams: state.catalog} :
      Object.fromEntries(new URL(url, "http://localhost").searchParams.getAll("stream")
        .map(stream => [stream, state.empty ? [] : [point(stream.startsWith("water.") ? "dry" : 78)]]))};
  };
  const storage = {getItem: key => savedValues.get(key) ?? null, setItem: (key, value) => savedValues.set(key, value)};
  const dependencies = {document: doc, fetch: request, storage, now: () => state.clock,
    plotlyLoader: async () => ({react: async (...args) => renders.push(args)})};
  return {doc, state, storage, requests, renders, dependencies, controller: createPlotsController(dependencies)};
}
async function settle(predicate) {
  for (let attempt = 0; attempt < 100; ++attempt) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 1));
  }
  assert.fail("UI operation did not finish");
}

test("numeric units use separate axes; state and equipment traces use steps", () => {
  const {traces, layout} = buildFigure({
    "temperature.water": [point(78), point(78, 101)],
    "ph.probe": [point(8.1)], "analog.voltage": [point(2.3)],
    "water.level": [point("dry"), point("normal", 101), point("unknown", 102)],
    "equipment.ac1": [point(0), point(1, 101)],
  }, catalog, "86400");
  assert.equal(traces.length, 5);
  assert.equal(new Set(traces.map(trace => trace.yaxis)).size, 5);
  assert.equal(layout.yaxis.title.text, "F");
  assert.equal(layout.yaxis2.title.text, "pH");
  assert.equal(layout.yaxis3.title.text, "V");
  assert.equal(traces[1].mode, "markers");
  assert.deepEqual(traces[3].y, ["dry", "normal", "unknown"]);
  assert.equal(traces[3].line.shape, "hv");
  assert.deepEqual(traces[4].y, ["OFF", "ON"]);
  assert.equal(traces[4].line.shape, "hv");
  assert.equal(layout.xaxis.rangeslider.visible, true);
  assert.equal(layout.uirevision, buildFigure({}, [], "86400").layout.uirevision);
  assert.notEqual(layout.uirevision, buildFigure({}, [], "3600").layout.uirevision);
});

test("single/equal values, missing data, and unknown states remain renderable", () => {
  assert.deepEqual(defaultSelection(catalog), ["temperature.water"]);
  assert.equal(buildFigure({}, [], "3600").points, 0);
  const figure = buildFigure({"water.level": [point("inactive"), point("unrecognized", 101)]}, catalog, "3600");
  assert.deepEqual(figure.traces[0].y, ["inactive", "unrecognized"]);
  assert.equal(figure.layout.yaxis.type, "category");
});

test("selections persist across opens, dashboard polling, catalog refresh, and page reload", async () => {
  const f = fixture();
  await f.controller.open();
  const select = f.doc.getElementById("plotStream");
  assert.deepEqual(select.selectedOptions.map(option => option.value), ["temperature.water"]);
  for (const option of select.options) option.selected = ["ph.probe", "water.level"].includes(option.value);
  select.dispatchEvent(new Event("change"));
  await settle(() => f.renders.length === 2);
  const previousOptions = select.options;
  for (let poll = 0; poll < 20; ++poll) f.doc.dispatchEvent(new Event("dashboard-refresh"));
  assert.equal(f.requests.length, 3);
  await f.controller.open();
  assert.equal(select.options, previousOptions);
  f.state.clock += 31000;
  f.state.catalog = f.state.catalog.map(item => ({...item, count: 1234, last_ts: 1000}));
  await f.controller.open();
  assert.equal(select.options, previousOptions); // Count/timestamp changes cannot destroy selection or focus.
  assert.deepEqual(select.selectedOptions.map(option => option.value), ["ph.probe", "water.level"]);
  f.state.catalog.push({stream: "temperature.old", unit: "F", kind: "numeric"});
  f.doc.dispatchEvent(new Event("loggerhead:config-saved"));
  await f.controller.open();
  assert.equal(select.replacements, 2);
  assert.deepEqual(select.selectedOptions.map(option => option.value), ["ph.probe", "water.level"]);
  const requested = f.requests.filter(item => item.url.startsWith("/api/history?"));
  assert.deepEqual(new URL(requested.at(-1).url, "http://localhost").searchParams.getAll("stream"), ["ph.probe", "water.level"]);
  f.controller.dispose();
  const replacement = createPlotsController(f.dependencies);
  await replacement.open();
  assert.deepEqual(select.selectedOptions.map(option => option.value), ["ph.probe", "water.level"]);
  replacement.dispose();
});

test("catalog is lazy and cached; responsive PNG configuration and errors are visible", async () => {
  const f = fixture();
  assert.equal(f.requests.length, 0);
  await f.controller.open();
  assert.equal(f.requests.filter(item => item.url === "/api/history/streams").length, 1);
  assert.equal(f.renders[0][3].responsive, true);
  assert.equal(f.renders[0][3].toImageButtonOptions.format, "png");
  assert.deepEqual(f.renders[0][3].modeBarButtonsToRemove, ["sendChartToCloud"]);
  assert.equal(f.doc.getElementById("plot").attributes["aria-busy"], "false");
  f.state.empty = true;
  await f.controller.open();
  assert.equal(f.doc.getElementById("plot").dataset.state, "empty");
  f.state.error = true;
  await f.controller.open();
  assert.equal(f.doc.getElementById("plot").dataset.state, "error");
  assert.match(f.doc.getElementById("plotMessage").textContent, /database busy/);
  f.controller.dispose();
});

test("an initially empty catalog selects temperature when telemetry first appears", async () => {
  const f = fixture();
  f.state.catalog = [];
  await f.controller.open();
  f.state.catalog = structuredClone(catalog);
  f.state.clock += 31000;
  await f.controller.open();
  assert.deepEqual(f.doc.getElementById("plotStream").selectedOptions.map(option => option.value), ["temperature.water"]);
  f.controller.dispose();
});

test("superseded history responses never replace a newer chart", async () => {
  const f = fixture();
  let resolveOld;
  let historyCalls = 0;
  const dependencies = {...f.dependencies, fetch: async (url, options) => {
    if (url.startsWith("/api/history?") && historyCalls++ === 0) {
      return new Promise(resolve => { resolveOld = resolve; }); // Deliberately ignore AbortSignal.
    }
    return f.dependencies.fetch(url, options);
  }};
  f.controller.dispose();
  const controller = createPlotsController(dependencies);
  const old = controller.open();
  await settle(() => resolveOld);
  await controller.open();
  resolveOld({ok: true, json: async () => ({"temperature.water": [point(999)]})});
  await old;
  assert.equal(f.renders.length, 1);
  assert.deepEqual(f.renders[0][1][0].y, [78]);
  controller.dispose();
});
