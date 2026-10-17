from __future__ import annotations

import copy
import datetime as dt
import logging
import math
import threading
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from .controllers import DosingScheduler
from .drivers import HardwareFault
from .hardware import require_stepper

if TYPE_CHECKING:
    from .config import AppConfig
    from .service import LoggerheadService

LOGGER = logging.getLogger(__name__)


class DosingManager:
    """One supervised dosing thread; durable claims precede every motor operation."""

    GRACE_SECONDS = 30.0

    def __init__(self, service: LoggerheadService) -> None:
        self.service = service
        self.active: dict[str, Any] | None = None
        self.cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._armed_after = {item.id: time.time() for item in service.config.dosing}
        self._handled: dict[str, float] = {}
        interrupted = service.store.recover_interrupted_doses()
        for row in interrupted:
            message = f"Scheduled dose {row['schedule_id']} has an unacknowledged {row['status']} run: {row['error']}"
            service.state.dosing_paused[row["schedule_id"]] = True
            service.state.dosing_faults[row["schedule_id"]] = message
            service._activate_alarm(f"dosing:{row['schedule_id']}", message, priority="high")
        self._history = service.store.recent_doses()
        if interrupted:
            service._save_state()

    def config_changed(self, previous: AppConfig) -> None:
        old = {item.id: asdict(item) for item in previous.dosing}
        with self.service._prime_lock:
            for item in self.service.config.dosing:
                if old.get(item.id) != asdict(item) or previous.steppers != self.service.config.steppers:
                    self._armed_after[item.id] = time.time()

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="loggerhead-dosing", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.cancel.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)
            if self._thread.is_alive():
                raise HardwareFault("Scheduled dosing worker did not stop.")

    def run(self) -> None:
        while not self.service._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                LOGGER.exception("Scheduled dosing supervisor failed: %s", exc)
                try:
                    self.service._activate_alarm("dosing:scheduler", f"Dosing scheduler failed: {exc}", priority="high")
                except Exception:
                    LOGGER.exception("Dosing fault reporting failed; the supervisor will continue without starting an unclaimed dose.")
            self.service._stop.wait(0.5)

    def tick(self, now_ts: float | None = None, *, _live_batch: bool = False) -> None:
        service = self.service
        use_wall_clock = now_ts is None
        # Configuration updates serialize new starts, while Stop never takes this lock.
        with service._config_write_lock, service._prime_lock:
            now_ts = time.time() if now_ts is None else now_ts
            now = dt.datetime.fromtimestamp(now_ts)
            if service._stop.is_set() or service._motion_owner or self.active or any(t.is_alive() for t in service._prime_threads.values()):
                return
            selected = None
            for schedule in service.config.dosing:
                if not schedule.enabled or service.state.dosing_paused.get(schedule.id) or service.state.dosing_faults.get(schedule.id):
                    continue
                slot = DosingScheduler.due_slot(schedule, now)
                if slot is None:
                    continue
                planned, day = slot
                stamp = planned.timestamp()
                if stamp < self._armed_after.get(schedule.id, now_ts) or now_ts < stamp:
                    continue
                if self._handled.get(schedule.id, float("-inf")) >= stamp:
                    continue
                if now_ts - stamp > self.GRACE_SECONDS:
                    self._handled[schedule.id] = stamp
                    service.store.log_event("dosing", f"{schedule.name}: missed slot skipped without catch-up.",
                                            {"schedule_id": schedule.id, "slot_ts": stamp})
                    continue
                if _live_batch and time.time() >= DosingScheduler.window(schedule, dt.date.fromisoformat(day))[1].timestamp():
                    continue
                pump = copy.deepcopy(service._stepper_profile(schedule.actuator))
                if service.stepper_engine._fault or pump.assignment in service.ato.blocked_pumps:
                    continue
                if any(p.assigned_actuator == pump.id and service.state.ato.get(p.id) and service.state.ato[p.id].faulted for p in service.config.ato):
                    continue
                volume = schedule.daily_volume_ml / schedule.doses_per_day
                steps = math.floor(volume * pump.steps_per_ml)
                run_id = service.store.claim_dose(
                    schedule_id=schedule.id, slot_ts=stamp, window_date=day, pump_id=pump.id,
                    requested_ml=volume, requested_steps=steps, daily_limit=schedule.daily_volume_ml,
                )
                self._handled[schedule.id] = stamp
                if run_id is None:
                    continue
                if use_wall_clock and not 0 <= time.time() - stamp <= self.GRACE_SECONDS:
                    service.store.finish_dose(run_id, "skipped", error="Slot expired while its durable claim was being committed.")
                    self._history = service.store.recent_doses()
                    continue
                if _live_batch and time.time() >= DosingScheduler.window(schedule, dt.date.fromisoformat(day))[1].timestamp():
                    service.store.finish_dose(run_id, "skipped", error="Dosing window ended while waiting for another pump.")
                    self._history = service.store.recent_doses()
                    continue
                schedule = copy.deepcopy(schedule)
                service._claim_motion(f"dosing:{schedule.id}", pump.id)
                self.cancel.clear()
                self.active = {"schedule_id": schedule.id, "pump_id": pump.id, "requested_ml": volume,
                               "requested_steps": steps, "started_at": time.time()}
                with service._state_lock:
                    service.state.stepper_active = pump.id
                selected = schedule, pump, steps, run_id
                break
        if selected is None:
            return
        schedule, pump, steps, run_id = selected
        status, error, confirmed, possible = "failed", "", 0, steps
        try:
            if self.cancel.is_set() or service._stop.is_set():
                status = "interrupted" if service._stop.is_set() else "stopped"
                possible, error = 0, "Dose cancelled before motion."
            else:
                result = service.stepper_engine.run_continuous(
                    require_stepper(pump.assignment), stop_event=self.cancel,
                    steps_per_second=pump.dose_speed_steps_per_second,
                    run_current_ma=pump.run_current_ma, hold_current_ma=pump.hold_current_ma,
                    microsteps=pump.microsteps, stallguard_threshold=pump.stallguard_threshold,
                    direction_high=pump.direction_high, max_seconds=schedule.max_run_seconds, max_steps=steps,
                )
                confirmed, possible = result.steps_sent, result.possible_steps
                if result.reason == "max_steps" and confirmed == steps and not result.interrupted_batch:
                    status = "completed"
                else:
                    status = "stopped" if self.cancel.is_set() and not service._stop.is_set() else "interrupted"
                    error = f"Dose ended with {result.reason}; delivered volume is uncertain."
        except Exception as exc:
            error = str(exc)
            LOGGER.exception("Scheduled dose %s failed.", schedule.id)
        finally:
            try:
                service.stepper_engine.stop(require_stepper(pump.assignment))
            except Exception as exc:
                status, error = "failed", f"Motor disable could not be verified: {exc}"
                try:
                    service._activate_alarm(f"stepper:{pump.id}:cleanup", error, priority="critical")
                except Exception:
                    LOGGER.exception("Could not report critical dosing cleanup failure: %s", error)
            try:
                service.store.finish_dose(run_id, status, confirmed_steps=confirmed, possible_steps=possible, error=error)
                history = service.store.recent_doses()
            except Exception as exc:
                history = self._history
                status, error = "failed", f"Dose result could not be persisted: {exc}"
            with service._prime_lock:
                service._release_motion(f"dosing:{schedule.id}")
                with service._state_lock:
                    service.state.stepper_active = None
                    if status != "completed":
                        service.state.dosing_paused[schedule.id] = True
                        if status != "stopped":
                            service.state.dosing_faults[schedule.id] = error
                self._history = history
                self.active = None
            service._save_state()
            service.store.log_event("dosing", f"{schedule.name}: {status}.", {"run_id": run_id, "error": error})
            if status in {"failed", "interrupted"}:
                service._activate_alarm(f"dosing:{schedule.id}", f"{schedule.name}: {error}", priority="high")
        # Drain same-tick collisions serially using the original observation time.
        # Later polling still skips genuinely missed slots rather than catching up.
        self.tick(now_ts, _live_batch=use_wall_clock or _live_batch)

    def pause(self, schedule_id: str, paused: bool) -> None:
        service = self.service
        if type(paused) is not bool:
            raise ValueError("Dosing paused must be true or false.")
        with service._prime_lock:
            if not any(item.id == schedule_id for item in service.config.dosing):
                raise KeyError(schedule_id)
            if not paused and service.state.dosing_faults.get(schedule_id):
                raise RuntimeError("Reset the dosing fault before resuming this schedule.")
            with service._state_lock:
                previous = service.state.dosing_paused.get(schedule_id, False)
                service.state.dosing_paused[schedule_id] = paused
            if paused and self.active and self.active["schedule_id"] == schedule_id:
                self.cancel.set()
                service.stepper_engine.request_stop()
            try:
                service._save_state()
            except Exception:
                if not paused:
                    with service._state_lock:
                        service.state.dosing_paused[schedule_id] = previous
                raise
            if not paused:
                self._armed_after[schedule_id] = time.time()
        service.store.log_event("dosing", f"Schedule {schedule_id} {'paused' if paused else 'resumed'}.")

    def reset(self, schedule_id: str) -> None:
        service = self.service
        with service._prime_lock:
            if self.active:
                raise RuntimeError("Stop the active dose before resetting a fault.")
            if getattr(service.stepper_engine, "_fault", None):
                raise RuntimeError("Resolve the latched motor-control fault before resetting this schedule.")
            if not any(item.id == schedule_id for item in service.config.dosing):
                raise KeyError(schedule_id)
            # Persist a pause before clearing the durable journal lockout. A
            # crash between these commits must never implicitly resume dosing.
            with service._state_lock:
                service.state.dosing_paused[schedule_id] = True
            service._save_state()
            service.store.acknowledge_dosing_fault(schedule_id)
            with service._state_lock:
                service.state.dosing_faults.pop(schedule_id, None)
                service.state.dosing_paused[schedule_id] = True
                alarm = service.state.alarms.get(f"dosing:{schedule_id}")
                if alarm:
                    alarm.active = False
            service._save_state()
        service.store.log_event("dosing", f"Schedule {schedule_id} fault acknowledged; schedule remains paused.")

    @staticmethod
    def preview(config: AppConfig, now_ts: float | None = None) -> dict[str, Any]:
        now_ts = time.time() if now_ts is None else now_ts
        rows = []
        for schedule in config.dosing:
            pump = next((p for p in config.steppers if p.id == schedule.actuator), None)
            volume = schedule.daily_volume_ml / schedule.doses_per_day if schedule.doses_per_day else 0
            steps = math.floor(volume * pump.steps_per_ml) if pump else 0
            slots = []
            probe = dt.datetime.fromtimestamp(now_ts)
            for _ in range(8):
                slot = DosingScheduler.next_slot(schedule, probe)
                if slot is None:
                    break
                slots.append(slot.timestamp())
                probe = slot + dt.timedelta(microseconds=1)
            rows.append({"id": schedule.id, "next_ts": slots[0] if slots else None, "slots": slots,
                         "volume_per_dose": volume, "steps_per_dose": steps,
                         "estimated_seconds": steps / pump.dose_speed_steps_per_second if pump else None})
        return {"schedules": rows, "timezone": time.tzname[bool(time.localtime(now_ts).tm_isdst)]}

    def snapshot(self) -> dict[str, Any]:
        with self.service._prime_lock:
            result = self.preview(self.service.config)
            for row in result["schedules"]:
                row["paused"] = self.service.state.dosing_paused.get(row["id"], False)
                row["fault"] = self.service.state.dosing_faults.get(row["id"], "")
            result["active"] = copy.deepcopy(self.active)
            result["recent_runs"] = copy.deepcopy(self._history)
        return result
