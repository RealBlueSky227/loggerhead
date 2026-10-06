# Architecture

Loggerhead is organized around a small runtime core:

- `config.py` defines all user-editable profiles and performs hardware-map validation.
- `hardware.py` is the single source of truth for fixed BCM/I2C assignments.
- `drivers.py` contains optional hardware integrations with simulation-safe fallbacks.
- `controllers.py` contains pure decision logic for thermal control, dosing, ATO, and alerts.
- `database.py` owns SQLite telemetry, event logs, history downsampling, and disk guardrails.
- `state.py` stores reboot-recovery snapshots.
- `notifications.py` implements Telegram and Home Assistant MQTT integration.
- `service.py` wires drivers, controllers, state, database, alarms, and MQTT together.
- `web.py` serves the local dashboard and JSON API using the Python standard library.

The design keeps hardware I/O behind narrow interfaces so safety-critical behavior is testable without physical aquarium equipment. On a Raspberry Pi, the same interfaces use `smbus2`, `pigpio`, `pyserial`, and `RPi.GPIO` when available.

## Safety Model

Loggerhead fails closed. Configuration is validated before hardware initialization. The service refuses unknown pins or addresses, drives stepper enable lines off after every move, sets inactive stepper current to `0 mA` by default, applies ATO lockouts until explicit user reset, and records equipment/alarm transitions immediately to SQLite.
