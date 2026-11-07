from __future__ import annotations

import json
import logging
import os
import time
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import DATA_DIR
from core.db import (
    connect,
    list_discovery_sources,
    log_event,
    recover_interrupted_tasks,
    utc_now,
)
from core.discovery_engine import DiscoveryEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


@dataclass
class DaemonHeartbeat:
    pid: int
    status: str
    last_run_at: str
    active_sources_count: int
    interrupted_tasks_recovered: int
    cycle_count: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AutonomousDaemonScheduler:
    """
    Autonomous Daemon & Scheduler for Unattended Operation.
    Features:
    - Periodic discovery polling across permitted sources.
    - Crash detection that flags interrupted tasks for owner review.
    - Persistent health heartbeat logging to data/daemon_heartbeat.json.
    - Safe execution bounded strictly to local permissions.
    """

    def __init__(self, heartbeat_path: Path | None = None):
        self.heartbeat_path = heartbeat_path or (DATA_DIR / "daemon_heartbeat.json")
        self.is_running = False
        self.cycle_count = 0

    def run_maintenance_and_recovery(self) -> list[int]:
        """Detects and safely recovers any tasks interrupted by an abrupt shutdown/crash."""
        recovered = recover_interrupted_tasks()
        if recovered:
            logging.warning(f"Crash recovery: flagged {len(recovered)} interrupted opportunities for manual review: {recovered}")
        return recovered

    def poll_due_sources(self) -> dict[str, Any]:
        """Checks and polls all active, permitted discovery sources whose poll interval has elapsed."""
        sources = list_discovery_sources(active_only=True)
        results = {"polled": 0, "imported": 0, "errors": []}

        now_ts = datetime.now(timezone.utc).timestamp()

        for s in sources:
            if not s.get("is_permitted", 1):
                continue

            last_polled = s.get("last_polled_at")
            interval_sec = int(s.get("poll_interval_minutes", 60)) * 60

            should_poll = False
            if not last_polled:
                should_poll = True
            else:
                try:
                    lp_ts = datetime.fromisoformat(last_polled).timestamp()
                    if (now_ts - lp_ts) >= interval_sec:
                        should_poll = True
                except Exception:
                    should_poll = True

            if should_poll:
                try:
                    res = DiscoveryEngine.poll_source(s, auto_commit=True)
                    results["polled"] += 1
                    results["imported"] += res.leads_imported
                    if res.errors:
                        results["errors"].extend(res.errors)
                except Exception as exc:
                    results["errors"].append(f"Error polling source '{s['name']}': {exc}")

        return results

    def run_once(self) -> DaemonHeartbeat:
        """Executes a single complete daemon cycle with crash recovery, polling, and heartbeat update."""
        self.cycle_count += 1
        errors: list[str] = []

        # 1. Crash recovery check
        recovered = self.run_maintenance_and_recovery()

        # 2. Source polling check
        poll_res = self.poll_due_sources()
        if poll_res["errors"]:
            errors.extend(poll_res["errors"])

        # 3. Write heartbeat
        sources_count = len(list_discovery_sources(active_only=True))
        heartbeat = DaemonHeartbeat(
            pid=os.getpid(),
            status="healthy" if not errors else "degraded",
            last_run_at=utc_now(),
            active_sources_count=sources_count,
            interrupted_tasks_recovered=len(recovered),
            cycle_count=self.cycle_count,
            errors=errors[:10],
        )

        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path.write_text(json.dumps(heartbeat.to_dict(), indent=2), encoding="utf-8")
        return heartbeat

    def run_forever(
        self,
        interval_seconds: int = 300,
        stop_event: threading.Event | None = None,
        max_cycles: int | None = None,
    ) -> None:
        """Run recurring discovery/maintenance cycles until stopped.

        This scheduler polls authorized discovery sources and flags interrupted
        tasks for manual review. It does NOT accept offers, execute client work,
        send proposals, submit deliverables, or process payments.
        """
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or not 10 <= interval_seconds <= 86400:
            raise ValueError("interval_seconds must be an integer between 10 and 86400")
        if max_cycles is not None and (isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1):
            raise ValueError("max_cycles must be a positive integer when supplied")
        self.is_running = True
        completed = 0
        try:
            while not (stop_event and stop_event.is_set()):
                try:
                    self.run_once()
                except Exception as exc:
                    logging.exception("Scheduler cycle failed")
                    heartbeat = DaemonHeartbeat(
                        pid=os.getpid(),
                        status="degraded",
                        last_run_at=utc_now(),
                        active_sources_count=len(list_discovery_sources(active_only=True)),
                        interrupted_tasks_recovered=0,
                        cycle_count=self.cycle_count,
                        errors=[str(exc)[:500]],
                    )
                    self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
                    self.heartbeat_path.write_text(json.dumps(heartbeat.to_dict(), indent=2), encoding="utf-8")
                completed += 1
                if max_cycles is not None and completed >= max_cycles:
                    break
                if stop_event is not None:
                    if stop_event.wait(interval_seconds):
                        break
                else:
                    time.sleep(interval_seconds)
        finally:
            self.is_running = False

    def get_last_heartbeat(self) -> dict[str, Any] | None:
        """Reads the latest heartbeat from disk."""
        if not self.heartbeat_path.exists():
            return None
        try:
            return json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
        except Exception:
            return None
