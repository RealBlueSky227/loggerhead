// Run with Playwright installed for development; no browser tooling is needed on the Pi.
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {createInterface} from "node:readline";
import {plotDateMs} from "../loggerhead/static/plots.mjs";

const require = createRequire(import.meta.url);
const engines = require(process.env.LOGGERHEAD_PLAYWRIGHT_MODULE || "playwright");
const engine = process.env.LOGGERHEAD_BROWSER_ENGINE || "chromium";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const python = process.env.LOGGERHEAD_PYTHON || path.join(root, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
const server = spawn(python, ["scripts/benchmark_plots.py", "--serve", "--rows", "600"], {
  cwd: root, env: {...process.env, PYTHONPATH: root}, stdio: ["pipe", "pipe", "pipe"], windowsHide: true,
});
let browser;
try {
  const address = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Fixture server startup timed out")), 15000);
    server.on("exit", code => { clearTimeout(timer); reject(new Error(`Fixture server exited: ${code}`)); });
    server.stderr.on("data", data => process.stderr.write(data));
    const lines = createInterface({input: server.stdout});
    lines.on("line", line => {
      if (!line.startsWith("{")) return;
      clearTimeout(timer);
      resolve(JSON.parse(line));
    });
  });
  browser = await engines[engine].launch({headless: true,
    ...(engine === "chromium" && process.env.LOGGERHEAD_BROWSER_EXECUTABLE ? {executablePath: process.env.LOGGERHEAD_BROWSER_EXECUTABLE} : {}),
  });
  const context = await browser.newContext({viewport: {width: 1365, height: 900}, acceptDownloads: true,
    timezoneId: "America/New_York", locale: "en-US"});
  const page = await context.newPage();
  const errors = [], calls = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", entry => { if (entry.type() === "error") console.error("Browser:", entry.text()); });
  page.on("request", request => calls.push(request.url()));
  await page.goto(`http://127.0.0.1:${address.port}`);
  await page.waitForSelector("#heartbeatDot.ok");
  assert.equal(calls.filter(url => url.includes("/api/history")).length, 0);
  assert.equal(calls.filter(url => url.includes("plotly-basic")).length, 0);
  await page.getByRole("button", {name: "Plots", exact: true}).click();
  await page.waitForSelector('#plot[data-state="ready"]');
  await page.selectOption("#plotStream", ["temperature.water", "ph.probe", "analog.voltage", "water.level", "equipment.ac1"]);
  await page.waitForFunction(() => document.getElementById("plot").data?.length === 5);
  const chart = () => page.evaluate(() => {
    const element = document.getElementById("plot");
    return {traces: element.data.length, units: [element.layout.yaxis.title.text, element.layout.yaxis2.title.text],
      range: element.layout.xaxis.range, revision: element.layout.uirevision, width: element.getBoundingClientRect().width};
  });
  assert.equal((await chart()).traces, 5);
  assert.equal(await page.locator("#plot .cartesianlayer").first().locator(".scatterlayer .trace").count(), 5);
  const counts = calls.filter(url => url.includes("/api/history")).length;
  await page.locator("#plotStream option").first().evaluate(element => { element.dataset.identity = "preserved"; });
  await page.waitForTimeout(4300);
  assert.equal(calls.filter(url => url.includes("/api/history")).length, counts);
  assert.equal(await page.locator("#plotStream option").first().getAttribute("data-identity"), "preserved");
  await page.evaluate(async () => {
    const chart = document.getElementById("plot"), data = chart.data[0].x;
    await window.Plotly.relayout(chart, {"xaxis.range": [data[2], data[10]], "xaxis.autorange": false});
  });
  const zoomed = (await chart()).range;
  await page.getByRole("button", {name: "Refresh", exact: true}).click();
  await page.waitForSelector('#plot[data-state="ready"]');
  (await chart()).range.forEach((value, index) => {
    assert.ok(Math.abs(plotDateMs(value)-plotDateMs(zoomed[index])) < 1,
      `Refresh must preserve the zoomed time range: ${JSON.stringify({value, before: zoomed[index]})}`);
  });
  await page.locator('#plot [data-title="Pan"]').click();
  assert.equal(await page.evaluate(() => document.getElementById("plot")._fullLayout.dragmode), "pan");
  await page.evaluate(() => window.Plotly.Fx.hover(document.getElementById("plot"), [{curveNumber: 0, pointNumber: 3}]));
  assert.ok(await page.locator("#plot .hovertext").count());
  await page.evaluate(() => window.Plotly.Fx.unhover(document.getElementById("plot")));
  assert.equal(await page.locator('#plot [data-title="Share chart..."]').count(), 0);
  const downloadPromise = page.waitForEvent("download", {timeout: 10000}).catch(error => ({testError: error.message}));
  await page.locator('#plot [data-title="Download plot as a PNG"]').click();
  const download = await downloadPromise;
  if (download.testError) {
    console.error("Export notification:", await page.locator(".plotly-notifier").allTextContents());
    console.error("Page errors:", errors);
    throw new Error(download.testError);
  }
  assert.match(download.suggestedFilename(), /loggerhead-history.*\.png$/);
  assert.equal(await download.failure(), null);
  await page.locator(".plotly-notifier").evaluateAll(elements => elements.forEach(element => element.remove()));
  const screenshots = process.env.LOGGERHEAD_SCREENSHOT_DIR;
  if (screenshots) {
    await mkdir(screenshots, {recursive: true});
    await page.screenshot({path: path.join(screenshots, "plots-desktop.png"), fullPage: true});
  }
  await page.setViewportSize({width: 390, height: 844});
  await page.waitForTimeout(500);
  assert.ok((await chart()).width <= 366);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  const boxes = await page.evaluate(() => {
    const chart = document.getElementById("plot");
    const legend = chart.querySelector(".legend").getBoundingClientRect();
    const slider = chart.querySelector(".rangeslider-container").getBoundingClientRect();
    return {legendBottom: legend.bottom, sliderTop: slider.top};
  });
  assert.ok(boxes.legendBottom < boxes.sliderTop, "Legend must not overlap the range slider");
  if (screenshots) await page.screenshot({path: path.join(screenshots, "plots-mobile.png"), fullPage: true});
  await page.route("**/api/history?**", route => route.fulfill({status: 503, json: {ok: false, message: "database busy"}}));
  await page.getByRole("button", {name: "Refresh", exact: true}).click();
  await page.waitForSelector('#plot[data-state="error"]');
  assert.match(await page.locator("#plotMessage").textContent(), /database busy/);
  await page.unroute("**/api/history?**");
  await page.route("**/api/history?**", route => route.fulfill({json: {"temperature.water": []}}));
  await page.getByRole("button", {name: "Refresh", exact: true}).click();
  await page.waitForSelector('#plot[data-state="empty"]');
  await page.unroute("**/api/history?**");
  const fallPoints = [{ts: Date.parse("2026-11-01T05:30:00Z")/1000, value: 78, unit: "F"},
    {ts: Date.parse("2026-11-01T06:30:00Z")/1000, value: 79, unit: "F"}];
  await page.route("**/api/history?**", route => route.fulfill({json: {"temperature.water": fallPoints}}));
  await page.selectOption("#plotStream", ["temperature.water"]);
  await page.waitForFunction(() => document.getElementById("plot").data?.[0]?.x?.length === 2);
  await page.evaluate(() => window.Plotly.relayout(document.getElementById("plot"), {"xaxis.autorange": true}));
  await page.waitForFunction(() => {
    const ticks = document.getElementById("plot").layout.xaxis.ticktext || [];
    return ticks.some(text => text.includes("EDT")) && ticks.some(text => text.includes("EST"));
  });
  assert.equal(await page.evaluate(() => {
    const chart = document.getElementById("plot");
    return Date.parse(chart.data[0].x[1])-Date.parse(chart.data[0].x[0]);
  }), 3600000);
  for (const [index, zone] of [[0, "EDT"], [1, "EST"]]) {
    await page.evaluate(index => window.Plotly.Fx.hover(document.getElementById("plot"), [{curveNumber: 0, pointNumber: index}]), index);
    const hover = await page.locator("#plot .hovertext").allTextContents();
    assert.ok(hover.some(text => text.includes("01:30:00") && text.includes(zone)), hover.join(" "));
    await page.evaluate(() => window.Plotly.Fx.unhover(document.getElementById("plot")));
  }
  const beforeZoom = calls.filter(url => url.includes("/api/history")).length;
  await page.evaluate(() => window.Plotly.relayout(document.getElementById("plot"), {
    "xaxis.range": ["2026-11-01T06:10:00Z", "2026-11-01T06:40:00Z"], "xaxis.autorange": false,
  }));
  await page.waitForFunction(() => document.getElementById("plot").layout.xaxis.ticktext.every(text => text.includes("EST")));
  assert.equal(calls.filter(url => url.includes("/api/history")).length, beforeZoom);
  assert.deepEqual(errors, []);
  assert.equal(calls.filter(url => url.startsWith("http") && new URL(url).hostname !== "127.0.0.1").length, 0);
  console.log("Browser checks passed: local assets, polling isolation, selection, multiple axes, zoom, pan, hover, PNG, desktop/mobile, errors, empty data, local timezone and DST.");
} finally {
  await browser?.close();
  const exited = new Promise(resolve => server.once("exit", resolve));
  if (server.exitCode === null) {
    server.stdin.on("error", () => {});
    server.stdin.end("shutdown\n");
    const timeout = setTimeout(() => server.kill(), 3000);
    try { await exited; } finally { clearTimeout(timeout); }
  }
}
