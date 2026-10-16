# Runtime Performance and Calibration Responsiveness

## Investigation: October 16, 2026

The reported incident was Safari on an iPhone around 9 PM ET on October 15.
Neither the retained service/kernel journal nor SQLite events contained entries
for 20:45-21:30 ET. The historical cause cannot be established from those logs.
The following is a reproduced failure mechanism, not proof of that incident.

Calibration startup held `_prime_lock` while saving/fsyncing runtime state.
Status requests require that lock through the dosing snapshot. A slow save
therefore blocked status. The browser launched another poll every two seconds
without an in-flight limit or deadline, exhausting its same-origin connections.
Even fetching the home page then appeared hung. Stop also waited on the lock.
Completion persistence had the same lock-scope issue.

Changes:

- Reserve a pending start under the pump lock, then persist without holding it.
  The configuration/start lock still serializes new starts and configuration
  updates. Recheck both the per-operation Stop event and service shutdown before
  starting the motor thread. A cancelled pending command never starts later.
- Retain the Stop event until the worker has exited/finished hardware cleanup.
  Persist completion outside the pump lock. Preserve actual motor fault alarms.
- Allow only one browser status request at a time; abort it after eight seconds.
  HTTP errors/invalid status mark the heartbeat stale, and later polls recover.
  Do not automatically retry calibration or other actuator POST commands.
- Add bounded operation timing and a read-only thread inventory, independent of
  equipment/prime locks. No extra background monitoring thread is created.

## Fault Injection Results

All calibration fault injection used separate temporary configurations and
databases, `simulation=True`, and a mocked motor operation. No physical dosing
pump was energized. The injected delay was **20 seconds** in calibration-start
persistence, not a destructive SD-card or GPIO fault.

| WebKit, iPhone-sized viewport | Before, Windows fixture | After, Windows fixture | After, Pi fixture |
| --- | ---: | ---: | ---: |
| Status median during delay | 6,410 ms | 20 ms | 183 ms |
| Status maximum during delay | 18,471 ms | 71 ms | 294 ms |
| Peak outstanding browser requests | 11 | 2 | 2 |
| New home-page fetch | Timed out after 2,022 ms | 20 ms | 63 ms |
| Stop request | Timed out after 2,011 ms | 62 ms | 103 ms |
| Pending calibration after Stop | Old implementation could start before stopping | Cancelled before motion | Cancelled before motion |

The Pi fixture ran alongside the real controller but used no real hardware.
Its ordinary mock calibration completed 4,000 steps in the accounting fixture;
this does **not** establish physical pulse timing or delivery accuracy. The
prime worker consumed 26 ms of CPU over approximately 2.05 seconds wall time
in that mock run. Its motor is an Event wait, not pigpio waveform generation.

## Live Per-Thread Measurements

Pi Zero, Python 3.13, 90.109-second production sample after restart, with read-only
dashboard/configuration/plots traffic. The simulation fixture had exited before
this sample. CPU percentages are fractions of **one core**, from `/proc` tick
deltas, not lifetime `ps %CPU` values.

| Persistent thread | CPU | Notes |
| --- | ---: | --- |
| HYDROS pigpio notification receiver | 27.001% | SensePort 2, GPIO17, HIGH, about 396.83 Hz rising edges |
| Control/safety loop (`loggerhead-poll`) | 7.069% | 70 iterations; mean measured work 273 ms, plus 1-second wait |
| SensePort 4 DS18B20 worker | 1.753% | GPIO5; 10-second interval; CRC-valid, 12-bit, externally powered |
| Stepper diagnostics | 0.422% | Nine iterations, 364 ms aggregate CPU |
| HTTP accept thread | 0.244% | Per-request CPU is additional, below |
| Dosing scheduler | 0.122% | Idle: no schedules enabled |
| Main thread | 0.089% | Service lifecycle wait |
| HYDROS publication worker | 0.044% | Four-second interval, separate from edge capture |
| CPU-temperature worker | 0.022% | Ten-second interval |
| Buzzer pigpio notification receiver | 0.000% | No notification processing observed |
| Stepper pigpio notification receiver | 0.000% | Motors idle |

There were 11 persistent Python threads, with a peak of 12 OS threads in the
one-second samples. Short-lived HTTP threads can start and finish between
samples; their CPU is captured by named-operation instrumentation instead:

| Operation over the same sample | Count | Aggregate thread CPU | Aggregate wall time |
| --- | ---: | ---: | ---: |
| Status HTTP requests | 75 | 5.311 s | 11.372 s |
| History request | 1 | 0.560 s | 1.907 s |
| Historical stream catalog | 1 | 0.022 s | 0.057 s |
| Performance observer requests | 10 | 0.412 s | 1.256 s |
| State persistence, nested within control work | 70 | 3.655 s | 9.749 s |

Nested operation timings overlap: **do not add** state persistence to control
CPU, or status construction to HTTP CPU. The mean HTTP-status CPU was 70.8 ms
per request; state persistence accounted for about 58% of measured control CPU.
Synchronous state serialization/fsync remains an optimization target, not a
claim that all storage stalls have been eliminated. A real prolonged storage
failure can still delay persistence-dependent actions and the control loop;
Stop publishes cancellation before waiting for persistence, but an HTTP Stop
acknowledgment can still be delayed by genuinely blocked storage.

A 45.11-second pre-change sample showed the same two dominant threads at 27.8%
and 7.27%. This is a responsiveness fix, **not** evidence of a large steady-state
CPU reduction. Signal state, other clients, and sample durations affect load.
The earlier dry HYDROS signal was about 20 Hz; the present 397 Hz signal has a
much higher callback load. Thresholds, edge capture, and safety were not changed.
Separate `pigpiod` lifetime CPU was 16.9% when inspected, not an interval-matched
thread measurement. Memory available was 213 MiB; swap used 24 MiB; temperature
51.4 C; `get_throttled` was `0x0`. These are current observations, not evidence
about host conditions at the historical outage.

Live sensors remained online, the DS18B20 success timestamp advanced, and motors
remained idle. The existing HIGH water-level alarm was retained, not cleared.
MQTT/network-notification and active physical prime/dose execution were not
benchmarked as active workloads because they were not enabled/run for this test.

## Browser Measurements

Live WebKit 26.5, 390x664 CSS-pixel iPhone 13 emulation, 20 seconds per tab:

| Tab | Status median / max | Maximum event-loop lag | Result |
| --- | --- | ---: | --- |
| Dashboard | 220 / 348 ms | 26 ms | Connected |
| Config | 245 / 318 ms | 25 ms | Connected |
| Plots | 238 / 477 ms | 234 ms | Chart ready, connected |

No page errors or failed requests occurred. Only one status poll was outstanding;
the two-request peak on Plots includes its history/catalog traffic. Opening
Plotly has a brief initialization cost. An earlier live Chromium sample showed
a 400 ms cold-plot long task; this is distinct from the reproduced multi-second
connection backlog. The delayed-calibration WebKit main-thread lag was only
45 ms before the fix: the failure was blocked connections, not a spinning JS loop.

The final live Chromium run (10 seconds per tab) also remained connected:

| Tab | Browser main-thread task / script time | JS heap | Status median / max |
| --- | --- | ---: | --- |
| Dashboard | 94.9 / 10.2 ms | 3.04 MB | 282 / 323 ms |
| Config | 91.7 / 8.8 ms | 3.31 MB | 186 / 290 ms |
| Plots | 394.1 / 149.7 ms | 6.15 MB | 232 / 535 ms |

Plots included one 278 ms initialization long task. These CDP main-thread task
durations are not total browser-process CPU measurements. Different-length,
non-simultaneous samples are not a controlled browser-speed comparison.

These browsers ran on **Windows**, not the Pi or an actual iPhone. WebKit does
not expose Chromium CDP CPU metrics, and lack of long-task entries there is not
proof of zero long tasks. The script reports event-loop lag for both engines.
Real Safari CPU/memory, Wi-Fi loss, background-tab suspension, and physical
calibration under DMA/UART load still require a supervised iPhone/Pi check.

## Repeating or Capturing an Incident

Normal diagnostics never actuate hardware:

```bash
cd ~/controller/loggerhead
PID=$(systemctl show loggerhead --property MainPID --value)
../venv/bin/python scripts/benchmark_threads.py --pid "$PID" --seconds 90
curl -fsS 'http://127.0.0.1:8080/api/performance?stacks=1'
```

`/api/performance` reports native thread IDs, pigpio owner roles, active-operation
age, counts, CPU/wall timings, and recent-128 completion p95. `stacks=1` adds
bounded file basenames/functions/lines, never local-variable values. It does not
take controller locks or perform sensor/privileged-helper operations. Treat it
as a LAN-only diagnostic like the existing controller API; do not expose it to
the Internet. Measurements reset at process restart. Sampler operation deltas
cover its interval; recent p95 may include earlier completions. Sampling itself
has overhead and may miss short-lived thread IDs.

With Playwright installed on a separate browser machine:

```bash
node scripts/benchmark_browser.mjs --url http://192.168.60.3:8080 --engine webkit --iphone
node scripts/benchmark_browser.mjs --url http://192.168.60.3:8080 --engine chromium
```

Live mode blocks mutating browser requests. `--calibration` refuses targets
without the simulation fixture's identity endpoint. Do not use it against the
production service. For isolated reproduction, run from the repository:

```bash
PYTHONPATH=. ../venv/bin/python scripts/benchmark_calibration_fixture.py --port 8081 --lifetime 180
node scripts/benchmark_browser.mjs --url http://127.0.0.1:8081 --seconds 3 --calibration --engine webkit --iphone
```

The fixture binds localhost unless explicitly changed, uses a temporary data
directory, and stops automatically at its lifetime limit. Close it after use.
Record the exact timestamp/timezone and fetch performance stacks, service and
kernel journals, host memory/throttling, and client network errors during any
recurrence. Never retry an uncertain motor command automatically.

## Deployment and Validation

The fix and instrumentation were backed up/deployed to the Pi, then the user
restarted the service. Backup: `~/controller/backups/runtime-benchmark-20261016-014540`.
Configuration, hardware drivers, overlay helper, current settings, GPIO map,
dosing calibration, schedules, and systemd boot-enable preference were unchanged.
The service remains unprivileged and was already disabled at boot. The temporary
Pi fixture exited; only the production controller remains running.

For another installation, copy the Python package including `performance.py`,
the updated `static/dosing.mjs`, and desired benchmark scripts, then restart the
existing service. No new runtime dependencies are needed. Browser benchmarks
require development-only Playwright and its WebKit/Chromium browser; none were
installed on the Pi.

Validation: 267 mocked Python tests, 12 JavaScript unit tests, existing dosing
and plots browser tests, the new WebKit single-flight/timeout/error/recovery
test, and Ruff passed. Added Python tests cover blocked persistence, genuine
write-lock contention, cancellation before motion/shutdown, retained Stop events,
failed start persistence, completion persistence, bounded metrics, and HTTP
diagnostics while controller locks are held. No automated test operated pumps.
