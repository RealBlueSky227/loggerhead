"""Explicit, bounded real-hardware priming benchmark through the running controller."""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PINS = {"dose1": 16, "dose2": 8, "dose3": 24, "dose4": 18}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Authorize physical motor operation")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--pumps", nargs="+", choices=list(PINS), default=list(PINS))
    parser.add_argument("--speeds", nargs="+", type=int, default=[400, 1000])
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--stop-after", type=float, help="Explicit Stop after this many seconds")
    parser.add_argument("--check-interlock", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required: this benchmark operates physical pumps")
    if not 0.5 <= args.seconds <= 10 or not 1 <= args.cycles <= 3:
        parser.error("Duration must be 0.5-10 seconds and cycles must be 1-3")
    if any(not 1 <= speed <= 2000 for speed in args.speeds):
        parser.error("Benchmark speed must be 1-2000 step/s")
    if args.stop_after is not None and not 0 < args.stop_after < args.seconds:
        parser.error("--stop-after must be positive and below --seconds")

    import pigpio
    from smbus2 import SMBus

    def request(path, data=None):
        req = urllib.request.Request(
            args.url + path, data=None if data is None else json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.load(response)

    def stop(pump):
        return request("/api/prime", {"id": pump, "on": False})

    pi = pigpio.pi()
    if not pi.connected:
        raise RuntimeError("pigpiod is unavailable")
    bus = SMBus(1)
    callbacks = []
    results = []
    edges_lock = threading.Lock()
    edges = {pin: [] for pin in PINS.values()}

    def capture(gpio, level, tick):
        if level in (0, 1):
            with edges_lock:
                edges[gpio].append((level, tick))

    def enabled():
        raw = bus.read_byte_data(0x20, 0x12)
        return [pump for index, pump in enumerate(PINS) if not raw & (1 << (4 + index))]

    def cpu_usec():
        path = Path("/sys/fs/cgroup/system.slice/loggerhead.service/cpu.stat")
        if not path.exists():
            return 0
        return int(dict(line.split() for line in path.read_text().splitlines())["usage_usec"])

    try:
        initial = request("/api/status")
        if initial["simulation"] or any(initial["manual_priming"].values()) or enabled():
            raise RuntimeError("Expected real hardware with every pump idle and disabled")
        saved_profiles = initial["steppers"]
        for pin in PINS.values():
            callbacks.append(pi.callback(pin, pigpio.EITHER_EDGE, capture))
        for cycle in range(args.cycles):
            for speed in args.speeds:
                for pump in args.pumps:
                    with edges_lock:
                        for values in edges.values():
                            values.clear()
                    if enabled():
                        raise RuntimeError("A driver remained enabled before the next trial")
                    stop_data = {}
                    cancelled = threading.Event()

                    def timed_stop(pump=pump, event=cancelled, data=stop_data):
                        delay = args.stop_after if args.stop_after is not None else args.seconds + 3
                        if not event.wait(delay):
                            data["tick"] = pi.get_current_tick()
                            data["time"] = time.monotonic()
                            try:
                                data["response"] = stop(pump)
                            except Exception as exc:
                                data["error"] = str(exc)

                    timer = threading.Thread(target=timed_stop, daemon=True)
                    began = time.monotonic()
                    cpu_before = cpu_usec()
                    timer.start()
                    try:
                        response = request("/api/prime", {
                            "id": pump, "on": True, "speed_steps_per_second": speed, "max_seconds": args.seconds,
                        })
                        print("START", pump, speed, response, flush=True)
                        states = set()
                        status_times = []
                        next_status = began
                        seen_enabled = False
                        disabled_at = None
                        interlock_checked = False
                        while time.monotonic() - began < args.seconds + 5:
                            active = enabled()
                            states.add(tuple(active))
                            if any(item != pump for item in active) or len(active) > 1:
                                raise RuntimeError(f"ENN exclusivity failure: {active}")
                            seen_enabled |= bool(active)
                            if seen_enabled and not active:
                                disabled_at = time.monotonic()
                                break
                            if args.check_interlock and active and not interlock_checked:
                                other = next(item for item in PINS if item != pump)
                                try:
                                    request("/api/prime", {"id": other, "on": True, "max_seconds": 1})
                                except urllib.error.HTTPError as exc:
                                    if exc.code != 409:
                                        raise
                                    interlock_checked = True
                                else:
                                    raise RuntimeError("Concurrent priming command was accepted")
                            if time.monotonic() >= next_status:
                                t = time.monotonic()
                                status = request("/api/status")
                                status_times.append((time.monotonic() - t) * 1000)
                                faults = [a["message"] for k, a in status["alarms"].items() if a["active"] and k.startswith("stepper:")]
                                if faults:
                                    raise RuntimeError(str(faults))
                                next_status = time.monotonic() + 0.5
                            time.sleep(0.01)
                        if disabled_at is None:
                            raise RuntimeError("No verified enabled-to-disabled transition before deadline")
                        cancelled.set()
                        timer.join(4)
                        if timer.is_alive() or "error" in stop_data:
                            raise RuntimeError(f"Stop request did not finish: {stop_data}")
                        time.sleep(0.25)
                        with edges_lock:
                            captured = {pin: list(values) for pin, values in edges.items()}
                        rises = [tick for level, tick in captured[PINS[pump]] if level == 1]
                        periods = [pigpio.tickDiff(a, b) for a, b in zip(rises, rises[1:], strict=False)]
                        widths = [pigpio.tickDiff(tick, nxt) for (level, tick), (other, nxt) in zip(captured[PINS[pump]], captured[PINS[pump]][1:], strict=False) if level == 1 and other == 0]
                        if not periods or any(level == 1 for pin, values in captured.items() if pin != PINS[pump] for level, _ in values):
                            raise RuntimeError("Missing selected STEP output or unexpected pulses on another motor")
                        levels = {pin: pi.read(pin) for pin in PINS.values()}
                        if any(levels.values()) or enabled():
                            raise RuntimeError("A motor did not reach disabled/STEP LOW state")
                        elapsed = time.monotonic() - began
                        last_edge_after_stop = None
                        if "tick" in stop_data:
                            last_edge_after_stop = pigpio.tickDiff(stop_data["tick"], rises[-1])
                            if last_edge_after_stop > 2**31:
                                last_edge_after_stop -= 2**32
                        result = {
                            "pump": pump, "speed": speed, "cycle": cycle + 1, "pulse_count": len(rises),
                            "observed_speed": 1e6 / statistics.median(periods),
                            "period_us": {"min": min(periods), "median": statistics.median(periods), "max": max(periods)},
                            "high_width_us": {"min": min(widths), "median": statistics.median(widths), "max": max(widths)} if widths else {},
                            "long_gaps": sum(value > 1.5e6 / speed for value in periods),
                            "long_gap_indices": [index for index, value in enumerate(periods) if value > 1.5e6 / speed][:20],
                            "status_median_ms": statistics.median(status_times), "status_max_ms": max(status_times),
                            "controller_cpu_percent": 100 * (cpu_usec() - cpu_before) / 1e6 / elapsed,
                            "enn_states": sorted(states), "final_step_levels": levels,
                            "interlock_rejected": interlock_checked,
                            "stop_to_disable_ms": (disabled_at - stop_data["time"]) * 1000 if "time" in stop_data else None,
                            "last_edge_after_stop_us": last_edge_after_stop,
                        }
                        results.append(result)
                        print("RESULT", json.dumps(result), flush=True)
                    finally:
                        try:
                            stop(pump)
                        finally:
                            cancelled.set()
                            timer.join(4)
                    time.sleep(0.5)
        final = request("/api/status")
        if final["steppers"] != saved_profiles:
            raise RuntimeError("Saved motor configuration changed during testing")
    finally:
        # Only stop commands on cleanup; never write GPIOs, UART, PWM, or waveform state here.
        for pump in PINS:
            try:
                stop(pump)
            except Exception as exc:
                print("STOP_ERROR", pump, str(exc), flush=True)
        for callback in callbacks:
            callback.cancel()
        bus.close()
        pi.stop()
        if args.output:
            args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
