from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .service import LoggerheadService


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Loggerhead aquarium controller.")
    parser.add_argument("--config", type=Path, default=Path("config/loggerhead.json"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--simulation", action="store_true", help="Do not touch local hardware drivers.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s %(message)s")
    service = LoggerheadService(args.config, args.data_dir, simulation=args.simulation)
    service.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
