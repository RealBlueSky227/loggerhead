# Stepper Waveform Hardware Validation

This procedure validates Loggerhead stepper timing on the Raspberry Pi without dosing into the aquarium.

## Safety Setup

1. Disconnect dosing lines from the aquarium and route them into a waste container.
2. Confirm all four TMC2209 ENN lines idle HIGH before starting Loggerhead.
3. Use one pump at a time. Do not run scheduled dosing during this test.
4. Keep the dashboard Stop/Prime control available before energizing a driver.
5. Use a logic analyzer or oscilloscope on the selected STEP GPIO and ground.

## Measurements

Measure at 100, 400, and 1000 steps/second:

- STEP HIGH pulse width. The commanded default is 20 microseconds.
- STEP period and frequency.
- STEP LOW interval.
- Jitter across at least 100 pulses.
- Inter-batch gap every waveform batch.
- Total pulse count for fixed moves.
- Stop latency from dashboard stop to final STEP edge.
- ENN transition back to HIGH after stop or limit.

## Commands

Run Loggerhead in real hardware mode from the Pi virtualenv:

```bash
cd ~/controller/loggerhead
source .venv/bin/activate
python -m loggerhead --config config/loggerhead.json --data-dir data
```

Use the dashboard manual prime controls with the dosing line disconnected. Start with Dose 1 at 100 step/s, then repeat at 400 and 1000 step/s.

## Interpretation

The software can report requested pulses, completed waveform batches, confirmed completed pulses, possible pulses in an interrupted batch, elapsed time, and stop reason. It cannot prove motor rotation or liquid volume from STEP pulses alone. Calibration and current settings should only be changed after electrical timing, coil wiring, mechanical load, and tubing behavior are verified.

Do not treat low `SG_RESULT` values during startup or low-speed priming as a stall unless a separate, validated StallGuard policy has been configured for this pump and load.

## Bounded API Benchmark

With Loggerhead already running, the manual benchmark requires explicit
`--execute` authorization. It operates real pumps through `/api/prime`, reads
the MCP23017 ENN register, and adds read-only pigpio STEP callbacks. It does
not create/cancel waveforms, change GPIO modes, access UART, or change saved
configuration. Do not launch a second controller instance for this test.

```bash
cd ~/controller/loggerhead
./.venv/bin/python scripts/benchmark_steppers.py --execute \
  --speeds 400 1000 --seconds 3 --check-interlock \
  --output /tmp/loggerhead-stepper-matrix.json

./.venv/bin/python scripts/benchmark_steppers.py --execute \
  --speeds 1000 --seconds 6 --stop-after 2 --cycles 2 \
  --output /tmp/loggerhead-stepper-stops.json
```

For this deployment, `.venv` links to `/home/reef/controller/venv`.
The benchmark caps each requested run at 10 seconds and each test speed at
2,000 step/s. These are benchmark bounds, not additional validated operating
speeds. The normal application still enforces its configured speed/current
settings, a single active pump, and hardware fault protections.

`POST /api/prime` accepts optional `speed_steps_per_second` (integer 1-5,000)
and `max_seconds` (positive, finite, no greater than the saved manual limit)
for a single intentional Prime command. Omitting them preserves existing
dashboard behavior. Overrides never change saved profiles, calibration,
microstepping, direction, current, or scheduled dosing. Stop ignores these
optional fields, so invalid settings cannot prevent a stop command.

```json
{"id":"dose1","on":true,"speed_steps_per_second":1000,"max_seconds":3}
```

## Verified on Reef (2026-10-15)

Tests used the real Pi Zero (ARMv6, kernel `6.18.34+rpt-rpi-v6`), the running
controller, actual TMC2209/MCP23017 hardware, and normal sensor acquisition.
The operator monitored rotation and reported smooth operation at 1,000 step/s.
Saved profiles remained at 400 step/s, 700 mA configured run current,
16 microsteps, and 800 steps/ml. Current scaling still lacks board-specific
sense-resistor/VREF calibration; these tests do not establish actual coil
current or dispensed volume.

The initial Stop trial exposed an accounting race: `request_stop()` canceled
DMA before the next `wave_tx_busy()` check, so the loop exited without marking
the chain interrupted. A three-second run with 1,047 captured rising edges
was incorrectly logged as 23,886 confirmed steps. The engine now checks Stop
and shutdown after the busy loop too. Interrupted chains conservatively report
zero confirmed steps and retain the requested chain length as an upper bound
on possible pulses, rather than inventing an exact transmitted count.

Initial 8 us pulse chains showed missing sampled edges at main-to-tail
waveform transitions. A smaller repeat unit increased missed observations
and was rejected. The retained implementation uses the original larger DMA
patterns with 20 us HIGH and a correspondingly shorter LOW: pulse count and
requested period are unchanged. This also matches the 20 us pulse margin in
the [upstream pigpio chain example](https://github.com/joan2937/pigpio/blob/master/pigpio.py).

Final retained-code results:

| Test | Result |
| --- | --- |
| All four pumps, 400 step/s, three-second cap | 2,500 us median period; no long intervals |
| All four pumps, 1,000 step/s, three-second cap | 1,000 us median period; no long intervals |
| Eight automatic-stop pulse counts | Capture matched controller confirmed counts exactly |
| Eight overlapping Prime attempts | Rejected with HTTP 409; no unexpected driver enable or STEP pulses observed |
| Two explicit Stop cycles per pump at 1,000 step/s | All eight passed; no long intervals |
| Sampled local HTTP Stop-to-ENN disable | 55-170 ms; final STEP edge 30-82 ms after submission |
| Dose 1, 1,000 step/s, ten-second cap | 9,461 captured/confirmed steps; no long intervals |
| Final state | All ENN HIGH, STEP LOW, no active waveform, all configured sensors online |

"Long interval" means greater than 1.5 times the expected rising-edge period.
The sampler observed 990-1,010 us periods at 1,000 step/s and 2,490-2,510 us
at 400 step/s across the final matrix. Median HIGH width was 20 us, with
some shorter sampled transition pulses. Capture uses pigpio's own DMA samples,
not an independent logic analyzer; it cannot establish exact wire pulse widths,
explain the missing-edge mechanism conclusively, prove motor torque, or
validate liquid calibration. Stop-to-disable includes HTTP handling and
sampling delay and is not a guaranteed worst-case bound.

During these instrumented trials, median status latency varied about
190-364 ms and controller cgroup CPU about 39-55%. The benchmark makes more
status requests than the ordinary dashboard and adds GPIO callbacks plus I2C
monitoring; these figures are not directly comparable with the ordinary
dashboard's two-second polling baseline. The ten-second run measured 43.4%
controller CPU and 326 ms median status latency under that load.

Raw final reports are on the Pi under `/home/reef/controller/backups/`:

- `stepper-final-20us-400-1000.json`
- `stepper-final-20us-stops.json`
- `stepper-final-20us-sustained.json`

The pre-test source/config/state backup is
`/home/reef/controller/backups/stepper-20261015-094414`.
