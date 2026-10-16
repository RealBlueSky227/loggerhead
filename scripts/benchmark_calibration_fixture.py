"""Isolated simulated controller for calibration responsiveness tests. No hardware."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from benchmark_plots import seed

from loggerhead.drivers import StepperRunResult
from loggerhead.performance import PerformanceMonitor
from loggerhead.service import LoggerheadService
from loggerhead.web import DashboardHandler, DashboardServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--lifetime', type=int, default=300)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='loggerhead-calibration-benchmark-') as directory:
        root = Path(directory)
        service = LoggerheadService(root / 'config.json', root / 'data', simulation=True)
        if not hasattr(service, 'performance'):
            service.performance = PerformanceMonitor()
        assert service.simulation and service.relay_board.simulation and service.uart.simulation
        seed(service, 600)
        injection = {'delay': 0.0, 'entered': False}
        original_save = service._save_state

        def delayed_save():
            if injection['delay'] and any(service.state.manual_priming.values()) and 'process_request_thread' in threading.current_thread().name:
                seconds = injection['delay']
                injection.update(delay=0, entered=True)
                with service.performance.measure('injected.persistence_delay'):
                    time.sleep(seconds)
            original_save()

        def mock_motor(_assignment, **kwargs):
            stopped = kwargs['stop_event'].wait(2)
            return StepperRunResult(0 if stopped else kwargs['max_steps'], 'stopped' if stopped else 'max_steps',
                                    possible_steps=kwargs['max_steps'] if stopped else kwargs['max_steps'], interrupted_batch=stopped)

        service._save_state = delayed_save
        service.stepper_engine.run_continuous = mock_motor

        class Handler(DashboardHandler):
            def do_GET(self):
                if self.path == '/api/benchmark/info':
                    self._send_json({'simulation': True, 'isolated': True, 'injection': injection})
                elif self.path.startswith('/api/performance'):
                    self._send_json(service.performance.snapshot(stacks=True))
                else:
                    super().do_GET()

            def do_POST(self):
                if self.path == '/api/benchmark/delay':
                    seconds = self._read_json().get('seconds', 0)
                    if not isinstance(seconds, int | float) or not 0 <= seconds <= 25:
                        self._send_json({'ok': False}, status=400)
                        return
                    injection.update(delay=seconds, entered=False)
                    self._send_json({'ok': True})
                else:
                    super().do_POST()

        server = DashboardServer(service, host=args.host, port=args.port)
        server.RequestHandlerClass = Handler
        service._start_sensor_workers()
        service.dosing.start()
        threads = [threading.Thread(target=service._poll_loop, name='loggerhead-poll', daemon=True),
                   threading.Thread(target=service._diagnostics_loop, name='loggerhead-diagnostics', daemon=True)]
        for thread in threads:
            thread.start()

        def shutdown_on_input():
            for line in sys.stdin:
                if line.strip() == 'shutdown':
                    server.shutdown()
                    return

        threading.Thread(target=shutdown_on_input, daemon=True).start()
        timer = threading.Timer(args.lifetime, server.shutdown)
        timer.daemon = True
        timer.start()
        print(json.dumps({'port': server.server_port, 'simulation': True, 'isolated': True}), flush=True)
        try:
            server.serve_forever()
        finally:
            timer.cancel()
            server.server_close()
            service.stop()
            for thread in threads:
                thread.join(3)


if __name__ == '__main__':
    main()
