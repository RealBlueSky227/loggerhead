from __future__ import annotations

import json
import logging
import signal
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import (
    AppConfig,
    EquipmentProfile,
    OneWireMode,
    SensePortDevice,
    load_config,
    materialized_analog_sensors,
    materialized_temperature_sensors,
    materialized_water_level_sensors,
    save_config,
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
    HostHealthMonitor,
    HydrosTripleClassifier,
    KasaHS300Client,
    MCP23017RelayBoard,
    StepperPulseEngine,
    TemperatureReader,
)
from .hardware import RELAYS, SENSE_PORTS, EquipmentDriver, TemperatureDriver, WaterLevelDriver
from .notifications import HomeAssistantNotifier, MQTTHomeAssistantBridge, NotificationLimiter, TelegramNotifier
from .state import EquipmentState, SensorReading, StateStore
from .web import DashboardServer

LOGGER = logging.getLogger(__name__)


class LoggerheadService:
    """Long-running aquarium controller service.

    Implements SRS 1.1, 1.2, 2.4, 5.1, 5.4.1, 6.1, 6.2, 6.3, and the orchestration
    path for all hardware/control subsystems.
    """

    def __init__(self, config_path: Path, data_dir: Path, *, simulation: bool = False) -> None:
        self.config_path = config_path
        self.data_dir = data_dir
        self.simulation = simulation
        self.config = load_config(config_path)
        self.store = TelemetryStore(data_dir / "loggerhead.sqlite3", max_points_default=self.config.plot_max_points)
        self.state_store = StateStore(data_dir / "state.json")
        self.state = self.state_store.load()
        self.relay_board = MCP23017RelayBoard(simulation=simulation)
        self.analog_reader = ADS1115AnalogReader(simulation=simulation)
        self.temperature_reader = TemperatureReader(simulation=simulation)
        self.ph_sensor = EzoPHSensor(simulation=simulation)
        self.buzzer = Buzzer(frequency_hz=self.config.buzzer.frequency_hz, simulation=simulation)
        self.uart = TMC2209UART(simulation=simulation)
        self.stepper_engine = StepperPulseEngine(self.uart, self.relay_board, simulation=simulation)
        self.health = HostHealthMonitor()
        self.telegram = TelegramNotifier(self.config.telegram)
        self.ha_notifier = HomeAssistantNotifier(self.config.home_assistant_notify)
        self.mqtt = MQTTHomeAssistantBridge(self.config.mqtt)
        self.mqtt.set_command_handler(self.handle_mqtt_command)
        self.notifications = NotificationLimiter()
        self._stop = threading.Event()
        self._stop_lock = threading.RLock()
        self._stopped = False
        self._threads: list[threading.Thread] = []
        self._prime_stop: dict[str, threading.Event] = {}
        self._prime_threads: dict[str, threading.Thread] = {}
        self._level_since: dict[str, float] = {}
        self._hydros: dict[str, HydrosTripleClassifier] = {
            sensor.id: HydrosTripleClassifier(
                debounce_samples=sensor.debounce_samples,
                activity_timeout=sensor.activity_timeout,
            )
            for sensor in materialized_water_level_sensors(self.config)
            if sensor.driver == WaterLevelDriver.HYDROS_TRIPLE
        }
        self._last_polled_log = 0.0
        self._clear_transient_stepper_state()
        self._restore_equipment_defaults()

    def run(self, *, host: str = "0.0.0.0", port: int = 8080) -> None:
        LOGGER.info("Starting Loggerhead on %s:%s", host, port)
        self._notify("Loggerhead", "Loggerhead aquarium controller started.")
        server = DashboardServer(self, host=host, port=port)
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
                self.state_store.save(self.state)
            if shutdown_error is not None:
                raise shutdown_error

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stop.set()
            for event in self._prime_stop.values():
                event.set()
            self._clear_transient_stepper_state()
            errors: list[Exception] = []
            for action in (self.stepper_engine.shutdown, self.relay_board.disable_all_steppers, self.buzzer.stop):
                try:
                    action()
                except Exception as exc:
                    errors.append(exc)
                    LOGGER.critical("Hardware shutdown action failed: %s", exc)
            for stepper_id, thread in list(self._prime_threads.items()):
                if thread is threading.current_thread():
                    continue
                thread.join(timeout=2.0)
                if thread.is_alive():
                    LOGGER.critical("Manual priming thread %s did not stop within timeout.", stepper_id)
            self.state_store.save(self.state)
            self._stopped = True
            if errors:
                raise HardwareFault(f"Loggerhead shutdown could not verify all actuators disabled: {errors[0]}") from errors[0]

    def status(self) -> dict[str, Any]:
        return {
            "config": asdict(self.config),
            "sense_ports": [asdict(item) for item in self.config.sense_ports],
            "sensor_catalog": self._sensor_catalog(),
            "steppers": [asdict(item) for item in self.config.steppers],
            "equipment": {key: asdict(value) for key, value in self.state.equipment.items()},
            "readings": {key: asdict(value) for key, value in self.state.readings.items()},
            "water_levels": {key: value.value for key, value in self.state.water_levels.items()},
            "alarms": {key: asdict(value) for key, value in self.state.alarms.items()},
            "ato": {key: asdict(value) for key, value in self.state.ato.items()},
            "manual_priming": self.state.manual_priming,
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
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.config = load_config(self.config_path)
        save_config(self.config_path, self.config)
        self._hydros = {
            sensor.id: HydrosTripleClassifier(
                debounce_samples=sensor.debounce_samples,
                activity_timeout=sensor.activity_timeout,
            )
            for sensor in materialized_water_level_sensors(self.config)
            if sensor.driver == WaterLevelDriver.HYDROS_TRIPLE
        }
        self.store.log_event("config", "Configuration reloaded from UI.")
        return self.config

    def set_sense_port(self, number: int, payload: dict[str, Any]) -> None:
        port = next(item for item in self.config.sense_ports if item.number == number)
        if "device" in payload:
            port.device = SensePortDevice(payload["device"])
        if "name" in payload:
            port.name = str(payload["name"])
        if "one_wire_mode" in payload:
            port.one_wire_mode = OneWireMode(payload["one_wire_mode"])
        save_config(self.config_path, self.config)
        self.config = load_config(self.config_path)
        self._hydros = {
            sensor.id: HydrosTripleClassifier(
                debounce_samples=sensor.debounce_samples,
                activity_timeout=sensor.activity_timeout,
            )
            for sensor in materialized_water_level_sensors(self.config)
            if sensor.driver == WaterLevelDriver.HYDROS_TRIPLE
        }
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
        self.state.equipment[equipment_id] = EquipmentState(equipment_id, on, source)
        self.store.log_event("equipment", f"{profile.name} turned {'ON' if on else 'OFF'} by {source}.", {"id": equipment_id})
        self.mqtt.publish(f"equipment/{equipment_id}", {"on": on, "source": source})
        self.state_store.save(self.state)

    def reset_ato(self, ato_id: str) -> None:
        profile = next(item for item in self.config.ato if item.id == ato_id)
        ATOController.reset(profile, self.state)
        self.store.log_event("ato", f"ATO {profile.name} reset from dashboard.")
        self.state_store.save(self.state)

    def silence_buzzer(self) -> None:
        self.state.buzzer_muted_until = time.time() + self.config.buzzer.rearm_seconds
        self.buzzer.stop()
        self.store.log_event("alarm", "Buzzer temporarily silenced.")
        self.state_store.save(self.state)

    def set_alarm_enabled(self, enabled: bool) -> None:
        self.config.buzzer.alarm_enabled = enabled
        save_config(self.config_path, self.config)
        if not enabled:
            self.buzzer.stop()
        self.store.log_event("alarm", f"Global alarm {'enabled' if enabled else 'disabled'}.")
        self.state_store.save(self.state)

    def set_manual_priming(self, stepper_id: str, enabled: bool) -> None:
        profile = next(item for item in self.config.steppers if item.id == stepper_id)
        if enabled and self._stop.is_set():
            raise RuntimeError("Loggerhead is stopping; manual priming cannot be started.")
        if not enabled:
            stop = self._prime_stop.get(stepper_id)
            if stop:
                stop.set()
            self.state.manual_priming[stepper_id] = False
            self.store.log_event("dosing", f"{profile.name} manual priming stopped.")
            self.state_store.save(self.state)
            return
        if any(self.state.manual_priming.values()):
            raise RuntimeError("Only one pump may be manually primed at a time.")
        stop = threading.Event()
        self._prime_stop[stepper_id] = stop
        self.state.manual_priming[stepper_id] = True
        thread = threading.Thread(target=self._prime_loop, args=(profile, stop), name=f"prime-{stepper_id}", daemon=True)
        self._prime_threads[stepper_id] = thread
        self.store.log_event("dosing", f"{profile.name} manual priming started.")
        thread.start()
        self.state_store.save(self.state)

    def _prime_loop(self, profile, stop: threading.Event) -> None:
        from .hardware import require_stepper

        assignment = require_stepper(profile.assignment)
        while not stop.is_set():
            try:
                self.stepper_engine.move(
                    assignment,
                    steps=max(1, profile.manual_speed_steps_per_second),
                    steps_per_second=max(1, profile.manual_speed_steps_per_second),
                    run_current_ma=profile.run_current_ma,
                    hold_current_ma=profile.hold_current_ma,
                )
            except Exception as exc:
                self._activate_alarm(f"stepper:{profile.id}", f"{profile.name} manual priming fault: {exc}", priority="high")
                break
        self.state.manual_priming[profile.id] = False
        self.state_store.save(self.state)

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
        self.state.stepper_active = None
        self.state.manual_priming = {item.id: False for item in self.config.steppers}

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            self._poll_temperature(now)
            self._poll_ph(now)
            self._poll_analog(now)
            self._poll_water_levels(now)
            self._poll_health(now)
            self._evaluate_ato(now)
            self._sound_buzzer_if_needed(now)
            self._publish_telemetry()
            if now - self._last_polled_log >= self.config.database_poll_seconds:
                self._last_polled_log = now
                self._log_poll_snapshot(now)
            if self.store.prune_if_critical():
                self._activate_alarm("storage:disk", "Primary filesystem free space is below warning threshold.", priority="high")
            self.state_store.save(self.state)
            self._stop.wait(1.0)

    def _poll_temperature(self, now: float) -> None:
        for sensor in materialized_temperature_sensors(self.config):
            value = self._read_temperature(sensor)
            self.state.readings[sensor.id] = SensorReading(sensor.id, round(value, 3), "F", ts=now)
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
                    self.set_equipment(sensor.assigned_equipment, desired, source="thermal")

    def _read_temperature(self, sensor) -> float:
        if sensor.driver == TemperatureDriver.ONE_WIRE_BUS:
            if sensor.sense_port is not None:
                port = SENSE_PORTS[sensor.sense_port]
                self.temperature_reader.configure_kernel_one_wire(port.digital_bcm)
            return self.temperature_reader.read_one_wire_bus(sensor.sensor_id)
        if sensor.driver == TemperatureDriver.BIT_BANGED_ONE_WIRE:
            port = SENSE_PORTS[sensor.sense_port or 1]
            return self.temperature_reader.read_bit_banged(port.digital_bcm)
        return self.temperature_reader.read_host_cpu()

    def _poll_ph(self, now: float) -> None:
        for sensor in self.config.ph_sensors:
            value = self.ph_sensor.read_ph()
            self.state.readings[sensor.id] = SensorReading(sensor.id, round(value, 3), "pH", ts=now)
            self.store.log_value(f"ph.{sensor.id}", value, unit="pH", ts=now)

    def _poll_water_levels(self, now: float) -> None:
        for sensor in materialized_water_level_sensors(self.config):
            if sensor.driver == WaterLevelDriver.BINARY:
                port = SENSE_PORTS[sensor.sense_port]
                level = BinaryLevelSensor(port.digital_bcm, invert=sensor.invert_binary, simulation=self.simulation).read_state()
            else:
                classifier = self._hydros[sensor.id]
                # Real edge capture is attached through pigpio callbacks on a Pi. Simulation exposes stable Normal.
                level = classifier.observe_period_us(2520) if self.simulation else classifier.activity_state()
            previous = self.state.water_levels.get(sensor.id)
            self.state.water_levels[sensor.id] = level
            sensor.current_state = level
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
                self.set_equipment(profile.assigned_actuator, should_run, source="ato")
            if alarm:
                self._register_alarm(alarm)
                self._notify("Loggerhead ATO", alarm.message)
                self.store.log_event("ato", alarm.message)
            self.state.ato[profile.id] = ato_state

    def _register_alarm(self, alarm) -> None:
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

    def _sound_buzzer_if_needed(self, now: float) -> None:
        active = [alarm for alarm in self.state.alarms.values() if alarm.active and alarm.priority.value in {"high", "critical"}]
        if not self.config.buzzer.enabled or not self.config.buzzer.alarm_enabled or not active or now < self.state.buzzer_muted_until:
            self.buzzer.stop()
            return
        self.buzzer.sound(max((alarm.priority for alarm in active), key=lambda p: ["info", "warning", "high", "critical"].index(p.value)))

    def _poll_analog(self, now: float) -> None:
        for sensor in materialized_analog_sensors(self.config):
            port = SENSE_PORTS[sensor.sense_port]
            raw = self.analog_reader.read_voltage(port.ads1115_address, port.analog_channel)
            value = raw * sensor.scale + sensor.offset
            self.state.readings[sensor.id] = SensorReading(sensor.id, round(value, 3), sensor.unit, ts=now)
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
            catalog.append({"id": item.id, "name": item.name, "kind": "water", "main": True, "desired": item.desired_state.value})
        for item in materialized_temperature_sensors(self.config):
            catalog.append({"id": item.id, "name": item.name, "kind": "temperature", "main": item.driver != TemperatureDriver.HOST_CPU})
        for item in self.config.ph_sensors:
            catalog.append({"id": item.id, "name": item.name, "kind": "ph", "main": True})
        for item in materialized_analog_sensors(self.config):
            catalog.append({"id": item.id, "name": item.name, "kind": "analog", "main": True})
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
            "buzzer_alarm_enabled": self.config.buzzer.alarm_enabled,
            "mqtt_enabled": self.config.mqtt.enabled,
            "telegram_enabled": self.config.telegram.enabled,
            "home_assistant_notify_enabled": self.config.home_assistant_notify.enabled,
        }
