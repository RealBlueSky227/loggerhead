# Pump Configuration and Scheduled Dosing

The Config tab contains pump settings, fixed-step calibration, and daily dosing
schedules. The existing Save Config button saves pump, schedule, sensor, and
raw-config changes together. Unsaved editors are retained during dashboard
polling; global Cancel returns them to the latest saved configuration.

## Pump Settings and Calibration

Each configured onboard pump exposes its name, calibrated steps/ml, priming
speed, scheduled dosing speed, and manual priming time/step limits. Physical
assignments, driver current, and microsteps remain visible. The new
`dose_speed_steps_per_second` defaults to 400 for existing profiles and is
independent of `manual_speed_steps_per_second`.

Calibration is an explicit physical action:

1. Route the pump output to a measured container and prime the line.
2. Request a fixed number of calibration steps at the chosen test speed.
3. Wait for the full requested count to finish. Stop remains available.
4. Enter the measured volume. Apply Calibration stages
   `steps_per_ml = confirmed_steps / measured_ml` in the editor.
5. Save Config persists the staged calibration.

Interrupted, timed-out, failed, or unverified runs cannot be used by the
calibration worksheet. A controller restart invalidates transient calibration
results. Changing the measured volume or running another test does not silently
change calibration. Direct steps/ml entry remains available for an independently
measured calibration. Actual delivered volume and coil current cannot be
established from STEP pulses alone.

Calibration uses the normal motor engine and its exclusive motor interlock,
configured current/microsteps/direction, electrical/thermal checks, and shutdown
path. It does not change driver registers outside that existing path.

## Daily Schedules

New and legacy schedules default to `enabled: false`. A schedule requires an
onboard stepper pump, a daily ml amount, 1-96 equal doses, a start/end window,
a per-dose volume cap, and a maximum runtime. Only one enabled schedule per
pump is permitted. Disabled legacy relay-dosing records remain loadable, but
automatic relay dosing is not offered by this implementation.

Times use the controller host's local timezone, shown with the schedule.
An end time equal to or earlier than the start time represents an overnight
window (equal start/end gives 24 hours). The start is included; the end is
excluded, so there is no extra dose at the end boundary. For example, 24 ml
across 12 doses from 08:00 to 20:00 schedules 2 ml at 08:00 through 19:00.

The motor receives `floor(daily_ml / doses_per_day * steps_per_ml)` pulses at
the pump's scheduled speed. Validation rejects sub-step doses, doses exceeding
their volume cap, and estimated runs that do not fit the runtime cap with a
two-second startup allowance. A runtime cap cannot exceed 600 seconds.
Pulse rounding is deliberately downward, so the calibrated command cannot
exceed the requested per-dose or daily volume through upward rounding.
Preview validates proposed configuration without saving it, enabling it,
or operating a pump.

Enabled schedules start only at future slots after application startup,
configuration changes, or explicit Resume. Missed doses are never made up.
A scheduled slot may wait up to 30 seconds for manual priming/calibration;
after that it is skipped and logged. Simultaneous slots observed together are
drained serially through the single-motor interlock, but cannot start after
their dosing window closes. Manual Prime is rejected
while a scheduled dose is active. Config changes affecting pumps or schedules
are rejected until the active motor is stopped.

Pause, or Stop on the active scheduled pump, stops the operation and persists
the paused state. It does not automatically resume. A partial dose retains
its uncertainty in the run journal. Hardware, runtime-limit, shutdown, and
persistence failures pause and fault the schedule with a high-priority alarm;
an unverified motor disable retains a critical motor cleanup alarm.
Reset Fault acknowledges the schedule fault and leaves the schedule paused.
Resume is a separate intentional action and arms future slots only.

## Persistence and Recovery

SQLite `dosing_runs` records the schedule/slot identity, daily window date,
pump, requested ml/steps, confirmed and possible pulse counts, start/end times,
status, and error. The slot is claimed atomically and committed before any
motor operation. A unique constraint prevents duplicate execution, while
daily volume reservations conservatively include uncertain and failed runs.
The UI labels volume as requested, not independently measured delivery.

A process restart marks any unfinished claim interrupted, pauses/faults that
schedule, and never resumes or retries the dose. A fresh claim requires a
future slot and explicit recovery from the fault. Schedule pause/fault states
also persist in `state.json`, with backward-compatible defaults for older files.
Failed/interrupted journal rows retain an unacknowledged-fault marker until
explicit Reset Fault. Startup reconstructs lockouts from that durable evidence
even if a prior crash happened before `state.json` could be saved.
Reset first persists the paused state before acknowledging the journal fault.
State snapshot generations prevent an older concurrent save from overwriting
a newer persisted pause.
Claims are not distributed transactions with physical liquid delivery:
crashes can leave an unknown delivered amount, which is deliberately handled
by lockout rather than a retry.

The scheduler has its own persistent thread. Motor I/O, database writes, and
notifications do not hold the equipment state lock. Normal status polling
uses cached run history and does not query the dosing journal. Pump-only saves
do not rebuild sensor workers or manipulate 1-Wire overlays.

## Deployment and Verification

Deploy the complete package, including `loggerhead/dosing.py` and
`loggerhead/static/dosing.mjs`. Existing configuration is migrated in memory;
no schedule is enabled automatically. The SQLite journal and indexes are
created on startup. Back up the configuration, state, and database before a
deployment and restart the existing unprivileged Loggerhead service.

Software tests use simulated/mocked hardware for duplicate claims, daily
reservations, restart interruption, overnight/end-boundary planning, manual
interlocks, configuration save failures, calibration completion/cleanup faults,
and HTTP actions. Browser tests exercise editing, unified saves, calibration
validation, schedule previews, and responsive layouts without physical motors.
Physical measured-volume calibration and an explicitly enabled supervised
scheduled dose remain necessary to validate a particular installed pump.

Developer checks (no physical motor operation):

```text
python -m pytest -q
python -m ruff check .
node --test tests/dosing.test.mjs tests/plots.test.mjs
node tests/dosing.browser.mjs
```

The browser test uses the local simulation fixture and intercepts all mutating
requests. It requires development-only Playwright and a Chromium browser, not
additional packages on the Pi. Set `LOGGERHEAD_PLAYWRIGHT_MODULE`,
`LOGGERHEAD_BROWSER_EXECUTABLE`, or `LOGGERHEAD_PYTHON` for nonstandard paths.
