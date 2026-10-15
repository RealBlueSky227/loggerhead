# Plots runtime and validation

The dashboard uses the vendored `plotly.js-basic-dist-min` 4.1.2 browser bundle
(MIT), not a CDN, Python Plotly, or Dash. The existing standard-library HTTP
server and SQLite database remain in use. No Node installation is needed on
the Raspberry Pi. The Plotly JavaScript is 1,198,523 bytes; the precompressed
405,016-byte gzip asset avoids compression work on the Pi. It loads only when
Plots opens. The default Plotly cloud-sharing button is removed.

## Runtime boundaries

- `loggerhead/static/plots.mjs` owns selection, fetching, Plotly traces, loading
  states, and rendering. Normal dashboard refresh never invokes plot code.
- Selections use browser local storage. Catalog changes preserve remaining
  selections; initial selection prefers an available `temperature.*` stream.
- Plots loads `/api/history/streams` on opening, with a 30-second browser cache.
  Refresh forces catalog reload. Saving configuration invalidates the browser
  catalog without starting a background plot request.
- `/api/status` no longer includes `history_streams`. It copies application
  state under the state lock, then reads recent events after releasing it.
- SQLite maintains a `telemetry_streams` summary using insert/delete triggers.
  Existing databases are backfilled once in a transaction at startup. Subsequent
  catalogs query only that small table. Python caches it for 30 seconds, with
  immediate invalidation for locally written new stream IDs, units, or kinds
  and retention deletions. Other processes' inserts/deletes become visible at
  the next TTL refresh. Telemetry is append-only apart from retention deletes;
  direct SQL UPDATEs to historical telemetry are not a supported write API.
- The history API preserves the existing `{stream: [{ts, value, unit}]}` shape.
  It accepts repeated `stream` parameters, at most 12 streams, finite lookbacks
  from 1 second to 7 days, and no more than 2,000 points per stream or 8,000
  total. The configured `plot_max_points` remains the usual per-stream bound.
- Sparse histories are exact. Dense histories use bounded indexed time-uniform
  seeks and retain the first/last row, rather than fetching/scanning an entire
  history in Python. Repeated target hits are deduplicated. Sampling can omit
  brief spikes or state changes; chart history is not a complete event audit.
  Select a shorter API time range for more detail. Browser range-slider zoom
  operates on already returned points and does not fetch additional resolution.
- History queries use WAL, a 1-second SQLite busy timeout, and a 5-second query
  deadline. A busy/interrupted query returns structured HTTP 503; no partial
  result is presented as complete. Every connection closes deterministically.
- Units get separate, vertically aligned Y axes with a shared time axis.
  HYDROS/binary strings remain categorical values, including UNKNOWN/INACTIVE.
  Equipment uses OFF/ON steps. State lines use horizontal-then-vertical steps;
  one-point histories use markers. `Plotly.react` and stable `uirevision`
  preserve zoom until the selected time range changes. The range slider,
  toolbar pan/zoom, hover, resize, and local PNG export run in the browser.

## 1-Wire helper status coalescing

The client serializes and coalesces successful helper status calls for at most
2 seconds, only while the root-owned ownership file, configfs overlay entries,
and kernel master directory identities/timestamps remain unchanged. It checks
those fingerprints on every status request and before/after refreshing the
helper result. Missing/inaccessible evidence disables caching entirely.

Activation/removal always calls the privileged helper and invalidates the
cache, including on failure. Exceptions never reuse a previous cached success.
The temperature reader still verifies the bus-to-GPIO/platform relationship
and sensor membership on each read. No GPIO, overlay ownership policy, driver
configuration, or control decision was changed. Confirm the target Pi exposes
readable fingerprint paths; otherwise correctness is preserved but helper-call
reduction will be smaller.

## Deployment

Keep the complete Python package and its `static` directory together. Installing
a wheel includes the assets through `pyproject.toml` package data. Source/scp
deployments must include `loggerhead/static/plots.mjs` and all files under
`loggerhead/static/vendor/`, including the gzip bundle and license.

For the existing source deployment, use the same venv already used to launch
Loggerhead (`~/controller/venv`, or `~/controller/loggerhead/.venv` for the
installer). Stop Loggerhead during the planned update and back up its data
directory before the first SQLite catalog migration. Update the source,
then restart using the existing launch command/service. Neither privileged
helper reinstallation nor GPIO/boot overlay changes are needed for this update.
Keep the application unprivileged.

Example scp from the Windows repository directory:

```powershell
scp loggerhead/web.py loggerhead/database.py loggerhead/service.py loggerhead/onewire.py reef@192.168.60.3:/home/reef/controller/loggerhead/loggerhead/
scp -r loggerhead/static reef@192.168.60.3:/home/reef/controller/loggerhead/loggerhead/
```

Open Plots and refresh the page once after deployment. The application module
uses ETag revalidation; the versioned vendor bundle is immutable-cacheable.
Rollback restores the previous Python files and browser page. The additional
SQLite summary table/triggers are compatible with the previous append/delete
telemetry API; do not delete existing telemetry or alarms.

## Automated checks

```bash
python -m pytest
python -m ruff check .
node --test tests/plots.test.mjs
```

`tests/plots.browser.mjs` exercises a real local dashboard/Plotly asset in a
Chromium browser: lazy requests, polling isolation, selections, multiple axes,
zoom preservation, pan, hover, PNG download, desktop/mobile layout, empty data,
and API errors. It starts an isolated simulation fixture and terminates it
afterward. Install Playwright only on a development machine, not on the Pi:

```bash
npm install --no-save playwright
npx playwright install chromium
node tests/plots.browser.mjs
```

Environment overrides: `LOGGERHEAD_PYTHON` for the test interpreter,
`LOGGERHEAD_PLAYWRIGHT_MODULE` for an existing Playwright package,
`LOGGERHEAD_BROWSER_EXECUTABLE` for an installed Chromium-based browser,
and `LOGGERHEAD_SCREENSHOT_DIR` for screenshots. Default Python is `.venv`.

## Benchmarking on the Pi

The benchmark creates a temporary database and simulation-only service. It
does not open hardware, start acquisition/control workers, change configuration
in the running controller, or operate any motor/equipment. It does consume CPU,
memory and disk, so run the larger seed benchmark during a maintenance window.

```bash
cd ~/controller/loggerhead
. ~/controller/venv/bin/activate  # Or . .venv/bin/activate for the installer.
python -m scripts.benchmark_plots --rows 300000 --iterations 20
```

Results include platform, cold/cached catalog latency, status/history median
and p95 latency, status under concurrent history load, response sizes, and
process CPU time. CPU includes both local HTTP client and server. The projected
2-second status CPU percentage is a synthetic single-client estimate, not a
measurement of the full live controller. Cold measurements include one request;
desktop timer granularity and browser/test workloads can affect CPU figures.

Development-machine example (Windows x64, 300,000 rows, six streams,
20 requests per endpoint): cached catalog median 3.5 ms; status median 10.0 ms;
seven-day/six-stream history median 133.9 ms, p95 214.8 ms, about 268 KB JSON;
status during history median 10.2 ms. These are **not Pi Zero measurements**.

Remaining hardware validation: run the script on the Pi Zero with its actual
SD card, available RAM and Python 3.13; monitor live status/control latency
during normal sensor acquisition and browser use; verify helper call counts and
ownership invalidation on the real configfs/sysfs topology. Confirm fresh/stale
sensor fail-safes during authorized sensor changes with hazardous equipment
disconnected. No actual Raspberry Pi or pump was operated by these tests.
