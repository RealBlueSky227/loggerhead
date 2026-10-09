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

- STEP HIGH pulse width. Expected default is about 8 microseconds.
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
source ~/controller/venv/bin/activate
python -m loggerhead --config ~/controller/loggerhead.json --data-dir ~/controller/data
```

Use the dashboard manual prime controls with the dosing line disconnected. Start with Dose 1 at 100 step/s, then repeat at 400 and 1000 step/s.

## Interpretation

The software can report requested pulses, completed waveform batches, confirmed completed pulses, possible pulses in an interrupted batch, elapsed time, and stop reason. It cannot prove motor rotation or liquid volume from STEP pulses alone. Calibration and current settings should only be changed after electrical timing, coil wiring, mechanical load, and tubing behavior are verified.

Do not treat low `SG_RESULT` values during startup or low-speed priming as a stall unless a separate, validated StallGuard policy has been configured for this pump and load.
