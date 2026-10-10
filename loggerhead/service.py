from __future__ import annotations

import copy
import logging
import signal
import threading
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from .config import (
    MAX_MANUAL_PRIME_STEPS_PER_SECOND,
    MIN_MANUAL_PRIME_STEPS_PER_SECOND,
    AppConfig,
    EquipmentProfile,
    OneWireMode,
    SensePortDevice,
    StepperProfile,
    apply_config_migrations,
    from_dict,
    load_config,
    materialized_analog_sensors,
    materialized_temperature_sensors,
    materialized_water_level_sensors,
    save_config,
    validate_config,
)
from .controllers import AlertEvaluator, ATOController, ThermalController
from .database import TelemetryStore
from .drivers import (
    TMC2209UART,
    ADS1115AnalogReader,
    BinaryLevelSensor,
    Buzzer,
    EzoPHSensor,
    HardwareFault,
    HardwareUnavailable,
    HostHealthMonitor,
    HydrosPulseReader,
    HydrosTripleClassifier,
    KasaHS300Client,
    MCP23017RelayBoard,
    StepperPulseEngine,
    TemperatureReader,
)
from .hardware import (
    RELAYS,
    SENSE_PORTS,
    AlarmPriority,
    DiagnosticHalt,
    EquipmentDriver,
    LevelState,
    TemperatureDriver,
    WaterLevelDriver,
    require_stepper,
)
from .notifications import HomeAssistantNotifier, MQTTHomeAssistantBridge, NotificationLimiter, TelegramNotifier
from .sensor_workers import SensorReadResult, SensorWorker, SensorWorkerSnapshot, SensorWorkerSpec
from .state import AlarmState, EquipmentState, SensorHealth, SensorReading, StateStore
from .web import DashboardServer

LOGGER = logging.getLogger(__name__)


class LoggerheadService:
    """Long-running aquarium controller service.

    Implements SRS 1.1, 1.2, 2.4, 5.1, 5.4.1, 6.1, 6.2, 6.3, and the orchestration
    path for all hardware/control subsystems.
    """

    MANUAL_PRIME_LIMIT_REASONS = {"max_seconds", "max_steps"}
    MANUAL_PRIME_LIMIT_CHIRP_SECONDS = 0.15

    def __init__(self, config_path: Path, data_dir: Path, *, simulation: bool = False) -> None:
        self.config_path = config_path
        self.data_dir = data_dir
        self.simulation = simulation
        self.config = load_config(config_path)
        self.store = TelemetryStore(data_dir / "loggerhead.sqlite3", max_points_default=self.config.plot_max_points)
        self._state_lock = threading.RLock()
        self.state_store = StateStore(data_dir / "state.json", state_lock=self._state_lock)
        self.state = self.state_store.load()
        self.buzzer = Buzzer(frequency_hz=self.config.buzzer.frequency_hz, simulation=simulation)
        self.relay_board = MCP23017RelayBoard(simulation=simulation)
        self.analog_reader = ADS1115AnalogReader(simulation=simulation)
        self.temperature_reader = TemperatureReader(simulation=simulation)
        self.ph_sensor = EzoPHSensor(simulation=simulation)
        self.uart = TMC2209UART(simulation=simulation)
        self.stepper_engine = StepperPulseEngine(self.uart, self.relay_board, simulation=simulation)
        for profile in self.config.steppers:
            self.stepper_engine.configure_driver(
                require_stepper(profile.assignment),
                microsteps=profile.microsteps,
                stallguard_threshold=profile.stallguard_threshold,
                direction_high=profile.direction_high,
            )
        self.health = HostHealthMonitor()
        self.telegram = TelegramNotifier(self.config.telegram)
        self.ha_notifier = HomeAssistantNotifier(self.config.home_assistant_notify)
        self.mqtt = MQTTHomeAssistantBridge(self.config.mqtt)
        self.mqtt.set_command_handler(self.handle_mqtt_command)
        self.notifications = NotificationLimiter()
        self._stop = threading.Event()
        self._stop_lock = threading.RLock()
        self._stopped = False
        self._prime_lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._prime_stop: dict[str, threading.Event] = {}
        self._prime_threads: dict[str, threading.Thread] = {}
        self._restart_suppressed_alarm_ids = self._active_audible_alarm_ids()
        self._level_since: dict[str, float] = {}
        self._sensor_next_due: dict[str, float] = {}
        self._sensor_worker_lock = threading.RLock()
        self._sensor_workers: dict[str, SensorWorker] = {}
        self._sensor_workers_started = False
        self._sensor_last_applied_attempt: dict[str, float] = {}
        self._i2c_lock = threading.RLock()
        self._gpio_lock = threading.RLock()
        self._hydros: dict[str, HydrosTripleClassifier] = {}
        self._hydros_readers: dict[str, HydrosPulseReader] = {}
        self._rebuild_sensor_runtime()
        self._last_polled_log = 0.0
        self._clear_transient_stepper_state()
        self._retire_manual_prime_limit_alarms()
        self._save_state()
        self.buzzer.stop()
        self._restore_equipment_defaults()

    def run(self, *, host: str = "0.0.0.0", port: int = 8080) -> None:
        LOGGER.info("Starting Loggerhead on %s:%s", host, port)
        self._notify("Loggerhead", "Loggerhead aquarium controller started.")
        server = DashboardServer(self, host=host, port=port)
        self._start_sensor_workers()
        self._threads = [
            threading.Thread(target=self._poll_loop, name="loggerhead-poll", daemon=True),
            threading.Thread(target=server.serve_forever, name="loggerhead-web", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        shutdown_error: HardwareFault | None = None
        try:
            while not self._stop.is_set():
                time.sleep(0.25)
        finally:
            try:
                self.stop()
            except HardwareFault as exc:
                shutdown_error = exc
                LOGGER.critical("Loggerhead shutdown completed with hardware fault: %s", exc)
            finally:
                server.shutdown()
                self.mqtt.close()
                self._save_state()
            if shutdown_error is not None:
                raise shutdown_error

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            LOGGER.info("Loggerhead shutdown starting.")
            self._stop.set()
            with self._prime_lock:
                for event in self._prime_stop.values():
                    event.set()
            self.stepper_engine.request_stop()
            errors: list[Exception] = []
            for name, action in (
                ("stepper engine", self.stepper_engine.shutdown),
                ("stepper ENN disable", self.relay_board.disable_all_steppers),
                ("sensor workers", self._stop_sensor_workers_checked),
                ("sensor callbacks", self._close_sensor_readers),
                ("buzzer", self.buzzer.stop),
            ):
                LOGGER.info("Shutdown phase: %s.", name)
                try:
                    action()
                except Exception as exc:
                    errors.append(exc)
                    LOGGER.critical("Hardware shutdown action failed during %s: %s", name, exc)
            LOGGER.info("Shutdown phase: waiting for priming workers.")
            for stepper_id, thread in self._prime_threads_snapshot().items():
                if thread is threading.current_thread():
                    continue
                thread.join(timeout=2.0)
                if thread.is_alive():
                    message = f"Manual priming thread {stepper_id} did not stop within timeout."
                    errors.append(HardwareFault(message))
                    LOGGER.critical(message)
            self._clear_transient_stepper_state()
            self._save_state()
            self._stopped = True
            LOGGER.info("Loggerhead shutdown complete.")
            if errors:
                raise HardwareFault(f"Loggerhead shutdown could not verify all actuators disabled: {errors[0]}") from errors[0]

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "config": asdict(self.config),
                "sense_ports": [asdict(item) for item in self.config.sense_ports],
                "sensor_catalog": self._sensor_catalog(),
                "steppers": [asdict(item) for item in self.config.steppers],
                "equipment": {key: asdict(value) for key, value in self.state.equipment.items()},
                "readings": {key: asdict(value) for key, value in self.state.readings.items()},
                "sensor_health": {key: asdict(value) for key, value in self.state.sensor_health.items()},
                "sensor_workers": {key: asdict(value) for key, value in self._sensor_worker_snapshots().items()},
                "water_levels": {key: value.value for key, value in self.state.water_levels.items()},
                "alarms": {key: asdict(value) for key, value in self.state.alarms.items()},
                "ato": {key: asdict(value) for key, value in self.state.ato.items()},
                "manual_priming": dict(self.state.manual_priming),
                "diagnostics": self._diagnostics(),
                "heartbeat": {"ok": True, "ts": time.time()},
                "events": self.store.recent_events(30),
                "time": time.time(),
                "simulation": self.simulation,
            }

    def history(self, streams: list[str], start_ts: float, end_ts: float | None = None) -> dict[str, Any]:
        return self.store.history(streams, start_ts=start_ts, end_ts=end_ts, max_points=self.config.plot_max_points)

    def update_config(self, payload: dict[str, Any]) -> AppConfig:
        # Implements SRS 5.4.1 Dynamic Application by replacing the active config
        # after validation and rebuilding per-config helper state.
        candidate = from_dict(AppConfig, copy.deepcopy(payload))
        apply_config_migrations(candidate)
        validate_config(candidate)
        previous = self.config
        self.config = candidate
        try:
            self._rebuild_sensor_runtime()
        except Exception:
            self.config = previous
            raise
        save_config(self.config_path, self.config)
        self.config = load_config(self.config_path)
        self.store.log_event("config", "Configuration reloaded from UI.")
        return self.config

    def set_sense_port(self, number: int, payload: dict[str, Any]) -> None:
        candidate = copy.deepcopy(self.config)
        port = next(item for item in candidate.sense_ports if item.number == number)
        field_names = {field.name for field in fields(port)}
        for key, value in payload.items():
            if key == "number" or key not in field_names:
                continue
            if key == "device":
                value = SensePortDevice(value)
            elif key == "one_wire_mode":
                value = OneWireMode(value)
            setattr(port, key, value)
        apply_config_migrations(candidate)
        validate_config(candidate)
        previous = self.config
        self.config = candidate
        try:
            self._rebuild_sensor_runtime()
        except Exception:
            self.config = previous
            raise
        save_config(self.config_path, self.config)
        self.config = load_config(self.config_path)
        self.store.log_event("config", f"Sense Port {number} set to {port.device.value}.")

    def set_equipment(self, equipment_id: str, on: bool, *, source: str = "manual") -> None:
        if self._stop.is_set():
            raise RuntimeError("Loggerhead is stopping; equipment commands are blocked.")
        profile = self._equipment_profile(equipment_id)
        if profile.driver == EquipmentDriver.MCP23017_RELAY:
            relay = RELAYS[profile.pin_or_outlet]
            active = MCP23017RelayBoard.software_to_physical(on, normally_on=profile.normally_on, relay=relay)
            self.relay_board.set_pin(relay.mcp_pin, active)
        elif profile.driver == EquipmentDriver.KASA_HS300:
            KasaHS300Client(profile.kasa_host).set_outlet(int(profile.pin_or_outlet), on)
        with self._state_lock:
            self.state.equipment[equipment_id] = EquipmentState(equipment_id, on, source)
        self.store.log_event("equipment", f"{profile.name} turned {'ON' if on else 'OFF'} by {source}.", {"id": equipment_id})
        self.mqtt.publish(f"equipment/{equipment_id}", {"on": on, "source": source})
        self._save_state()

    def reset_ato(self, ato_id: str) -> None:
        profile = next(item for item in self.config.ato if item.id == ato_id)
        with self._state_lock:
            ATOController.reset(profile, self.state)
        self.store.log_event("ato", f"ATO {profile.name} reset from dashboard.")
        self._save_state()

    def silence_buzzer(self) -> None:
        with self._state_lock:
            self.state.buzzer_muted_until = time.time() + self.config.buzzer.rearm_seconds
        self.buzzer.stop()
        self.store.log_event("alarm", "Buzzer temporarily silenced.")
        self._save_state()

    def set_alarm_enabled(self, enabled: bool) -> None:
        self.config.buzzer.alarm_enabled = enabled
        save_config(self.config_path, self.config)
        if not enabled:
            self.buzzer.stop()
        self.store.log_event("alarm", f"Global alarm {'enabled' if enabled else 'disabled'}.")
        self._save_state()

    def set_manual_priming(self, stepper_id: str, enabled: bool) -> dict[str, Any]:
        with self._prime_lock:
            profile = self._stepper_profile(stepper_id)
            speed = self._manual_prime_speed(profile)
            if enabled and self._stop.is_set():
                raise RuntimeError("Loggerhead is stopping; manual priming cannot be started.")
            if not enabled:
                stop = self._prime_stop.get(stepper_id)
                thread = self._prime_threads.get(stepper_id)
                if stop:
                    stop.set()
                    self.stepper_engine.request_stop()
                if not thread or not thread.is_alive():
                    self._clear_prime_worker_locked(stepper_id)
                with self._state_lock:
                    self.state.manual_priming[stepper_id] = False
                    if self.state.stepper_active == stepper_id:
                        self.state.stepper_active = None
                self.store.log_event("dosing", f"{profile.name} manual priming stop requested.")
                self._save_state()
                return {"id": stepper_id, "priming": False, "state": "stopping" if thread and thread.is_alive() else "stopped"}
            for key, thread in list(self._prime_threads.items()):
                if not thread.is_alive():
                    self._clear_prime_worker_locked(key)
            active_threads = {
                key: thread for key, thread in self._prime_threads.items() if thread.is_alive()
            }
            if active_threads:
                raise RuntimeError(f"Manual priming is already active for {next(iter(active_threads))}.")
            with self._state_lock:
                manual_active = any(self.state.manual_priming.values())
            if manual_active:
                raise RuntimeError("Only one pump may be manually primed at a time.")
            stop = threading.Event()
            thread = threading.Thread(
                target=self._prime_loop,
                args=(profile, stop, speed),
                name=f"prime-{stepper_id}",
                daemon=True,
            )
            self._prime_stop[stepper_id] = stop
            self._prime_threads[stepper_id] = thread
            with self._state_lock:
                self.state.manual_priming[stepper_id] = True
                self.state.stepper_active = stepper_id
            self._save_state()
            try:
                thread.start()
            except Exception:
                self._clear_prime_worker_locked(stepper_id)
                with self._state_lock:
                    self.state.manual_priming[stepper_id] = False
                    if self.state.stepper_active == stepper_id:
                        self.state.stepper_active = None
                self._save_state()
                raise
            self.store.log_event("dosing", f"{profile.name} manual priming started at {speed} step/s.")
            return {"id": stepper_id, "priming": True, "state": "started", "speed_steps_per_second": speed}

    def _prime_loop(self, profile: StepperProfile, stop: threading.Event, speed_steps_per_second: int) -> None:
        assignment = require_stepper(profile.assignment)
        try:
            result = self.stepper_engine.run_continuous(
                assignment,
                stop_event=stop,
                steps_per_second=speed_steps_per_second,
                run_current_ma=profile.run_current_ma,
                hold_current_ma=profile.hold_current_ma,
                microsteps=profile.microsteps,
                stallguard_threshold=profile.stallguard_threshold,
                direction_high=profile.direction_high,
                max_seconds=profile.manual_max_seconds,
                max_steps=profile.manual_max_steps,
            )
            if result.reason in self.MANUAL_PRIME_LIMIT_REASONS:
                self._handle_manual_prime_limit(profile, result)
        except Exception as exc:
            LOGGER.exception("%s manual priming fault.", profile.name)
            self._activate_alarm(f"stepper:{profile.id}", f"{profile.name} manual priming fault: {exc}", priority="high")
        finally:
            try:
                self.stepper_engine.stop(assignment)
            except Exception as exc:
                LOGGER.critical("%s manual priming cleanup could not verify driver disabled: %s", profile.name, exc)
                self._activate_alarm(f"stepper:{profile.id}:cleanup", f"{profile.name} cleanup fault: {exc}", priority="critical")
            with self._prime_lock:
                self._clear_prime_worker_locked(profile.id)
                with self._state_lock:
                    self.state.manual_priming[profile.id] = False
                    if self.state.stepper_active == profile.id:
                        self.state.stepper_active = None
                self._save_state()

    def handle_mqtt_command(self, topic: str, payload: Any) -> None:
        # Implements SRS 4.8.2 Command Subscription.
        if topic.startswith("equipment/"):
            equipment_id = topic.split("/", 1)[1]
            on = bool(payload.get("on") if isinstance(payload, dict) else payload)
            self.set_equipment(equipment_id, on, source="mqtt")

    def _restore_equipment_defaults(self) -> None:
        for item in self.config.equipment:
            existing = self.state.equipment.get(item.id)
            desired = existing.on if existing else item.default_on
            self.set_equipment(item.id, desired, source="restore")

    def _clear_transient_stepper_state(self) -> None:
        with self._state_lock:
            self.state.stepper_active = None
            self.state.manual_priming = {item.id: False for item in self.config.steppers}

    def _rebuild_sensor_runtime(self) -> None:
        restart_workers = self._sensor_workers_started
        stuck_workers = self._stop_sensor_workers()
        if stuck_workers:
            message = f"Cannot replace sensor configuration; workers did not stop: {', '.join(stuck_workers)}."
            LOGGER.critical(message)
            self._activate_alarm("sensor:worker-stop", message, priority="high")
            raise HardwareFault(message)
        self._close_sensor_readers()
        self._hydros = {}
        self._hydros_readers = {}
        for sensor in materialized_water_level_sensors(self.config):
            if sensor.driver != WaterLevelDriver.HYDROS_TRIPLE:
                continue
            classifier = HydrosTripleClassifier(
                debounce_samples=sensor.debounce_samples,
                activity_timeout=sensor.activity_timeout,
            )
            self._hydros[sensor.id] = classifier
            port = SENSE_PORTS[sensor.sense_port]
            try:
                self._hydros_readers[sensor.id] = HydrosPulseReader(
                    port.digital_bcm,
                    classifier,
                    simulation=self.simulation,
                )
            except Exception as exc:
                LOGGER.warning("HYDROS reader unavailable for %s on BCM %s: %s", sensor.id, port.digital_bcm, exc)
        configured_ids = {sensor["id"] for sensor in self._configured_sensor_catalog()}
        self._sensor_next_due = {key: value for key, value in self._sensor_next_due.items() if key in configured_ids}
        self._sensor_last_applied_attempt = {
            key: value for key, value in self._sensor_last_applied_attempt.items() if key in configured_ids
        }
        with self._state_lock:
            self.state.sensor_health = {
                key: value for key, value in self.state.sensor_health.items() if key in configured_ids
            }
        self._install_sensor_workers(self._build_sensor_worker_specs())
        if restart_workers:
            self._start_sensor_workers()

    def _close_sensor_readers(self) -> None:
        for reader in self._hydros_readers.values():
            try:
                reader.close()
            except Exception as exc:
                LOGGER.warning("HYDROS callback cleanup failed: %s", exc)
        self._hydros_readers = {}

    def _build_sensor_worker_specs(self) -> list[SensorWorkerSpec]:
        specs: list[SensorWorkerSpec] = []
        for sensor in materialized_temperature_sensors(self.config):
            stale_after = self._stale_after(sensor.check_frequency)

            def read_temperature(stop: threading.Event, sensor=sensor, stale_after=stale_after) -> SensorReadResult:
                del stop, stale_after
                value = self._read_temperature_threadsafe(sensor)
                return SensorReadResult(sensor.id, "temperature", round(value, 3), "F")

            specs.append(
                SensorWorkerSpec(
                    sensor.id,
                    "temperature",
                    sensor.check_frequency,
                    stale_after,
                    read_temperature,
                    read_timeout_seconds=stale_after,
                )
            )
        for sensor in self.config.ph_sensors:
            stale_after = self._stale_after(sensor.check_frequency)

            def read_ph(stop: threading.Event, sensor=sensor) -> SensorReadResult:
                if self.simulation:
                    value = self.ph_sensor.read_ph()
                else:
                    with self._i2c_lock:
                        self.ph_sensor.request_read()
                    if stop.wait(0.9):
                        raise RuntimeError("pH read cancelled.")
                    with self._i2c_lock:
                        value = self.ph_sensor.read_response()
                return SensorReadResult(sensor.id, "ph", round(value, 3), "pH")

            specs.append(
                SensorWorkerSpec(sensor.id, "ph", sensor.check_frequency, stale_after, read_ph, read_timeout_seconds=stale_after)
            )
        for sensor in materialized_analog_sensors(self.config):
            stale_after = self._stale_after(sensor.check_frequency)

            def read_analog(_stop: threading.Event, sensor=sensor) -> SensorReadResult:
                port = SENSE_PORTS[sensor.sense_port]
                with self._i2c_lock:
                    raw = self.analog_reader.read_voltage(port.ads1115_address, port.analog_channel)
                value = raw * sensor.scale + sensor.offset
                return SensorReadResult(
                    sensor.id,
                    "analog",
                    round(value, 3),
                    sensor.unit,
                    metadata={"raw_voltage": raw, "ads1115_address": port.ads1115_address, "analog_channel": port.analog_channel},
                )

            specs.append(
                SensorWorkerSpec(
                    sensor.id,
                    "analog",
                    sensor.check_frequency,
                    stale_after,
                    read_analog,
                    read_timeout_seconds=stale_after,
                )
            )
        for sensor in materialized_water_level_sensors(self.config):
            stale_after = self._stale_after(sensor.check_frequency, sensor.activity_timeout)
            if sensor.driver == WaterLevelDriver.BINARY:

                def read_binary(_stop: threading.Event, sensor=sensor) -> SensorReadResult:
                    port = SENSE_PORTS[sensor.sense_port]
                    with self._gpio_lock:
                        level = BinaryLevelSensor(port.digital_bcm, invert=sensor.invert_binary, simulation=self.simulation).read_state()
                    return SensorReadResult(
                        sensor.id,
                        "water",
                        level.value,
                        metadata={"driver": sensor.driver.value, "bcm_pin": port.digital_bcm},
                    )

                specs.append(
                    SensorWorkerSpec(
                        sensor.id,
                        "water",
                        sensor.check_frequency,
                        stale_after,
                        read_binary,
                        read_timeout_seconds=stale_after,
                    )
                )
                continue

            def read_hydros(_stop: threading.Event, sensor=sensor) -> SensorReadResult:
                reader = self._hydros_readers.get(sensor.id)
                if reader is None:
                    raise HardwareUnavailable(f"HYDROS PWM reader is unavailable for {sensor.id}.")
                level = reader.read_state()
                diagnostics = reader.diagnostics()
                if level == LevelState.INACTIVE:
                    return SensorReadResult(
                        sensor.id,
                        "water",
                        level.value,
                        data_status="stale",
                        error="HYDROS PWM pulses are inactive.",
                        metadata=diagnostics,
                    )
                if level == LevelState.UNKNOWN:
                    return SensorReadResult(
                        sensor.id,
                        "water",
                        level.value,
                        data_status="read_error",
                        error="HYDROS PWM frequency is outside known windows.",
                        metadata=diagnostics,
                    )
                return SensorReadResult(sensor.id, "water", level.value, metadata=diagnostics)

            specs.append(
                SensorWorkerSpec(
                    sensor.id,
                    "water",
                    sensor.check_frequency,
                    stale_after,
                    read_hydros,
                    read_timeout_seconds=stale_after,
                )
            )
        return specs

    def _install_sensor_workers(self, specs: list[SensorWorkerSpec]) -> None:
        with self._sensor_worker_lock:
            self._sensor_workers = {spec.sensor_id: SensorWorker(spec) for spec in specs}

    def _start_sensor_workers(self) -> None:
        with self._sensor_worker_lock:
            self._sensor_workers_started = True
            for worker in self._sensor_workers.values():
                worker.start()

    def _stop_sensor_workers(self) -> list[str]:
        with self._sensor_worker_lock:
            workers = list(self._sensor_workers.values())
            self._sensor_workers_started = False
        stuck: list[str] = []
        for worker in workers:
            if not worker.stop(timeout=2.0):
                stuck.append(worker.sensor_id)
        return stuck

    def _stop_sensor_workers_checked(self) -> None:
        stuck_workers = self._stop_sensor_workers()
        if stuck_workers:
            raise HardwareFault(f"Sensor workers did not stop: {', '.join(stuck_workers)}.")

    def _sensor_worker_snapshots(self) -> dict[str, SensorWorkerSnapshot]:
        with self._sensor_worker_lock:
            return {key: worker.snapshot() for key, worker in self._sensor_workers.items()}

    def _supervise_sensor_workers(self) -> None:
        if not self._sensor_workers_started or self._stop.is_set():
            return
        with self._sensor_worker_lock:
            workers = list(self._sensor_workers.values())
        for worker in workers:
            snapshot = worker.snapshot()
            if snapshot.worker_state == "failed" and not snapshot.thread_alive:
                LOGGER.warning("Restarting failed sensor worker %s.", snapshot.sensor_id)
                worker.start()

    def _retire_manual_prime_limit_alarms(self) -> None:
        with self._state_lock:
            for alarm in self.state.alarms.values():
                if alarm.id.startswith("stepper:") and alarm.id.endswith(":prime-limit"):
                    alarm.active = False

    def _stepper_profile(self, stepper_id: str) -> StepperProfile:
        for item in self.config.steppers:
            if item.id == stepper_id:
                return item
        raise KeyError(stepper_id)

    def _manual_prime_speed(self, profile: StepperProfile) -> int:
        try:
            speed = int(profile.manual_speed_steps_per_second)
        except (TypeError, ValueError) as exc:
            raise DiagnosticHalt(f"{profile.name} has invalid manual priming speed.") from exc
        if speed < MIN_MANUAL_PRIME_STEPS_PER_SECOND or speed > MAX_MANUAL_PRIME_STEPS_PER_SECOND:
            raise DiagnosticHalt(
                f"{profile.name} manual priming speed must be between "
                f"{MIN_MANUAL_PRIME_STEPS_PER_SECOND} and {MAX_MANUAL_PRIME_STEPS_PER_SECOND} step/s."
            )
        return speed

    def _clear_prime_worker_locked(self, stepper_id: str) -> None:
        self._prime_stop.pop(stepper_id, None)
        thread = self._prime_threads.get(stepper_id)
        if thread is None or not thread.is_alive() or thread is threading.current_thread():
            self._prime_threads.pop(stepper_id, None)

    def _prime_threads_snapshot(self) -> dict[str, threading.Thread]:
        with self._prime_lock:
            return dict(self._prime_threads)

    def _save_state(self) -> None:
        self.state_store.save(self.state)

    def _configured_sensor_catalog(self) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for item in materialized_water_level_sensors(self.config):
            catalog.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "kind": "water",
                    "check_frequency": item.check_frequency,
                    "stale_after": self._stale_after(item.check_frequency, item.activity_timeout),
                }
            )
        for item in materialized_temperature_sensors(self.config):
            catalog.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "kind": "temperature",
                    "check_frequency": item.check_frequency,
                    "stale_after": self._stale_after(item.check_frequency),
                }
            )
        for item in self.config.ph_sensors:
            catalog.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "kind": "ph",
                    "check_frequency": item.check_frequency,
                    "stale_after": self._stale_after(item.check_frequency),
                }
            )
        for item in materialized_analog_sensors(self.config):
            catalog.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "kind": "analog",
                    "check_frequency": item.check_frequency,
                    "stale_after": self._stale_after(item.check_frequency),
                }
            )
        return catalog

    @staticmethod
    def _stale_after(check_frequency: float, activity_timeout: float | None = None) -> float:
        base = max(float(check_frequency) * 3, float(check_frequency) + 5.0, 5.0)
        if activity_timeout is not None:
            base = max(base, float(activity_timeout) * 2)
        return base

    def _sensor_due(self, sensor_id: str, check_frequency: float, now: float, *, stale_after: float | None = None) -> bool:
        self._mark_sensor_stale_if_needed(sensor_id, now)
        due_at = self._sensor_next_due.get(sensor_id, 0.0)
        if now < due_at:
            return False
        frequency = max(0.1, float(check_frequency))
        self._sensor_next_due[sensor_id] = now + frequency
        health = self.state.sensor_health.setdefault(sensor_id, SensorHealth(sensor_id))
        health.last_attempt_ts = now
        health.stale_after_seconds = stale_after if stale_after is not None else self._stale_after(frequency)
        if health.status == "initializing":
            health.status = "initializing"
        return True

    def _mark_sensor_success(self, sensor_id: str, now: float, check_frequency: float, *, stale_after: float | None = None) -> None:
        health = self.state.sensor_health.setdefault(sensor_id, SensorHealth(sensor_id))
        health.status = "online"
        health.worker_state = "running"
        health.data_status = "online"
        health.last_success_ts = now
        health.last_attempt_ts = now
        health.last_error = ""
        health.consecutive_failures = 0
        health.stale_after_seconds = stale_after if stale_after is not None else self._stale_after(check_frequency)
        reading = self.state.readings.get(sensor_id)
        if reading:
            reading.ok = True

    def _mark_sensor_failure(self, sensor_id: str, now: float, exc: Exception, check_frequency: float, *, stale_after: float | None = None) -> None:
        health = self.state.sensor_health.setdefault(sensor_id, SensorHealth(sensor_id))
        health.status = "disconnected" if isinstance(exc, HardwareUnavailable) else "read_error"
        health.worker_state = "running"
        health.data_status = health.status
        health.last_attempt_ts = now
        health.last_error = str(exc)
        health.consecutive_failures += 1
        health.stale_after_seconds = stale_after if stale_after is not None else self._stale_after(check_frequency)
        reading = self.state.readings.get(sensor_id)
        if reading:
            reading.ok = False
        LOGGER.warning("Sensor %s poll failed: %s", sensor_id, exc)
        self.store.log_event("sensor", f"{sensor_id} {health.status}: {exc}", {"id": sensor_id, "status": health.status})

    def _mark_sensor_stale_if_needed(self, sensor_id: str, now: float) -> bool:
        health = self.state.sensor_health.get(sensor_id)
        if not health or not health.last_success_ts or health.status in {"read_error", "disconnected"}:
            return False
        stale_after = health.stale_after_seconds or 30.0
        if now - health.last_success_ts <= stale_after:
            return False
        health.status = "stale"
        health.data_status = "stale"
        health.last_error = f"No successful reading for {now - health.last_success_ts:.1f}s."
        reading = self.state.readings.get(sensor_id)
        if reading:
            reading.ok = False
        return True

    def _sensor_status(self, sensor_id: str, now: float) -> str:
        self._mark_sensor_stale_if_needed(sensor_id, now)
        return self.state.sensor_health.get(sensor_id, SensorHealth(sensor_id)).status

    def _turn_off_equipment_if_on(self, equipment_id: str, *, source: str) -> None:
        current = self.state.equipment.get(equipment_id)
        if current is not None and not current.on:
            return
        try:
            self.set_equipment(equipment_id, False, source=source)
        except Exception as exc:
            LOGGER.critical("Could not fail-safe equipment %s OFF after sensor fault: %s", equipment_id, exc)
            self._activate_alarm(
                f"equipment:{equipment_id}:failsafe",
                f"Could not turn {equipment_id} off after sensor fault: {exc}",
                priority="high",
            )

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            try:
                self._control_loop_iteration(now)
            except Exception as exc:
                LOGGER.exception("Polling loop iteration failed but will continue: %s", exc)
            self._stop.wait(1.0)

    def _control_loop_iteration(self, now: float) -> None:
        self._supervise_sensor_workers()
        snapshots = self._sensor_worker_snapshots()
        equipment_commands: list[tuple[str, bool, str]] = []
        value_logs: list[tuple[str, float | str, str, float]] = []
        events: list[tuple[str, str, dict[str, Any] | None]] = []
        alarms: list[Any] = []
        ato_notifications: list[Any] = []
        with self._state_lock:
            self._apply_sensor_worker_snapshots_locked(
                snapshots,
                now,
                equipment_commands=equipment_commands,
                value_logs=value_logs,
                events=events,
                alarms=alarms,
            )
            self._poll_health(now)
            self._plan_ato_locked(now, equipment_commands=equipment_commands, alarms=alarms, notifications=ato_notifications)
        for equipment_id, on, source in equipment_commands:
            try:
                self.set_equipment(equipment_id, on, source=source)
            except Exception as exc:
                LOGGER.exception("Equipment command failed for %s from %s: %s", equipment_id, source, exc)
                self._activate_alarm(
                    f"equipment:{equipment_id}:{source}",
                    f"{source} could not command {equipment_id}: {exc}",
                    priority="high",
                )
        for stream, value, unit, ts in value_logs:
            self.store.log_value(stream, value, unit=unit, ts=ts)
        for category, message, metadata in events:
            self.store.log_event(category, message, metadata)
        for alarm in alarms:
            self._register_alarm(alarm)
        for alarm in ato_notifications:
            self._notify("Loggerhead ATO", alarm.message)
            self.store.log_event("ato", alarm.message)
        self._sound_buzzer_if_needed(now)
        self._publish_telemetry()
        if now - self._last_polled_log >= self.config.database_poll_seconds:
            self._last_polled_log = now
            self._log_poll_snapshot(now)
        if self.store.prune_if_critical():
            self._activate_alarm("storage:disk", "Primary filesystem free space is below warning threshold.", priority="high")
        self._save_state()

    def _apply_sensor_worker_snapshots_locked(
        self,
        snapshots: dict[str, SensorWorkerSnapshot],
        now: float,
        *,
        equipment_commands: list[tuple[str, bool, str]],
        value_logs: list[tuple[str, float | str, str, float]],
        events: list[tuple[str, str, dict[str, Any] | None]],
        alarms: list[Any],
    ) -> None:
        for snapshot in snapshots.values():
            self._apply_sensor_health_snapshot_locked(snapshot, now, events=events, alarms=alarms)
        for sensor in materialized_temperature_sensors(self.config):
            snapshot = snapshots.get(sensor.id)
            if snapshot is None:
                continue
            online = snapshot.data_status == "online"
            if not online:
                reading = self.state.readings.get(sensor.id)
                if reading:
                    reading.ok = False
                if sensor.assigned_equipment:
                    self._plan_equipment_off_if_on_locked(
                        sensor.assigned_equipment,
                        "sensor_stale" if snapshot.status == "stale" else "sensor_fault",
                        equipment_commands,
                    )
                continue
            if not self._snapshot_has_new_attempt(snapshot):
                continue
            value = float(snapshot.value)
            self.state.readings[sensor.id] = SensorReading(sensor.id, round(value, 3), "F", ts=snapshot.last_success_ts or now)
            value_logs.append((f"temperature.{sensor.id}", value, "F", snapshot.last_success_ts or now))
            alarm = AlertEvaluator.temperature_alarm(sensor, value, now)
            if alarm:
                alarms.append(alarm)
            elif f"temperature:{sensor.id}" in self.state.alarms:
                self.state.alarms[f"temperature:{sensor.id}"].active = False
            if sensor.assigned_equipment:
                current = self.state.equipment.get(sensor.assigned_equipment, EquipmentState(sensor.assigned_equipment, False)).on
                desired = ThermalController.evaluate(sensor, value, current)
                if desired != current:
                    equipment_commands.append((sensor.assigned_equipment, desired, "thermal"))
        for sensor in self.config.ph_sensors:
            snapshot = snapshots.get(sensor.id)
            if snapshot is None:
                continue
            if snapshot.data_status != "online":
                reading = self.state.readings.get(sensor.id)
                if reading:
                    reading.ok = False
                continue
            if self._snapshot_has_new_attempt(snapshot):
                value = float(snapshot.value)
                self.state.readings[sensor.id] = SensorReading(sensor.id, round(value, 3), "pH", ts=snapshot.last_success_ts or now)
                value_logs.append((f"ph.{sensor.id}", value, "pH", snapshot.last_success_ts or now))
        for sensor in materialized_analog_sensors(self.config):
            snapshot = snapshots.get(sensor.id)
            if snapshot is None:
                continue
            if snapshot.data_status != "online":
                reading = self.state.readings.get(sensor.id)
                if reading:
                    reading.ok = False
                continue
            if self._snapshot_has_new_attempt(snapshot):
                value = float(snapshot.value)
                self.state.readings[sensor.id] = SensorReading(sensor.id, round(value, 3), sensor.unit, ts=snapshot.last_success_ts or now)
                value_logs.append((f"analog.{sensor.id}", value, sensor.unit, snapshot.last_success_ts or now))
        for sensor in materialized_water_level_sensors(self.config):
            snapshot = snapshots.get(sensor.id)
            if snapshot is None:
                continue
            new_attempt = self._snapshot_has_new_attempt(snapshot)
            if snapshot.data_status != "online":
                previous = self.state.water_levels.get(sensor.id)
                self.state.water_levels[sensor.id] = LevelState.UNKNOWN
                sensor.current_state = LevelState.UNKNOWN
                if previous != LevelState.UNKNOWN and new_attempt:
                    self._level_since[sensor.id] = now
                    events.append(("water_level", f"{sensor.name} read error; state set to unknown.", {"id": sensor.id}))
                continue
            if not new_attempt:
                continue
            level = LevelState(snapshot.value)
            previous = self.state.water_levels.get(sensor.id)
            self.state.water_levels[sensor.id] = level
            sensor.current_state = level
            if previous != level:
                self._level_since[sensor.id] = now
                events.append(("water_level", f"{sensor.name} changed to {level.value}.", {"id": sensor.id}))
            observed_since = self._level_since.setdefault(sensor.id, now)
            alarm = AlertEvaluator.water_alarm(sensor, observed_since, now)
            if alarm:
                alarms.append(alarm)
            value_logs.append((f"water.{sensor.id}", level.value, "", snapshot.last_success_ts or now))

    def _apply_sensor_health_snapshot_locked(
        self,
        snapshot: SensorWorkerSnapshot,
        now: float,
        *,
        events: list[tuple[str, str, dict[str, Any] | None]],
        alarms: list[Any],
    ) -> None:
        health = self.state.sensor_health.setdefault(snapshot.sensor_id, SensorHealth(snapshot.sensor_id))
        previous_status = health.status
        health.status = snapshot.status
        health.worker_state = snapshot.worker_state
        health.data_status = snapshot.data_status
        health.last_attempt_ts = snapshot.last_attempt_ts
        health.last_success_ts = snapshot.last_success_ts
        health.read_duration_seconds = snapshot.read_duration_seconds
        health.next_scheduled_ts = snapshot.next_scheduled_ts
        health.last_error = snapshot.error
        health.consecutive_failures = snapshot.consecutive_failures
        health.stale_after_seconds = snapshot.stale_after_seconds
        health.diagnostics = dict(snapshot.metadata)
        alarm_id = f"sensor:{snapshot.sensor_id}"
        if snapshot.status == "online":
            existing = self.state.alarms.get(alarm_id)
            if existing and existing.active:
                existing.active = False
                events.append(("sensor", f"{snapshot.sensor_id} recovered.", {"id": snapshot.sensor_id, "status": "online"}))
            return
        if snapshot.status in {"initializing", "starting"}:
            return
        if previous_status != snapshot.status:
            events.append(
                (
                    "sensor",
                    f"{snapshot.sensor_id} {snapshot.status}: {snapshot.error or 'no valid reading'}",
                    {"id": snapshot.sensor_id, "status": snapshot.status},
                )
            )
        existing = self.state.alarms.get(alarm_id)
        if existing and existing.active and existing.message.endswith(snapshot.error):
            return
        message = f"{snapshot.sensor_id} sensor {snapshot.status}: {snapshot.error or 'no valid reading'}"
        alarms.append(AlarmState(alarm_id, message, AlarmPriority.WARNING, first_seen=now))

    def _snapshot_has_new_attempt(self, snapshot: SensorWorkerSnapshot) -> bool:
        if not snapshot.last_attempt_ts:
            return False
        last_applied = self._sensor_last_applied_attempt.get(snapshot.sensor_id, 0.0)
        if snapshot.last_attempt_ts <= last_applied:
            return False
        self._sensor_last_applied_attempt[snapshot.sensor_id] = snapshot.last_attempt_ts
        return True

    def _plan_equipment_off_if_on_locked(
        self,
        equipment_id: str,
        source: str,
        equipment_commands: list[tuple[str, bool, str]],
    ) -> None:
        current = self.state.equipment.get(equipment_id)
        if current is not None and not current.on:
            return
        equipment_commands.append((equipment_id, False, source))

    def _plan_ato_locked(
        self,
        now: float,
        *,
        equipment_commands: list[tuple[str, bool, str]],
        alarms: list[Any],
        notifications: list[Any],
    ) -> None:
        equipment_ids = {item.id for item in self.config.equipment}
        for profile in self.config.ato:
            should_run, ato_state, alarm = ATOController.evaluate(profile, self.state, now)
            current = self.state.equipment.get(profile.assigned_actuator, EquipmentState(profile.assigned_actuator, False)).on
            if profile.assigned_actuator in equipment_ids and should_run != current:
                equipment_commands.append((profile.assigned_actuator, should_run, "ato"))
            if alarm:
                alarms.append(alarm)
                notifications.append(alarm)
            self.state.ato[profile.id] = ato_state

    def _poll_temperature(self, now: float) -> None:
        for sensor in materialized_temperature_sensors(self.config):
            stale_after = self._stale_after(sensor.check_frequency)
            if not self._sensor_due(sensor.id, sensor.check_frequency, now, stale_after=stale_after):
                if sensor.assigned_equipment and self._sensor_status(sensor.id, now) == "stale":
                    self._turn_off_equipment_if_on(sensor.assigned_equipment, source="sensor_stale")
                continue
            try:
                value = self._read_temperature(sensor)
            except Exception as exc:
                self._mark_sensor_failure(sensor.id, now, exc, sensor.check_frequency, stale_after=stale_after)
                if sensor.assigned_equipment:
                    self._turn_off_equipment_if_on(sensor.assigned_equipment, source="sensor_fault")
                continue
            self.state.readings[sensor.id] = SensorReading(sensor.id, round(value, 3), "F", ts=now)
            self._mark_sensor_success(sensor.id, now, sensor.check_frequency, stale_after=stale_after)
            self.store.log_value(f"temperature.{sensor.id}", value, unit="F", ts=now)
            alarm = AlertEvaluator.temperature_alarm(sensor, value, now)
            if alarm:
                self._register_alarm(alarm)
            elif f"temperature:{sensor.id}" in self.state.alarms:
                self.state.alarms[f"temperature:{sensor.id}"].active = False
            if sensor.assigned_equipment:
                current = self.state.equipment.get(sensor.assigned_equipment, EquipmentState(sensor.assigned_equipment, False)).on
                desired = ThermalController.evaluate(sensor, value, current)
                if desired != current:
                    try:
                        self.set_equipment(sensor.assigned_equipment, desired, source="thermal")
                    except Exception as exc:
                        LOGGER.exception("Thermal equipment command failed for %s: %s", sensor.assigned_equipment, exc)
                        self._activate_alarm(
                            f"equipment:{sensor.assigned_equipment}:thermal",
                            f"Thermal control could not command {sensor.assigned_equipment}: {exc}",
                            priority="high",
                        )

    def _read_temperature(self, sensor) -> float:
        if sensor.driver == TemperatureDriver.ONE_WIRE_BUS:
            if sensor.sense_port is not None:
                port = SENSE_PORTS[sensor.sense_port]
                self.temperature_reader.configure_kernel_one_wire(port.digital_bcm)
                return self.temperature_reader.read_one_wire_gpio(port.digital_bcm)
            raise HardwareUnavailable(f"Kernel 1-Wire sensor {sensor.id} is not assigned to a fixed sense-port GPIO.")
        if sensor.driver == TemperatureDriver.BIT_BANGED_ONE_WIRE:
            if sensor.sense_port is None:
                raise HardwareUnavailable(f"Bit-banged DS18B20 sensor {sensor.id} is not assigned to a fixed sense-port GPIO.")
            port = SENSE_PORTS[sensor.sense_port]
            return self.temperature_reader.read_bit_banged(port.digital_bcm)
        return self.temperature_reader.read_host_cpu()

    def _read_temperature_threadsafe(self, sensor) -> float:
        if sensor.driver == TemperatureDriver.HOST_CPU:
            return self.temperature_reader.read_host_cpu()
        with self._gpio_lock:
            return self._read_temperature(sensor)

    def _poll_ph(self, now: float) -> None:
        for sensor in self.config.ph_sensors:
            stale_after = self._stale_after(sensor.check_frequency)
            if not self._sensor_due(sensor.id, sensor.check_frequency, now, stale_after=stale_after):
                continue
            try:
                value = self.ph_sensor.read_ph()
            except Exception as exc:
                self._mark_sensor_failure(sensor.id, now, exc, sensor.check_frequency, stale_after=stale_after)
                continue
            self.state.readings[sensor.id] = SensorReading(sensor.id, round(value, 3), "pH", ts=now)
            self._mark_sensor_success(sensor.id, now, sensor.check_frequency, stale_after=stale_after)
            self.store.log_value(f"ph.{sensor.id}", value, unit="pH", ts=now)

    def _poll_water_levels(self, now: float) -> None:
        for sensor in materialized_water_level_sensors(self.config):
            stale_after = self._stale_after(sensor.check_frequency, sensor.activity_timeout)
            if not self._sensor_due(sensor.id, sensor.check_frequency, now, stale_after=stale_after):
                if self._sensor_status(sensor.id, now) == "stale":
                    self.state.water_levels[sensor.id] = LevelState.UNKNOWN
                continue
            try:
                if sensor.driver == WaterLevelDriver.BINARY:
                    port = SENSE_PORTS[sensor.sense_port]
                    level = BinaryLevelSensor(port.digital_bcm, invert=sensor.invert_binary, simulation=self.simulation).read_state()
                else:
                    reader = self._hydros_readers.get(sensor.id)
                    if reader is None:
                        raise HardwareUnavailable(f"HYDROS PWM reader is unavailable for {sensor.id}.")
                    level = reader.read_state()
            except Exception as exc:
                self._mark_sensor_failure(sensor.id, now, exc, sensor.check_frequency, stale_after=stale_after)
                previous = self.state.water_levels.get(sensor.id)
                self.state.water_levels[sensor.id] = LevelState.UNKNOWN
                sensor.current_state = LevelState.UNKNOWN
                if previous != LevelState.UNKNOWN:
                    self._level_since[sensor.id] = now
                    self.store.log_event("water_level", f"{sensor.name} read error; state set to unknown.", {"id": sensor.id})
                continue
            previous = self.state.water_levels.get(sensor.id)
            self.state.water_levels[sensor.id] = level
            sensor.current_state = level
            self._mark_sensor_success(sensor.id, now, sensor.check_frequency, stale_after=stale_after)
            if previous != level:
                self._level_since[sensor.id] = now
                self.store.log_event("water_level", f"{sensor.name} changed to {level.value}.", {"id": sensor.id})
            observed_since = self._level_since.setdefault(sensor.id, now)
            alarm = AlertEvaluator.water_alarm(sensor, observed_since, now)
            if alarm:
                self._register_alarm(alarm)
            self.store.log_value(f"water.{sensor.id}", level.value, ts=now)

    def _poll_health(self, now: float) -> None:
        for key, value in self.health.snapshot().items():
            self.state.readings[f"health_{key}"] = SensorReading(f"health_{key}", round(value, 3), "", ts=now)
            self.store.log_value(f"health.{key}", value, ts=now)

    def _evaluate_ato(self, now: float) -> None:
        for profile in self.config.ato:
            should_run, ato_state, alarm = ATOController.evaluate(profile, self.state, now)
            current = self.state.equipment.get(profile.assigned_actuator, EquipmentState(profile.assigned_actuator, False)).on
            if profile.assigned_actuator in {item.id for item in self.config.equipment} and should_run != current:
                try:
                    self.set_equipment(profile.assigned_actuator, should_run, source="ato")
                except Exception as exc:
                    LOGGER.exception("ATO equipment command failed for %s: %s", profile.assigned_actuator, exc)
                    self._activate_alarm(
                        f"equipment:{profile.assigned_actuator}:ato",
                        f"ATO could not command {profile.assigned_actuator}: {exc}",
                        priority="high",
                    )
            if alarm:
                self._register_alarm(alarm)
                self._notify("Loggerhead ATO", alarm.message)
                self.store.log_event("ato", alarm.message)
            self.state.ato[profile.id] = ato_state

    def _register_alarm(self, alarm) -> None:
        with self._state_lock:
            existing = self.state.alarms.get(alarm.id)
            if existing and existing.active:
                alarm.first_seen = existing.first_seen
                alarm.last_notified = existing.last_notified
            self.state.alarms[alarm.id] = alarm
        self.mqtt.publish(f"alerts/{alarm.id}", {"active": True, "message": alarm.message, "priority": alarm.priority.value})
        if not self.config.buzzer.alarm_enabled:
            return
        if self.notifications.should_send(alarm.id, 300):
            self._notify("Loggerhead alarm", alarm.message)
            alarm.last_notified = time.time()
            self.store.log_event("alarm", alarm.message, {"priority": alarm.priority.value})

    def _activate_alarm(self, alarm_id: str, message: str, *, priority: str = "warning") -> None:
        from .hardware import AlarmPriority
        from .state import AlarmState

        self._register_alarm(AlarmState(alarm_id, message, AlarmPriority(priority), first_seen=time.time()))

    def _handle_manual_prime_limit(self, profile: StepperProfile, result) -> None:
        reason_text = "time limit" if result.reason == "max_seconds" else "step limit"
        message = f"{profile.name} manual priming stopped at {result.steps_sent} steps after reaching the {reason_text}."
        LOGGER.info(message)
        with self._state_lock:
            existing = self.state.alarms.get(f"stepper:{profile.id}:prime-limit")
            if existing:
                existing.active = False
        self.store.log_event(
            "dosing",
            message,
            {
                "id": profile.id,
                "steps": result.steps_sent,
                "reason": result.reason,
                "event": "manual_prime_limit",
            },
        )
        self.mqtt.publish(
            f"dosing/{profile.id}/prime-limit",
            {
                "id": profile.id,
                "steps": result.steps_sent,
                "reason": result.reason,
                "message": message,
                "ts": time.time(),
            },
            retain=False,
        )
        self._quick_buzzer_chirp()

    def _quick_buzzer_chirp(self) -> None:
        if (
            self._stop.is_set()
            or not self.config.buzzer.enabled
            or not self.config.buzzer.alarm_enabled
            or time.time() < self.state.buzzer_muted_until
        ):
            return
        if self._active_audible_alarm_ids():
            return

        def chirp() -> None:
            try:
                self.buzzer.sound(AlarmPriority.INFO)
                self._stop.wait(self.MANUAL_PRIME_LIMIT_CHIRP_SECONDS)
            finally:
                self._sound_buzzer_if_needed(time.time())

        threading.Thread(target=chirp, name="prime-limit-chirp", daemon=True).start()

    def _sound_buzzer_if_needed(self, now: float) -> None:
        active_ids = self._active_audible_alarm_ids()
        self._restart_suppressed_alarm_ids.intersection_update(active_ids)
        active = [
            alarm
            for alarm in self.state.alarms.values()
            if alarm.id in active_ids and alarm.id not in self._restart_suppressed_alarm_ids
        ]
        if not self.config.buzzer.enabled or not self.config.buzzer.alarm_enabled or not active or now < self.state.buzzer_muted_until:
            self.buzzer.stop()
            return
        self.buzzer.sound(max((alarm.priority for alarm in active), key=lambda p: ["info", "warning", "high", "critical"].index(p.value)))

    def _active_audible_alarm_ids(self) -> set[str]:
        return {
            alarm.id
            for alarm in self.state.alarms.values()
            if alarm.active and alarm.priority.value in {"high", "critical"}
        }

    def _poll_analog(self, now: float) -> None:
        for sensor in materialized_analog_sensors(self.config):
            stale_after = self._stale_after(sensor.check_frequency)
            if not self._sensor_due(sensor.id, sensor.check_frequency, now, stale_after=stale_after):
                continue
            try:
                port = SENSE_PORTS[sensor.sense_port]
                raw = self.analog_reader.read_voltage(port.ads1115_address, port.analog_channel)
                value = raw * sensor.scale + sensor.offset
            except Exception as exc:
                self._mark_sensor_failure(sensor.id, now, exc, sensor.check_frequency, stale_after=stale_after)
                continue
            self.state.readings[sensor.id] = SensorReading(sensor.id, round(value, 3), sensor.unit, ts=now)
            self._mark_sensor_success(sensor.id, now, sensor.check_frequency, stale_after=stale_after)
            self.store.log_value(f"analog.{sensor.id}", value, unit=sensor.unit, ts=now)

    def _publish_telemetry(self) -> None:
        self.mqtt.publish("state", self.status(), retain=False)

    def _log_poll_snapshot(self, now: float) -> None:
        for key, reading in self.state.readings.items():
            self.store.log_value(f"snapshot.{key}", reading.value, unit=reading.unit, ts=now)
        for key, state in self.state.equipment.items():
            self.store.log_value(f"equipment.{key}", int(state.on), ts=now)

    def _equipment_profile(self, equipment_id: str) -> EquipmentProfile:
        for item in self.config.equipment:
            if item.id == equipment_id:
                return item
        raise KeyError(equipment_id)

    def _notify(self, title: str, message: str) -> None:
        if not self.config.buzzer.alarm_enabled:
            return
        self.telegram.send(message)
        self.ha_notifier.send(title, message)
        self.mqtt.publish("alerts/latest", {"title": title, "message": message, "ts": time.time()}, retain=False)

    def _sensor_catalog(self) -> list[dict[str, Any]]:
        catalog = []
        for item in materialized_water_level_sensors(self.config):
            catalog.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "kind": "water",
                    "main": True,
                    "desired": item.desired_state.value,
                    "check_frequency": item.check_frequency,
                    "stale_after": self._stale_after(item.check_frequency, item.activity_timeout),
                }
            )
        for item in materialized_temperature_sensors(self.config):
            catalog.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "kind": "temperature",
                    "main": item.driver != TemperatureDriver.HOST_CPU,
                    "check_frequency": item.check_frequency,
                    "stale_after": self._stale_after(item.check_frequency),
                }
            )
        for item in self.config.ph_sensors:
            catalog.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "kind": "ph",
                    "main": True,
                    "check_frequency": item.check_frequency,
                    "stale_after": self._stale_after(item.check_frequency),
                }
            )
        for item in materialized_analog_sensors(self.config):
            catalog.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "kind": "analog",
                    "main": True,
                    "check_frequency": item.check_frequency,
                    "stale_after": self._stale_after(item.check_frequency),
                }
            )
        return catalog

    def _diagnostics(self) -> dict[str, Any]:
        stepper_diagnostics = {}
        for item in self.config.steppers:
            try:
                from .hardware import require_stepper

                assignment = require_stepper(item.assignment)
                stepper_diagnostics[item.id] = self.uart.diagnostics(assignment.uart_address)
            except Exception as exc:
                stepper_diagnostics[item.id] = {"error": str(exc)}
        return {
            "steppers": stepper_diagnostics,
            "sensor_workers": {key: asdict(value) for key, value in self._sensor_worker_snapshots().items()},
            "hydros_pwm": {
                key: reader.diagnostics()
                for key, reader in self._hydros_readers.items()
            },
            "buzzer_alarm_enabled": self.config.buzzer.alarm_enabled,
            "mqtt_enabled": self.config.mqtt.enabled,
            "telegram_enabled": self.config.telegram.enabled,
            "home_assistant_notify_enabled": self.config.home_assistant_notify.enabled,
        }
