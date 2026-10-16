# Bounded HYDROS PWM Acquisition

## Implementation

`HydrosPulseReader` now keeps one dedicated pigpio connection and notification
thread for its configured physical SensePort GPIO, but subscribes only while
the existing sensor worker is acquiring a measurement. There is no new sampling
thread or thread-per-poll design. Non-ATO sensors retain their configured polling
frequency. No GPIO mapping, frequency window, relay, dosing, or overlay ownership
was changed. Configuration field names and existing JSON remain compatible.

Each acquisition:

1. Resets its classification/streak and records pigpio's current 32-bit tick.
2. Registers a rising-edge callback on this reader's GPIO.
3. Requires `max(8, debounce_samples)` consecutive periods in the same existing
   HIGH/NORMAL/LOW/DRY window. An invalid or different period resets the streak.
   A majority of mixed classifications is never considered a valid reading.
4. Wakes its worker through an Event when complete, without socket commands in
   the callback. It aborts noisy captures after `4 * required_periods + 1` edges.
5. Cancels the callback and explicitly pauses its dedicated notification handle,
   checking pigpio's return value before publishing a valid result.

The installed pigpio code was inspected read-only on the Pi. Its callback
`cancel()` removes the GPIO from `_callback_thread.monitor` and sends the new
notification mask to pigpiod. This stops the underlying edge notifications, not
just application callback processing. Explicit `notify_pause()` additionally
checks daemon acknowledgment because `cancel()` discards the command result.
The handle comes from the connection's `_notify.handle`; this narrow reliance
on pigpio internals is isolated in the reader. Revalidate it when upgrading
pigpio. The dedicated handle never pauses buzzer/stepper/other sensor handles.
See the [upstream implementation](https://github.com/joan2937/pigpio/blob/master/pigpio.py).

While paused the notification thread remains socket-blocked, preserving its
connection for reuse. pigpiod may still send occasional keepalive messages;
there is no promise of literally zero wakeups forever. No glitch/noise filters,
PWM, waves, kernel ownership, or other daemon-global GPIO settings are modified
by capture/cancellation. Normal initial GPIO input/pull-up setup is preserved.

## Validity, Cancellation, and Cleanup

- The default acquisition deadline is 0.75 seconds: eight maximum-length dry
  periods (480 ms), phase/scheduling margin, and cleanup. Longer configured
  debounce counts increase the deadline, but it is capped at five seconds.
  Excessive legacy debounce counts therefore time out rather than returning an
  unverified value. Noise cannot keep an acquisition running indefinitely.
- `activity_timeout` means **missing edges during an active acquisition**. It no
  longer means time since an edge while intentionally unsubscribed. Overall
  sensor freshness is still enforced separately by monotonic worker timestamps.
- A stopped/malformed/unstable acquisition returns INACTIVE or UNKNOWN; it never
  returns the preceding valid level. Errors leave the worker failed/invalid and
  use existing sensor alarms/recovery handling. Diagnostics retain the previous
  successful measurement timestamp only as explicitly aged historical evidence.
- Each callback closes over its own capture object. Identity checks reject old
  callbacks. Pre-arm queued pigpio ticks are rejected using modulo-32-bit
  comparisons; period differences also handle tick rollover. Ticks and readings
  are never attributed to another physical port.
- Waiting is Event-driven, with cancellation checked at most every 25 ms during
  capture. Commands on the dedicated connection use one-second socket timeouts.
  Actual cancellation completion can additionally include bounded cancel/pause
  commands; the worker stop deadline remains 15 seconds. Socket scheduling and
  Linux load are not hard real-time guarantees.
- A subscription/pause failure invalidates data, retires the connection, and
  latches a reader fault requiring safe SensePort reinitialization. Missing
  sensor pulses recover normally on the next acquisition without new sockets.
  The initial `pigpio.pi()` connection handshake still uses the installed
  library's constructor behavior; this change does not make a frozen daemon's
  initial connection handshake interruptible.
- Close cancels an active acquisition, pauses notifications, stops the connection,
  and joins its notification thread. If cleanup cannot be verified, the service
  retains that failed reader and blocks GPIO handoff. It does not simply log and
  give another backend the pin. Repeated healthy reads reuse the same thread and
  sockets; repeated configuration changes stop/close before replacement.

The refactor also fixes a publication defect exposed by blocking acquisition:
an attempt's timestamp advances before it has a new value. Consuming that attempt
while it is still reading can hide its eventual result. Workers now publish
explicit monotonic completion timestamps; central control consumes completions
once, including when an overrunning worker has already started its next read.

## ATO Latency and Fail-Safe Policy

Periodic capture necessarily has blind intervals. Even eight good periods cannot
detect a short transient that begins and ends entirely between acquisitions.
This is a safety tradeoff, not equivalent to continuous edge observation.

For both inputs of every **enabled** ATO profile, the effective HYDROS interval
is capped at one second (`min(configured_interval, 1 second)`), even before the
pump starts. This avoids a slow-to-fast scheduling transition while pumping.
Configured faster intervals are preserved (the worker's existing minimum
interval remains 100 ms). Their freshness/read-in-progress limit is at most two
seconds. The saved configuration is not rewritten. Worker/reader diagnostics
expose configured and effective intervals and whether the sensor is an ATO input.
Disabled ATO profiles do not accelerate their sensors. Binary inputs retain
their existing acquisition behavior. Enabling/disabling ATO or changing its
input references rebuilds the effective worker schedules through the existing
safe configuration lifecycle, even if no sensor field changed.

With default debounce, responsive pigpio, and a responsive control loop:

- Normal detection bound: up to 1 s until acquisition, up to 0.75 s to finish,
  then up to one control-loop wait (1 s), plus control work and actuator IO.
- Missing/hung acquisition: the last successful value expires within 2 s, then
  the next control evaluation commands the ATO off. This is approximately 3 s
  plus control work and actuator IO, **not** a hard real-time three-second limit.
- Typical capture-only durations are around 20/40/80/400 ms for HIGH/NORMAL/LOW/DRY,
  plus initial phase and command/scheduling overhead. With larger debounce counts
  or a delayed command, the two-second ATO freshness limit can deliberately
  cause a conservative pump shutoff during an otherwise recoverable read.
- The existing ATO maximum-runtime lockout and manual fault reset are unchanged.
  They are software protections evaluated by the control loop, not an independent
  hardware cutoff. A stalled control loop or actuator communication can exceed
  the nominal bounds, as before. Backend capture never commands equipment.

Acceptable added volume depends on pump flow and sump headroom: calculate
`flow_ml_per_second * worst_validated_shutoff_seconds`. Physical ATO detection,
shutoff, missing-pulse tests, and safe remaining volume must be validated before
deployment/enabling automatic top-off. No aquarium equipment was operated in
this work; acceptance of actual shutoff latency is **not** established by mocks.

## Passive Raspberry Pi Results: October 16, 2026

During initial validation the user authorized only passive observation. Code was copied into a separate
`/tmp/loggerhead-hydros-passive.*` package. It never changed pin mode/pulls, active
settings, production files, overlays, or equipment, and never restarted Loggerhead.
The production continuous reader remained running throughout those passive tests. The extra reader
verified the existing GPIO17 input mode and used `configure_gpio=False`.

### Initial DRY Run

The initial signal was **DRY, about 19.84 Hz**. Each phase lasted 24 seconds:

| Metric | Baseline production | Production plus isolated bounded observer |
| --- | ---: | ---: |
| Production continuous HYDROS notification CPU | 1.999% of one core | 1.957% |
| Production Loggerhead process CPU | 17.993% | 17.782% |
| Total system busy CPU, excluding idle/iowait | 31.846% | 31.430% |
| Isolated bounded observer notification CPU | Not running | 0.167% |
| Entire isolated observer process CPU | 0.021% baseline sampler | 0.571% |

System/production differences are background variation, **not** deployed savings:
the production reader was unchanged. The new observer's notification CPU was
about 92% lower than the continuous reader on the same DRY signal. This is an
interval measurement, not lifetime `ps %CPU`. It has 10 ms `/proc` tick resolution
and includes sampler overhead, so use longer/repeated runs for precise estimates.

All six acquisitions agreed with production (DRY), with zero invalid periods.
Each used exactly nine rising callbacks/eight full periods and paused its daemon
subscription afterward. Capture median was **459.9 ms**, maximum **480.3 ms**;
post-capture measurement age was 3-7 ms. The notification thread accumulated
**0 observed CPU ticks between captures**. The temporary process exited and its
connection/thread were closed. One earlier reporting-format exception was fixed
and the complete benchmark repeated; its `finally` also closed that test reader.

### Submerged HIGH Run

After the user confirmed the sensor was completely submerged, live production
reported HIGH at 396.825 Hz / 2,520 us. The authorized passive test was repeated
with the same four-second interval and separate 24-second baseline/test phases:

| Metric | Baseline production | Production plus isolated bounded observer |
| --- | ---: | ---: |
| Production continuous HYDROS notification CPU | 29.244% of one core | 29.189% |
| Production Loggerhead process CPU | 42.658% | 42.721% |
| Total system busy CPU, excluding idle/iowait | 72.985% | 73.103% |
| Isolated bounded observer notification CPU | Not running | 0.083% |
| Entire isolated observer process CPU | 0.023% baseline sampler | 0.348% |

All six captures correctly reported HIGH at approximately 396.8-397.0 Hz, with
zero invalid periods. Capture median was **30.9 ms**, maximum **35.7 ms**. Each
measurement used nine rising edges/eight valid periods. There were 57 delivered
rising callbacks for 54 accepted edges across all six acquisitions (9-10 delivered
callbacks per cycle); boundary packets not used in the result were rejected by
the capture guards. Post-capture measurement age was 4-8 ms. The observer's daemon
subscription was paused after every acquisition, and its notification thread
accumulated **0 observed idle CPU ticks** between captures.

This establishes short-run HIGH classification and notification CPU below 1%
for the isolated reader: about **99.7% less notification-thread CPU** than the
continuous reader on that HIGH signal. Tick resolution and short duration limit
precision. Production was not replaced: its process/system CPU stayed high
because the continuous reader remained running. These are not deployed
controller-wide before/after savings. No active ATO or physical shutoff test ran.
Both temporary observer processes exited and closed their connections; the
production PID remained 7030, with its original 01:46:48 EDT service start time.

At HIGH, the theoretical event reduction is 1,588 -> approximately 9 rising edges
per four seconds (99.4% fewer), with a few possible boundary callbacks as measured.
With the one-second ATO interval it is about 397 -> 9 per second (97.7% fewer).
Command setup, cancellation, socket traffic, and scheduling prevent assuming the
same CPU percentage at different intervals. The one-second ATO policy still
needs its own hardware measurement. With enabled ATO inputs in DRY, faster
one-second capture also trades some CPU savings for safety. The existing
control-loop work is not optimized by this change.

Outstanding physical validation:

- NORMAL/LOW capture and CPU at their real frequencies, without changing
  classifier windows or synthesizing readings in production.
- Controlled transitions/noise/missing sensor pulses, electrical behavior, and
  actual ATO shutoff/maximum-runtime behavior with safe non-aquarium loads.
- Longer/repeated deployed CPU comparisons, including separate pigpiod sampling,
  under representative browser and dosing-related GPIO load. Initial authorized
  production verification is recorded below.
- Multiport captures, prolonged cycling, daemon disconnect/restart behavior,
  callback/socket cleanup and resource counts over hours, not just short tests.

## Validation and Reproduction

Automated tests cover all four windows, eight-period stability, stricter configured
debounce, noise/short/invalid periods, missing pulses/recovery, rollover, pre-arm
and late callback packets, cancellation, close/rearm/concurrent reads, stuck
notification threads, subscription/pause failures, 100 repeated acquisitions,
no idle edge subscription, configuration transition cleanup, ATO invalid/stale
shutoff decisions, unchanged runtime lockout, and completion publication during
overrunning reads. They do not prove physical wave capture accuracy or ATO timing.

Software validation: 306 mocked Python tests, 12 JavaScript unit tests, the
dosing/plots/WebKit responsiveness browser regressions, Ruff, compilation, and
whitespace checks passed. No physical actuator operations were used in testing.

For an explicitly authorized passive measurement, from an isolated source copy
on the Pi with no production service restart:

```bash
PYTHONPATH=. /home/reef/controller/venv/bin/python scripts/benchmark_hydros_passive.py \
  --passive-gpio17 --seconds 24 --interval 4
```

The script fails if SensePort 2 is not configured as HYDROS, if GPIO17 is not
already an input, if the service restarts/settings change, or if the observer's
notification mask is still enabled between reads. It has no arbitrary GPIO
argument, no equipment APIs, and no GPIO setup. Do not use this observer report
as a substitute for an approved production-wide before/after benchmark.

For deployment later: update `drivers.py`, `sensor_workers.py`, and `service.py`
together, then restart only after explicit authorization and hardware-safety
review. There are no new runtime dependencies or JSON migrations. The normal
installation was **not** changed during the initial passive-only phase.

## Authorized Deployment Follow-Up

The user subsequently authorized committing, pushing, deploying, and restarting.
Code commit `79b93e0` was pushed to GitHub and its runtime files deployed by SCP.
The Pi's unrelated/differing test files were left untouched. Source hashes were
checked against known previous versions before replacement. Configuration,
GPIO mapping, helper permissions, schedules, and the service's disabled-at-boot
preference were unchanged. No dosing schedules or ATO profiles were enabled,
and pumps were idle before deployment and throughout verification.

Successful, integrity-checked backup:
`~/controller/backups/hydros-bounded-20261016-110505`.
It contains prior source, configuration, state, SQLite, and a hash manifest.
An earlier live SQLite backup timed out before any code was applied; that
incomplete attempt must not be used for recovery. The successful retry pinned
a read transaction so ongoing telemetry could not repeatedly restart the copy.
SQLite `quick_check` returned `ok` before replacement.

The service restarted at 11:05:32 EDT on October 16, 2026, as unprivileged `reef`,
PID 13673. The restart completed actuator-disable cleanup; one DS18B20 read
reported expected cooperative cancellation while the old process shut down.
The new process recovered the verified GPIO5 kernel bus and began reporting
fresh CRC-valid DS18B20 and bounded GPIO17 HYDROS readings.

Read-only production verification, 60 seconds, same submerged/HIGH signal and
four-second HYDROS interval:

| Metric | Earlier continuous HIGH baseline, 24 s | Deployed bounded capture, 60 s |
| --- | ---: | ---: |
| HYDROS notification thread CPU | 29.244% of one core | 0.167% |
| Total Loggerhead process CPU | 42.658% | 16.533% |
| Total system busy CPU, excluding idle/iowait | 72.985% | 31.613% |

The different-length samples and other background/client activity are not a
fully controlled A/B experiment, but the signal state/frequency were comparable
and the bounded implementation was verified active. No extra passive observer
was running during deployed sampling. The control loop and state persistence
remain separate CPU costs; this change does not remove them.

All 15 sampled sensor-health snapshots were online. HYDROS consistently reported
HIGH with zero invalid periods, eight qualifying periods/nine accepted edges
per acquisition, and paused subscriptions in its published capture results.
Its successful timestamps advanced, as did the DS18B20's. Sampled capture median
was 31.3 ms, maximum 47.9 ms; status API median was 137.8 ms, maximum 217.6 ms.
The existing HIGH-level alarm was retained rather than cleared or suppressed.
A short read-only WebKit check in an iPhone-sized viewport kept Dashboard,
Config, and Plots connected, rendered the chart, and reported no page errors or
failed requests. WebKit ran on Windows, not the physical iPhone.

This validates actual deployed HIGH capture and short-run CPU reduction, not
physical ATO shutoff, NORMAL/LOW hardware classification, or long-duration fault
recovery. Those outstanding hardware tests still apply. No equipment commands,
sensor-setting changes, or physical motor operations were used for verification.
