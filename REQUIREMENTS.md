# Loggerhead SRS Traceability

This file maps the pasted Aquarium Controller SRS to the implementation. Source comments also cite specific SRS clauses near the corresponding classes and logic.

## 1.0 System Overview

- `loggerhead.service.LoggerheadService` orchestrates monitoring, control, telemetry, persistence, and the web server.
- The project targets Python 3.13 and avoids architecture-specific compilation, supporting Raspberry Pi OS 32-bit and 64-bit.

## 2.0 Sensing and Monitoring

- Temperature: `drivers.TemperatureReader` supports kernel 1-Wire, GPIO bit-banged DS18B20 reads, and host CPU thermal files. Kernel 1-Wire DS18B20 reads are sense-port/GPIO owned: the application verifies the device-tree `w1-gpio` bus for the configured BCM pin, reads the sole DS18B20 on that bus, and never asks users to configure ROM IDs or searches the global 1-Wire sensor list as a fallback.
- HYDROS triple-optical water sensors use GPIO rising-edge PWM period capture with debouncing and activity timeout handling; they are not DS18B20/kernel 1-Wire devices.
- Sensor acquisition uses persistent, supervised per-sensor workers for blocking reads while the service control loop centrally enforces freshness, heater safety, ATO safety, alarms, and telemetry. Health tracks worker state, data validity, last attempts, last successes, stale state, read duration, diagnostics, and read errors. Dependent heaters/chillers/fans and ATO actuators fail off when required sensor data is unavailable.
- pH: `drivers.EzoPHSensor` reads the fixed EZO pH I2C address `0x63`.
- Water level: `drivers.BinaryLevelSensor` and `drivers.HydrosTripleClassifier` implement binary and Hydros Triple modes.
- Hydros classification windows are full rising-edge periods: High `2000-3000 us`, Normal `4000-6000 us`, Low `8000-12000 us`, Dry `40000-60000 us`.
- Host health: `drivers.HostHealthMonitor` reads CPU utilization, memory, disk, and uptime from Linux interfaces.

## 3.0 Equipment Control and Actuation

- MCP23017: `drivers.MCP23017RelayBoard` drives the fixed expander address `0x20`.
- Kasa HS300: `drivers.KasaHS300Client` implements TCP port `9999`, 4-byte length framing, autokey XOR encryption, outlet addressing, telemetry extraction, timeout, and retries.
- Polarity: `MCP23017RelayBoard.software_to_physical` handles NO/NC and normally-on profiles.
- Thermal automation: `controllers.ThermalController`.
- Dosing schedule: `controllers.DosingScheduler`.
- TMC2209: `drivers.TMC2209UART` implements native UART datagrams and current/diagnostic helpers. `drivers.StepperPulseEngine` enforces one active motor, zero idle current, stall/thermal diagnostics, and bounded pulse queue chunks.
- ATO: `controllers.ATOController` handles dry trigger, backup wet failsafe, max runtime, alarm, and lockout reset.

## 4.0 Telemetry and Notifications

- Telegram: `notifications.TelegramNotifier`.
- Notification rate limiting: `notifications.NotificationLimiter`.
- Buzzer: `drivers.Buzzer`, with dashboard silence in `service.LoggerheadService.silence_buzzer`.
- Disk alerts and critical pruning: `database.TelemetryStore.prune_if_critical`.
- MQTT bridge: `notifications.MQTTHomeAssistantBridge` and `service.LoggerheadService.handle_mqtt_command`.

## 5.0 UI and Configuration

- Local web UI: `web.DashboardServer`.
- Dashboard readouts, equipment blocks, alarm silence, scaling slider, live clock, plots, health tab, and config editor are implemented in `web.INDEX_HTML`.
- Configuration profiles are dataclasses in `config.py` and validate through `validate_config`.
- First-run safe defaults are created by `config.default_config`.

## 6.0 Data Management

- SQLite telemetry and event logging: `database.TelemetryStore`.
- Snapshot persistence and reboot recovery: `state.StateStore`.
- History is retained indefinitely under normal disk conditions. Pruning only begins below the critical free-space threshold and removes oldest rows in small batches.

## 7.0 Architecture and Dependency Standards

- Native Python 3.13 codebase.
- Optional external packages are recognized Pi/Linux ecosystem packages: `smbus2`, `pigpio`, `pyserial`, and `paho-mqtt`.
- The TMC2209 driver is a native module and does not depend on third-party TMC2209 packages.
- GPIO is represented exclusively as BCM identifiers.
- Source docstrings and comments include SRS clause references for traceability.

## 8.0 Physical Hardware Mapping

- The fixed hardware map lives in `hardware.py`.
- Startup/config validation rejects any pin, relay, stepper, pH address, Kasa outlet, or buzzer mapping outside the SRS-defined hardware interfaces.
