# Loggerhead

**Raspberry Pi aquarium controller with fixed-pin hardware safety, local telemetry, automation, and a dark web dashboard.**

Loggerhead implements the Aquarium Controller SRS as a native Python 3.13 application. It monitors temperature, pH, water level, and Raspberry Pi host health; controls onboard MCP23017 relays or Kasa HS300 outlets; schedules dosing; manages ATO lockout safety; logs telemetry to SQLite; bridges Home Assistant over MQTT; and sends Telegram alerts.

The runtime is intentionally safe to import and test on non-Pi systems. Hardware integrations use optional, recognized packages (`smbus2`, `pigpio`, `pyserial`, `paho-mqtt`) and fall back to simulation when started with `--simulation`.

## What it does

- Temperature sensing through kernel 1-Wire, bit-banged 1-Wire hooks, and host CPU thermal files.
- EZO pH readings over I2C address `0x63`.
- Binary level sensors and Hydros Triple PWM period classification with debouncing and inactivity faults.
- MCP23017 relay control with normally-open/normally-closed polarity mapping.
- Kasa HS300 LAN control using TP-Link XOR framing on TCP port `9999`.
- Heater, chiller, and fan hysteresis automation.
- Dosing schedule math plus TMC2209/NEMA17 native UART and pigpio-ready pulse engine.
- ATO dry-trigger, backup wet failsafe, max-runtime lockout, dashboard reset, Telegram, and buzzer alarms.
- SQLite telemetry, event logging, state snapshot recovery, history downsampling, and disk-space guardrails.
- Local web UI with dashboard, historical plots, system health, configurable widget sizing, and JSON config editing.
- MQTT telemetry and command topics under a configurable Home Assistant base topic.

## Install on Raspberry Pi OS

For a full `systemd` deployment on the reef Pi, see [DEPLOYMENT.md](DEPLOYMENT.md).

```bash
git clone https://github.com/RealBlueSky227/loggerhead.git
cd loggerhead
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m loggerhead --config config/loggerhead.json --data-dir data
```

For laptop testing without GPIO/I2C/UART hardware:

```bash
python -m loggerhead --simulation --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`.

## Configuration

On first run, Loggerhead creates `config/loggerhead.json` with a safe default map matching the SRS hardware layout. Any attempt to configure pins, relay names, pH address, Kasa outlet indexes, stepper assignments, or buzzer pins outside the fixed map raises a diagnostic halt before hardware initialization.

The dashboard Config tab writes the same JSON configuration and reloads active service state without requiring a process restart.

## Hardware Map

Loggerhead enforces only BCM GPIO names and fixed I2C addresses:

- Stepper UART RX: BCM `15`
- Buzzer PWM: BCM `12`
- pH EZO circuit: I2C `0x63`
- MCP23017 relay/enable expander: I2C `0x20`
- Relays: `AC1` through `AC8`
- Sense ports: `Sensor 1` through `Sensor 10`
- TMC2209 drivers: `dose1` through `dose4`, UART addresses `0` through `3`

See [REQUIREMENTS.md](REQUIREMENTS.md) for the SRS traceability map.

## Development

```bash
python -m pip install -r requirements_test.txt
python -m compileall -q loggerhead tests
ruff check .
pytest --cov=loggerhead --cov-report=term-missing
```

The tests cover safety-critical behavior: pin validation, Hydros classification/debounce, HS300 framing, relay polarity, thermal control, dosing schedule math, ATO lockout, SQLite history downsampling, and stepper interlock release.
