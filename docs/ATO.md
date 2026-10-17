# Automatic Top Off

## Configuration

Config > Automatic Top Off > Add ATO creates a **disabled** draft. Select
two different physical water-level SensePorts and either an equipment relay or
an onboard stepper. Save Config saves ATO, sensor, pump, schedule, and raw JSON
edits together. Cancel discards drafts; polling does not replace focused edits.
Errors do not discard the draft. Existing enabled/disabled settings are retained.

HYDROS thresholds follow DRY < LOW < NORMAL < HIGH. Start at/below the start
threshold, continue through the intermediate levels, and stop at/above the stop
threshold. Start must be lower than stop. Binary inputs use DRY/SUBMERGED;
inversion remains the sensor's setting. SensePort Desired state is **only an
alert target**, independent of ATO thresholds. Backup DRY/LOW permits filling;
SUBMERGED/WET/NORMAL/HIGH stops and locks out filling.

Legacy relay profiles retain their runtime, enable flag, relay assignment, and DRY
start/first-non-DRY stop behavior. Legacy relay type aliases are normalized on
load. Previously inert stepper ATO entries without the new volume limits load
disabled; set/verify limits before intentionally enabling them. An upgrade must
not unexpectedly activate a formerly nonfunctional stepper ATO. New stepper ATO
requires positive per-refill and daily ml limits. Its
calibration, dose speed, current, microsteps, direction and diagnostics come
from the selected pump's existing configuration; verify calibration physically.
There is no new motor-current calibration or independently implemented driver.

## Actuators And Ownership

Relay refills use the existing equipment polarity mapping. ATO owns the relay
while enabled or faulted; manual/MQTT ON commands cannot compete. Manual OFF
is always available and stops/locks out ATO rather than allowing immediate
automatic restart. Shared
physical relay aliases and heater/ATO assignments are rejected. ATO relays
start OFF regardless of persisted/default ON intent. Reassignment first stops
old refills and sets old/new relay outputs OFF, before publishing new settings.

Stepper refills run on a background worker through `StepperPulseEngine`, with
one shared motion reservation for **all** ATO, dosing and priming operations.
The reservation survives cancellation until the worker has completed motor-safe
cleanup and accounting. Cleanup failures latch the engine's critical fault,
blocking subsequent motion even after the reservation is released.

An enabled chemical dosing schedule cannot use an ATO's pump. Different pumps
still share the global single-motor interlock: an occupied subsystem causes
ATO to report waiting, not queue a refill. Every later start rechecks current
water levels. Dosing retains its existing due-slot grace/skip policy. Priming
and calibration of an enabled ATO's own pump require disabling ATO first;
other priming requests are rejected while ATO owns motion. Dashboard STOP
also cancels ATO motion and latches an explicit-reset lockout.

## Accounting And Fault Recovery

SQLite `ato_runs` is a durable, transactional journal independent of the JSON
snapshot. Each refill reserves its maximum possible volume **before** motion;
no claim means no actuation. Confirmed DMA pulses update estimated delivery,
not requested pulses. Cancelled partial windows are charged at their possible
upper bound. Exceptions without trustworthy final counts retain the full
reservation. Calibration estimates volume, not proof of actual fluid flow.

The daily limit is a conservative rolling 24-hour budget, shared by profiles
using the same physical stepper assignment, not a midnight reset. Accounting includes all
runs, not just the 30 displayed history rows. Completed/uncertain runs remain
charged for 24 hours after completion; running reservations also remain
charged. Reset does not erase volume usage. Changing the profile ID does not
reset the actuator budget. Renaming a stepper profile does not reset it either.

Runtime and volume limits, invalid/stale required sensors, backup high water,
unexpected stops, faults, shutdown during refill, and active configuration
changes produce persistent lockouts. Restarted running claims become faults;
refills never resume or replay. A graceful intentional stop at the primary
stop threshold is normal and permits a later hysteresis cycle. Runtime uses
monotonic time. Brief invalid readings do not silently begin a new time budget.

Reset requires no active refill, fresh valid sensors, backup below its high-water
condition, and primary restored to the stop level. Relay OFF must be verified
by a successful output command; latched motor faults must be resolved first.
Deleting/changing faulted sensor/actuator assignments cannot bypass reset.
After reset, filling still requires a *new* fall to the start threshold.

## Software Timing And Residual Risks

The existing ATO HYDROS schedule is retained: at most 1 second between starts,
8 or more consistent full periods, normally about 20/40/80/400 ms for
HIGH/NORMAL/LOW/DRY plus first-edge and pigpio overhead. Higher configured
debounce counts take longer. Its existing bounded capture timeout and 2-second
stale-data policy are unchanged. Binary ATO inputs now also poll at most every
1 second and expire after 2 seconds. Paused HYDROS capture is not a disconnect.

The independent ATO watchdog checks about every 100 ms. It checks actual
worker validity/measurement ages, published control readings, configuration,
monotonic runtime, and a 3-second control-loop heartbeat. Under normally
scheduled operation, a primary/backup change should stop filling within about
**2.5 seconds** (1-second acquisition schedule, normal capture, up to 1-second
control publication, then 100 ms watchdog/driver overhead). Higher debounce,
I/O congestion, Linux scheduling and network relays can extend this estimate.
Sensor-data expiry targets **2 seconds + 100 ms**; heartbeat loss targets
**3 seconds + 100 ms**. These are expected software response times, not limits
proven on a Raspberry Pi or guaranteed worst-case bounds.

ATO stepper DMA chains are bounded to 250 ms at ordinary speeds (one step
minimum at very low speeds). Cancellation uses the engine's existing waveform
stop mechanism, usually checked every 10 ms, plus independent ENN disable.
It does not wait for a large requested refill to finish. The 250 ms bound also
limits uncertain pulse accounting. Repeated short chains can add boundary
gaps; their speed/flow accuracy needs physical measurement. Electrical and
thermal protections and UART diagnostics are unchanged.

Relay OFF and ENN disable are attempted by the watchdog without waiting for
refill-thread database logging. Relay ON/OFF are serialized; a late ON cannot
overtake cancellation. A blocked GPIO/I2C/UART/socket, a held Python lock,
process death, stopped interpreter, failed pigpiod/kernel, welded relay, or
power failure can defeat software stopping. Network relay command success is
not independent electrical readback. No software state is proof of zero flow.

**Use an independent high-water cutoff and hardware pump-runtime cutoff**
in series with the refill pump for unattended use. A finite reservoir limits
spill consequences. This change does not install a hardware watchdog or claim
hardware timing guarantees. GPIO mappings, ENN polarity, 1-Wire overlays and
other sensor backends are unchanged.

## Verification And Deployment

Automated tests use simulation/mocked drivers only. They cover thresholds,
relay and stepper operation, ownership, stale/unknown inputs, backup faults,
runtime/volume limits, uncertain accounting, restart recovery, transitions,
and editor Save/Cancel/reset behavior. Browser writes are intercepted.

Before enabling ATO on the Pi, with hazardous equipment disconnected, verify:

- Physical primary/backup independence, all HYDROS states, and binary inversion.
- Relay OFF polarity and electrical state at startup, shutdown and process kill.
- Stepper rotation, direction, actual calibrated volume and chain-boundary gaps.
- Stop latency under web traffic, all sensors, dosing contention and UART faults.
- Loss of sensor pulses, pigpiod, network, SD storage and main process.
- External high-water/runtime cutoff and reservoir capacity.

Back up configuration, state and SQLite before an authorized deployment.
Update Python modules plus `static/ato.mjs` and `web.py` together, restart only
in a planned safe state, and reload browser assets. This implementation work
does not deploy, restart, alter Pi configuration, or actuate physical equipment.
