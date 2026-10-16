# Event-Driven Kernel 1-Wire Lifecycle

Loggerhead configures kernel `w1-gpio` at startup and during SensePort changes.
The physical SensePort/GPIO remains the identity; ROM IDs only identify the sole
probe currently attached to its verified bus. No pin assignments, GPIO modes,
calibration, dosing, heater logic, or overlay-helper permissions are changed.

## Verification and Reads

`TemperatureReader.configure_kernel_one_wire()` performs privileged lifecycle
verification and, where needed, authorized activation using the existing helper.
After the configuration transaction finishes, all successful mappings are sealed
against the final topology. Workers retain these process-local verified mappings.
They are never restored from a persisted temperature or inferred from master
numbering or global ROM searches.

Normal reads do not call configuration, `dtoverlay`, `gpioinfo`, or helper status.
They compare unprivileged ownership evidence before and after the kernel
conversion: root-owned ownership-file identity, applied overlay contents and
instance, kernel master object identities, bound w1-gpio platform/DT evidence,
and direct bus-to-GPIO evidence where the kernel supplies it. Evidence checks
are not an expiring privileged lease. The helper client's status TTL applies
only when lifecycle callers actually request status, not to normal polling.

Master directory timestamps are not ownership evidence: probe replacement can
change them without replacing the master. Master object identity is checked
instead. Slave membership and the sole ROM are checked again before publishing;
replacement resets the 85 C power-up-default retry context. CRC, conversion
plausibility, cancellation, successful-reading timestamps, stale deadlines,
independent workers and centralized equipment interlocks remain enforced.

No shared lifecycle lock is held during `w1_slave` conversion I/O. A slow
conversion cannot monopolize other ports' reads or the control loop.

## Change and Fault Events

Changed, missing, conflicting, or unreadable ownership evidence invalidates the
cached reading path immediately. A changed mapping is never accepted as the
result of that same conversion. The normal worker error/freshness paths force
dependent equipment off; a still-running/hung read cannot extend its freshness.

On a subsequent acquisition, only configured ports may perform bounded,
verification-only helper recovery, at most once per 30 seconds per port.
CRC failures or a disconnected probe on an otherwise valid bus do not invoke
the helper. Healthy polling never invokes the helper just because time passed.

Recovery cannot activate or remove overlays, change GPIO modes, or steal a line
from another backend. If an overlay is absent, restore it through an explicit
SensePort configuration transaction or application startup. A captured detached
master that disappears/reappears under the same name is rejected; a stale helper
record cannot make that replacement object valid in the running process.
Resolve such a fault through controlled SensePort reinitialization, not by
selecting a different ROM or accepting a numerically similar master.

Configuration transitions still invalidate readings before disruption, stop and
join workers, release the old backend, enforce safe handoff, revalidate affected
overlay-stack mappings, and only then restart workers. Cancellation is checked
before/after bounded helper calls and before read publication. Python cannot
forcibly cancel a kernel file read; existing supervisor timeouts and fail-safe
freshness handling remain necessary.

The privileged helper and its root-owned capture records remain the authority
for Trixie's detached sysfs topology at process startup. Preexisting boot buses
without direct GPIO ancestry or a verified captured association still fail
closed. This change does not adopt, reorder, or automatically migrate external
or boot-time overlays. See the existing migration/rollback procedure in
`DEPLOYMENT.md`.

## Deployment and Measurement

Deploy `loggerhead/drivers.py`, `loggerhead/onewire.py`, and
`loggerhead/service.py` together, then restart the existing unprivileged service.
The installed privileged helper is unchanged; no sudoers or boot edits are
needed. Back up source, configuration, state, and SQLite first.

Run the same read-only benchmark before and after, after startup has settled:

```sh
python3 scripts/benchmark_onewire_lifecycle.py --seconds 90
```

The benchmark reads service cgroup CPU usage (including helper children), checks
status every 10 seconds, and counts sudo audit entries for the exact helper
command. It never calls the helper itself, changes settings, or operates pumps.
It rejects measurements spanning a service restart. Journal access is required;
CPU is reported as a percentage of one core. Background dashboard traffic and
other activity can affect CPU/latency; these are observed service measurements,
not a controlled whole-host CPU benchmark.

Observed on the installed Pi Zero on October 15, 2026, with a 10-second
DS18B20 polling interval and 4-second HYDROS polling interval:

| Build | Duration | Helper calls during polling | Service CPU seconds | CPU (% of one core) |
| --- | ---: | ---: | ---: | ---: |
| Baseline | 90 s | 2 status | 18.1964 | 20.218 |
| Initial lifecycle build | 90 s | 0 | 15.2931 | 16.992 |
| Final build, including capture-race guard | 90 s | 0 | 15.1853 | 16.872 |

All sampled sensors remained online and all sampled active-pump states were
idle. Temperature success timestamps advanced throughout both post-change runs.
The live GPIO5/SensePort 4 probe returned CRC-valid 12-bit, externally powered
readings, and asynchronous HYDROS capture on GPIO17 remained online. Cold startup
activated the desired GPIO5 overlay; warm restart recovered its verified mapping
without another activation. Lifecycle helper calls are intentionally excluded
from the normal-polling measurements.

The host rebooted between baseline and post-change validation. CPU/background
load is therefore not a controlled same-boot A/B experiment; the observed
20.2% -> 16.9% service CPU change is indicative, not a guaranteed reduction.
Zero steady-state helper invocations was reproduced in both post-change runs.
The benchmark's HTTP latency samples varied; no dashboard-latency improvement
is claimed from these measurements.

The deployed code is backed up under
`~/controller/backups/onewire-lifecycle-20261015-230335` (original baseline)
and `~/controller/backups/onewire-lifecycle-20261015-231217` (before final guard).
Configuration, state, and SQLite backups are included. Runtime settings and
the installed helper were not edited. The existing systemd service was disabled
at boot; this refactor leaves that deployment preference unchanged.

Regression coverage includes stable polling beyond cache expiry, startup,
topology/ownership changes, stale captured master rejection, direct GPIO
conflicts, CRC recovery, probe replacement, mid-conversion changes, cancellation,
bounded helper failure, independent conversions, and release/restart behavior.
Mock tests do not validate electrical handoff, overlay stacking, or physical
multiport switching on Raspberry Pi hardware. Do not fault-inject GPIO changes
or energize pumps to run this benchmark. Final software validation: all 260
Python tests, 12 JavaScript unit tests, Ruff, compilation and whitespace checks
passed. No physical motor operations or disruptive hardware fault injection
were used for this work.
