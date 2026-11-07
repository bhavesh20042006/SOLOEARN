from __future__ import annotations

import logging
import os
import signal
import threading

from core.scheduler import AutonomousDaemonScheduler


def main() -> int:
    try:
        interval = int(os.getenv("SOLOEARN_SCHEDULER_INTERVAL_SECONDS", "300"))
    except ValueError:
        raise SystemExit("SOLOEARN_SCHEDULER_INTERVAL_SECONDS must be an integer")
    stop_event = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop_event.set())
        except (ValueError, OSError):
            pass
    logging.info("SOLOEARN discovery scheduler starting; interval=%s seconds", interval)
    logging.info("This process polls authorized sources only; it does not send proposals or execute paid jobs.")
    scheduler = AutonomousDaemonScheduler()
    scheduler.run_forever(interval_seconds=interval, stop_event=stop_event)
    logging.info("SOLOEARN discovery scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
