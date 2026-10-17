from __future__ import annotations

import copy
import logging
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from .config import ATOProfile, ato_levels, materialized_water_level_sensors
from .controllers import ATOController
from .drivers import HardwareFault
from .hardware import AlarmPriority, LevelState, require_stepper
from .state import AlarmState, ATOState, RuntimeState

if TYPE_CHECKING:
    from .service import LoggerheadService

LOGGER = logging.getLogger(__name__)


class ATOJournal:
    """Reserve worst-case delivery before energizing; never forget uncertainty."""

    def __init__(self, store) -> None:
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ato_runs (
                id INTEGER PRIMARY KEY, profile_id TEXT NOT NULL, actuator TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                reserved_ml REAL NOT NULL, charged_ml REAL NOT NULL,
                confirmed_steps INTEGER NOT NULL DEFAULT 0, possible_steps INTEGER NOT NULL DEFAULT 0,
                steps_per_ml REAL NOT NULL, duration REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '', acknowledged INTEGER NOT NULL DEFAULT 0)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ato_profile_time ON ato_runs(profile_id, started_at)")

    def recover(self) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            db.execute("""UPDATE ato_runs SET status='fault', finished_at=?,
                reason='Controller restarted before refill stop/accounting was verified.' WHERE status='running'""", (time.time(),))
            return [dict(row) for row in db.execute("SELECT * FROM ato_runs WHERE status='fault' AND acknowledged=0")]

    def claim(self, profile: ATOProfile, steps_per_ml: float, capacity: float, *, actuator_key: str | None = None) -> tuple[int, float]:
        now = time.time()
        actuator_key = actuator_key or profile.assigned_actuator
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("""SELECT COALESCE(SUM(charged_ml),0) FROM ato_runs
                WHERE actuator=? AND COALESCE(finished_at,started_at+3600)>=?""",
                              (actuator_key, now-86400)).fetchone()[0]
            if db.execute("SELECT 1 FROM ato_runs WHERE actuator=? AND (status='running' OR (status='fault' AND acknowledged=0))",
                          (actuator_key,)).fetchone():
                raise HardwareFault("An interrupted or faulted refill requires explicit reset.")
            if steps_per_ml:
                capacity = min(capacity, profile.max_daily_ml-used)
                capacity = max(0, math.floor(capacity * steps_per_ml) / steps_per_ml)
                if capacity <= 0:
                    raise HardwareFault("Maximum rolling 24-hour refill volume reached.")
            cursor = db.execute("""INSERT INTO ato_runs
                (profile_id,actuator,started_at,status,reserved_ml,charged_ml,steps_per_ml)
                VALUES (?,?,?,'running',?,?,?)""",
                                (profile.id, actuator_key, now, capacity, capacity, steps_per_ml))
            return cursor.lastrowid, capacity

    def finish(self, run_id: int, *, fault: bool, confirmed: int, possible: int, duration: float, reason: str) -> None:
        with self.store.connect() as db:
            # An exception with unknown progress retains the full reservation.
            db.execute("""UPDATE ato_runs SET status=?,finished_at=?,confirmed_steps=?,
                possible_steps=CASE WHEN ? < 0 THEN CAST(ROUND(reserved_ml * steps_per_ml) AS INTEGER) ELSE ? END,duration=?,reason=?,
                charged_ml=CASE WHEN steps_per_ml>0 AND ? >= 0 THEN MIN(reserved_ml, ? / steps_per_ml) ELSE charged_ml END
                WHERE id=?""", ("fault" if fault else "stopped", time.time(), confirmed, possible, possible, duration, reason,
                                possible, possible, run_id))

    def acknowledge(self, profile_id: str) -> None:
        with self.store.connect() as db:
            db.execute("UPDATE ato_runs SET acknowledged=1 WHERE profile_id=? AND status='fault'", (profile_id,))

    def blocked_actuators(self) -> set[str]:
        with self.store.connect() as db:
            return {row[0] for row in db.execute("SELECT DISTINCT actuator FROM ato_runs WHERE status='fault' AND acknowledged=0")}

    def totals(self) -> dict[str, float]:
        with self.store.connect() as db:
            return dict(db.execute("""SELECT actuator,SUM(charged_ml) FROM ato_runs
                WHERE COALESCE(finished_at,started_at+3600)>=? GROUP BY actuator""", (time.time()-86400,)))

    def recent(self) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM ato_runs ORDER BY id DESC LIMIT 30")]


@dataclass
class Refill:
    profile: ATOProfile
    fingerprint: Any
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    started: float = 0.0
    reason: str = ""
    fault: bool = False
    run_id: int | None = None
    motion_started: bool = False
    confirmed: int = 0
    possible: int = 0
    capacity: float = 0.0
    pump: Any = None
    actuator_lock: threading.RLock = field(default_factory=threading.RLock)
    cancel_lock: threading.Lock = field(default_factory=threading.Lock)
    disabled: bool = False


class ATOManager:
    WATCH_SECONDS = 0.1
    CONTROL_TIMEOUT = 3.0
    PULSE_WINDOW_SECONDS = 0.25

    def __init__(self, service: LoggerheadService) -> None:
        self.service = service
        self.journal = ATOJournal(service.store)
        self.jobs: dict[str, Refill] = {}
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._control_seen = 0.0
        self._transitioning = False
        self.blocked_pumps: set[str] = set()
        for row in self.journal.recover():
            self._latch(row['profile_id'], row['reason'])
        for key, state in service.state.ato.items():
            if state.running:
                self._latch(key, "Controller restarted during an unfinished refill.")
            state.running = False
        self.history = self.journal.recent()
        self.blocked_pumps.update(self.journal.blocked_actuators())
        self._totals = self.journal.totals()
        for key, state in list(service.state.ato.items()):
            if state.faulted:
                service._activate_alarm(f'ato:{key}', f'ATO {key}: {state.fault_reason or "Persisted safety lockout."}', priority='high')

    def start(self) -> None:
        self._watch_thread = threading.Thread(target=self._watch, name="loggerhead-ato-watchdog", daemon=True)
        self._watch_thread.start()

    def _latch(self, profile_id: str, reason: str) -> None:
        profile = next((p for p in self.service.config.ato if p.id == profile_id), None)
        if profile and profile.actuator_device_type == 'stepper':
            self.blocked_pumps.add(self.service._stepper_profile(profile.assigned_actuator).assignment)
        with self.service._state_lock:
            state = self.service.state.ato.setdefault(profile_id, ATOState(profile_id))
            state.faulted = True
            state.fault_reason = reason
            alarm_id = f'ato:{profile_id}'
            existing = self.service.state.alarms.get(alarm_id)
            self.service.state.alarms[alarm_id] = AlarmState(
                alarm_id, f'ATO {profile.name if profile else profile_id}: {reason}',
                existing.priority if existing and existing.active else AlarmPriority.HIGH,
                first_seen=existing.first_seen if existing and existing.active else time.time(),
                last_notified=existing.last_notified if existing and existing.active else 0,
            )

    def _inputs(self, profile: ATOProfile, now: float) -> RuntimeState:
        service = self.service
        # The control loop publishes values/health; the watchdog checks actual
        # acquisition ages too, independently of a stalled control loop.
        with service._state_lock:
            state = RuntimeState(water_levels=dict(service.state.water_levels),
                                 ato={profile.id: copy.deepcopy(service.state.ato.get(profile.id, ATOState(profile.id)))})
            for key in (profile.primary_level_sensor, profile.backup_failsafe_sensor):
                health = service.state.sensor_health.get(key)
                if not health or health.data_status != "online" or health.last_success_ts <= 0 or not 0 <= now-health.last_success_ts <= min(health.stale_after_seconds, 2.0):
                    state.water_levels[key] = LevelState.UNKNOWN
        snapshots = service._sensor_worker_snapshots()
        for key in (profile.primary_level_sensor, profile.backup_failsafe_sensor):
            snapshot = snapshots.get(key)
            if snapshot and snapshot.data_status != "online":
                state.water_levels[key] = LevelState.UNKNOWN
        return state

    def fingerprint(self) -> Any:
        config = self.service.config
        return config.ato, config.steppers, config.equipment, config.sense_ports, config.water_level_sensors

    def tick(self, now: float | None = None) -> None:
        service = self.service
        self._control_seen = time.monotonic()
        now = time.time() if now is None else now
        if self._transitioning or service._stop.is_set():
            return
        # Never block the control loop on a configuration transition.
        if not service._config_write_lock.acquire(blocking=False):
            return
        try:
            for profile in service.config.ato:
                with service._prime_lock:
                    if profile.id in self.jobs:
                        continue
                observed = self._inputs(profile, now)
                desired, proposed, alarm = ATOController.evaluate(profile, observed, now)
                if alarm:
                    self._latch(profile.id, proposed.fault_reason)
                    service._register_alarm(alarm)
                    continue
                if not desired:
                    with service._state_lock:
                        state = service.state.ato.setdefault(profile.id, ATOState(profile.id))
                        state.waiting_reason = 'Waiting for fresh, valid water sensors.' if any(
                            observed.water_levels.get(key, LevelState.UNKNOWN) in {LevelState.UNKNOWN, LevelState.INACTIVE}
                            for key in (profile.primary_level_sensor, profile.backup_failsafe_sensor)
                        ) else ''
                    continue
                with service._prime_lock:
                    if profile.id in self.jobs or self._transitioning or service._stop.is_set():
                        continue
                    if profile.actuator_device_type == 'stepper':
                        try:
                            service._claim_motion(f"ato:{profile.id}", profile.assigned_actuator)
                        except RuntimeError as exc:
                            if service.stepper_engine._fault:
                                self._latch(profile.id, f'Motor-control safety fault: {service.stepper_engine._fault}')
                            with service._state_lock:
                                service.state.ato.setdefault(profile.id, ATOState(profile.id)).waiting_reason = str(exc)
                            continue  # No queue: next tick must revalidate the live sensors.
                    job = Refill(copy.deepcopy(profile), copy.deepcopy(self.fingerprint()))
                    if profile.actuator_device_type == 'stepper':
                        job.pump = copy.deepcopy(service._stepper_profile(profile.assigned_actuator))
                    self.jobs[profile.id] = job
                    job.thread = threading.Thread(target=self._refill, args=(job,), name=f"ato-{profile.id}", daemon=True)
                    try:
                        job.thread.start()
                    except Exception:
                        self.jobs.pop(profile.id, None)
                        if job.pump:
                            service._release_motion(f'ato:{profile.id}')
                        self._latch(profile.id, 'Refill worker could not start.')
                        raise
        finally:
            service._config_write_lock.release()

    def _stop_condition(self, job: Refill) -> tuple[str, bool] | None:
        service = self.service
        if service._stop.is_set():
            return "Controller shutdown interrupted refill.", True
        if self._transitioning or job.fingerprint != self.fingerprint():
            return "Configuration changed during refill; reset required.", True
        if time.monotonic()-self._control_seen > self.CONTROL_TIMEOUT:
            return "Control loop heartbeat expired.", True
        if job.started and time.monotonic()-job.started >= job.profile.max_run_time:
            return "Maximum refill runtime reached.", True
        state = self._inputs(job.profile, time.time())
        state.ato[job.profile.id].running = True
        # Runtime is independently measured with monotonic time, not wall time.
        state.ato[job.profile.id].started_at = time.time()
        desired, proposed, _ = ATOController.evaluate(job.profile, state)
        if proposed.faulted:
            return proposed.fault_reason or "ATO safety lockout.", True
        if not desired:
            return "Stop threshold reached.", False
        return None

    def cancel(self, job: Refill, reason: str, fault: bool) -> None:
        # Set the cancellation token before any potentially blocking I/O.
        if self.jobs.get(job.profile.id) is not job:
            return
        job.stop.set()
        with job.cancel_lock:
            if not job.reason or (fault and not job.fault):
                job.reason = reason
            job.fault = job.fault or fault
        if fault:
            self._latch(job.profile.id, reason)
        # Independent of the refill thread, storage, and event delivery. The
        # lock serializes relay ON against a concurrent watchdog OFF.
        with job.actuator_lock:
            if job.motion_started and not job.disabled:
                try:
                    if job.pump:
                        self.service.relay_board.set_stepper_enabled(require_stepper(job.pump.assignment), False)
                    else:
                        self.service._write_equipment_output(job.profile.assigned_actuator, False, source=f'ato:{job.profile.id}')
                    job.disabled = True
                except Exception as exc:
                    job.fault = True
                    job.reason = f'Actuator disable could not be verified: {exc}'
                    self._latch(job.profile.id, job.reason)
                    if job.pump:
                        self.service.stepper_engine._fault = HardwareFault(job.reason)
                    LOGGER.critical('ATO %s: %s', job.profile.id, job.reason)
        if job.pump:
            self.service.stepper_engine.request_stop()

    def watch_once(self) -> None:
        jobs = list(self.jobs.copy().values())
        for job in jobs:
            condition = self._stop_condition(job)
            if condition:
                self.cancel(job, *condition)
            with self.service._state_lock:
                state = self.service.state.ato.setdefault(job.profile.id, ATOState(job.profile.id))
                if job.started:
                    state.elapsed_seconds = time.monotonic()-job.started

    def _watch(self) -> None:
        while not self._watch_stop.wait(self.WATCH_SECONDS):
            try:
                self.watch_once()
            except Exception:
                LOGGER.exception("ATO watchdog failed; cancelling every active refill.")
                jobs = list(self.jobs.copy().values())
                for job in jobs:
                    self.cancel(job, "ATO watchdog evaluation failed.", True)

    def _refill(self, job: Refill) -> None:
        service, profile = self.service, job.profile
        unknown = False
        cleanup_error = ""
        try:
            pump = job.pump
            calibration = pump.steps_per_ml if pump else 0.0
            capacity = min(profile.max_refill_ml, profile.max_run_time*pump.dose_speed_steps_per_second/calibration) if pump else 0.0
            job.run_id, job.capacity = self.journal.claim(profile, calibration, capacity,
                                                       actuator_key=pump.assignment if pump else profile.assigned_actuator)
            condition = self._stop_condition(job)
            if condition:
                self.cancel(job, *condition)
            if job.stop.is_set():
                return
            job.started = time.monotonic()
            with service._state_lock:
                state = service.state.ato.setdefault(profile.id, ATOState(profile.id))
                state.running, state.started_at, state.run_id = True, time.time(), job.run_id
                state.waiting_reason = state.stop_reason = ""
                state.confirmed_steps = state.possible_steps = 0
                state.dispensed_ml = state.elapsed_seconds = 0.0
            service._save_state()
            # fsync may have stalled: never energize without revalidating.
            condition = self._stop_condition(job)
            if condition:
                self.cancel(job, *condition)
            if job.stop.is_set():
                return
            service.store.log_event('ato', f'{profile.name}: refill started.', {'run_id': job.run_id})
            if pump:
                steps = math.floor(job.capacity*calibration + 1e-8)

                def progress(confirmed: int, possible: int) -> None:
                    job.confirmed, job.possible = confirmed, possible
                    with service._state_lock:
                        state.confirmed_steps, state.possible_steps = confirmed, possible
                        state.dispensed_ml = confirmed/calibration

                job.motion_started = True
                unknown = True
                result = service.stepper_engine.run_continuous(
                    require_stepper(pump.assignment), stop_event=job.stop,
                    steps_per_second=pump.dose_speed_steps_per_second, run_current_ma=pump.run_current_ma,
                    hold_current_ma=pump.hold_current_ma, microsteps=pump.microsteps,
                    stallguard_threshold=pump.stallguard_threshold, direction_high=pump.direction_high,
                    max_seconds=max(0.001, profile.max_run_time-(time.monotonic()-job.started)), max_steps=steps,
                    pulse_window_seconds=self.PULSE_WINDOW_SECONDS, progress=progress,
                )
                unknown = False
                progress(result.steps_sent, max(result.steps_sent, result.possible_steps))
                if result.reason == 'max_steps':
                    self.cancel(job, 'Refill or rolling 24-hour volume limit reached.', True)
                elif result.reason == 'max_seconds':
                    self.cancel(job, 'Maximum refill runtime reached.', True)
                elif not job.stop.is_set() or result.reason not in {'stopped', 'shutdown'}:
                    self.cancel(job, f'Unexpected motion stop: {result.reason}.', True)
                # A deliberately cancelled partial DMA window is not verified
                # delivery, but its full possible volume still consumes budget.
            else:
                with job.actuator_lock:
                    if job.stop.is_set():
                        return
                    job.motion_started = True
                    service._write_equipment_output(profile.assigned_actuator, True, source=f'ato:{profile.id}')
                while not job.stop.wait(self.WATCH_SECONDS):
                    condition = self._stop_condition(job)
                    if condition:
                        self.cancel(job, *condition)
        except Exception as exc:
            LOGGER.exception('ATO %s refill failed.', profile.id)
            self.cancel(job, f'Refill failed: {exc}', True)
        finally:
            try:
                if job.pump and job.motion_started:
                    service.stepper_engine.stop(require_stepper(job.pump.assignment))
                elif not job.pump:
                    service._write_equipment_output(profile.assigned_actuator, False, source=f'ato:{profile.id}')
            except Exception as exc:
                cleanup_error = f'Actuator disable could not be verified: {exc}'
                if job.pump:
                    service.stepper_engine._fault = HardwareFault(cleanup_error)
                self.cancel(job, cleanup_error, True)
                LOGGER.critical('ATO %s: %s', profile.id, cleanup_error)
            duration = time.monotonic()-job.started if job.started else 0.0
            try:
                if job.run_id is not None:
                    self.journal.finish(job.run_id, fault=job.fault, confirmed=job.confirmed,
                                        possible=-1 if unknown else job.possible, duration=duration, reason=job.reason)
                self.history = self.journal.recent()
                self._totals = self.journal.totals()
            except Exception as exc:
                self.cancel(job, f'Refill accounting could not be committed: {exc}', True)
            with service._prime_lock:
                with service._state_lock:
                    state = service.state.ato.setdefault(profile.id, ATOState(profile.id))
                    state.running = False
                    state.elapsed_seconds = duration
                    state.stop_reason = job.reason
                    if job.fault:
                        state.faulted, state.fault_reason = True, cleanup_error or job.reason
                    state.daily_volume_ml = self._totals.get(job.pump.assignment if job.pump else profile.assigned_actuator, 0)
                if job.pump:
                    service._release_motion(f'ato:{profile.id}')
                self.jobs.pop(profile.id, None)
            try:
                service._save_state()
                service.store.log_event('ato', f'{profile.name}: {job.reason or "stopped"}',
                                        {'run_id': job.run_id, 'duration': duration, 'confirmed_steps': job.confirmed,
                                         'possible_steps': None if unknown else job.possible,
                                         'estimated_ml': job.confirmed/job.pump.steps_per_ml if job.pump else None})
                if job.fault:
                    service._activate_alarm(f'ato:{profile.id}', f'{profile.name}: {cleanup_error or job.reason}',
                                            priority='critical' if cleanup_error else 'high')
            except Exception:
                LOGGER.exception('Could not persist/report ATO %s final state; journal remains authoritative.', profile.id)

    def reset(self, profile_id: str) -> None:
        service = self.service
        with service._config_write_lock, service._prime_lock:
            profile = next((p for p in service.config.ato if p.id == profile_id), None)
            if profile is None:
                raise KeyError(profile_id)
            if profile_id in self.jobs:
                raise RuntimeError('Wait for the refill actuator to stop before resetting.')
            if profile.actuator_device_type == 'stepper' and service.stepper_engine._fault:
                raise RuntimeError('Resolve the latched motor fault before resetting ATO.')
            if profile.actuator_device_type != 'stepper':
                service._write_equipment_output(profile.assigned_actuator, False, source=f'ato:{profile.id}')
            observed = self._inputs(profile, time.time())
            if any(observed.water_levels.get(key, LevelState.UNKNOWN) in {LevelState.UNKNOWN, LevelState.INACTIVE}
                   for key in (profile.primary_level_sensor, profile.backup_failsafe_sensor)):
                raise RuntimeError('Both water sensors must have fresh, valid readings before reset.')
            if observed.water_levels[profile.backup_failsafe_sensor] not in {LevelState.DRY, LevelState.LOW}:
                raise RuntimeError('Backup high-water failsafe is still active.')
            # Reset is an acknowledgement, not a new refill command: require
            # restored water level. This prevents repeatedly resetting a dry
            # reservoir from bypassing runtime/per-refill protections.
            sensor = next(s for s in materialized_water_level_sensors(service.config) if s.id == profile.primary_level_sensor)
            levels = ato_levels(sensor)
            stop = profile.stop_level or levels[1]
            value = observed.water_levels[profile.primary_level_sensor]
            if value == LevelState.WET:
                value = LevelState.SUBMERGED
            if value not in levels or levels.index(value) < levels.index(stop):
                raise RuntimeError('Restore primary water level to the stop threshold before reset.')
            self.journal.acknowledge(profile_id)
            self.blocked_pumps = self.journal.blocked_actuators()
            with service._state_lock:
                state = service.state.ato.setdefault(profile_id, ATOState(profile_id))
                state.faulted, state.fault_reason = False, ''
                alarm = service.state.alarms.get(f'ato:{profile_id}')
                if alarm:
                    alarm.active = False
            service._save_state()
        service.store.log_event('ato', f'{profile.name}: fault acknowledged; waiting for a new low-water condition.')

    def quiesce(self, reason: str) -> None:
        self._transitioning = True
        jobs = list(self.jobs.copy().values())
        for job in jobs:
            self.cancel(job, reason, True)
        for job in jobs:
            if job.thread and job.thread is not threading.current_thread():
                job.thread.join(3)
                if job.thread.is_alive():
                    raise HardwareFault('ATO worker did not stop; configuration transition blocked.')
        for profile in self.service.config.ato:
            if profile.actuator_device_type != 'stepper':
                try:
                    self.service._write_equipment_output(profile.assigned_actuator, False, source=f'ato:{profile.id}')
                except Exception as exc:
                    self._latch(profile.id, f'Actuator disable could not be verified: {exc}')
                    self.service._activate_alarm(f'ato:{profile.id}', f'ATO {profile.name}: actuator disable failed: {exc}', priority='critical')
                    raise HardwareFault(f'ATO {profile.id} failed safe shutdown; transition blocked.') from exc

    def config_changed(self) -> None:
        self._transitioning = False

    def stop(self) -> None:
        try:
            self.quiesce('Controller shutdown interrupted refill.')
        finally:
            self._watch_stop.set()
            if self._watch_thread and self._watch_thread is not threading.current_thread():
                self._watch_thread.join(2)

    def snapshot(self) -> dict[str, Any]:
        service = self.service
        with service._state_lock:
            states = {p.id: asdict(service.state.ato.get(p.id, ATOState(p.id))) for p in service.config.ato}
            for profile in service.config.ato:
                row = states[profile.id]
                row.update(status='FAULT' if row['faulted'] else 'DISABLED' if not profile.enabled else 'REFILLING' if row['running'] else 'IDLE',
                           primary=service.state.water_levels.get(profile.primary_level_sensor, LevelState.UNKNOWN).value,
                           backup=service.state.water_levels.get(profile.backup_failsafe_sensor, LevelState.UNKNOWN).value)
        for profile in list(service.config.ato):
            observed = self._inputs(profile, time.time())
            if profile.id in states:
                states[profile.id]['primary'] = observed.water_levels.get(profile.primary_level_sensor, LevelState.UNKNOWN).value
                states[profile.id]['backup'] = observed.water_levels.get(profile.backup_failsafe_sensor, LevelState.UNKNOWN).value
        return {'profiles': states, 'recent_runs': copy.deepcopy(self.history)}
